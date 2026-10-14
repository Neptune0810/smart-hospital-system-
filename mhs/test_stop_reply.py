import asyncio,time,unittest
from unittest.mock import patch
from .bridge import HospitalBridge

class StopReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_is_prompt_but_next_motion_waits_for_measured_stop(self):
        class Device:
            def __init__(self):self.values={};self.problem=None;self.faults=[]
            def update(self,**values):self.values.update(values)
        bridge=object.__new__(HospitalBridge);bridge.dev=Device();bridge.stop_waiter=None;bridge.connected=True
        still=[False]
        def request(path,args=None):
            if path=='/api/mhs/stop':return {'requested':True}
            return {'pose_fresh':True,'physical_pose':{'position':[0,0,0]},'navigation_ready':True,'crowd':{'fresh':True},'stopped_confirmed':still[0]}
        bridge.latest=request('/api/state')
        with patch('mhs.bridge.request',request):
            start=time.monotonic();await bridge.stop_native()
            self.assertLess(time.monotonic()-start,.5)
            self.assertFalse(bridge.dev.values['stop_confirmed'])
            self.assertEqual(bridge.unsafe(None),'previous_motion_not_stopped')
            await asyncio.sleep(.05);still[0]=True
            await asyncio.wait_for(bridge.stop_waiter,1)
            self.assertTrue(bridge.dev.values['stop_confirmed'])
            self.assertIsNone(bridge.unsafe(None))
if __name__=='__main__':unittest.main()
