"""Revoke previous delivery sessions on scene reset without editing AGH records."""
import json,time
from .manage import ROOT
from .runtime import data_dir

def revoke_previous():
    base=data_dir(ROOT);base.mkdir(parents=True,exist_ok=True);path=base/'session-bindings.json'
    bindings=json.loads(path.read_text()) if path.exists() else {}
    trace=base/'mcp-calls.jsonl'
    if trace.exists():
        for line in trace.read_text().splitlines():
            session=json.loads(line).get('session')
            if session:bindings.setdefault(session,{})
    for value in bindings.values():value.update(revoked=True,revoked_wall=time.time())
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(bindings,indent=2));temporary.replace(path)
    return len(bindings)

if __name__=='__main__':print(json.dumps({'revoked_sessions':revoke_previous()}))
