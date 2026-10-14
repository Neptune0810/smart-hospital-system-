"""Record actual JPEG frames and their capture timestamps; no rendered substitutes."""
import json,time,subprocess,threading,hashlib
from pathlib import Path
from .manage import ROOT,api

class Recorder:
    def __init__(self,directory):self.directory=Path(directory);self.done=threading.Event()
    def start(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        self.proc=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','image2pipe','-framerate','2','-vcodec','mjpeg','-i','pipe:0','-c:v','libx264','-pix_fmt','yuv420p',str(self.directory/'simulation.mp4')],stdin=subprocess.PIPE)
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start();return self
    def run(self):
        while not self.done.is_set():
            try:
                meta=api('/api/frame');raw=(ROOT/'runs/latest.jpg').read_bytes()
                if hashlib.sha256(raw).hexdigest()!=meta['sha256']:continue
                self.proc.stdin.write(raw);self.proc.stdin.flush()
                with (self.directory/'frames.jsonl').open('a') as f:f.write(json.dumps(meta)+'\n')
            except Exception:pass
            self.done.wait(.5)
    def stop(self):
        self.done.set();self.thread.join(4);self.proc.stdin.close();self.proc.wait(timeout=20)
