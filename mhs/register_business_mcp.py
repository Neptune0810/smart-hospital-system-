import json,re
from .setup import recorded
from .manage import ROOT
def main():
    listing=recorded(['mcp','list'])
    out=recorded(['mcp','get','hospital-business']) if 'hospital-business' in listing else recorded(['mcp','add','hospital-business','--name','Hospital business','--stdio',str(ROOT/'mhs/mcp.sh')])
    revision=re.search(r'revision=([a-f0-9]{64})',out).group(1)
    recorded(['mcp','trust','hospital-business','--expected-revision',revision])
    current=recorded(['mcp','get','hospital-business']);revision=re.search(r'revision=([a-f0-9]{64})',current).group(1)
    recorded(['mcp','test','hospital-business','--expected-revision',revision])
    # This generic open-world entry deliberately remains disabled. Its empty
    # catalog is not a transport acceptance result; the project plugin checks
    # and calls the fixed eight-tool MCP catalog on every real session.
if __name__=='__main__':main()
