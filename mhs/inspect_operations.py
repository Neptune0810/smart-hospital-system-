import json
from .manage import HOME
d=json.loads((HOME/'data/daemon/package-operations/operations.json').read_text())
for x in d['operations'][-8:]:
    op=x['operation']
    print(json.dumps({k:v for k,v in op.items() if k!='preview'},indent=2))
