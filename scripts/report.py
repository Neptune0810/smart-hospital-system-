"""Summarize actual runs; missing outcomes stay NA."""
import json,csv,shutil
from pathlib import Path
def enrich(report,directory,case):
    root=Path(__file__).resolve().parents[1]
    source=root/'runs'/report['run_id']/'events.jsonl'
    if source.exists():shutil.copyfile(source,directory/'events.jsonl')
    events=[json.loads(x) for x in source.read_text().splitlines()] if source.exists() else []
    ledgers=[]
    ledger=next((directory/'model').glob('*/agh-session.jsonl'), directory/'model'/'agh-session.jsonl')
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                row=json.loads(line)
                if row.get('type')=='cost/ledger':ledgers.append(row['data'])
            except (ValueError,KeyError):pass
    seen=set();unique=[]
    for item in ledgers:
        key=item.get('effectId')
        if key not in seen:seen.add(key);unique.append(item)
    keys=['input','output','cacheRead','cacheWrite','reasoning']
    report['token_usage']={k:sum(v.get('tokens',{}).get(k,0) for v in unique) for k in keys} if unique else (dict.fromkeys(keys,0) if report['method']=='rule' else None)
    report['model_requests']=sum(v.get('purpose')=='inference' for v in unique) if unique else (0 if report['method']=='rule' else None)
    report['inference_duration_ms']=sum(v.get('timing',{}).get('durationMs',0) for v in unique if v.get('purpose')=='inference') if unique else None
    report['billing_usd']=None
    report['billing_note']='AGH ledger estimated zero; actual monetary cost unavailable'
    report['delivery_rate']=report['delivered']/report['total'] if report['total'] else None
    report['on_time_rate']=report['on_time']/report['total'] if report['total'] else None
    restored={e['station']:e['sim_time'] for e in events if e['kind']=='receiver_changed' and e['available']}
    eligible=[o for o in json.loads((directory/'final-state.json').read_text())['orders'].values() if o['dropoff_station'] in restored]
    report['recovery_eligible']=len(eligible)
    report['recovered']=sum(o['status']=='delivered' and o['receipt']['sim_time']>=restored[o['dropoff_station']] for o in eligible)
    report['recovery_rate']=report['recovered']/len(eligible) if eligible else None
    report['delivery_sequence']=[e['receipt']['order_id'] for e in events if e['kind']=='delivery_confirmed']
    report['navigation_cancellations']=sum(e['kind']=='navigation_canceled' for e in events)
    probes=directory/'boundary-probes.json'
    report['boundary_probes_passed']=all(v['passed'] for v in json.loads(probes.read_text())) if probes.exists() else None
    final=json.loads((directory/'final-state.json').read_text())
    n=final['navigation']
    report['returned_to_depot']='depot' in final['at_stations'] and not (n and n['status'] in ['pending','running','canceling'])
    report['mission_completed']=not report['error'] and not report['unfinished'] and report['returned_to_depot']
    report['status']='completed' if report['mission_completed'] else 'failed_or_incomplete'
    return report
def summarize(root):
    root=Path(root);batch=root/'runs/experiment-v6';protocol=json.loads((root/'configs/experiment.json').read_text())
    rows=[]
    for case in protocol['cases']:
        for seed in protocol['seeds']:
            for method in protocol['methods']:
                p=batch/(case['id']+'_'+str(seed)+'_'+method)/'report.json'
                row=json.loads(p.read_text()) if p.exists() else dict(case_id=case['id'],seed=seed,method=method,status='NA_not_run')
                rows.append(row)
    batch.mkdir(exist_ok=True);(batch/'all-results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    fields=['case_id','seed','method','status','mission_completed','returned_to_depot','total','delivered','delivery_rate','on_time_rate','recovery_rate','path_length','wall_seconds','elapsed_sim','error_deliveries','model_turns','model_requests','billing_usd','error']
    with (batch/'results.csv').open('w',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        writer.writerows({k:('NA' if row.get(k) is None else row.get(k)) for k in fields} for row in rows)
    actual=[v for v in rows if v.get('delivered') is not None]
    summary=[]
    for method in protocol['methods']:
        group=[v for v in actual if v['method']==method]
        total=sum(v.get('total',0) for v in group);delivered=sum(v.get('delivered',0) for v in group)
        elig=sum(v.get('recovery_eligible',0) for v in group)
        summary.append(dict(method=method,actual_runs=len(group),completed_runs=sum(v.get('mission_completed',False) for v in group),failed_runs=sum(not v.get('mission_completed',False) for v in group),planned_runs=24,delivery_rate=delivered/total if total else None,on_time_rate=sum(v.get('on_time',0) for v in group)/total if total else None,recovery_rate=sum(v.get('recovered',0) for v in group)/elig if elig else None,error_deliveries=sum(v.get('error_deliveries',0) for v in group),total_path_m=sum(v.get('path_length',0) for v in group)))
    result=dict(planned=72,actual_runs=len(actual),completed_runs=sum(v.get('mission_completed',False) for v in actual),failed_runs=sum(not v.get('mission_completed',False) for v in actual),missing=72-len(actual),methods=summary,notes=['All missing outcomes remain NA.','Aggregate rates use only actual runs, never fill missing runs as successes.','Package loading and handoff simulated; movement uses Isaac Sim and Nav2.','Simulator truth only validates physical arrival, never drives Nav2.','No collision metric is available.'])
    (batch/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    def fmt(v):return 'NA' if v is None else (f'{v:.3f}' if isinstance(v,float) else str(v))
    md=['# 医院配送实际实验结果',f"实际运行 {len(actual)}/72；缺失 {72-len(actual)}，保留 NA。",'','| 方法 | 实际运行 | 完成 | 失败 | 交付率 | 准时率 | 恢复率 | 错误交付 |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for v in summary:md.append('| '+' | '.join(fmt(v[k]) for k in ['method','actual_runs','completed_runs','failed_runs','delivery_rate','on_time_rate','recovery_rate','error_deliveries'])+' |')
    md+=['','交付率只计包裹交接，完整任务还要求返回物资库；请同时查看完成与失败次数。这些汇总不能在尚未完成全部运行时作为完整方法比较。真实 tokens 见各运行报告；AGH 金额为估算零值，实际费用记 NA。','', '全部运行明细：[results.csv](results.csv)。原始 AGH 工具调用、事件和交接回执分别保存在各运行目录。']
    (batch/'REPORT.md').write_text('\n'.join(md))
    return result
if __name__=='__main__':print(json.dumps(summarize(Path(__file__).resolve().parents[1]),ensure_ascii=False))
