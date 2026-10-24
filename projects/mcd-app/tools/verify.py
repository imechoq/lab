"""Read-only verification of the local McDonald's proxy.

Checks the GET endpoints the page uses plus calculate-price (a price quote,
which creates nothing). Never calls /api/order/create — that would place a real
order in the user's account.

Run:  python3 tools/verify.py [--port 8791]
"""
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8791
if '--port' in sys.argv:
    PORT = int(sys.argv[sys.argv.index('--port') + 1])
BASE = f'http://127.0.0.1:{PORT}'
LOG = pathlib.Path('/tmp/mcd-verify-server.log')


def get(path, timeout=120):
    started = time.time()
    with urllib.request.urlopen(BASE + path, timeout=timeout) as res:
        body = json.loads(res.read().decode())
    return body, round((time.time() - started) * 1000)


def post(path, payload, timeout=120):
    started = time.time()
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        body = json.loads(res.read().decode())
    return body, round((time.time() - started) * 1000)


def main():
    report = {}

    def step(name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - one broken endpoint must not stop the rest
            report[name] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def health():
        body, ms = get('/api/health', 15)
        report['health'] = {'ok': body.get('ok'), 'ms': ms}

    def nearby():
        body, ms = get('/api/stores?mode=nearby&keyword=%E6%A7%BD%E5%AE%9D%E8%B7%AF&city=%E4%B8%8A%E6%B5%B7')
        rows = body.get('stores') or []
        report['nearby'] = {'ok': body.get('ok'), 'count': len(rows), 'ms': ms,
                            'first': rows[0]['name'] if rows else None}
        report['_nearbyCodes'] = [r['code'] for r in rows[:5]]

    def deliver():
        body, ms = get('/api/stores?mode=deliver&beType=2')
        rows = body.get('stores') or []
        report['deliver'] = {'ok': body.get('ok'), 'count': len(rows), 'ms': ms,
                             'first': rows[0]['name'] if rows else None}
        report['_deliverCodes'] = [r['code'] for r in rows[:5]]

    def menu():
        codes = report.get('_deliverCodes') or report.get('_nearbyCodes') or ['1450256']
        code = codes[0]
        body, ms = get(f'/api/menu?code={code}&beType=2')
        m = body.get('menu') or {}
        items = [i for c in (m.get('categories') or []) for i in c['items']]
        report['menu'] = {
            'ok': body.get('ok'), 'ms': ms, 'store': code,
            'categories': len(m.get('categories') or []),
            'products': m.get('productCount'),
            'withImage': sum(1 for i in items if i.get('image')),
            'discounted': sum(1 for i in items if (i.get('original') or 0) > (i.get('price') or 0)),
            'withNutrition': sum(1 for i in items if i.get('nutrition')),
            'cheapest': [{'name': i['name'], 'price': i['price']} for i in
                         sorted(items, key=lambda x: x['price'])[:2]],
        }
        report['_menuItems'] = [{'code': i['code'], 'name': i['name'], 'price': i['price']}
                                for i in sorted(items, key=lambda x: x['price'])[:2]]
        combo = next((i for i in items if '套餐' in i['name']), None)
        if combo:
            report['_comboCode'] = combo['code']

    def detail():
        code = report.get('_comboCode') or (report.get('_menuItems') or [{'code': None}])[0]['code']
        store = (report.get('menu') or {}).get('store')
        body, ms = get(f'/api/detail?code={store}&product={code}&beType=2')
        d = body.get('detail') or {}
        rounds = d.get('rounds') or []
        report['detail'] = {'ok': body.get('ok'), 'ms': ms, 'code': code,
                            'name': d.get('name'), 'rounds': len(rounds),
                            'choices': sum(len(r.get('choices') or []) for r in rounds)}

    def addresses():
        body, ms = get('/api/addresses')
        report['addresses'] = {'ok': body.get('ok'),
                               'count': len(body.get('addresses') or [])}

    def quote():
        items = report.get('_menuItems') or []
        if not items:
            report['quote'] = {'ok': False, 'error': 'no items'}
            return
        payload = {
            'storeCode': (report.get('menu') or {}).get('store'),
            'beType': 2, 'needTableware': False,
            'items': [{'productCode': i['code'], 'quantity': 1} for i in items],
        }
        body, ms = post('/api/order/quote', payload)
        q = body.get('quote') or {}
        data = q.get('data') or {}
        report['quote'] = {'ok': body.get('ok'), 'ms': ms, 'error': body.get('error'),
                           'listPrice': sum(i['price'] for i in items),
                           'price': data.get('price'),
                           'keys': sorted(data.keys())[:14]}

    for name, fn in [('health', health), ('nearby', nearby), ('deliver', deliver),
                     ('menu', menu), ('detail', detail), ('addresses', addresses),
                     ('quote', quote)]:
        step(name, fn)

    for key in [k for k in report if k.startswith('_')]:
        report.pop(key)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    log = open(LOG, 'w')
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'tools' / 'mcd-server.py'), '--port', str(PORT)],
        stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                get('/api/health', 2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        main()
    finally:
        proc.terminate()
        log.close()
