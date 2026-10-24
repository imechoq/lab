"""Collect the real MCP fields the recommendation engine needs.

Everything written to outputs/ is traceable to one MCP response:
  - store distance (metres) from query-nearby-stores
  - deliverable stores per address/channel from delivery-query-stores
  - per-product detail (sauce options, combo rounds) from query-meal-detail
  - group discount rules from query-promotions
  - store coupons from query-store-coupons

Read-only: no create-order, no coupon binding, no address mutation.
"""
import json, pathlib, threading, queue, time, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'outputs'
STORES = [
    ('1450256', '麦当劳上海闵行七宝领展餐厅', '145025602', '145025681'),
    ('1450442', '麦当劳上海虹桥万创中心餐厅', '145044202', '145044281'),
    ('1450374', '麦当劳上海融信上坤中心餐厅', '145037402', '145037481'),
    ('1450516', '麦当劳上海虹口双狮汇大厦餐厅', '145051602', '145051681'),
    ('1450604', '麦当劳上海虹口东长治路友邦金融中心餐厅', '145060402', '145060481'),
    ('1450395', '麦当劳上海虹口星展未来中心餐厅', '145039502', '145039581'),
]
ADDRESSES = {
    'qibao': {
        'addressId': '1036947850192220178092339034',
        'city': '上海',
        'keyword': '上海市闵行区漕宝路3509号 京东MALL(上海七宝店)',
        'label': '上海市闵行区漕宝路3509号 京东MALL(上海七宝店)',
    },
    'hongkou': {
        'addressId': '1036838920091152101750153050',
        'city': '上海',
        'keyword': '上海市虹口区周家嘴路338弄1-3号 馨虹苑2号楼302室',
        'label': '上海市虹口区周家嘴路338弄1-3号 馨虹苑2号楼302室',
    },
}

sys.path.insert(0, '/tmp')
from mcd_lib import M  # noqa: E402

# ---------------------------------------------------------------- collection
results = {}
MENUS = {}
lock = threading.Lock()


def store_job(m, code, name, be2, be6):
    """One detail fetch per (store, channel, product)."""
    jobs = []
    menus = {}
    for be, bec in ((2, be2), (6, be6)):
        r = m.call('query-meals', {'storeCode': code, 'beCode': bec, 'beType': be, 'orderType': 2})
        menus[str(be)] = r['data']
        meals = r['data']['meals']
        for pcode in meals:
            jobs.append((be, bec, pcode))
    with lock:
        MENUS[code] = menus
    for be, bec, pcode in jobs:
        try:
            d = m.call('query-meal-detail', {
                'storeCode': code, 'beCode': bec, 'beType': be, 'orderType': 2, 'code': pcode})
            with lock:
                results[(code, str(be), pcode)] = d
        except Exception as exc:
            with lock:
                results[(code, str(be), pcode)] = {'error': str(exc)[:200]}
        time.sleep(0.02)


def worker(wid, task_q):
    m = M().init()
    while True:
        try:
            task = task_q.get_nowait()
        except queue.Empty:
            return
        try:
            store_job(m, *task)
        except Exception as exc:
            import traceback
            print('worker', wid, 'failed:', str(exc)[:160], flush=True)
            traceback.print_exc()


def main():
    # 1) store distance + deliverable stores + coupons + promotions
    m = M().init()
    geo = {'capturedAt': time.strftime('%Y-%m-%d %H:%M:%S'), 'addresses': {}}
    for label, a in ADDRESSES.items():
        entry = {'addressId': a['addressId'], 'label': a['label'], 'channels': {}}
        for be in (2, 6):
            stores = m.call('delivery-query-stores', {'addressId': a['addressId'], 'beType': be})['data']
            entry['channels'][str(be)] = {
                'deliverable': [{'storeCode': s['storeCode'], 'beCode': s['beCode'],
                                 'storeName': s['storeName'],
                                 'businessStatus': s.get('businessStatus'),
                                 'businessStartTime': s.get('businessStartTime'),
                                 'businessEndTime': s.get('businessEndTime'),
                                 'reservation': s.get('reservation')} for s in stores],
            }
        near = m.call('query-nearby-stores', {
            'beType': 1, 'searchType': 2, 'city': a['city'], 'keyword': a['keyword']})['data']
        entry['nearby'] = [{'storeCode': s['storeCode'], 'storeName': s['storeName'],
                            'address': s.get('address'), 'distance': s.get('distance'),
                            'businessStatus': s.get('businessStatus')} for s in near]
        geo['addresses'][label] = entry
        print('geo', label, 'deliverable', {k: len(v['deliverable']) for k, v in entry['channels'].items()},
              'nearby', len(near), flush=True)

    promos = {}
    for code, name, be2, be6 in STORES:
        try:
            p = m.call('query-promotions', {'storeCode': code, 'beCode': be6, 'beType': 6, 'orderType': 2})
            promos[code] = p.get('data') or []
        except Exception as exc:
            promos[code] = {'error': str(exc)[:200]}
        print('promotions', code, len(promos[code]) if isinstance(promos[code], list) else 'err', flush=True)

    coupons = {}
    for code, name, be2, be6 in STORES:
        coupons[code] = {}
        for be, bec in ((2, be2), (6, be6)):
            try:
                c = m.call('query-store-coupons', {
                    'storeCode': code, 'beCode': bec, 'beType': be, 'orderType': 2})
                coupons[code][str(be)] = c.get('data') or []
            except Exception as exc:
                coupons[code][str(be)] = {'error': str(exc)[:200]}
        print('coupons', code, {k: len(v) if isinstance(v, list) else 'err' for k, v in coupons[code].items()},
              flush=True)

    assistance = {}
    for code, name, be2, be6 in STORES:
        try:
            a = m.call('query-meal-assistance', {
                'storeCode': code, 'beCode': be6, 'beType': 6, 'orderType': 2})
            assistance[code] = a.get('data') or {}
        except Exception as exc:
            assistance[code] = {'error': str(exc)[:200]}
    print('assistance done', flush=True)

    # 2) details for every product of every store/channel
    task_q = queue.Queue()
    for s in STORES:
        task_q.put(s)
    threads = [threading.Thread(target=worker, args=(i, task_q)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print('details fetched', len(results), flush=True)

    serial = {
        'geo': geo,
        'promotions': promos,
        'coupons': coupons,
        'assistance': assistance,
        'menus': MENUS,
        'details': {f'{c}|{b}|{p}': v for (c, b, p), v in results.items()},
    }
    (OUT / 'rec-raw.json').write_text(json.dumps(serial, ensure_ascii=False, indent=1))
    print('wrote', OUT / 'rec-raw.json')


if __name__ == '__main__':
    main()
