"""Read-only local proxy in front of the McDonald's MCP server.

Why this exists: the MCP endpoint needs a personal Bearer token. Putting that
token in browser JavaScript would leak it to anyone who opens the page. So the
token stays in this process, and the browser only ever talks to localhost.

Run:  python3 tools/mcd-server.py [--port 8777]
Then open http://127.0.0.1:8777/  (the app degrades to the static snapshot
when this server is not running).

MCP credentials: this proxy reads the MCP server URL and Bearer token from a
config file. By default it looks for `mcp.json` next to this repository, then
`~/.workbuddy/mcp.json`. Override with the MCD_MCP_CONFIG env var:

    MCD_MCP_CONFIG=/path/to/mcp.json python3 tools/mcd-server.py

The file must contain an `mcpServers.mcd-mcp` entry with `url` and `headers`.
No credentials are committed to this repository. If the config is missing the
server still starts and serves the static snapshot; only the live /api/* calls
report an error.
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

# tools/mcd-server.py -> project root -> app/ and data/ live one level up.
TOOLS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
APP_DIR = ROOT / 'app'
DATA_DIR = ROOT / 'data'


def resolve_mcp_config():
    """Locate the MCP config file. Never raises; returns None when absent."""
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


MCP_CONFIG = resolve_mcp_config()

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


class McdConfigError(RuntimeError):
    """Raised when the MCP config file is missing or malformed."""


class McdService:
    """Talks to the MCP server on demand.

    The config is read lazily so that the server still starts (and still serves
    the static snapshot) on a machine that has no MCP credentials at all.
    """

    def __init__(self):
        self.cache = Cache()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self._local = threading.local()
        self._pool_lock = threading.Lock()
        self._clients = []
        self.nutrition_rows = None
        self._config = None

    @property
    def config(self):
        if self._config is None:
            if MCP_CONFIG is None:
                raise McdConfigError(
                    'MCP config not found. Create an mcp.json with an '
                    'mcpServers.mcd-mcp entry (url + headers) and point '
                    'MCD_MCP_CONFIG at it, or put it at ~/.workbuddy/mcp.json. '
                    'The static snapshot still works without it.')
            try:
                raw = json.loads(MCP_CONFIG.read_text(encoding='utf-8'))
                entry = raw['mcpServers']['mcd-mcp']
                self._config = {'url': entry['url'], 'headers': entry['headers']}
            except Exception as exc:
                raise McdConfigError(
                    f'Cannot read MCP config at {MCP_CONFIG}: {exc}') from exc
        return self._config

    @property
    def url(self):
        return self.config['url']

    @property
    def headers(self):
        return self.config['headers']

    def client(self):
        client = getattr(self._local, 'client', None)
        if client is None:
            client = McpClient(self.url, self.headers)  # raises McdConfigError
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
        clean = name.replace('™', '').replace('（', '(').replace('）', ')').strip()
        rows = self.nutrition_map()
        for key, row in rows.items():
            if key.replace('（', '(').replace('）', ')') == clean:
                return {k: row.get(k) for k in
                        ('energyKcal', 'protein', 'fat', 'carbohydrate', 'sodium')}
        for key, row in rows.items():
            base = key.split('-')[0]
            if base and (base in clean or clean in base):
                return {k: row.get(k) for k in
                        ('energyKcal', 'protein', 'fat', 'carbohydrate', 'sodium')}
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
        """Delivery business codes follow a fixed suffix on the store code."""
        return store_code + ('81' if be_type == 6 else '02')

    def menu(self, store_code, be_type=2):
        key = f'menu:{store_code}:{be_type}'
        cached = self.cache.get(key, MENU_TTL)
        if cached is not None:
            return cached
        be_code = self.be_code(store_code, be_type)
        params = {'storeCode': store_code, 'beCode': be_code,
                  'beType': be_type, 'orderType': 2}
        menu = self.call('query-meals', params)
        coupons = self.call('query-store-coupons', params)
        promotions = []
        if be_type == 6:
            try:
                promotions = self.call('query-promotions', params).get('data') or []
            except Exception:  # noqa: BLE001 - promo rules are optional context
                promotions = []
        payload = self.transform(store_code, be_code, be_type, menu, coupons, promotions)
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
        result = self.call('query-meal-detail', {
            'storeCode': store_code, 'beCode': self.be_code(store_code, be_type),
            'beType': be_type, 'orderType': 2, 'code': code})
        self.cache.put(key, result.get('data') or {})
        return result.get('data') or {}


SERVICE = McdService()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

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
                    return self._json({'ok': True, 'mode': mode,
                                       'stores': SERVICE.favorites(int(one('beType', 1)))})
                if mode == 'deliver':
                    stores = SERVICE.deliverable(one('addressId'), int(one('beType', 2)))
                    return self._json({'ok': True, 'mode': mode, 'stores': stores})
                keyword = one('keyword', '漕宝路')
                city = one('city', '上海')
                be_type = int(one('beType', 1))
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
            if parsed.path == '/api/health':
                try:
                    mcp_url = SERVICE.url
                    credentialed = True
                except McdConfigError as exc:
                    mcp_url = None
                    credentialed = False
                    health_error = str(exc)
                else:
                    health_error = None
                return self._json({'ok': True, 'service': 'mcd-local-proxy',
                                   'mcp': mcp_url,
                                   'credentialed': credentialed,
                                   'error': health_error,
                                   'time': time.strftime('%F %T')})
            return self._json({'ok': False, 'error': 'unknown endpoint'}, 404)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            return self._json({'ok': False, 'error': str(exc)}, 502)


def main():
    port = 8777
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'McDonald\'s local proxy on http://127.0.0.1:{port}/', flush=True)
    if MCP_CONFIG is None:
        print('No MCP config found - serving the static snapshot only.',
              flush=True)
        print('Live data needs an mcp.json (mcpServers.mcd-mcp with url + headers).',
              flush=True)
    else:
        print(f'MCP config: {MCP_CONFIG}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
