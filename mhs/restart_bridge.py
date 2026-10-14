"""Restart only the verified project adapter after measured stillness."""
from .manage import ROOT,ENV,api,stop_process,spawn

def main():
    state=api()
    if not state['stopped_confirmed']:raise RuntimeError('physical_stop_not_confirmed')
    stop_process('bridge')
    spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV)
    print('restarted_owned_bridge')

if __name__=='__main__':main()
