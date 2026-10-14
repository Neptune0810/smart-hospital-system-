"""Read-only proof that real MCP observations retain facts without polylines."""
import json,time
from .manage import ROOT,api
from .mcp_server import compact_result

def main():
    raw=api();compact=compact_result(raw)
    rows={'wall_time':time.time(),'run_id':raw['run_id'],'raw_json_bytes':len(json.dumps(raw,ensure_ascii=False).encode()),'model_observation_json_bytes':len(json.dumps(compact,ensure_ascii=False).encode()),'same_orders':raw['orders']==compact['orders'],'same_pose':raw['pose']==compact['pose'],'same_crowd':raw['crowd']==compact['crowd'],'same_planning_version':raw['planning_version']==compact['planning_version'],'model_events':len(compact['events']),'source_events_retained':len(raw['events']),'scope':'actual observation serialization, not token usage or model acceptance'}
    p=ROOT/'runs/mhs-v2/acceptance'/('prompt-size-'+time.strftime('%Y%m%d-%H%M%S'));p.mkdir(parents=True)
    (p/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps({'directory':str(p),**rows}))

if __name__=='__main__':main()
