import {smart,businessNames} from './runtime.js';
const business=new Set(businessNames.map(x=>'hospital_'+x));
const deviceTools=new Set(['read_device','call_device','stop_device','watch_device','unwatch_device']);
const commands=new Map([['hospital-robot-01','navigate_route'],['hospital-cabinet-b','handoff_order'],['hospital-cabinet-c','handoff_order'],['hospital-cabinet-lab','handoff_order'],['hospital-light-301','set_brightness'],['hospital-light-303','set_brightness']]);
const devices=new Set([...commands.keys(),'hospital-sensor-301','hospital-sensor-303']);
export function allowTool({name,args={}}){
  if(name==='tool_describe')return business.has(args.name)||deviceTools.has(args.name)||args.name==='list_devices'?{allow:true}:{allow:false,reason:'仅可读取本项目已授权配送和MHS工具的说明；禁止描述其他工具。'};
  if(business.has(name)||name==='list_devices')return {allow:true};
  if(!deviceTools.has(name))return {allow:false,reason:'仅允许医院业务 MCP 与本项目 MHS 工具；禁止 shell、文件、网络和其他工具。请使用配送接口继续。'};
  if(name!=='unwatch_device'&&!(smart?devices.has(args.device):args.device==='hospital-robot-01'))return {allow:false,reason:'仅允许本项目已登记设备。'};
  if(name==='call_device'&&(smart?commands.get(args.device)!==args.tool:args.tool!=='navigate_route'))return {allow:false,reason:'设备与命令不在精确白名单内；传感器只读，运动停止使用 stop_device。'};
  return {allow:true};
}
