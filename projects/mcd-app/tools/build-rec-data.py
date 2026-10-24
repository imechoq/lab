"""Compile raw MCP responses into the browser data file.

Every value traces back to one MCP response:
  distance   <- query-nearby-stores
  deliver    <- delivery-query-stores
  price/name <- query-meals
  sauce      <- query-meal-detail (modification values + round choices)
  servings   <- query-meal-detail (round structure, cross-checked with name)
  ingredients<- query-meal-detail (name + round choices + modification values)
  promos     <- query-promotions
  coupons    <- query-store-coupons
  assist     <- query-meal-assistance
  campaigns  <- campaign-calendar
"""
import json, pathlib, re, hashlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
raw = json.loads((DATA_DIR / 'rec-raw.json').read_text(encoding='utf-8'))
snap = json.loads((DATA_DIR / 'menu-snapshot.json').read_text(encoding='utf-8'))

STORES = {
    '1450256': '麦当劳上海闵行七宝领展餐厅',
    '1450442': '麦当劳上海虹桥万创中心餐厅',
    '1450374': '麦当劳上海融信上坤中心餐厅',
    '1450516': '麦当劳上海虹口双狮汇大厦餐厅',
    '1450604': '麦当劳上海虹口东长治路友邦金融中心餐厅',
    '1450395': '麦当劳上海虹口星展未来中心餐厅',
}

# Avoid options are keyed by what the interface actually exposes.
# Each entry lists the real product/ingredient markers seen in MCP responses.
INGREDIENT_RULES = {
    '牛': ['牛', '安格斯', '牛肉', '肉霸堡'],
    '猪': ['猪', '培根', '猪柳'],
    '鸡': ['鸡', '脆鸡', '鸡腿', '鸡翅', '鸡柳', '炸鸡'],
    '鱼': ['鱼', '鳕鱼'],
    '奶': ['奶', '芝士', '吉士', '干酪', '冰淇淋', '新地', '拿铁', '奶铁', '麦旋风', '派', '蛋挞'],
    '蛋': ['蛋'],
    '酱': ['酱'],
}
# 虾 and 花生 never appear in any MCP product string, so they are not offered.


def money(value):
    try:
        return int(round(float(str(value).strip()) * 100))
    except (TypeError, ValueError):
        return None


def parse_toon(raw_text):
    rows = {}
    if not isinstance(raw_text, str):
        return rows
    header = raw_text.split('\n', 1)[0]
    match = re.search(r'\{([^}]*)\}', header)
    fields = [f.strip() for f in match.group(1).split(',')] if match else []
    if not fields or fields[0] != 'productName':
        return rows
    numeric = fields[1:]
    for line in raw_text.split('\n')[1:]:
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


nutrition_rows = parse_toon(snap.get('nutrition', {}).get('data'))


def nutrition_for(name):
    clean = name.replace('™', '').replace('（', '(').replace('）', ')').strip()
    for key, row in nutrition_rows.items():
        if key.replace('（', '(').replace('）', ')') == clean:
            return _nutri(row)
    for key, row in nutrition_rows.items():
        base = key.split('-')[0]
        if base and (base in clean or clean in base):
            return _nutri(row)
    return None


def _nutri(row):
    return {'kcal': row.get('energyKcal'), 'protein': row.get('protein'),
            'fat': row.get('fat'), 'carb': row.get('carbohydrate'), 'sodium': row.get('sodium')}


SAUCE_RE = re.compile(r'酱')
SERVING_RE = re.compile(r'(单人|双人|三人|四人|五人|多人)')
SERVING_TABLE = {'单人': 1, '双人': 2, '三人': 3, '四人': 4, '五人': 5, '多人': 2}


def mod_groups(detail):
    """Every modification group with the round choice that owns it."""
    out = []
    for group in (detail.get('rounds') or []):
        for choice in (group.get('choices') or []):
            modification = choice.get('modification')
            if modification:
                for item in (modification.get('items') or []):
                    out.append((choice.get('name') or '', item, choice.get('code')))
    modification = detail.get('modification')
    if modification:
        for item in (modification.get('items') or []):
            out.append(('', item, detail.get('code')))
    return out


