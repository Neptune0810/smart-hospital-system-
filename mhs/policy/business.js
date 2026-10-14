import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { remoteInputSchema } from '../../agent/agh-source-mhs/packages/base/src/mcp-json-schema.js';
import { appendFileSync } from 'node:fs';
import {root,data,businessNames} from './runtime.js';
const names=new Set(businessNames);
const writes=new Set(['publish_delivery_plan','create_delivery_orders','pickup_package','confirm_delivery','defer_order','close_order','create_specimen_delivery','publish_service_plan']);
export async function connectBusiness(agnes,onVerification=()=>{}){
  const transport=new StdioClientTransport({command:root+'/mhs/mcp.sh',cwd:root,stderr:'pipe'});
  const client=new Client({name:'hospital-scoped-business',version:'1.0.0'});
  await client.connect(transport);
  const catalog=await client.listTools();
  if(catalog.tools.length!==names.size||catalog.tools.some(t=>!names.has(t.name))){await client.close();throw Error('Unexpected hospital MCP catalog');}
  const disposers=catalog.tools.map(t=>agnes.registerTool({
    name:'hospital_'+t.name,description:t.description,parameters:remoteInputSchema((()=>{const schema=structuredClone(t.inputSchema);if(writes.has(t.name)){delete schema.properties.operation_id;schema.required=(schema.required??[]).filter(k=>k!=='operation_id');}return schema;})()),
    meta:{isReadOnly:!writes.has(t.name),isDestructive:false,isConcurrencySafe:false,isOpenWorld:false,
      replay:writes.has(t.name)?'idempotent':'safe',costHint:undefined,deferLoading:false,requiresApproval:'never'},
    async execute(args,call){
      const started=Date.now();
      if(writes.has(t.name)){
        if(!call.session.toolUseId)throw Error('missing_tool_call_identity');
        args={...args,operation_id:'biz-'+call.session.toolUseId};
      }
      try{
        const result=await client.callTool({name:t.name,arguments:args},undefined,{signal:call.signal,timeout:t.name==='get_route_options'?80000:30000});
        if(!Array.isArray(result.content)||result.content.some(x=>x.type!=='text'))throw Error('Unexpected business result type');
        appendFileSync(data+'/mcp-calls.jsonl',JSON.stringify({time:started/1000,session:call.session.key,name:t.name,args,result,elapsed_ms:Date.now()-started})+'\n');
        const rejected=result.structuredContent?.ok===false||result.content.some(x=>{try{return JSON.parse(x.text)?.ok===false;}catch{return false;}});
        if(['verify_run','verify_services'].includes(t.name)&&!rejected&&!result.isError){
          for(const part of result.content){
            try{const body=JSON.parse(part.text);if(body.ok&&body.result&&(t.name!=='verify_services'||body.result.ready_to_finalize&&!body.result.pending?.length))onVerification(call.session.key,body.result);}catch{}
          }
        }
        return {content:result.content,isError:result.isError===true||rejected,structured:result.structuredContent};
      }catch(error){return {content:[{type:'text',text:String(error)}],isError:true};}
    }
  }));
  return async()=>{for(const dispose of disposers)dispose();await client.close();};
}
