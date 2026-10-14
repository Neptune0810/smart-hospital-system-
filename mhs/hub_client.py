import asyncio,json,time,uuid
from websockets.asyncio.client import connect

class HubClient:
    def __init__(self,url='ws://127.0.0.1:4192/ws/hub',record=None):
        self.url=url;self.record=record;self.pending={};self.jobs={}
    def log(self,message,direction):
        if self.record:
            with self.record.open('a') as f:f.write(json.dumps({'time':time.time(),'direction':direction,'message':message},ensure_ascii=False)+'\n')
    async def __aenter__(self):
        self.ws=await connect(self.url);self.reader=asyncio.create_task(self.read())
        await self.rpc('hub/hello',{'role':'agent','client':{'name':'hospital-acceptance','version':'1.0'}})
        return self
    async def __aexit__(self,*args):
        self.reader.cancel();await asyncio.gather(self.reader,return_exceptions=True);await self.ws.close()
    async def read(self):
        async for raw in self.ws:
            m=json.loads(raw);self.log(m,'in')
            if 'id' in m and m['id'] in self.pending:
                future=self.pending.pop(m['id'])
                if 'error' in m:future.set_exception(RuntimeError(str(m['error'])))
                else:future.set_result(m['result'])
            elif m.get('method')=='hub/result':self.jobs[m['params']['job']]=m['params']
    async def rpc(self,method,params):
        ident=uuid.uuid4().hex;f=asyncio.get_running_loop().create_future();self.pending[ident]=f
        m=dict(jsonrpc='2.0',id=ident,method=method,params=params);self.log(m,'out');await self.ws.send(json.dumps(m))
        return await asyncio.wait_for(f,15)
    async def navigate(self,station,operation=None):
        return await self.rpc('hub/call',{'device':'hospital-robot-01','tool':'navigate_to','arguments':{'station_id':station,'operation_id':operation or uuid.uuid4().hex}})
    async def finish(self,job,timeout=600):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if job in self.jobs:return self.jobs[job]
            await asyncio.sleep(.2)
        raise TimeoutError('Hub_job_timeout:'+job)
