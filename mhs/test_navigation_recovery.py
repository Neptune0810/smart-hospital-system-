"""ROS-interface mocks for restart recovery; these do not prove real motion."""
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class Future:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.callbacks = []

    def result(self):
        if self.error:
            raise self.error
        return self.value

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def done(self): return False


class UUID:
    def __init__(self, uuid=None):
        self.uuid = [0] * 16 if uuid is None else uuid


class CancelRequest:
    def __init__(self):
        self.goal_info = types.SimpleNamespace(goal_id=UUID(), stamp=types.SimpleNamespace(sec=0, nanosec=0))


class Goal:
    def __init__(self):
        self.pose = types.SimpleNamespace(header=types.SimpleNamespace(), pose=types.SimpleNamespace(
            position=types.SimpleNamespace(), orientation=types.SimpleNamespace()))


class Client:
    def __init__(self, *args):
        self.sent = []
        self.requests = []
        self.future = Future()
        self.error = None

    def server_is_ready(self): return True
    def service_is_ready(self): return True

    def send_goal_async(self, goal, **kwargs):
        if self.error:
            raise self.error
        self.sent.append((goal, kwargs))
        return self.future

    def call_async(self, request):
        self.requests.append(request)
        return self.future


class Node:
    def __init__(self, *args, **kwargs):
        self.subscriptions_seen = []
        self.clients_seen = []

    def create_subscription(self, kind, topic, callback, qos):
        item = (topic, callback, qos)
        self.subscriptions_seen.append(item)
        return item

    def create_client(self, kind, topic):
        client = Client()
        self.clients_seen.append((topic, client))
        return client

    def create_timer(self, *args, **kwargs): pass
    def get_clock(self):
        return types.SimpleNamespace(now=lambda: types.SimpleNamespace(to_msg=lambda: object()))


def module(**attributes):
    value = types.ModuleType('mock_ros')
    value.__dict__.update(attributes)
    return value


def load_navigation():
    mocks = {
        'rclpy': module(), 'rclpy.node': module(Node=Node),
        'rclpy.action': module(ActionClient=Client),
        'rclpy.qos': module(qos_profile_sensor_data=object(),
            QoSProfile=lambda **kw: types.SimpleNamespace(**kw),
            DurabilityPolicy=types.SimpleNamespace(VOLATILE='volatile', TRANSIENT_LOCAL='retained'),
            ReliabilityPolicy=types.SimpleNamespace(RELIABLE='reliable')),
        'geometry_msgs': module(), 'geometry_msgs.msg': module(PoseWithCovarianceStamped=object, Twist=object, PoseStamped=Goal),
        'nav_msgs': module(), 'nav_msgs.msg': module(Odometry=object, OccupancyGrid=object),
        'rosgraph_msgs': module(), 'rosgraph_msgs.msg': module(Clock=object),
        'nav2_msgs': module(), 'nav2_msgs.action': module(NavigateToPose=types.SimpleNamespace(Goal=Goal),
            NavigateThroughPoses=types.SimpleNamespace(Goal=Goal), ComputePathThroughPoses=object),
        'nav2_msgs.msg': module(CostmapFilterInfo=object, Costmap=object, SpeedLimit=object),
        'action_msgs': module(), 'action_msgs.msg': module(GoalStatus=types.SimpleNamespace(
            STATUS_SUCCEEDED=4, STATUS_CANCELED=5), GoalStatusArray=object),
        'action_msgs.srv': module(CancelGoal=types.SimpleNamespace(Request=CancelRequest)),
        'unique_identifier_msgs': module(), 'unique_identifier_msgs.msg': module(UUID=UUID),
        'tf2_ros': module(Buffer=object), 'tf2_msgs': module(), 'tf2_msgs.msg': module(TFMessage=object),
    }
    path = Path(__file__).with_name('navigation_base.py')
    spec = importlib.util.spec_from_file_location('_mock_navigation_recovery', path)
    navigation = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, mocks):
        spec.loader.exec_module(navigation)
    return navigation, mocks


NAV, ROS_MOCKS = load_navigation()
ACTION = '/carter1/navigate_to_pose'
ROUTE_ACTION = '/carter1/navigate_through_poses'
GOAL_UUID = 'a1' * 16


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.identities = []
        self.results = []
        self.terminals = []
        self.uncertain = []
        self.persisted = True
        self.confirmed = True
        self.guard = types.SimpleNamespace(armed=True)

    def nav_goal_identity(self, *identity):
        self.identities.append(identity)
        return {'persisted': self.persisted}

    def nav_result(self, *result): self.results.append(result)
    def nav_goal_uncertain(self, *identity): self.uncertain.append(identity)
    def nav_goal_terminal(self, *terminal):
        self.terminals.append(terminal)
        return {'confirmed': self.confirmed}
    def fresh(self): return True


