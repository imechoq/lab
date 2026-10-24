"""Transform the read-only MCP snapshot into a compact, credential-free data file."""
import json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
snapshot = json.loads((ROOT / 'outputs/menu-snapshot.json').read_text())
assert snapshot.get('complete') is True

def money(value):
    try:
        return int(round(float(str(value).strip()) * 100))
    except (TypeError, ValueError):
        return None

def parse_toon(raw):
    """Parse the nutrition TOON table by fields, never by naive comma positions."""
    rows = {}
    if not isinstance(raw, str):
        return rows
    header = raw.split('\n', 1)[0]
    match = re.search(r'\{([^}]*)\}', header)
    fields = [f.strip() for f in match.group(1).split(',')] if match else []
    if not fields or fields[0] != 'productName':
        raise RuntimeError('Unexpected nutrition header')
    numeric = fields[1:]
    for line in raw.split('\n')[1:]:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.rsplit(',', len(numeric))]
        if len(parts) != len(numeric) + 1:
            continue
        name, values = parts[0], parts[1:]
        record = {}
        for key, value in zip(numeric, values):
            if key == 'nutritionDescription':
                record[key] = None if value in ('', 'null') else value
                continue
            try:
                record[key] = None if value in ('', 'null') else float(value)
            except ValueError:
                record[key] = None
        rows[name] = record
    return rows

nutrition_raw = snapshot.get('nutrition', {}).get('data')
nutrition = parse_toon(nutrition_raw)

def nutrition_for(name):
    clean = name.replace('™', '').replace('（', '(').replace('）', ')').strip()
    for key, row in nutrition.items():
        if key.replace('（', '(').replace('）', ')') == clean:
            return {'kcal': row.get('energyKcal'), 'protein': row.get('protein'),
                    'fat': row.get('fat'), 'carb': row.get('carbohydrate'), 'sodium': row.get('sodium')}
    for key, row in nutrition.items():
        base = key.split('-')[0]
        if base and (base in clean or clean in base):
            return {'kcal': row.get('energyKcal'), 'protein': row.get('protein'),
                    'fat': row.get('fat'), 'carb': row.get('carbohydrate'), 'sodium': row.get('sodium')}
    return None

stores = []
for store in snapshot['stores']:
    entry = {'code': store['code'], 'name': store['name'], 'channels': {}}
    for kind, channel in store['channels'].items():
        menu = channel['menu']['data']
        meals = menu['meals']
        categories = []
        seen = set()
        for category in menu['categories']:
            items = []
            for ref in category['meals']:
                code = ref['code']
                if code in seen or code not in meals:
                    continue
                seen.add(code)
                meal = meals[code]
                price = money(meal.get('currentPrice'))
                original = money(meal.get('originalPrice'))
                if price is None:
                    continue
                items.append({
                    'code': code,
                    'name': meal.get('name', '').strip(),
                    'image': meal.get('image'),
                    'price': price,
                    'original': original,
                    'discount': meal.get('discountType') or None,
                    'withOrder': bool(meal.get('canWithOrder')),
                    'tags': [t.strip() for t in ref.get('tags', []) if t.strip()],
                    'nutrition': nutrition_for(meal.get('name', '')),
                })
            if items:
                categories.append({'name': category['name'].replace('\n', ' '), 'items': items})
        promoted = channel.get('promotions', {}).get('data') or []
        rules = []
        for rule in promoted:
            detail = (rule.get('ruleDetail') or {}).get('orderDiscount') or {}
            if not detail:
                continue
            rules.append({
                'service': rule.get('gmServiceCode'),
                'discount': detail.get('discount'),
                'threshold': detail.get('startDiscountPoint'),
            })
        entry['channels'][kind] = {
            'beCode': channel['beCode'],
            'categories': categories,
            'productCount': sum(len(c['items']) for c in categories),
            'couponCount': len(channel.get('coupons', {}).get('data') or []),
            'promotions': rules,
        }
    stores.append(entry)

payload = {
    'capturedAt': snapshot.get('source', '') and '2026-10-24 13:47 (Asia/Shanghai)',
    'address': '上海市闵行区漕宝路3509号 京东MALL(上海七宝店)',
    'stores': stores,
    'details': snapshot.get('details', {}),
}
(ROOT / 'outputs/menu-data.js').write_text(
    'window.MCD_DATA = ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + ';\n')
print('stores', len(stores))
for store in stores:
    print(store['name'], {k: (v['productCount'], len(v['categories'])) for k, v in store['channels'].items()})
print('nutrition rows', len(nutrition))
