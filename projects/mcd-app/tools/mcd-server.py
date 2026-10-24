"""Read-only local proxy in front of the McDonald's MCP server.

Why this exists: the MCP endpoint needs a personal Bearer token. Putting that
token in browser JavaScript would leak it to anyone who opens the page. So the
token stays in this process, and the browser only ever talks to localhost.

Run:  python3 tools/mcd-server.py [--port 8777]
Then open http://127.0.0.1:8777/  (the app degrades to the static snapshot
when this server is not running).
"""
import json
import os
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / 'outputs'
def _find_mcp_config():
    candidates = []
    env = os.environ.get('MCD_MCP_CONFIG')
    if env:
        candidates.append(pathlib.Path(env).expanduser())
    candidates.append(ROOT / 'mcp.json')
    candidates.append(pathlib.Path.home() / '.workbuddy' / 'mcp.json')
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None

MCP_CONFIG = _find_mcp_config()

MENU_TTL = 900          # 15 min
STORE_TTL = 300         # 5 min
NUTRITION_TTL = 86400   # 1 day
DETAIL_TTL = 900

# Landmarks used to widen the browsable store pool. Each query returns the
# stores nearest to that landmark, so we merge several and keep the closest.
SHANGHAI_LANDMARKS = [
    '漕宝路', '七宝', '莘庄', '古美', '徐家汇', '吴泾', '梅陇',
    '虹梅路', '颛桥', '马桥', '浦江镇', '松江', '虹桥', '徐汇', '静安',
]


def money(value):
    """API prices arrive as yuan strings; the UI works in cents."""
    try:
        return int(round(float(str(value).strip()) * 100))
    except (TypeError, ValueError):
        return None


# ---------- nutrition name guards ----------
# The nutrition table and the menu are different data sources. A loose match
# silently invents numbers, so every fuzzy match must clear these guards.
COMBO_WORDS = ('套餐', '组合', '双人', '三人', '四人', '分享', '桶', '盒', '拼',
               '件套', '随心选', '会员', '任选')
SIZE_RE = re.compile(r'(超大杯|大杯|中杯|小杯|大份|中份|小份|大薯条|中薯条|小薯条|\d+块|\d+件|标准|去冰|少冰|多冰|加冰)')
SUGAR_RE = re.compile(r'(无糖|零度|低糖|0糖|零糖)')


def _norm_name(value):
    text = str(value or '')
    for ch in ('™', '®', '（', '）', '(', ')', ' ', '\u3000', '·', '・', '\n'):
        text = text.replace(ch, '')
    return text.strip()


def _is_combo(text):
    return any(word in text for word in COMBO_WORDS)


def _size_sig(text):
    return '|'.join(sorted(set(SIZE_RE.findall(text))))


def _sugar_sig(text):
    return SUGAR_RE.findall(text)


class McpClient:
    """One MCP session. Sessions expire, so every call checks liveness."""

    def __init__(self, url, headers):
        self.url = url
        self.headers = headers
        self.session = None
        self.counter = 0
        self.lock = threading.Lock()

    def _rpc(self, method, params=None):
        with self.lock:
            self.counter += 1
            headers = {
                **self.headers,
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2024-11-05',
            }
            if self.session:
                headers['Mcp-Session-Id'] = self.session
            payload = {'jsonrpc': '2.0', 'id': self.counter, 'method': method}
            if params is not None:
                payload['params'] = params
            req = urllib.request.Request(
                self.url, data=json.dumps(payload).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    self.session = response.headers.get('Mcp-Session-Id', self.session)
                    text = response.read().decode()
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 400) and self.session:
                    # Stale session id: drop it and let the caller retry.
                    self.session = None
                    raise
                raise
            if text.lstrip().startswith('{'):
                result = json.loads(text)
            else:
                items = [json.loads(line[5:].strip()) for line in text.splitlines()
                         if line.startswith('data:') and line[5:].strip().startswith('{')]
                result = next(x for x in items if x.get('id') == self.counter)
            if 'error' in result:
                raise RuntimeError(str(result['error']))
            return result.get('result', {})

    def _ensure(self):
        if self.session:
            return
        self._rpc('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'mcd-local-proxy', 'version': '2.0'},
        })

    def call(self, name, args):
        last = None
        for attempt in range(2):
            try:
                self._ensure()
                result = self._rpc('tools/call', {'name': name, 'arguments': args})
                return self._parse(result)
            except Exception as exc:  # noqa: BLE001 - session churn is expected
                last = exc
                self.session = None
        raise last

    @staticmethod
    def _parse(result):
        for block in result.get('content', []):
            text = block.get('text', '')
            start = text.find('{"success"')
            if start >= 0:
                obj, _ = json.JSONDecoder().raw_decode(text[start:])
                if not obj.get('success'):
                    raise RuntimeError(obj.get('message') or 'MCP call failed')
                return obj
        raise RuntimeError('No JSON payload in MCP response')


