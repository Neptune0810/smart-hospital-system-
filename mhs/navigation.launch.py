from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os
def generate_launch_description():
    bringup=IncludeLaunchDescription(PythonLaunchDescriptionSource('/project/mhs/nav2_launch/bringup_launch.py'),launch_arguments={
        'namespace':'carter1','use_namespace':'True','use_sim_time':'True','autostart':'False','use_composition':'False',
        'map':os.environ.get('HOSPITAL_MAP_YAML','/project/maps/carter_hospital_navigation.yaml'),'params_file':'/project/configs/nav2.yaml'}.items())
    scan=Node(package='pointcloud_to_laserscan',executable='pointcloud_to_laserscan_node',namespace='carter1',name='cloud_to_scan',
        parameters=[{'use_sim_time':True,'target_frame':'front_3d_lidar','transform_tolerance':0.1,
        'min_height':-0.4,'max_height':1.5,'angle_min':-1.5708,'angle_max':1.5708,'angle_increment':0.0087,
        'scan_time':0.3333,'range_min':0.05,'range_max':100.0,'use_inf':True}],
        remappings=[('cloud_in','front_3d_lidar/lidar_points'),('/tf','/carter1/tf'),('/tf_static','/carter1/tf_static')],output='screen')
    return LaunchDescription([scan,bringup])
