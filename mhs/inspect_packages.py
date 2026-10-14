import json
from .manage import HOME
for filename in ['agnes-lock.json','profile.yaml']:
    d=json.loads((HOME/'profiles/local-dev'/filename).read_text())
    print(filename,json.dumps({k:d.get(k) for k in ['packages','presets']},indent=2))
