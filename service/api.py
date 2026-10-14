import json,time,os
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from service.state import Delivery
ROOT=Path(os.environ.get('HOSPITAL_ROOT','/project'))
engine=Delivery(ROOT,json.loads((ROOT/'configs/stations.json').read_text()),require_sim_truth=True)
from service.navigation import launch
launch(engine)

class Handler(BaseHTTPRequestHandler):
    def reply(self,body,status=200,kind='application/json'):
        raw=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        self.path=self.path.split('?',1)[0]
        if self.path=='/api/state':return self.reply(engine.state())
        if self.path=='/api/agent':
            p=ROOT/'runs/agent-live.json'
            return self.reply(json.loads(p.read_text()) if p.exists() else {'phase':'not_started'})
        if self.path=='/api/report':return self.reply(engine.verify())
        if self.path=='/':return self.reply((ROOT/'service/dashboard.html').read_bytes(),kind='text/html; charset=utf-8')
        if self.path=='/map.png':return self.reply((ROOT/'maps/carter_hospital_navigation.png').read_bytes(),kind='image/png')
        if self.path=='/frame.jpg':
            p=ROOT/'runs/latest.jpg'
            return self.reply(p.read_bytes(),kind='image/jpeg') if p.exists() else self.reply({'error':'frame_not_ready'},404)
        return self.reply({'error':'not_found'},404)
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=65536:raise ValueError('invalid_body_size')
            a=json.loads(self.rfile.read(size))
            if self.path=='/api/tool':
                if a['name']=='wait_event':
                    deadline=time.time()+min(5,max(0,a['arguments'].get('timeout_seconds',3)))
                    while time.time()<deadline:
                        result=engine.state(a['arguments'].get('since_event_id',0))
                        if result['events']:break
                        time.sleep(0.1)
                    else:result=engine.state(a['arguments'].get('since_event_id',0))
                else:result=engine.do(a['name'],a.get('arguments',{}))
            elif self.path=='/api/admin/receiver':
                engine.receiver(a['station_id'],a['available']);result={'ok':True}
            elif self.path=='/api/admin/reset':result=engine.reset()
            else:return self.reply({'error':'not_found'},404)
            return self.reply({'ok':True,'result':result})
        except Exception as e:return self.reply({'ok':False,'error':str(e)},400)
    def log_message(self,*args):pass
ThreadingHTTPServer(('127.0.0.1',4188),Handler).serve_forever()
