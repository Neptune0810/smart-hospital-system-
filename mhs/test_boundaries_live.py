"""Real MCP business rejection checks with actual Nav2, separate from Agent tests."""
import asyncio,json,time,uuid
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from .manage import ROOT,api
from .hub_client import HubClient
from .record import Recorder
from .test_navigation_live import stopped

async def main():
    start=api()
    if start['orders'] or not start['stopped_confirmed'] or 'depot' not in start['at_stations']:raise RuntimeError('fresh_empty_stationary_depot_run_required')
    directory=ROOT/'runs/mhs-v1/acceptance'/('boundaries-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True);rows=[];rec=Recorder(directory).start()
    async with stdio_client(StdioServerParameters(command=str(ROOT/'mhs/mcp.sh'),cwd=str(ROOT))) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            async def call(name,args):
                response=await session.call_tool(name,args)
                body=json.loads(next(x.text for x in response.content if x.type=='text'))
                with (directory/'mcp.jsonl').open('a') as f:f.write(json.dumps({'wall_time':time.time(),'name':name,'arguments':args,'response':body},ensure_ascii=False)+'\n')
                return body
            async def reject(case,name,args,error):
                response=await call(name,args)
                rows.append({'case':case,'pass':response.get('ok') is False and response.get('error')==error,'response':response,'state':api()})
            try:
                await reject('unknown_station_rejected','create_delivery_orders',{'items':[{'destination':'unknown'}],'operation_id':'boundary-bad-create'},'invalid_station')
                await reject('rejected_creation_operation_conflict','create_delivery_orders',{'items':[{'destination':'A站'}],'operation_id':'boundary-bad-create'},'operation_id_conflict')
                response=await call('create_delivery_orders',{'items':[{'destination':'A护士站'},{'destination':'C站'},{'destination':'B站'}],'operation_id':'boundary-create'})
                orders=response['result']['orders']
                async with HubClient(record=directory/'hub.jsonl') as hub:
                    before=api();receipt=await hub.navigate('station_a','boundary-empty-navigation')
                    empty=await hub.finish(receipt['job']) if receipt.get('job') else receipt
                    after=await stopped()
                    rows.append({'case':'empty_delivery_navigation_rejected','pass':empty.get('status')!='done' and after.get('navigation') is None and after['stopped_confirmed'],'response':empty,'before':before,'after':after})
                for i in range(2):await call('pickup_package',{'order_id':orders[i]['order_id'],'operation_id':'boundary-pick-'+str(i)})
                await reject('capacity_two_rejects_third','pickup_package',{'order_id':orders[2]['order_id'],'operation_id':'boundary-overcapacity'},'capacity_exceeded')
                args={'order_id':orders[0]['order_id'],'package_id':orders[0]['package_id'],'operation_id':'boundary-wrong-location'}
                await reject('wrong_destination_rejected','confirm_delivery',args,'not_at_destination')
                await reject('wrong_package_rejected','confirm_delivery',dict(args,package_id='not-the-package',operation_id='boundary-wrong-package'),'package_mismatch')
                async with HubClient(record=directory/'hub.jsonl') as hub:
                    receipt=await hub.navigate('station_a');result=await hub.finish(receipt['job']);await stopped()
                    if result['status']!='done':raise RuntimeError('actual_A_navigation_failed')
                    handoff={'order_id':orders[0]['order_id'],'package_id':orders[0]['package_id'],'operation_id':'boundary-handoff'}
                    api('/api/admin/receiver',{'station_id':'station_a','available':False})
                    await reject('receiver_rejects_first_handoff','confirm_delivery',dict(handoff,operation_id='boundary-outage-1'),'receiver_unavailable')
                    await reject('repeat_failed_operation_preserves_result','confirm_delivery',dict(handoff,operation_id='boundary-outage-1'),'receiver_unavailable')
                    await reject('receiver_rejects_second_handoff','confirm_delivery',dict(handoff,operation_id='boundary-outage-2'),'receiver_unavailable')
                    await reject('third_failed_attempt_is_rejected','confirm_delivery',dict(handoff,operation_id='boundary-outage-3'),'handoff_retry_limit')
                    api('/api/admin/receiver',{'station_id':'station_a','available':True})
                    first=await call('confirm_delivery',handoff);same=await call('confirm_delivery',handoff)
                    repeated=await call('confirm_delivery',dict(handoff,operation_id='boundary-handoff-new'))
                    receipts=api('/api/report')['receipts']
                    rows.append({'case':'duplicate_handoff_one_receipt','pass':first==same and len(receipts)==1 and repeated['result'].get('already_delivered') is True,'first':first,'same':same,'new_operation':repeated,'state':api()})
                    for order in orders[1:]:await call('close_order',{'order_id':order['order_id'],'reason':'boundary-test-order-explicitly-not-delivered','operation_id':'boundary-close-'+order['order_id']})
                    receipt=await hub.navigate('depot');result=await hub.finish(receipt['job']);after=await stopped()
                    rows.append({'case':'return_after_boundary_checks','pass':result['status']=='done' and 'depot' in after['at_stations'],'state':after})
            except Exception as error:
                rows.append({'case':'validation_error','pass':False,'error':str(error)});api('/api/mhs/stop',{'reason':'boundary_acceptance_error'})
            finally:
                rec.stop();(directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps({'directory':str(directory),'results':[{'case':x['case'],'pass':x['pass']} for x in rows]}))
if __name__=='__main__':asyncio.run(main())
