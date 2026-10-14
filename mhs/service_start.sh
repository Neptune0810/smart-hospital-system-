#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=73 RMW_IMPLEMENTATION=rmw_fastrtps_cpp HOSPITAL_ROOT=/project PYTHONPATH=/project:$PYTHONPATH
exec python3 -m mhs.api --ros-args -p use_sim_time:=true
