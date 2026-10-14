"""Read-only latency evidence for the project identity endpoint."""
import json,time,urllib.request
from .manage import ROOT
rows=[]
for _ in range(8):
    start=time.monotonic()
    with urllib.request.urlopen('http://127.0.0.1:4188/api/mhs/identity',timeout=3) as response:
        value=json.load(response)
    rows.append({'seconds':time.monotonic()-start,'run_id':value['run_id']})
directory=ROOT/'runs/mhs-v2/acceptance/camera-dialogue-20261011'
(directory/'identity-latency.json').write_text(json.dumps(rows,indent=2))
print(json.dumps(rows))
