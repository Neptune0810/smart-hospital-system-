"""Read exported synthetic project records; never sends a prompt or tool call."""
import argparse,json
from pathlib import Path
from .manage import ROOT

def main():
    parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
    folder=Path(args.directory).resolve()
    if ROOT/'runs/mhs-v2/acceptance' not in folder.parents:raise ValueError('outside_project_acceptance')
    rows=[json.loads(line) for line in (folder/'session.jsonl').read_text().splitlines()]
    calls={r['data']['toolUseId']:r for r in rows if r['type']=='tool/call'}
    watches=[]
    for row in rows:
        if row['type']!='tool/result':continue
        call=calls.get(row['data']['toolUseId'])
        if not call or call['data']['name']!='watch_device':continue
        watches.append({'seq':row['seq'],'ts':row['ts'],'arguments':call['data'].get('args'),'error':row['data'].get('isError',False),'result':row['data'].get('content')})
    notifications=[r for r in rows if r['type']=='user/message' and any(c.get('text','').startswith('[AgnesHub]') for c in r['data'].get('content',[]))]
    print(json.dumps({'requests':sum(r['type']=='request/sent' for r in rows),'watch_results':watches,'native_notifications':[{'seq':r['seq'],'ts':r['ts'],'content':r['data']['content']} for r in notifications[-3:]],'last_tools':[r['data']['name'] for r in list(calls.values())[-5:]]},ensure_ascii=False))

if __name__=='__main__':main()
