"""Summarize actual exported calls; no inference from UI Completed badges."""
import argparse,collections,json,sys
from datetime import datetime
from pathlib import Path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('directory');parser.add_argument('--since-wall',type=float);args=parser.parse_args()
    directory=Path(args.directory);rows=[json.loads(x) for x in (directory/'session.jsonl').read_text().splitlines()]
    if args.since_wall is not None:rows=[r for r in rows if datetime.fromisoformat(r['ts'].replace('Z','+00:00')).timestamp()>=args.since_wall]
    calls=[r for r in rows if r['type']=='tool/call']; names=collections.Counter(r['data']['name'] for r in calls)
    byid={r['data']['toolUseId']:r for r in calls};verifications=[];rejections=[]
    for row in rows:
        if row['type']!='tool/result':continue
        data=row['data'];call=byid.get(data['toolUseId'])
        if not call:continue
        if data.get('isError'):rejections.append({'seq':row['seq'],'tool':call['data']['name'],'error':''.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text')[:500]})
        if call['data']['name']=='hospital_verify_run':
            for part in data.get('content',[]):
                if part.get('type')=='text':
                    try:
                        body=json.loads(part['text']);report=body.get('result',{})
                        verifications.append({'seq':row['seq'],'segment':report.get('segment'),'order_ids':report.get('order_ids'),'returned_to_depot':report.get('returned_to_depot'),'complete_task':report.get('complete_task'),'final_station':report.get('final_station'),'final_stopped':report.get('final_stopped'),'remaining_cargo':report.get('remaining_cargo'),'delivered':report.get('delivered'),'total':report.get('total'),'failed':report.get('failed'),'unfinished':report.get('unfinished'),'sim_time':report.get('sim_time')})
                    except json.JSONDecodeError:pass
    notifications=[]
    for row in rows:
        if row['type']=='user/message':
            for part in row['data'].get('content',[]):
                if part.get('type')=='text' and part['text'].startswith('[AgnesHub]'):
                    notifications.append({'seq':row['seq'],'text':part['text']})
    intents={r['data']['effectId']:r for r in rows if r['type']=='effect/intent' and r['data'].get('kind')=='inference'}
    waits=[]
    for row in rows:
        if row['type']!='effect/settled':continue
        intent=intents.get(row['data'].get('effectId'))
        if intent:
            seconds=(datetime.fromisoformat(row['ts'].replace('Z','+00:00'))-datetime.fromisoformat(intent['ts'].replace('Z','+00:00'))).total_seconds()
            waits.append({'start_seq':intent['seq'],'end_seq':row['seq'],'wall_seconds':seconds})
    choices=[{'seq':r['seq'],'tool':r['data']['name'],'arguments':r['data'].get('input',r['data'].get('arguments',r['data'].get('args')))} for r in calls if r['data']['name'] in {'call_device','hospital_publish_delivery_plan','hospital_get_route_options'}]
    result={'since_wall':args.since_wall,'model_request_count':sum(r['type']=='request/sent' for r in rows),'tool_calls':dict(names),'native_notifications':notifications,'verifications':verifications,
            'final_return_verified_by_agent':any(x['returned_to_depot'] is True and not x['unfinished'] for x in verifications),
            'final_standby_verified_by_agent':any(x['complete_task'] is True and x['final_stopped'] is True and x['remaining_cargo']==0 and not x['unfinished'] for x in verifications),
            'planning_and_motion_calls':choices,'model_waits':waits,'total_model_wait_wall_seconds':sum(x['wall_seconds'] for x in waits),
            'unsettled_model_requests':len(intents)-len(waits),'rejected_calls':rejections}
    (directory/'session-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
