"""Preserve post-completion idle anomaly separately from the frozen acceptance."""
import json,time
from .manage import ROOT,api
directory=ROOT/'runs/mhs-v2/acceptance'/('post-completion-idle-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir()
state=api();(directory/'state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
(directory/'live-report.json').write_text(json.dumps(api('/api/report'),ensure_ascii=False,indent=2))
source=ROOT/'runs/mhs-v2'/state['run_id']
for name in ['events.jsonl','report.json']:
    (directory/name).write_bytes((source/name).read_bytes())
print(directory)
