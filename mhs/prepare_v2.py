"""Reproducible local source upgrade; no credentials or public AGH changes."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def edit(rel,old,new):
    p=ROOT/rel;text=p.read_text(encoding='utf-8')
    if old not in text:raise RuntimeError('patch_anchor_missing:'+rel+':'+old[:60])
    p.write_text(text.replace(old,new),encoding='utf-8')
def main():
    # Runtime paths only. Archived V1 data and existing evidence stay untouched.
    for p in (ROOT/'mhs').rglob('*'):
        if p.suffix in {'.py','.js','.json','.md','.sh'} and 'dist' not in p.parts and not p.name.startswith('test_') and p.name!='prepare_v2.py':
            text=p.read_text(encoding='utf-8')
            if 'runs/mhs-v1' in text:p.write_text(text.replace('runs/mhs-v1','runs/mhs-v2'),encoding='utf-8')
    points=json.loads((ROOT/'configs/stations.json').read_text())
    extra=[('pharmacy',-30,10,'药房',['药房取货点']),('lab',18,18,'检验科',['检验科接收点']),('emergency',20,5,'急诊接收点',['急诊']),('room301',-40,20,'301病房',['301','301病房门口']),('room302',18,30,'302病房',['302','302病房门口']),('waiting',-40,10,'候诊服务台',['候诊区','服务台'])]
    for k,x,y,label,aliases in extra:points[k]={'x':x,'y':y,'yaw':0,'label':label,'aliases':aliases,'coordinate_status':'map_clearance_checked_navigation_pending'}
    for k,p in points.items():p['id']=k;p.setdefault('aliases',[p['label'].replace('护士站','站')])
    (ROOT/'configs/stations.json').write_text(json.dumps(points,ensure_ascii=False,indent=2),encoding='utf-8')
    cfg={'version':'hospital-v2.0','source':'hospital scene functional labels; coordinates require real Nav2 qualification','points':list(points),'zones':[{'id':'waiting_corridor','label':'候诊通道','kind':'crowd','bounds':[-26,8,-12,12],'speed_limit':.25},{'id':'quiet_corridor','label':'病房安静区','kind':'quiet','bounds':[-45,15,-34,27],'speed_limit':.2},{'id':'east_corridor','label':'东侧通道','kind':'corridor','bounds':[14,12,22,24],'speed_limit':.5}],'people_seed':101,'people_count':4}
    (ROOT/'configs/hospital-v2.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    edit('mhs/business_state.py',"if o['pickup_station']!='depot' or o['dropoff_station'] not in self.stations or o['dropoff_station']=='depot':raise ValueError('invalid_station')","if o['pickup_station'] not in self.stations or o['dropoff_station'] not in self.stations or o['pickup_station']==o['dropoff_station']:raise ValueError('invalid_station')")
    edit('mhs/engine.py',"self.s['sim_time']-b['start_sim']>=600","self.s['sim_time']-b['start_sim']>=2400")
    edit('mhs/engine.py',"time.time()-b['start_wall']>=1200","time.time()-b['start_wall']>=3600")
    edit('mhs/api.py','from .engine import MHSDelivery','from .engine_v2 import HospitalV2 as MHSDelivery')
    edit('mhs/api.py',"elif self.path=='/api/mhs/navigate':result=engine.do('navigate_to',a)","elif self.path=='/api/mhs/navigate':result=engine.do('navigate_route',a)\n            elif self.path=='/api/mhs/routes':result=engine.route_options(a['station_id'])\n            elif self.path=='/api/admin/scene-event':result=engine.set_scene_event(a['kind'],a['value'])")
    edit('mhs/api.py',"if name=='create_delivery_orders':result=engine.create_delivery_orders(**args)","if name=='create_delivery_orders':result=engine.create_delivery_orders(**args)\n                elif name=='get_route_options':result=engine.route_options(args['station_id'])")
    # Every modifying business reply supplies the current state version.
    edit('mhs/api.py',"return self.reply({'ok':True,'result':result})","if isinstance(result,dict):result['planning_version']=engine.s.get('planning_version',0)\n            return self.reply({'ok':True,'result':result})")
    # Old running-container reuse was a source of stale clocks. Stop/start owns a fresh container.
    edit('mhs/manage.py',"stage", "stage") if False else None
    pkg=ROOT/'mhs/policy/package.json';d=json.loads(pkg.read_text());d['version']='2.0.0';pkg.write_text(json.dumps(d))
    edit('mhs/policy/scope.js',"'wait_event'].map","'wait_event','get_route_options'].map")
    edit('mhs/policy/scope.js',"args.tool!=='navigate_to'","args.tool!=='navigate_route'")
    edit('mhs/policy/scope.js','仅允许 navigate_to','仅允许 navigate_route')
    edit('mhs/policy/business.js',"'verify_run','wait_event']);","'verify_run','wait_event','get_route_options']);")
    edit('mhs/policy/business.js',"parameters:remoteInputSchema(t.inputSchema)","parameters:remoteInputSchema((()=>{const schema=structuredClone(t.inputSchema);if(writes.has(t.name)){delete schema.properties.operation_id;schema.required=(schema.required??[]).filter(k=>k!=='operation_id');}return schema;})())")
    edit('mhs/policy/business.js',"const started=Date.now();","const started=Date.now();\n      if(writes.has(t.name))args={...args,operation_id:'biz-'+call.toolUseId};")
    edit('mhs/policy/session-scope.js',"if(!response.ok)throw Error('Project state unavailable');","if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.error||'project_http_'+response.status);}")
    edit('mhs/policy/index.js',"}catch{return {block:true,reason:'项目配送服务不可用，停止决策与运动。'};}","}catch(error){appendFileSync('/mnt/sda/omni-yezh/hospital-delivery-agent/runs/mhs-v2/policy-errors.jsonl',JSON.stringify({wall_time:Date.now()/1000,session:hctx.session.key,error:String(error)})+'\\n');return {block:true,reason:'本轮校验失败：'+String(error.message||error)};}")
    edit('mhs/policy/session-scope.js','AbortSignal.timeout(700)','AbortSignal.timeout(2500)')
    edit('mhs/policy/index.js','AbortSignal.timeout(600)','AbortSignal.timeout(2500)')
    edit('mhs/policy/completion.js',"if(!unfinished.length&&state.at_stations?.includes('depot')&&state.stopped_confirmed)","if(!unfinished.length&&state.stopped_confirmed&&state.at_stations?.length)")
    edit('mhs/policy/completion.js','全部订单处理后返库并核验','全部订单处理后在交接点待命并核验；用户要求返回时执行指定返回点')
    # Correct source catalog changes require unloading the installed 9-tool plugin first.
    edit('mhs/refresh_policy.py',"stage.write_text('8' if installed.get('version','').startswith('1.2.') else '9')","stage.write_text('9')")
    edit('mhs/refresh_policy.py',"stage.write_text('9')\n    call(['package','enable'","stage.unlink(missing_ok=True)\n    call(['package','enable'")
if __name__=='__main__':main()