def analyse_sauce(detail):
    """Sauce facts read from the interface.

    removable : a sauce the interface lets you drop (value minQuantity=0 and the
                group minValues=0) -> the item can be ordered without it
    inherent  : a sauce baked into the product (its own round choice that is part
                of the set, or carried in the product name) -> cannot be dropped
    """
    removable = []
    inherent = set()
    if SAUCE_RE.search(detail.get('name') or ''):
        inherent.add('商品名称含酱')
    for group in (detail.get('rounds') or []):
        for choice in (group.get('choices') or []):
            choice_name = str(choice.get('name') or '')
            if not SAUCE_RE.search(choice_name):
                continue
            if (choice.get('quantity') or 0) > 0 or (group.get('minQuantity') or 0) >= 1:
                inherent.add(choice_name)
    for owner, item, _ in mod_groups(detail):
        for value in (item.get('values') or []):
            value_name = str(value.get('name') or '')
            if not SAUCE_RE.search(value_name):
                continue
            if value.get('minQuantity') == 0 and item.get('minValues') == 0:
                removable.append({
                    'name': value_name,
                    'code': value.get('code'),
                    'owner': owner,
                    'price': value.get('price') or 0,
                    'selectedKey': value.get('selectedKey'),
                    'unselectedKey': value.get('unselectedKey'),
                })
            else:
                inherent.add(value_name)
    unique = {}
    for entry in removable:
        unique[(entry['name'], entry['owner'])] = entry
    removable = sorted(unique.values(), key=lambda e: (e['owner'], e['name']))
    return {
        'removable': removable,
        'inherent': sorted(inherent),
        'hasSauce': bool(removable) or bool(inherent),
        'canRemove': bool(removable),
    }


def analyse_servings(detail):
    name = detail.get('name') or ''
    label = SERVING_RE.search(name)
    if label:
        return SERVING_TABLE[label.group(1)], 'name:' + label.group(1)
    rounds = detail.get('rounds') or []
    if not rounds:
        return 1, 'single'
    mains = 0
    drinks = 0
    for group in rounds:
        group_name = str(group.get('name') or '')
        quantity = group.get('quantity') or 0
        if re.search(r'主食|汉堡|主餐', group_name):
            mains += quantity
        if re.search(r'饮料|饮品|咖啡', group_name):
            drinks = max(drinks, quantity)
    return max(mains, drinks, 1), 'rounds'


def analyse_combo(detail):
    rounds = detail.get('rounds') or []
    if not rounds:
        return {'isCombo': False, 'rounds': 0, 'pickCount': 0}
    pickable = sum(1 for g in rounds if len(g.get('choices') or []) > 1)
    return {'isCombo': True, 'rounds': len(rounds), 'pickCount': pickable}


def analyse_ingredients(detail):
    """Which avoid groups this product belongs to, from its real strings."""
    parts = [detail.get('name') or '']
    for group in (detail.get('rounds') or []):
        parts.append(str(group.get('name') or ''))
        for choice in (group.get('choices') or []):
            parts.append(str(choice.get('name') or ''))
        for _, item, _ in [(0, i, 0) for i in (group.get('modification') or {}).get('items', [])]:
            for value in (item.get('values') or []):
                parts.append(str(value.get('name') or ''))
    for item in (detail.get('modification') or {}).get('items', []):
        for value in (item.get('values') or []):
            parts.append(str(value.get('name') or ''))
    text = ' '.join(parts)
    flags = {}
    for key, markers in INGREDIENT_RULES.items():
        if key == '酱':
            continue  # sauce is handled by its own interface-driven analysis
        hit = [m for m in markers if m in text]
        if hit:
            flags[key] = hit
    return flags


def trim_detail(detail):
    """Only keep what the product sheet renders."""
    out = {'name': detail.get('name'), 'supportModify': detail.get('supportModify')}
    if detail.get('image'):
        out['image'] = detail['image']
    rounds = []
    for group in (detail.get('rounds') or []):
        choices = []
        for choice in (group.get('choices') or []):
            entry = {'code': choice.get('code'), 'name': choice.get('name'),
                     'quantity': choice.get('quantity'), 'isDefault': choice.get('isDefault'),
                     'supportModify': choice.get('supportModify'),
                     'diffPrice': choice.get('diffPrice')}
            modification = choice.get('modification')
            if modification:
                entry['modification'] = {'items': [
                    {'maxValues': i.get('maxValues'), 'minValues': i.get('minValues'),
                     'values': [{'code': v.get('code'), 'name': v.get('name'),
                                 'price': v.get('price'), 'maxQuantity': v.get('maxQuantity'),
                                 'minQuantity': v.get('minQuantity'),
                                 'selectedQuantity': v.get('selectedQuantity'),
                                 'selectedKey': v.get('selectedKey'),
                                 'unselectedKey': v.get('unselectedKey')}
                                for v in i.get('values', [])]}
                    for i in modification.get('items', [])]}
            choices.append(entry)
        rounds.append({'name': group.get('name'), 'quantity': group.get('quantity'),
                       'maxQuantity': group.get('maxQuantity'),
                       'minQuantity': group.get('minQuantity'), 'choices': choices})
    out['rounds'] = rounds
    modification = detail.get('modification')
    if modification:
        out['modification'] = {'items': [
            {'maxValues': i.get('maxValues'), 'minValues': i.get('minValues'),
             'values': [{'code': v.get('code'), 'name': v.get('name'),
                         'price': v.get('price'), 'maxQuantity': v.get('maxQuantity'),
                         'minQuantity': v.get('minQuantity'),
                         'selectedQuantity': v.get('selectedQuantity'),
                         'selectedKey': v.get('selectedKey'),
                         'unselectedKey': v.get('unselectedKey')}
                        for v in i.get('values', [])]}
            for i in modification.get('items', [])]}
    return out


