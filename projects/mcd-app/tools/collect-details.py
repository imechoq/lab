"""Append meal-detail snapshots for key products (read-only, no orders)."""
import json, os, pathlib, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
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

def _load_mcp_config():
    path = _find_mcp_config()
    if path is None:
        raise SystemExit('MCP config not found. Set MCD_MCP_CONFIG=/path/to/mcp.json or put it at ~/.workbuddy/mcp.json')
    try:
        return json.loads(path.read_text(encoding='utf-8'))['mcpServers']['mcd-mcp']
    except Exception as exc:
        raise SystemExit(f'Cannot read MCP config at {path}: {exc}')

config = _load_mcp_config()
path = ROOT / 'outputs/menu-snapshot.json'
snapshot = json.loads(path.read_text())
session = None
counter = 0

def rpc(method, params=None):
    global session, counter
    counter += 1
    headers = {**config['headers'], 'Content-Type': 'application/json',
               'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2024-11-05'}
    if session:
        headers['Mcp-Session-Id'] = session
    payload = {'jsonrpc': '2.0', 'id': counter, 'method': method}
    if params is not None:
        payload['params'] = params
    request = urllib.request.Request(config['url'], data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=25) as response:
        session = response.headers.get('Mcp-Session-Id', session)
        text = response.read().decode()
    if text.lstrip().startswith('{'):
        result = json.loads(text)
    else:
        rows = [json.loads(line[5:].strip()) for line in text.splitlines()
                if line.startswith('data:') and line[5:].strip().startswith('{')]
        result = next(row for row in rows if row.get('id') == counter)
    if 'error' in result:
        raise RuntimeError(str(result['error']))
    return result.get('result', {})

def call(name, params):
    result = rpc('tools/call', {'name': name, 'arguments': params})
    for block in result.get('content', []):
        text = block.get('text', '')
        start = text.find('{"success"')
        if start >= 0:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if not obj.get('success'):
                raise RuntimeError(name + ': ' + str(obj.get('message')))
            return obj
    raise RuntimeError('no payload for ' + name)

rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                   'clientInfo': {'name': 'detail-snapshot', 'version': '1.0'}})
targets = ['9900002995', '9900016490', '901100', '901440', '501205', '903050',
           '520688', '9900011129', '9900016540', '902372', '901401', '9900016510',
           '9900003001', '9900011071', '9900016503', '9900012938', '9900009882',
           '9900012940', '9900007762', '902060', '521953', '9900012945']
for code in targets:
    if code in snapshot['details']:
        continue
    try:
        snapshot['details'][code] = call('query-meal-detail', {
            'storeCode': '1450256', 'beCode': '145025602', 'beType': 2, 'orderType': 2, 'code': code})
        print('detail', code, flush=True)
    except Exception as error:
        print('skip', code, str(error)[:80], flush=True)
        continue
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
print('details total', len(snapshot['details']))
