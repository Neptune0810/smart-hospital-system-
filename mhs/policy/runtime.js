import {readFileSync} from 'node:fs';
export const root='/mnt/sda/omni-yezh/hospital-delivery-agent';
let configured='mhs-v2';
try{configured=JSON.parse(readFileSync(root+'/configs/runtime-profile.json','utf8')).mode??configured;}catch(e){if(e.code!=='ENOENT')throw e;}
export const smart=(process.env.HOSPITAL_MODE??configured)==='smart-ward-v1';
export const data=root+'/runs/'+(smart?'smart-ward-v1':'mhs-v2');
export const smartBusiness=['get_state','get_route_options','create_specimen_delivery','publish_service_plan','defer_order','close_order','verify_services','wait_event'];
export const legacyBusiness=['get_state','publish_delivery_plan','create_delivery_orders','pickup_package','confirm_delivery','defer_order','close_order','verify_run','wait_event','get_route_options'];
export const businessNames=smart?smartBusiness:legacyBusiness;
