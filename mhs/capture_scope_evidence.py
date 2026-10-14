"""Record existing guard denials and test outputs without running Agent commands."""
import json,time
from pathlib import Path
root=Path('/mnt/sda/omni-yezh/hospital-delivery-agent')
directory=root/'runs/mhs-v2/acceptance'/('tool-scope-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir()
source=root/'runs/mhs-v2/policy-denials.jsonl'
denials=[json.loads(x) for x in source.read_text().splitlines()] if source.exists() else []
report={'scope':'Observed native tool_call denials; assertion suites are separate',
        'denials':denials,'actual_native_guard_observed':any(x['name']=='call_device' for x in denials)}
for name in ['business-unit-final.log','scope-unit-final.log','session-scope-unit.log']:
    p=root/'logs/mhs'/name
    if p.exists():(directory/name).write_bytes(p.read_bytes())
(directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(directory)