def compile_promotions(store_code):
    """Real discount rules. Keeps the legacy {service, discount, threshold} shape
    the front end already reads, and adds the full rule for the new logic."""
    rules = raw['promotions'].get(store_code) or []
    out = []
    if not isinstance(rules, list):
        return out
    for rule in rules:
        detail = (rule.get('ruleDetail') or {}).get('orderDiscount') or {}
        threshold = detail.get('startDiscountPoint')
        discount = detail.get('discount')
        if threshold is None or discount is None:
            continue
        out.append({
            # legacy keys the front end reads
            'service': rule.get('gmServiceCode'),
            'discount': discount,
            'threshold': threshold,
            # full rule for the new tier logic
            'promotionId': rule.get('promotionId'),
            'promotionType': rule.get('promotionType'),
            'ruleCategory': rule.get('ruleCategory'),
            'gmServiceCode': rule.get('gmServiceCode'),
            'thresholdYuan': int(float(threshold)),
            'discountPct': int(float(discount)),
            'startTime': rule.get('startTime'),
            'endTime': rule.get('endTime'),
            'excluded': sorted({p.get('productCode') for p in (rule.get('products') or [])
                                if p.get('type') == '2' and p.get('productCode')}),
        })
    out.sort(key=lambda r: (r['gmServiceCode'] or '', r['thresholdYuan']))
    return out


def compile_assistance(store_code):
    data = raw['assistance'].get(store_code) or {}
    return [{'gmServiceCode': i.get('gmServiceCode'), 'gmServiceName': i.get('gmServiceName'),
             'serviceItems': i.get('serviceItems') or [], 'enable': bool(i.get('enable')),
             'unusableReason': i.get('unusableReason'), 'selected': bool(i.get('selected'))}
            for i in (data.get('mealAssistanceItems') or [])]


details = raw['details']
geo = raw['geo']
menus = raw.get('menus') or {}

distance_by_address = {}
for label, entry in geo['addresses'].items():
    table = {}
    for near in entry.get('nearby') or []:
        if near.get('distance') is not None:
            table[near['storeCode']] = int(near['distance'])
    distance_by_address[label] = table

deliver_by_address = {
    label: {str(be): [s['storeCode'] for s in ch['deliverable']]
            for be, ch in entry['channels'].items()}
    for label, entry in geo['addresses'].items()
}

store_address = {}
for entry in geo['addresses'].values():
    for near in entry.get('nearby') or []:
        store_address.setdefault(near['storeCode'], near.get('address'))

be_codes = {}
for entry in geo['addresses'].values():
    for be, ch in entry['channels'].items():
        for s in ch['deliverable']:
            be_codes[(s['storeCode'], be)] = s['beCode']

compiled_stores = []
detail_index = {}
pickup_menus = raw.get('pickupMenus') or {}

