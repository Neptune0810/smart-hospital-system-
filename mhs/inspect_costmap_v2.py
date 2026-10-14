"""Read-only Nav2 costmap evidence in the project's ROS domain."""
import json,time
from pathlib import Path
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile,DurabilityPolicy,ReliabilityPolicy
from nav2_msgs.msg import Costmap
from PIL import Image,ImageDraw
import numpy as np
ROOT=Path('/project')
def main():
    rclpy.init();node=Node('hospital_v2_costmap_inspection',namespace='carter1');received=[]
    qos=QoSProfile(depth=1,durability=DurabilityPolicy.TRANSIENT_LOCAL,reliability=ReliabilityPolicy.RELIABLE)
    sub=node.create_subscription(Costmap,'global_costmap/costmap_raw',received.append,qos)
    deadline=time.monotonic()+15
    while not received and time.monotonic()<deadline:rclpy.spin_once(node,timeout_sec=.2)
    if not received:raise RuntimeError('costmap_not_received')
    m=received[-1];info=m.metadata;p=info.origin.position;data=np.array(m.data,dtype=np.int16).reshape(info.size_y,info.size_x)
    folder=ROOT/'runs/mhs-v2/diagnostics'/('costmap-'+time.strftime('%Y%m%d-%H%M%S'));folder.mkdir(parents=True)
    image=Image.fromarray(np.where(data==255,127,255-data).astype(np.uint8)[::-1]);draw=ImageDraw.Draw(image)
    result={'wall_time':time.time(),'stamp':m.header.stamp.sec+m.header.stamp.nanosec/1e9,'source':'full actual costmap_raw','raw_cost_semantics':{'lethal':254,'inscribed':253,'unknown':255},'origin':[p.x,p.y],'resolution':info.resolution,'size':[info.size_x,info.size_y],'samples':[]}
    for x,y in [(18.125,17.075),(18,18),(18,19),(17,17),(19,17),(18,16),(18,20),(19.125,19.575),(20.5,19.9)]:
        cx=int((x-p.x)/info.resolution);cy=int((y-p.y)/info.resolution)
        result['samples'].append({'position':[x,y],'raw_cost':int(data[cy,cx])});iy=info.size_y-1-cy;draw.ellipse((cx-5,iy-5,cx+5,iy+5),outline=200)
    image.save(folder/'costmap.png');result['grid_file']='costmap.npy';np.save(folder/'costmap.npy',data)
    (folder/'report.json').write_text(json.dumps(result,indent=2));print(json.dumps({'folder':str(folder),**result}),flush=True)
    node.destroy_node();rclpy.shutdown()
if __name__=='__main__':main()
