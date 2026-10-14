"""Export an explicitly selected synthetic hospital conversation and ledger."""
import json,subprocess,sys
from pathlib import Path
from .manage import ROOT,CLI,ENV

def main():
    session=sys.argv[1];directory=Path(sys.argv[2]).resolve()
    if ROOT/'runs/mhs-v2/acceptance' not in directory.parents:raise ValueError('evidence_directory_outside_project_acceptance')
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/'session.jsonl'
    subprocess.run(CLI+['export',session,'--format','agnes','--raw','-o',str(target)],env=ENV,cwd=ROOT,check=True,capture_output=True,text=True)
    rows=[json.loads(line) for line in target.read_text().splitlines()]
    requests=[r for r in rows if r.get('type')=='request/sent']
    costs=[r['data'] for r in rows if r.get('type')=='cost/ledger']
    tokens={};seen=set();by_purpose={}
    for c in costs:
        ident=c.get('effectId')
        if ident and ident in seen:continue
        if ident:seen.add(ident)
        purpose=by_purpose.setdefault(c.get('purpose','unknown'),{})
        for key,value in c.get('tokens',{}).items():
            if isinstance(value,(int,float)):
                tokens[key]=tokens.get(key,0)+value;purpose[key]=purpose.get(key,0)+value
    result={'session':session,'url':'http://127.0.0.1:4189/?session='+session,'request_count':len(requests),'ledger_count':len(costs),'token_usage':tokens,'token_usage_by_purpose':by_purpose,'token_method':'Per-effect ledger entries, deduplicated by effectId; token counts are provider ledger estimates, not confirmed billing','actual_monetary_cost':None,'cost_note':'Actual billing unavailable; preserve raw estimated ledger separately'}
    (directory/'session-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