class Cache:
    def __init__(self):
        self.store = {}
        self.lock = threading.Lock()

    def get(self, key, ttl):
        with self.lock:
            hit = self.store.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return None

    def put(self, key, value):
        with self.lock:
            self.store[key] = (time.time(), value)


class McdService:
    def __init__(self):
        config = json.loads(MCP_CONFIG.read_text())['mcpServers']['mcd-mcp']
        self.url = config['url']
        self.headers = config['headers']
        self.cache = Cache()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self._local = threading.local()
        self._pool_lock = threading.Lock()
        self._clients = []
        self.nutrition_rows = None

    def client(self):
        client = getattr(self._local, 'client', None)
        if client is None:
            client = McpClient(self.url, self.headers)
            self._local.client = client
            with self._pool_lock:
                self._clients.append(client)
        return client

    def call(self, name, args):
        return self.client().call(name, args)

    # ---------- nutrition ----------
    def nutrition_map(self):
        if self.nutrition_rows is not None:
            return self.nutrition_rows
        cached = self.cache.get('nutrition', NUTRITION_TTL)
        if cached is not None:
            self.nutrition_rows = cached
            return cached
        raw = self.call('list-nutrition-foods', {}).get('data') or ''
        rows = {}
        if isinstance(raw, str):
            header = raw.split('\n', 1)[0]
            match = re.search(r'\{([^}]*)\}', header)
            fields = [f.strip() for f in match.group(1).split(',')] if match else []
            if fields and fields[0] == 'productName':
                numeric = fields[1:]
                for line in raw.split('\n')[1:]:
                    line = line.strip()
                    if not line:
                        continue
                    parts = [p.strip() for p in line.rsplit(',', len(numeric))]
                    if len(parts) != len(numeric) + 1:
                        continue
                    record = {}
                    for key, value in zip(numeric, parts[1:]):
                        if key == 'nutritionDescription':
                            record[key] = None if value in ('', 'null') else value
                            continue
                        try:
                            record[key] = None if value in ('', 'null') else float(value)
                        except ValueError:
                            record[key] = None
                    rows[parts[0]] = record
        self.nutrition_rows = rows
        self.cache.put('nutrition', rows)
        return rows

    def nutrition_for(self, name):
        """Match a menu item to one nutrition row, refusing lookalikes.

        The nutrition table is a separate data source from the menu, so any
        fuzzy match risks inventing numbers (a 套餐 borrowing a single burger's
        kcal, a large drink borrowing a medium one). Rules, in order:
          1. exact name after normalisation -> match='exact'
          2. same "size signature" and neither side is a combo/bundle, with a
             short substring relation -> match='derived'
          3. otherwise None, and the UI says the接口 didn't provide it.
        """
        clean = _norm_name(name)
        rows = self.nutrition_map()
        fields = ('energyKcal', 'protein', 'fat', 'carbohydrate', 'sodium')

        def shape(row, match, source):
            # energyKcal/carbohydrate are the API's names; kcal/carb are what the
            # page and the static snapshot already speak, so ship both.
            out = {k: row.get(k) for k in fields}
            out.update({'kcal': row.get('energyKcal'), 'carb': row.get('carbohydrate'),
                        'match': match, 'source': source})
            return out

        for key, row in rows.items():
            if _norm_name(key) == clean:
                return shape(row, 'exact', key)

        if _is_combo(clean):
            return None
        want = _size_sig(clean)
        best = None
        for key, row in rows.items():
            base = _norm_name(key.split('-')[0])
            if not base or _is_combo(base) or _size_sig(base) != want:
                continue
            if _sugar_sig(base) != _sugar_sig(clean):
                continue
            if abs(len(base) - len(clean)) > 3:
                continue
            if base in clean or clean in base:
                score = abs(len(base) - len(clean))
                if best is None or score < best[0]:
                    best = (score, key, row)
        if best:
            return shape(best[2], 'derived', best[1])
        return None

    # ---------- stores ----------
    @staticmethod
    def _store_entry(raw, area=None):
        return {
            'code': raw.get('storeCode'),
            'name': raw.get('storeName'),
            'address': raw.get('address'),
            'distance': raw.get('distance'),
            'businessStatus': raw.get('businessStatus'),
            'businessStartTime': raw.get('businessStartTime'),
            'businessEndTime': raw.get('businessEndTime'),
            'reservation': raw.get('reservation'),
            'reservationTimeOptions': raw.get('reservationTimeOptions') or [],
            'area': area,
        }

    def nearby(self, keyword, city='上海', be_type=1):
        key = f'nearby:{city}:{keyword}:{be_type}'
        cached = self.cache.get(key, STORE_TTL)
        if cached is not None:
            return cached
        result = self.call('query-nearby-stores', {
            'searchType': 2, 'city': city, 'keyword': keyword, 'beType': be_type})
        stores = [self._store_entry(s, keyword) for s in (result.get('data') or [])]
        self.cache.put(key, stores)
        return stores

    def favorites(self, be_type=1):
        key = f'fav:{be_type}'
        cached = self.cache.get(key, STORE_TTL)
        if cached is not None:
            return cached
        result = self.call('query-nearby-stores', {'searchType': 1, 'beType': be_type})
        stores = [self._store_entry(s, '收藏') for s in (result.get('data') or [])]
        self.cache.put(key, stores)
        return stores

    def deliverable(self, address_id, be_type=2):
        key = f'deliver:{address_id}:{be_type}'
        cached = self.cache.get(key, STORE_TTL)
        if cached is not None:
            return cached
        result = self.call('delivery-query-stores',
                           {'addressId': address_id, 'beType': be_type})
        stores = []
        for raw in (result.get('data') or []):
            entry = self._store_entry(raw, '可配送')
            entry['beCode'] = raw.get('beCode')
            stores.append(entry)
        self.cache.put(key, stores)
        return stores

    def deliverable_with_meta(self, address_id, be_type=2):
        """Deliverable stores enriched with address and distance.

        The directory lookup is best-effort: if the aggregate pool is cold we
        still return the stores rather than blocking the UI on 15 queries.
        """
        stores = self.deliverable(address_id, be_type)
        if not stores:
            return stores
        try:
            directory = {s['code']: s for s in self.browse()}
        except Exception:  # noqa: BLE001 - metadata is a nice-to-have
            return stores
        for entry in stores:
            known = directory.get(entry['code'])
            if known:
                entry['address'] = entry.get('address') or known.get('address')
                entry['distance'] = entry.get('distance') or known.get('distance')
                entry['area'] = known.get('area')
        return stores

    def default_address(self):
        """First saved delivery address, used when the client has none."""
        addresses = self.addresses()
        return addresses[0]['addressId'] if addresses else None

    def browse(self):
        """Merge landmark queries into one browsable Shanghai pool."""
        cached = self.cache.get('browse', STORE_TTL)
        if cached is not None:
            return cached
        futures = [self.pool.submit(self.nearby, kw) for kw in SHANGHAI_LANDMARKS]
        merged = {}
        for future in futures:
            try:
                rows = future.result()
            except Exception:  # noqa: BLE001 - one landmark failing is not fatal
                continue
            for row in rows:
                code = row['code']
                if code not in merged or (row.get('distance') or 0) < (merged[code].get('distance') or 0):
                    if code in merged:
                        row['areas'] = sorted(set(merged[code].get('areas', []) +
                                                 [merged[code].get('area', '')]))
                    else:
                        row['areas'] = [row.get('area', '')]
                    merged[code] = row
        stores = sorted(merged.values(), key=lambda r: r.get('distance') or 0)
        self.cache.put('browse', stores)
        return stores

    def addresses(self):
        result = self.call('delivery-query-addresses', {})
        return (result.get('data') or {}).get('addresses') or []

    # ---------- menu ----------
    @staticmethod
    def be_code(store_code, be_type):
        """Business codes follow a suffix on the store code for normal stores."""
        return store_code + ('81' if be_type == 6 else '02')

    def resolve_be(self, store_code, be_type):
        """Find a working (storeCode, beCode, beType, orderType) combination.

        Ordinary restaurants answer for the requested channel, but station
        kiosks and in-station counters only serve pickup (beType=1,
        orderType=1) and reject every delivery pairing. We probe the real
        menu endpoint and keep whatever the store actually accepts.

        The pairing rules are not interchangeable. beType=1/5 are pickup
        (orderType=1) and must NOT carry a delivery beCode, because the
        upstream silently answers with the delivery price list when it does.
        beType=2/6 are delivery (orderType=2) and must carry a beCode.
        """
        key = f'be:{store_code}:{be_type}'
        cached = self.cache.get(key, MENU_TTL)
        if cached is not None:
            return cached
        candidates = []
        if be_type in (1, 5):
            # Pickup: orderType=1. Try the drive-through code, then the
            # standard code, and try with and without a beCode, because the
            # upstream accepts a delivery beCode here but answers with the
            # WRONG (delivery) price list instead of failing.
            for be in (store_code + '41', store_code + '02', ''):
                candidates.append((be, be_type, 1))
                candidates.append((be, 1, 1))
        else:
            # Delivery: orderType=2 with a business code.
            candidates.append((store_code + ('81' if be_type == 6 else '02'), be_type, 2))
            # Some counters answer for pickup only.
            candidates.append((store_code + ('81' if be_type == 6 else '02'), 1, 1))
            candidates.append((store_code + '41', be_type, 2))
            candidates.append((store_code + '41', 1, 1))
            if be_type == 6:
                candidates.append((store_code + '02', 6, 2))
        last = None
        for be, use_type, order_type in candidates:
            args = {'storeCode': store_code, 'beType': use_type, 'orderType': order_type}
            if be:
                args['beCode'] = be
            try:
                self.call('query-meals', args)
                pair = (be, use_type, order_type)
                self.cache.put(key, pair)
                return pair
            except Exception as exc:  # noqa: BLE001 - try the next pairing
                last = exc
        raise last or RuntimeError('无法为该门店找到可用的 beCode')

    def menu(self, store_code, be_type=2):
        key = f'menu:{store_code}:{be_type}'
        cached = self.cache.get(key, MENU_TTL)
        if cached is not None:
            return cached
        be_code, use_type, order_type = self.resolve_be(store_code, be_type)
        params = {'storeCode': store_code, 'beCode': be_code,
                  'beType': use_type, 'orderType': order_type}
        menu = self.call('query-meals', params)
        coupons = self.call('query-store-coupons', params)
        promotions = []
        if use_type == 6:
            try:
                promotions = self.call('query-promotions', params).get('data') or []
            except Exception:  # noqa: BLE001 - promo rules are optional context
                promotions = []
        payload = self.transform(store_code, be_code, use_type, menu, coupons, promotions)
        payload['orderType'] = order_type
        payload['pickupOnly'] = order_type == 1
        payload['requestedBeType'] = be_type
        self.cache.put(key, payload)
        return payload

    def transform(self, store_code, be_code, be_type, menu, coupons, promotions):
        data = menu.get('data') or {}
        meals = data.get('meals') or {}
        categories = []
        seen = set()
        for category in (data.get('categories') or []):
            items = []
            for ref in (category.get('meals') or []):
                code = ref.get('code')
                if code in seen or code not in meals:
                    continue
                seen.add(code)
                meal = meals[code]
                price = money(meal.get('currentPrice'))
                if price is None:
                    continue
                items.append({
                    'code': code,
                    'name': (meal.get('name') or '').strip(),
                    'image': meal.get('image'),
                    'price': price,
                    'original': money(meal.get('originalPrice')),
                    'discount': meal.get('discountType') or None,
                    'withOrder': bool(meal.get('canWithOrder')),
                    'tags': [t.strip() for t in (ref.get('tags') or []) if t.strip()],
                    'nutrition': self.nutrition_for(meal.get('name') or ''),
                })
            if items:
                categories.append({'name': (category.get('name') or '').replace('\n', ' '),
                                   'items': items})
        rules = []
        for rule in promotions:
            detail = ((rule.get('ruleDetail') or {}).get('orderDiscount') or {})
            if not detail:
                continue
            rules.append({'service': rule.get('gmServiceCode'),
                          'discount': detail.get('discount'),
                          'threshold': detail.get('startDiscountPoint')})
        return {
            'code': store_code,
            'beCode': be_code,
            'beType': be_type,
            'categories': categories,
            'productCount': sum(len(c['items']) for c in categories),
            'couponCount': len(coupons.get('data') or []),
            'promotions': rules,
        }

    def detail(self, store_code, code, be_type=2):
        key = f'detail:{store_code}:{code}:{be_type}'
        cached = self.cache.get(key, DETAIL_TTL)
        if cached is not None:
            return cached
        be_code, use_type, order_type = self.resolve_be(store_code, be_type)
        result = self.call('query-meal-detail', {
            'storeCode': store_code, 'beCode': be_code,
            'beType': use_type, 'orderType': order_type, 'code': code})
        self.cache.put(key, result.get('data') or {})
        return result.get('data') or {}

    # ---------- ordering ----------
    @staticmethod
    def _apply_coupon(items, coupon_code, coupon_id):
        """Coupons belong on each item, not on the request envelope."""
        if not coupon_code and not coupon_id:
            return items
        out = []
        for item in items:
            entry = dict(item)
            if coupon_code and 'couponCode' not in entry:
                entry['couponCode'] = coupon_code
            if coupon_id and 'couponId' not in entry:
                entry['couponId'] = coupon_id
            out.append(entry)
        return out

    def quote(self, store_code, be_type, items, address_id=None, need_tableware=False,
              coupon_code=None, coupon_id=None, remark=None):
        """Price a basket through calculate-price. Never creates an order."""
        be_code, use_type, order_type = self.resolve_be(store_code, be_type)
        params = {'storeCode': store_code, 'beType': use_type, 'orderType': order_type,
                  'items': self._apply_coupon(items, coupon_code, coupon_id),
                  'needTableware': bool(need_tableware)}
        if order_type == 2:
            if be_code:
                params['beCode'] = be_code
            if address_id:
                params['addressId'] = address_id
        result = self.call('calculate-price', params)
        return {'beCode': be_code, 'beType': use_type, 'orderType': order_type,
                'data': result.get('data') or {}}

    def create_order(self, store_code, be_type, items, address_id=None,
                     need_tableware=False, remark=None, take_way_code=None,
                     coupon_code=None, coupon_id=None):
        """Create a real order. The caller is responsible for confirmation."""
        be_code, use_type, order_type = self.resolve_be(store_code, be_type)
        params = {'storeCode': store_code, 'beType': use_type, 'orderType': order_type,
                  'items': self._apply_coupon(items, coupon_code, coupon_id),
                  'needTableware': bool(need_tableware)}
        if order_type == 2:
            if be_code:
                params['beCode'] = be_code
            if address_id:
                params['addressId'] = address_id
        if take_way_code:
            params['takeWayCode'] = take_way_code
        if remark:
            params['remark'] = remark
        result = self.call('create-order', params)
        return {'beCode': be_code, 'beType': use_type, 'orderType': order_type,
                'data': result.get('data') or {}}

    def cancel_order(self, order_id, reason_code=1):
        result = self.call('cancel-order',
                           {'orderId': order_id, 'cancelReasonCode': reason_code})
        return result.get('data') or {}

    def query_order(self, order_id):
        result = self.call('query-order', {'orderId': order_id})
        return result.get('data') or {}


