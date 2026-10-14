"""Read the installed ROS timer API; no robot commands."""
import inspect,json
from rclpy.node import Node
from rclpy.clock import Clock,ClockType
print(json.dumps({'create_timer':str(inspect.signature(Node.create_timer)),'steady_clock_type':str(ClockType.STEADY_TIME),'clock':str(inspect.signature(Clock))}))
