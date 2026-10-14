import json,os,time,hashlib,signal,threading
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from .runtime import mode,SMART_MODE,map_image
from .navigation_v2 import launch
from .frame_sources import read_frame
from urllib.parse import urlsplit, parse_qs

ROOT=Path(os.environ.get('HOSPITAL_ROOT','/project'))
SMART=mode(ROOT)==SMART_MODE
if SMART:
    from .smart_ward import SmartWardV1 as MHSDelivery
else:
    from .engine_v2 import HospitalV2 as MHSDelivery
engine=MHSDelivery(ROOT,json.loads((ROOT/'configs/stations.json').read_text()),require_sim_truth=True)
launch(engine)

class Handler(BaseHTTPRequestHandler):
    def reply(self,body,status=200,kind='application/json'):
        raw=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        path=self.path.split('?',1)[0]
        if path=='/api/state':return self.reply(engine.state())
        if path=='/api/mhs/identity':
            with engine.lock:return self.reply({k:engine.s.get(k) for k in ['run_id','controller_session','operator_stopped']})
        if path=='/api/report':return self.reply(engine.verify())
        if path=='/':return self.reply((ROOT/'mhs/dashboard.html').read_bytes(),kind='text/html; charset=utf-8')
        if path=='/map.png':return self.reply(map_image(ROOT).read_bytes(),kind='image/png')
        if path=='/api/ward/operation' and SMART:
            try:return self.reply({'ok':True,'result':engine.ward_operation(parse_qs(urlsplit(self.path).query).get('id',[''])[0])})
            except Exception as e:return self.reply({'ok':False,'error':str(e)},400)
        if path=='/api/ward/model-admission/context' and SMART:
            try:return self.reply({'ok':True,'result':engine.model_admission_context(parse_qs(urlsplit(self.path).query).get('session_key',[''])[0])})
            except Exception as e:return self.reply({'ok':False,'error':str(e)},400)
        if path=='/api/admin/smart-ward/fault-qualification/operation' and SMART:
            try:return self.reply({'ok':True,'result':engine.fault_qualification_operation(parse_qs(urlsplit(self.path).query).get('id',[''])[0])})
            except Exception as e:return self.reply({'ok':False,'error':str(e)},400)
        if path in {'/api/frame','/frame.jpg'}:
            try:
                views=parse_qs(urlsplit(self.path).query).get('view',['third_person'])
                if len(views)!=1:raise ValueError('unknown_camera_view')
                d,raw=read_frame(ROOT,views[0])
                return self.reply(d) if path=='/api/frame' else self.reply(raw,kind='image/jpeg')
            except FileNotFoundError:return self.reply({'error':'frame_not_ready'},404)
            except (ValueError,KeyError):return self.reply({'error':'frame_stale_or_invalid_view'},409)
        return self.reply({'error':'not_found'},404)
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=65536:raise ValueError('invalid_body_size')
            a=json.loads(self.rfile.read(size))
            if self.path=='/api/tool':
                name=a['name'];args=a.get('arguments',{})
                if SMART and name in {'create_delivery_orders','pickup_package','confirm_delivery','publish_delivery_plan'}:raise ValueError('smart_ward_requires_incident_and_device_handoff')
                if SMART and name=='create_specimen_delivery':result=engine.create_specimen_delivery(**args)
                elif SMART and name=='publish_service_plan':result=engine.publish_service_plan(**args)
                elif SMART and name=='verify_services':result=engine.verify_services()
                elif name=='create_delivery_orders':result=engine.create_delivery_orders(**args)
                elif name=='get_route_options':result=engine.route_options(args['station_id'])
                elif name in {'get_state','verify_run','pickup_package','confirm_delivery','defer_order','close_order','publish_delivery_plan'}:result=engine.do(name,args)
                elif name=='wait_event':
                    deadline=time.monotonic()+min(5,max(0,args.get('timeout_seconds',3)))
                    result=engine.state(args.get('since_event_id',0))
                    while not result['events'] and time.monotonic()<deadline:
                        time.sleep(.1);result=engine.state(args.get('since_event_id',0))
                else:raise ValueError('business_tool_not_exposed')
            elif self.path=='/api/mhs/lease':
                with engine.lock:engine.guard.lease(a['connected']);result=engine.guard.state()
            elif self.path=='/api/mhs/navigate':result=engine.do('navigate_route',a)
            elif self.path=='/api/mhs/routes':result=engine.route_options(a['station_id'])
            elif self.path=='/api/admin/scene-event':result=engine.set_scene_event(a['kind'],a['value'])
            elif self.path=='/api/mhs/stop':result=engine.request_stop(a.get('reason','operator_stop'))
            elif self.path=='/api/mhs/decision':result=engine.decision(a['decision_id'],a.get('expected_run_id'),a.get('session_key'))
            elif self.path=='/api/ward/device' and SMART:result=engine.device_action(a['device_id'],a['tool'],a.get('arguments',{}))
            elif self.path=='/api/ward/model-request' and SMART:result=engine.record_model_request(a['request_id'],a['expected_run_id'],a['session_key'])
            elif self.path=='/api/ward/model-admission/reserve' and SMART:result=engine.reserve_model_dispatch(**a)
            elif self.path=='/api/ward/finalize' and SMART:result=engine.finalize_episode(a['expected_run_id'],a['session_key'])
            elif self.path=='/api/admin/smart-ward/start' and SMART:result=engine.start_watch(a['session_key'],a['expected_run_id'],a['operation_id'])
            elif self.path=='/api/admin/smart-ward/navigation-qualification/begin' and SMART:result=engine.begin_navigation_qualification(a['session_key'],a['expected_run_id'],a['operation_id'])
            elif self.path=='/api/admin/smart-ward/navigation-qualification/end' and SMART:result=engine.end_navigation_qualification(a['expected_run_id'],a['operation_id'],a.get('reason','qualification_end'))
            elif self.path=='/api/admin/smart-ward/fault-qualification/begin' and SMART:
                result=engine.begin_fault_qualification(a['session_key'],a['expected_run_id'],a['expected_epoch'],a['operation_id'],a.get('duration_seconds',240))
            elif self.path=='/api/admin/smart-ward/fault-qualification/command' and SMART:
                result=engine.fault_qualification_command(a['qualification_id'],a['session_key'],a['expected_run_id'],a['expected_epoch'],a['kind'],a.get('arguments',{}),a['operation_id'])
            elif self.path=='/api/admin/smart-ward/fault-qualification/end' and SMART:
                result=engine.end_fault_qualification(a['qualification_id'],a['session_key'],a['expected_run_id'],a['expected_epoch'],a['operation_id'],a.get('reason','qualification_end'))
            elif self.path=='/api/admin/smart-ward/command' and SMART:
                if a['expected_run_id']!=engine.s['run_id']:raise ValueError('run_mismatch')
                result=engine.ward_command(a['kind'],a.get('arguments',{}),a['operation_id'])
            elif self.path=='/api/admin/smart-ward/notifications/claim' and SMART:
                if a['expected_run_id']!=engine.s['run_id']:raise ValueError('run_mismatch')
                result=engine.claim_notification(a['worker_id'])
            elif self.path=='/api/admin/smart-ward/notifications/ack' and SMART:result=engine.ack_notification(a['notification_id'],a['status'],a.get('details'))
            elif self.path=='/api/admin/claim':result=engine.claim_session(a['session_key'],a['expected_run_id'])
            elif self.path=='/api/admin/stop-run':result=engine.stop_run(a['operation_id'],a['expected_run_id'],**({'reason':a.get('reason','user_stop')} if SMART else {}))
            elif self.path=='/api/admin/receiver':engine.receiver(a['station_id'],a['available']);result={'ok':True}
            elif self.path=='/api/admin/reset':
                if not engine.stopped():raise ValueError('stop_not_confirmed')
                result=engine.reset()
            else:return self.reply({'error':'not_found'},404)
            if isinstance(result,dict):result['planning_version']=engine.s.get('planning_version',0)
            return self.reply({'ok':True,'result':result})
        except Exception as e:return self.reply({'ok':False,'error':str(e)},400)
    def log_message(self,*args):pass

server=ThreadingHTTPServer(('127.0.0.1',4188),Handler)
def shutdown(*_):
    engine.request_stop('service_shutdown')
    threading.Thread(target=server.shutdown,daemon=True).start()
signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
try:server.serve_forever()
finally:
    server.server_close()
    import rclpy
    if rclpy.ok():rclpy.shutdown()
