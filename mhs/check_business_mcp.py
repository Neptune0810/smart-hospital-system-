"""Read-only transport check of the project's real, fixed MCP server."""
import asyncio,json,time
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from .manage import ROOT

async def main():
    expected={'get_state','publish_delivery_plan','create_delivery_orders','pickup_package','confirm_delivery','defer_order','close_order','verify_run','wait_event','get_route_options'}
    directory=ROOT/'runs/mhs-v2/acceptance'/('business-mcp-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True)
    server=StdioServerParameters(command=str(ROOT/'mhs/mcp.sh'),cwd=str(ROOT))
    async with stdio_client(server) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            initialized=await session.initialize()
            catalog=await session.list_tools()
            actual={t.name for t in catalog.tools}
            if actual!=expected:raise RuntimeError('unexpected_business_catalog')
            state=await session.call_tool('get_state',{})
            report={'initialize':initialized.model_dump(mode='json'),'tools':catalog.model_dump(mode='json'),'state':state.model_dump(mode='json'),'pass':not state.isError,'scope':'read-only real MCP; no navigation/cancel catalog'}
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps({'directory':str(directory),'tool_count':len(actual),'pass':report['pass']}))
if __name__=='__main__':asyncio.run(main())
