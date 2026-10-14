import unittest,tempfile,time,json
from pathlib import Path
from service.state import Delivery
class Backend:
 def ready(self):return True
 def start(self,*args):pass
 def cancel(self):pass
class Test(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();p=Path(self.t.name);(p/'runs').mkdir()
  self.e=Delivery(p,{'depot':{'x':0,'y':0,'yaw':0},'station_a':{'x':3,'y':0,'yaw':0},'station_b':{'x':5,'y':0,'yaw':0},'station_c':{'x':7,'y':0,'yaw':0}})
  self.e.backend=Backend();self.i=0
 def tearDown(self):self.e.db.close();self.t.cleanup()
 def do(self,name,**a):
  self.i+=1;return self.e.do(name,dict(a,operation_id=str(self.i)))
 def order(self,i='1',station='station_a'):
  return dict(order_id=i,package_id='P'+i,pickup_station='depot',dropoff_station=station,priority='normal',deadline_sim=200)
 def at(self,x):self.e.telemetry(x,0,0,0,0);self.e.telemetry(x,0,0,0,2)
 def test_receipt_and_retry(self):
  self.do('create_orders',orders=[self.order()]);self.at(0);self.do('pickup_package',order_id='1')
  with self.assertRaisesRegex(ValueError,'not_at_destination'):self.do('confirm_delivery',order_id='1',package_id='P1')
  self.at(3)
  with self.assertRaisesRegex(ValueError,'package_mismatch'):self.do('confirm_delivery',order_id='1',package_id='WRONG')
  self.e.receiver('station_a',False)
  with self.assertRaisesRegex(ValueError,'receiver_unavailable'):self.do('confirm_delivery',order_id='1',package_id='P1')
  self.do('defer_order',order_id='1',reason='receiver_unavailable',retry_after_sim=5)
  self.e.receiver('station_a',True);self.do('confirm_delivery',order_id='1',package_id='P1')
  self.do('confirm_delivery',order_id='1',package_id='P1');self.assertEqual(self.e.verify()['delivered'],1)
  events=self.e.state()['events'];self.assertEqual(sum(e['kind']=='delivery_confirmed' for e in events),1)
 def test_atomic_batch(self):
  with self.assertRaises(ValueError):self.do('create_orders',orders=[self.order(),self.order('2','unknown')])
  self.assertEqual(self.e.s['orders'],{})
 def test_capacity_and_cancel_preserves_payload(self):
  self.do('create_orders',orders=[self.order(str(i)) for i in range(3)]);self.at(0)
  self.do('pickup_package',order_id='0');self.do('pickup_package',order_id='1')
  with self.assertRaisesRegex(ValueError,'capacity'):self.do('pickup_package',order_id='2')
  nav=self.do('navigate_to',station_id='station_a')
  self.do('cancel_navigation',nav_task_id=nav['nav_task_id'])
  with self.assertRaisesRegex(ValueError,'navigation_busy'):self.do('navigate_to',station_id='station_b')
  self.e.nav_result(nav['nav_task_id'],'canceled')
  self.assertTrue(self.e.s['orders']['0']['loaded']);self.do('navigate_to',station_id='station_b')
 def test_idempotency_conflict(self):
  a={'orders':[self.order()],'operation_id':'stable'}
  first=self.e.do('create_orders',a);self.assertEqual(first,self.e.do('create_orders',a))
  with self.assertRaisesRegex(ValueError,'operation_id_conflict'):self.e.do('create_orders',dict(a,orders=[self.order('2')]))
 def test_reset_preserves_clock_and_archives(self):
  self.e.telemetry(0,0,0,0,88);old=self.e.s['run_id'];self.e.reset()
  self.assertEqual(self.e.s['sim_time'],88);self.assertEqual(self.e.verify()['elapsed_sim'],0)
  self.assertTrue((self.e.root/'runs'/old/'final-state.json').exists())
 def test_completed_archive_survives_later_clock_reset(self):
  self.e.telemetry(0,0,0,0,88);folder=self.e.root/'runs'/self.e.s['run_id']
  snapshot={'elapsed_sim':88,'status':'original_finished'}
  (folder/'report.json').write_text(json.dumps(snapshot));(folder/'final-state.json').write_text('{"status":"original_finished"}')
  self.e.telemetry(0,0,0,0,2);self.e.reset()
  self.assertEqual(json.loads((folder/'report.json').read_text()),snapshot)
  self.assertEqual(json.loads((folder/'final-state.json').read_text())['status'],'original_finished')
 def test_stale_pose_rejected(self):
  self.do('create_orders',orders=[self.order()]);self.at(0);self.e.last_pose_wall=0
  with self.assertRaisesRegex(ValueError,'not_at_pickup'):self.do('pickup_package',order_id='1')
 def test_turning_robot_cannot_load(self):
  self.do('create_orders',orders=[self.order()]);self.at(0)
  self.e.telemetry(0,0,0,0,3,angular_speed=.4)
  with self.assertRaisesRegex(ValueError,'not_at_pickup'):self.do('pickup_package',order_id='1')
  self.e.telemetry(0,0,0,0,4,angular_speed=0);self.e.telemetry(0,0,0,0,5.1,angular_speed=0)
  self.assertTrue(self.do('pickup_package',order_id='1')['ok'])
 def test_simulator_truth_must_be_fresh_and_at_station(self):
  self.e.require_sim_truth=True;self.at(0)
  self.assertFalse(self.e.at('depot'))
  file=self.e.root/'runs/physical-pose.json'
  file.write_text(json.dumps({'position':[0,0,0],'wall_time':time.time()}));self.assertTrue(self.e.at('depot'))
  file.write_text(json.dumps({'position':[1,0,0],'wall_time':time.time()}));self.assertFalse(self.e.at('depot'))
  file.write_text(json.dumps({'position':[0,0,0],'wall_time':0}));self.assertFalse(self.e.at('depot'))
if __name__=='__main__':unittest.main()
