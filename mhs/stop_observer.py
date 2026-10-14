"""End only this project's recording observers; preserve their original reports."""
import json,os,signal,time
from pathlib import Path
from .manage import ROOT

def main():
    stopped=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            args=p.joinpath('cmdline').read_bytes().split(b'\0')
            if args[1:3]!=[b'-m',b'mhs.workbench_acceptance']:continue
            if p.joinpath('cwd').resolve()!=ROOT:raise RuntimeError('observer_cwd_mismatch')
            before=p.joinpath('stat').read_text().split(') ',1)[1].split()
            if before[0]=='Z':continue
            # Recheck both ownership and start time immediately before signalling.
            if p.joinpath('stat').read_text().split(') ',1)[1].split()[19]!=before[19] or p.joinpath('cwd').resolve()!=ROOT or p.joinpath('cmdline').read_bytes().split(b'\0')[1:3]!=args[1:3]:raise RuntimeError('observer_identity_changed')
            os.kill(int(p.name),signal.SIGTERM);stopped.append({'pid':int(p.name),'start_time':before[19]})
        except FileNotFoundError:continue
    folder=ROOT/'runs/mhs-v2/diagnostics';folder.mkdir(exist_ok=True)
    (folder/('observer-stop-'+time.strftime('%Y%m%d-%H%M%S')+'.json')).write_text(json.dumps({'stopped':stopped,'reason':'No browser task submitted; stop idle capture before protocol testing','wall_time':time.time()},indent=2))
    print(json.dumps({'stopped':stopped,'motion_unchanged':True}))

if __name__=='__main__':main()
