"""Read-only MCP snapshot capture. Credentials stay in local config, never in output."""
import json, urllib.request, pathlib, sys, os

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'


def _find_mcp_config():
    """Locate mcp.json. Checks $MCD_MCP_CONFIG, then repo root, then ~/.workbuddy."""
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


def _load_mcp_config():
    """Return the mcd-mcp entry, or exit with an actionable message."""
    path = _find_mcp_config()
    if path is None:
        raise SystemExit(
            'MCP config not found.\n'
            'Create an mcp.json containing:\n'
            '  {"mcpServers": {"mcd-mcp": {"url": "...", "headers": {...}}}}\n'
            'then run with MCD_MCP_CONFIG=/path/to/mcp.json, or place it at\n'
            '~/.workbuddy/mcp.json')
    try:
        return json.loads(path.read_text(encoding='utf-8'))['mcpServers']['mcd-mcp']
    except Exception as exc:
        raise SystemExit(f'Cannot read MCP config at {path}: {exc}')

config = _load_mcp_config()
session=None
counter=0
def rpc(method, params=None):
    global session,counter
    counter+=1
    headers={**config['headers'],'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2024-11-05'}
    if session: headers['Mcp-Session-Id']=session
    payload={'jsonrpc':'2.0','id':counter,'method':method}
    if params is not None: payload['params']=params
    req=urllib.request.Request(config['url'],data=json.dumps(payload).encode(),headers=headers)
    with urllib.request.urlopen(req,timeout=25) as response:
        session=response.headers.get('Mcp-Session-Id',session)
        text=response.read().decode()
    if text.lstrip().startswith('{'): result=json.loads(text)
    else:
        items=[json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith('data:') and line[5:].strip().startswith('{')]
        result=next(x for x in items if x.get('id')==counter)
    if 'error' in result: raise RuntimeError(str(result['error']))
    return result.get('result',{})

def call(name,params):
    result=rpc('tools/call',{'name':name,'arguments':params})
    if result.get('isError'): raise RuntimeError('MCP returned an error for '+name)
    for block in result.get('content',[]):
        text=block.get('text','')
        # API text includes schema prose before the actual JSON.
        start=text.find('{"success"')
        if start>=0:
            obj,_=json.JSONDecoder().raw_decode(text[start:])
            if not obj.get('success'): raise RuntimeError(name+': '+str(obj.get('message')))
            return obj
    return {'content':result.get('content',[])}

rpc('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'qibao-readonly-snapshot','version':'1.0'}})
listing=rpc('tools/list',{})
names=[x['name'] for x in listing.get('tools',[])]
if 'query-meals' not in names: raise RuntimeError('Expected query-meals absent. Tools: '+','.join(names))
out=DATA_DIR/'menu-snapshot.json'
DATA_DIR.mkdir(parents=True, exist_ok=True)
if out.exists(): raise RuntimeError('Snapshot exists; refusing silent overwrite')
data={'source':'McDonalds MCP read-only snapshot','stores':[]}
for code,label,normal,group in [('1450256','七宝领展餐厅','145025602','145025681'),('1450442','虹桥万创中心餐厅','145044202','145044281'),('1450374','融信上坤中心餐厅','145037402','145037481')]:
    store={'code':code,'name':label,'channels':{}}
    for kind,be in [(2,normal),(6,group)]:
        params={'storeCode':code,'beCode':be,'beType':kind,'orderType':2}
        menu=call('query-meals',params)
        coupons=call('query-store-coupons',params)
        channel={'beCode':be,'menu':menu,'coupons':coupons}
        if kind==6: channel['promotions']=call('query-promotions',params)
        store['channels'][str(kind)]=channel
        print(label,kind,'categories',len(menu['data']['categories']),'products',len(menu['data']['meals']),flush=True)
    data['stores'].append(store)
    # Persist partial progress explicitly, UI only uses completed datasets.
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2))
data['nutrition']=call('list-nutrition-foods',{})
data['details']={}
for code in ['9900003041','901450','903071']:
    data['details'][code]=call('query-meal-detail',{'storeCode':'1450256','beCode':'145025602','beType':2,'orderType':2,'code':code})
data['complete']=True
out.write_text(json.dumps(data,ensure_ascii=False,indent=2))
print('Saved full snapshot without credentials:',out,flush=True)