def task(value=GOAL_UUID, action=ACTION):
    return dict(nav_task_id='task-old', nav_goal_uuid=value, nav_action_namespace=action, status='unknown')


def status(value=GOAL_UUID, code=5):
    return types.SimpleNamespace(status_list=[types.SimpleNamespace(status=code,
        goal_info=types.SimpleNamespace(goal_id=UUID(list(bytes.fromhex(value)))))])


class NavigationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.node = NAV.Navigator(self.engine)

    def test_identity_committed_before_exact_uuid_is_sent(self):
        original = self.node.client.send_goal_async
        def send(goal, **kwargs):
            self.assertEqual(len(self.engine.identities), 1)
            self.assertEqual(bytes(kwargs['goal_uuid'].uuid).hex(), self.engine.identities[0][1])
            return original(goal, **kwargs)
        self.node.client.send_goal_async = send
        self.node.start('task-new', dict(x=1, y=2, yaw=0))
        self.assertEqual(self.engine.identities[0][0::2], ('task-new', ACTION))

    def test_failed_persistence_never_sends_goal(self):
        self.engine.persisted = False
        with self.assertRaisesRegex(RuntimeError, 'not_durably_persisted'):
            self.node.start('task-new', dict(x=1, y=2, yaw=0))
        self.assertFalse(self.node.client.sent)

    def test_restart_cancel_is_only_the_persisted_uuid(self):
        self.node.reconcile_navigation(task())
        topic, client = self.node.clients_seen[0]
        self.assertEqual(topic, ACTION + '/_action/cancel_goal')
        request = client.requests[0]
        self.assertEqual(bytes(request.goal_info.goal_id.uuid).hex(), GOAL_UUID)
        self.assertEqual((request.goal_info.stamp.sec, request.goal_info.stamp.nanosec), (0, 0))
        self.assertFalse(self.engine.terminals)

    def test_old_or_invalid_identity_never_broadly_cancels(self):
        for candidate in ({'nav_task_id': 'old'}, task('0' * 32), task('invalid'),
                          task(action='/other_robot/navigate_to_pose')):
            with self.subTest(candidate=candidate):
                self.assertFalse(self.node.reconcile_navigation(candidate))
        self.assertFalse(self.node.clients_seen)
        self.assertFalse(self.node.recovering_goals)

    def test_cancel_response_and_missing_status_are_not_terminal(self):
        self.node.reconcile_navigation(task())
        client = self.node.clients_seen[0][1]
        client.future.value = types.SimpleNamespace(return_code=3, goals_canceling=[])
        for callback in client.future.callbacks:
            callback(client.future)
        self.node.goal_status(ACTION, types.SimpleNamespace(status_list=[]))
        self.assertFalse(self.engine.terminals)
        self.assertTrue(self.node.recovering_goals)

    def test_only_one_cancel_rpc_can_remain_outstanding(self):
        with patch.object(NAV.time, 'monotonic', return_value=10):
            self.node.reconcile_navigation(task())
        with patch.object(NAV.time, 'monotonic', return_value=20):
            self.node.reconcile_navigation(task())
        self.assertEqual(len(self.node.clients_seen[0][1].requests), 1)

    def test_only_new_matching_terminal_status_can_reconcile(self):
        self.node.goal_status(ACTION, status())  # before registration: never cached
        self.node.reconcile_navigation(task())
        self.assertFalse(self.engine.terminals)
        for message_action, message in ((ROUTE_ACTION, status()), (ACTION, status('b2' * 16)),
                                        (ACTION, status(code=2)), (ACTION, status(code=3))):
            self.node.goal_status(message_action, message)
        self.assertFalse(self.engine.terminals)
        self.node.goal_status(ACTION, status())
        self.assertEqual(self.engine.terminals, [('task-old', GOAL_UUID, ACTION, 5)])
        self.assertFalse(self.node.recovering_goals)

    def test_all_three_real_terminal_statuses_supported(self):
        for code in (4, 5, 6):
            with self.subTest(code=code):
                self.node.reconcile_navigation(task())
                self.node.goal_status(ACTION, status(code=code))
                self.assertEqual(self.engine.terminals[-1][-1], code)

    def test_engine_identity_rejection_retains_recovery_watch(self):
        self.engine.confirmed = False
        self.node.reconcile_navigation(task())
        self.node.goal_status(ACTION, status())
        self.assertTrue(self.node.recovering_goals)

    def test_status_qos_refuses_retained_pre_restart_observation(self):
        status_subs = [row for row in self.node.subscriptions_seen if row[0].endswith('/_action/status')]
        self.assertEqual(len(status_subs), 2)
        self.assertTrue(all(row[2].durability == 'volatile' for row in status_subs))

    def test_submission_exception_requires_reconciliation(self):
        self.node.client.error = RuntimeError('transport failed after send')
        self.node.start('task-new', dict(x=1, y=2, yaw=0))
        self.assertEqual(len(self.engine.uncertain), 1)
        self.assertTrue(self.node.recovering_goals)
        self.assertFalse(self.engine.results)

    def test_acceptance_or_result_errors_never_claim_terminal(self):
        for stage in ('accepted', 'finished'):
            with self.subTest(stage=stage):
                self.setUp()
                self.node.start('task-new', dict(x=1, y=2, yaw=0))
                getattr(self.node, stage)('task-new', Future(error=RuntimeError('lost action reply')))
                self.assertTrue(self.engine.uncertain)
                self.assertFalse(self.engine.results)

    def test_nonterminal_result_and_late_result_keep_recovery_lock(self):
        self.node.start('task-new', dict(x=1, y=2, yaw=0))
        self.node.finished('task-new', Future(types.SimpleNamespace(status=0)))
        self.node.finished('task-new', Future(types.SimpleNamespace(status=5)))
        self.assertTrue(self.node.recovering_goals)
        self.assertFalse(self.engine.results)

    def test_late_acceptance_cannot_restore_a_reconciled_goal_handle(self):
        self.node.start('task-new', dict(x=1, y=2, yaw=0))
        identity = dict(self.node.current_goal)
        self.node.goal_uncertain('task-new', 'lost acceptance')
        self.node.goal_status(ACTION, status(identity['nav_goal_uuid']))
        handle = types.SimpleNamespace(accepted=True, goal_id=UUID(list(bytes.fromhex(identity['nav_goal_uuid']))))
        self.node.accepted('task-new', Future(handle))
        self.node.finished('task-new', Future(types.SimpleNamespace(status=5)))
        self.assertIsNone(self.node.goal_handle)
        self.assertFalse(self.engine.results)

    def test_stop_before_acceptance_can_finish_as_canceled(self):
        self.node.start('task-new', dict(x=1, y=2, yaw=0))
        self.node.cancel()
        value = self.node.current_goal['nav_goal_uuid']
        cancellations = []
        handle = types.SimpleNamespace(accepted=True, goal_id=UUID(list(bytes.fromhex(value))),
            cancel_goal_async=lambda: cancellations.append(True), get_result_async=lambda: Future())
        self.node.accepted('task-new', Future(handle))
        self.node.finished('task-new', Future(types.SimpleNamespace(status=5)))
        self.assertEqual(cancellations, [True])
        self.assertEqual(self.engine.results, [('task-new', 'canceled', 'action_status_5')])
        self.assertIsNone(self.node.goal_handle)

    def test_v2_route_also_uses_durable_assigned_uuid(self):
        mocks = dict(ROS_MOCKS)
        mocks.update({'mhs.navigation': module(MHSNavigator=NAV.Navigator),
                      'numpy': module(), 'PIL': module(Image=object, ImageDraw=object)})
        spec = importlib.util.spec_from_file_location('mhs._mock_navigation_v2', Path(__file__).with_name('navigation_v2.py'))
        v2 = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, mocks):
            spec.loader.exec_module(v2)
        node = v2.V2Navigator.__new__(v2.V2Navigator)
        node.__dict__.update(self.node.__dict__)
        node.route_client = Client()
        node.sync_mask = lambda *a, **kw: None
        node.poses = lambda route: [Goal(), Goal()]
        node.publish_mask = lambda route: None
        class InlineThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        with patch.object(v2.threading, 'Thread', InlineThread):
            node.start_route('route-new', {})
        self.assertEqual(self.engine.identities[0][0::2], ('route-new', ROUTE_ACTION))
        goal, kwargs = node.route_client.sent[0]
        self.assertEqual(bytes(kwargs['goal_uuid'].uuid).hex(), self.engine.identities[0][1])
        self.assertIn('feedback_callback', kwargs)


if __name__ == '__main__':
    unittest.main()
