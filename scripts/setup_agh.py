#!/usr/bin/env python3
"""Rebuild project AGH registration from a non-secret profile template.
Uses the already authorized exact hospital tools; never changes public AGH.
Run only when creating a fresh project home, not on an active experiment.
"""
import json,subprocess,re,os
from pathlib import Path
import manage
ROOT=Path(__file__).resolve().parents[1]
PROJECT_AGH_HOME=ROOT/'agent/agh-home'
def call(*args):
    r=subprocess.run(['python3',str(ROOT/'scripts/agh_resource.py'),*args],cwd=ROOT,capture_output=True,text=True,timeout=90)
    (ROOT/'logs'/('setup-'+args[0]+'-'+args[1]+'.log')).write_text(r.stdout+r.stderr)
    r.check_returncode();print(r.stdout)
    return r.stdout
def main():
    profile=PROJECT_AGH_HOME/'profiles/local-dev/profile.yaml'
    if profile.exists():raise RuntimeError('Project profile already exists; refusing to overwrite an active configuration')
    config=json.loads((ROOT/'agent/profile.template.json').read_text())
    config['presets']['default']='standard';config['packages']=[]
    profile.parent.mkdir(parents=True,exist_ok=True);profile.write_text(json.dumps(config,indent=2))
    manage.start(agent_enabled=False)
    call('package','inspect','file:./agent/hospital-policy')
    call('package','add','file:./agent/hospital-policy')
    # Hashes are frozen for the bundled v1.0.2 package; changed code must be inspected and trusted anew.
    call('package','trust','hospital-delivery-policy','sha256-2f763e32026b5fc548d140e74309d42a6a5cd00579df3fbffd80606d7d12bc37','db8fc122cf79f2145756a30e1fc05c189a4b148e5344fd24eaabc07f8beec618')
    call('package','enable','hospital-delivery-policy')
    text=call('mcp','add','hospital-delivery','--name','Hospital delivery','--stdio',str(ROOT/'scripts/mcp.sh'))
    revision=re.search(r'revision=([a-f0-9]{64})',text).group(1)
    call('mcp','trust','hospital-delivery','--expected-revision',revision)
    call('mcp','enable','hospital-delivery','--expected-revision',revision)
    actual=json.loads(profile.read_text());actual['presets']={'default':'hospital-delivery','allowed':['standard','hospital-delivery']};profile.write_text(json.dumps(actual,indent=2))
    manage.agh_control('stop');manage.agh_control('start')
    call('mcp','get','hospital-delivery')
if __name__=='__main__':main()