for store_code, store_name in STORES.items():
    entry = {'code': store_code, 'name': store_name,
             'address': store_address.get(store_code), 'channels': {}}
    for be_type in ('1', '2', '6'):
        if be_type == '1':
            # Pickup: beType=1 / orderType=1 and NO beCode. Passing a delivery
            # beCode here silently returns the delivery price list.
            menu_data = pickup_menus.get(store_code)
        else:
            menu_data = (menus.get(store_code) or {}).get(be_type)
            if not menu_data:
                for snap_store in snap['stores']:
                    if snap_store['code'] == store_code and be_type in snap_store['channels']:
                        menu_data = snap_store['channels'][be_type]['menu']['data']
                        break
        if not menu_data:
            continue
        meals = menu_data['meals']
        categories = []
        seen = set()
        for category in menu_data['categories']:
            items = []
            for ref in category['meals']:
                code = ref['code']
                if code in seen or code not in meals:
                    continue
                seen.add(code)
                meal = meals[code]
                price = money(meal.get('currentPrice'))
                if price is None:
                    continue
                detail = (details.get(f'{store_code}|{be_type}|{code}') or {}).get('data') or {}
                servings, servings_src = analyse_servings(detail) if detail else (1, 'no-detail')
                sauce = analyse_sauce(detail) if detail else {
                    'removable': [], 'inherent': [], 'hasSauce': False, 'canRemove': False}
                combo = analyse_combo(detail) if detail else {'isCombo': False, 'rounds': 0, 'pickCount': 0}
                ingredients = analyse_ingredients(detail) if detail else {}
                tags = [t.strip() for t in (ref.get('tags') or []) if t and t.strip()]
                items.append({
                    'code': code,
                    'name': (meal.get('name') or '').strip(),
                    'image': meal.get('image'),
                    'price': price,
                    'original': money(meal.get('originalPrice')),
                    'discount': meal.get('discountType') or None,
                    'withOrder': bool(meal.get('canWithOrder')),
                    'tags': tags,
                    'nutrition': nutrition_for(meal.get('name') or ''),
                    'servings': servings,
                    'servingsSrc': servings_src,
                    'sauce': sauce,
                    'combo': combo,
                    'ingredients': ingredients,
                    'promoExcluded': False,
                })
            if items:
                categories.append({'name': (category.get('name') or '').replace('\n', ' '), 'items': items})
        # mark products excluded from the group discount rules
        if be_type == '6':
            excluded = set()
            for rule in compile_promotions(store_code):
                excluded.update(rule['excluded'])
            for category in categories:
                for item in category['items']:
                    item['promoExcluded'] = item['code'] in excluded
        coupons = raw['coupons'].get(store_code) or {}
        coupon_list = coupons.get(be_type) if isinstance(coupons, dict) else []
        entry['channels'][be_type] = {
            'beCode': be_codes.get((store_code, be_type), ''),
            'orderType': 1 if be_type == '1' else 2,
            'categories': categories,
            'productCount': sum(len(c['items']) for c in categories),
            'couponCount': len(coupon_list) if isinstance(coupon_list, list) else 0,
            'promotions': compile_promotions(store_code) if be_type == '6' else [],
            'assistance': compile_assistance(store_code) if be_type == '6' else [],
        }
    if entry['channels']:
        compiled_stores.append(entry)

# per store+channel detail index for the product sheet
for key, value in details.items():
    if not value.get('data'):
        continue
    detail_index[key] = trim_detail(value['data'])

# Legacy compatibility: the front end reads DATA.details[code].data and
# DATA.details[code].data.rounds to detect combo products. Keep a bare-code
# view for the primary store so that detection still works offline.
legacy_details = {}
primary = '1450256'
for key, value in details.items():
    store_code, be_type, code = key.split('|')
    # beType 2 is the channel the original snapshot shipped, and the one the
    # combo detection was calibrated against.
    if store_code != primary or be_type != '2':
        continue
    legacy_details[code] = value

payload = {
    'capturedAt': raw['geo']['capturedAt'],
    'source': 'McDonalds MCP (mcd-mcp) read-only snapshot',
    # legacy key: the front end shows this as the delivery address
    'address': geo['addresses']['qibao']['label'],
    'addresses': {
        label: {
            'label': entry['label'],
            'addressId': entry['addressId'],
            'deliver': {be: deliver_by_address[label].get(be, []) for be in ('2', '6')},
            'distances': distance_by_address[label],
        } for label, entry in geo['addresses'].items()
    },
    'stores': compiled_stores,
    # legacy shape: bare product code -> {data: {...}} (combo detection reads .data.rounds)
    'details': legacy_details,
    # full index: "storeCode|beType|code" -> trimmed detail
    'detailIndex': detail_index,
    'campaigns': raw.get('campaigns', []),
    'campaignText': raw.get('campaignText', ''),
}

(ROOT / 'app' / 'menu-data.js').write_text(
    'window.MCD_DATA = ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + ';\n')

print('stores', len(compiled_stores))
for s in compiled_stores:
    print(' ', s['code'], s['name'][:24], {k: v['productCount'] for k, v in s['channels'].items()})
print('legacy details', len(legacy_details), '| detail index', len(detail_index))
print('campaigns', len(payload['campaigns']))
ing = {}
for s in compiled_stores:
    for c in s['channels'].values():
        for cat in c['categories']:
            for i in cat['items']:
                for k in i['ingredients']:
                    ing[k] = ing.get(k, 0) + 1
print('ingredient flag counts:', ing)
rem = sum(1 for s in compiled_stores for c in s['channels'].values()
          for cat in c['categories'] for i in cat['items'] if i['sauce']['canRemove'])
inh = sum(1 for s in compiled_stores for c in s['channels'].values()
          for cat in c['categories'] for i in cat['items']
          if i['sauce']['hasSauce'] and not i['sauce']['canRemove'])
print('sauce removable', rem, '| sauce inherent', inh)
print('file size %.0f KB' % ((ROOT / 'app' / 'menu-data.js').stat().st_size / 1024))
