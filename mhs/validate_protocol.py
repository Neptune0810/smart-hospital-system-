import json,subprocess,time
from .manage import ROOT
def main():
    directory=ROOT/'runs/mhs-v2/acceptance'/('schema-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True);rows=[]
    for p in sorted((ROOT/'runs/mhs-v2/protocol').glob('*/messages.jsonl')):
        result=subprocess.run([str(ROOT/'agent/mhs-venv/bin/python'),'-m','agnes_mhs.validate',str(p)],capture_output=True,text=True)
        (directory/(p.parent.name+'.log')).write_text(result.stdout+result.stderr)
        rows.append({'source':str(p),'exit_code':result.returncode,'pass':result.returncode==0,'output':result.stdout[-1500:]})
    (directory/'report.json').write_text(json.dumps(rows,indent=2));print(json.dumps(rows,indent=2))
if __name__=='__main__':main()