SERVICE = McdService()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUTPUTS), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == '/':
            # Serve the app itself at the root instead of a directory listing.
            self.path = '/qibao-mcd-app.html'
            return super().do_GET()
        if not parsed.path.startswith('/api/'):
            return super().do_GET()
        query = parse_qs(parsed.query)
        one = lambda key, default=None: (query.get(key) or [default])[0]  # noqa: E731
        try:
            if parsed.path == '/api/stores':
                mode = one('mode', 'nearby')
                if mode == 'browse':
                    return self._json({'ok': True, 'mode': mode, 'stores': SERVICE.browse()})
                if mode == 'favorites':
                    # query-nearby-stores only accepts beType 1 or 5.
                    stores = SERVICE.favorites(1)
                    return self._json({'ok': True, 'mode': mode, 'stores': stores})
                if mode == 'deliver':
                    address_id = one('addressId') or SERVICE.default_address()
                    if not address_id:
                        return self._json({'ok': False, 'error': '账号没有可用配送地址'}, 400)
                    be_type = int(one('beType', 2))
                    stores = SERVICE.deliverable_with_meta(address_id, be_type)
                    return self._json({'ok': True, 'mode': mode, 'stores': stores})
                keyword = one('keyword', '漕宝路')
                city = one('city', '上海')
                # Position search supports pickup (1) and drive-through (5).
                be_type = int(one('beType', 1))
                if be_type not in (1, 5):
                    be_type = 1
                stores = SERVICE.nearby(keyword, city, be_type)
                return self._json({'ok': True, 'mode': mode, 'stores': stores})
            if parsed.path == '/api/menu':
                code = one('code')
                if not code:
                    return self._json({'ok': False, 'error': 'code required'}, 400)
                payload = SERVICE.menu(code, int(one('beType', 2)))
                return self._json({'ok': True, 'menu': payload})
            if parsed.path == '/api/detail':
                payload = SERVICE.detail(one('code'), one('product'), int(one('beType', 2)))
                return self._json({'ok': True, 'detail': payload})
            if parsed.path == '/api/addresses':
                return self._json({'ok': True, 'addresses': SERVICE.addresses()})
            if parsed.path == '/api/time':
                return self._json({'ok': True, 'now': SERVICE.call('now-time-info', {}).get('data')})
            if parsed.path == '/api/account':
                return self._json({'ok': True, 'account': SERVICE.call('query-my-account', {}).get('data')})
            if parsed.path == '/api/order/quote':
                body = self._read_body()
                items = body.get('items') or []
                if not items:
                    return self._json({'ok': False, 'error': '购物车为空'}, 400)
                payload = SERVICE.quote(
                    body.get('storeCode'), int(body.get('beType', 2)), items,
                    address_id=body.get('addressId'),
                    need_tableware=body.get('needTableware', False),
                    coupon_code=body.get('couponCode'), coupon_id=body.get('couponId'))
                return self._json({'ok': True, 'quote': payload})
            if parsed.path == '/api/order/create':
                body = self._read_body()
                items = body.get('items') or []
                if not items:
                    return self._json({'ok': False, 'error': '购物车为空'}, 400)
                payload = SERVICE.create_order(
                    body.get('storeCode'), int(body.get('beType', 2)), items,
                    address_id=body.get('addressId'),
                    need_tableware=body.get('needTableware', False),
                    remark=body.get('remark'),
                    take_way_code=body.get('takeWayCode'),
                    coupon_code=body.get('couponCode'), coupon_id=body.get('couponId'))
                return self._json({'ok': True, 'order': payload})
            if parsed.path == '/api/order/cancel':
                body = self._read_body()
                if not body.get('orderId'):
                    return self._json({'ok': False, 'error': 'orderId required'}, 400)
                data = SERVICE.cancel_order(body['orderId'],
                                            int(body.get('cancelReasonCode', 1)))
                return self._json({'ok': True, 'data': data})
            if parsed.path == '/api/order/query':
                body = self._read_body()
                if not body.get('orderId'):
                    return self._json({'ok': False, 'error': 'orderId required'}, 400)
                return self._json({'ok': True, 'order': SERVICE.query_order(body['orderId'])})
            if parsed.path == '/api/health':
                return self._json({'ok': True, 'service': 'mcd-local-proxy',
                                   'mcp': SERVICE.url, 'time': time.strftime('%F %T')})
            return self._json({'ok': False, 'error': 'unknown endpoint'}, 404)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            return self._json({'ok': False, 'error': str(exc)}, 502)

    def _read_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode())
        except ValueError:
            return {}

    def do_POST(self):
        self.do_GET()


def main():
    port = 8777
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'McDonald\'s local proxy on http://127.0.0.1:{port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
