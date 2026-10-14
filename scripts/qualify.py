#!/usr/bin/env python3
"""Run actual Agnes acceptance; preserve every attempt; no scripted fallback."""
import json,time,uuid,argparse
from pathlib import Path
from experiment import run,put
from agh_client import invoke,export
from report import enrich
ROOT=Path(__file__).resolve().parents[1]
def calls(directory):
    ledger=next((directory/'model').glob('*/agh-session.jsonl'))
    rows=[json.loads(line) for line in ledger.read_text().splitlines()]
    results={r['data']['toolUseId']:r['data'] for r in rows if r['type']=='tool/result'}
    good=[]
    for r in rows:
        if r['type']!='tool/call' or r.get('origin')!='model':continue
        value=results.get(r['data']['toolUseId'],{})
        if value.get('isError'):continue
        texts=[c['text'] for c in value.get('content',[]) if c.get('type')=='text']
        try:body=json.loads(texts[0])
        except (IndexError,ValueError):continue
        if body.get('ok'):good.append(dict(name=r['data']['name'],args=r['data']['args'],seq=r['seq']))
    return good
def verify(directory,report):
    s=json.loads((directory/'final-state.json').read_text())
    return report['error'] is None and not report['unfinished'] and 'depot' in s['at_stations'] and report['error_deliveries']==0 and any(v['name'].endswith('_verify_run') for v in calls(directory))
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--normal-existing');ap.add_argument('--resume-suite');a=ap.parse_args()
    protocol=json.loads((ROOT/'configs/experiment.json').read_text())
    byid={v['id']:v for v in protocol['cases']}
    proof=json.loads((ROOT/'evidence/acceptance.json').read_text())
    attempt=ROOT/'runs/acceptance-agent'/(a.resume_suite or ('suite-'+uuid.uuid4().hex[:8]));attempt.mkdir(parents=True,exist_ok=True)
    for caseid,label in [('normal_single','normal'),('urgent_on_route','urgent'),('receiver_recovery','recovery'),('receiver_permanent','permanent')]:
        previous=attempt/(label+'-gate.json')
        if previous.exists() and json.loads(previous.read_text())['status']=='passed':
            print(label,'previously passed; preserving evidence',flush=True);continue
        put(ROOT/'runs/qualification-current.json',{'phase':'running','case':caseid,'directory':str(attempt),'wall_time':time.time()})
        if label=='normal' and a.normal_existing:
            directory=ROOT/a.normal_existing
            report=json.loads((directory/'report.json').read_text())
            modeldir=next((directory/'model').iterdir());session=json.loads(sorted(modeldir.glob('turn-*.json'))[-1].read_text())['sessionId']
            r=invoke('只调用 hospital-delivery get_state 和 verify_run，根据真实回执报告 delivered、failed、unfinished 及返回depot的状态。不要修改业务或导航。',modeldir,session)
            export(r['sessionId'],modeldir)
            report['model_turns']+=1;report=enrich(report,directory,byid[caseid]);put(directory/'report.json',report)
        else:
            directory=attempt/label
            report=run(byid[caseid],101,'agnes_feedback',directory,record=label!='permanent')
        good=verify(directory,report)
        if label in ['normal','urgent','recovery']:good=good and report['delivered']==report['total']
        if label=='urgent':good=good and report['delivery_sequence']==['U1','N1'] and report['navigation_cancellations']>=1 and any(v['name'].endswith('_cancel_navigation') for v in calls(directory))
        if label=='recovery':good=good and report['recovered']==1 and (any(v['name'].endswith('_defer_order') for v in calls(directory)) or report['delivery_sequence']==['N2','N1'])
        if label=='permanent':good=good and len(report['failed'])==1 and report['delivered']==1
        result={'status':'passed' if good else 'failed','directory':str(directory.relative_to(ROOT)),'report':report,'actual_model_tools':calls(directory)}
        put(attempt/(label+'-gate.json'),result)
        if label in ['normal','urgent']:
            proof['agent'].update({label:result})
            proof['agent']['status']='passed' if label=='urgent' and good and proof['agent'].get('normal',{}).get('status')=='passed' else 'in_progress' if good else 'failed'
        else:
            proof['recovery'].update({label:result})
            proof['recovery']['status']='passed' if label=='permanent' and good and proof['recovery'].get('recovery',{}).get('status')=='passed' else 'in_progress' if good else 'failed'
        put(ROOT/'evidence/acceptance.json',proof)
        print(label,result['status'],report['delivery_sequence'],report.get('error'),flush=True)
        if not good:
            put(ROOT/'runs/qualification-current.json',{'phase':'failed','case':caseid,'directory':str(directory),'wall_time':time.time()})
            raise RuntimeError('actual_agent_acceptance_failed:'+label)
    put(ROOT/'runs/qualification-current.json',{'phase':'passed','directory':str(attempt),'wall_time':time.time()})
if __name__=='__main__':
    try:main()
    except Exception as e:
        current=ROOT/'runs/qualification-current.json'
        state=json.loads(current.read_text()) if current.exists() else {}
        state.update(phase='failed',error=str(e),wall_time=time.time());put(current,state)
        if state.get('directory'):
            folder=Path(state['directory'])
            if ROOT/'runs/acceptance-agent' in folder.parents:put(folder/('failure-'+uuid.uuid4().hex[:8]+'.json'),state)
        raise
