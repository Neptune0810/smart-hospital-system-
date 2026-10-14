import subprocess,os,pty,select,time,sys
env=dict(os.environ,AGH_HOME=os.environ.get("AGH_HOME","/mnt/sda/omni-yezh/hospital-delivery-agent/agent/agh-home"),AGNES_MCP_STDIO_ALLOWLIST="/mnt/sda/omni-yezh/hospital-delivery-agent/scripts/mcp.sh",AGNES_PROFILE="local-dev")
master,slave=pty.openpty()
p=subprocess.Popen(["node","/mnt/sda/omni-yezh/hospital-delivery-agent/agent/agh-runtime/agnes.mjs"]+sys.argv[1:],env=env,stdin=slave,stdout=slave,stderr=slave)
os.close(slave);text="";confirmed=False;start=time.time()
while time.time()-start<60:
 if select.select([master],[],[],.2)[0]:
  try:
   data=os.read(master,65536).decode(errors="replace");text+=data;sys.stdout.write(data);sys.stdout.flush()
   if ("Continue? [y/N]" in text or ("[y/N]" in text and "hospital-delivery-policy" in text and sys.argv[1] == "package" and sys.argv[2] in ["add","trust","enable","remove"])) and not confirmed:os.write(master,b"y\n");confirmed=True
  except OSError:break
 if p.poll() is not None:break
if p.poll() is None:p.terminate()
sys.exit(p.wait())
