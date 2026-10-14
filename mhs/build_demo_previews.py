"""Create visibly labeled accelerated previews; keep original recordings intact."""
import hashlib,json,subprocess,time,shutil
from pathlib import Path
from .manage import ROOT,api

SOURCES=[
    ('normal-213','workbench-normal-20261012-235051',3),
    ('urgent-211','workbench-urgent-20261012-220238',24),
    ('recovery-207','workbench-receiver-20261012-201629',4),
]


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def duration(p):
    result=subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(p)],text=True)
    return float(json.loads(result)['format']['duration'])


def main():
    s=api()
    if not s['stopped_confirmed'] or (s.get('navigation') or {}).get('status') in {'pending','running','canceling'}:raise RuntimeError('wait_until_acceptance_motion_finished')
    if s['orders'] and not (s.get('mhs_finished') or s.get('mhs_outcome') or s.get('operator_stopped') or (s.get('mhs_budget') or {}).get('stopped_reason')):raise RuntimeError('wait_until_acceptance_task_finished')
    font=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if not font.is_file():raise RuntimeError('preview_label_font_missing')
    ffmpeg=None
    for candidate in dict.fromkeys(['/usr/bin/ffmpeg',shutil.which('ffmpeg')]):
        if candidate and Path(candidate).is_file() and ' drawtext ' in subprocess.check_output([candidate,'-hide_banner','-filters'],text=True,stderr=subprocess.DEVNULL):ffmpeg=candidate;break
    if not ffmpeg:raise RuntimeError('no_existing_ffmpeg_with_visible_text_filter')
    directory=ROOT/'runs/mhs-v2/demos'/('previews-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True,exist_ok=False)
    manifest=[]
    for label,attempt,speed in SOURCES:
        original=ROOT/'runs/mhs-v2/acceptance'/attempt/'simulation.mp4'
        target=directory/f'{label}-{speed}x.mp4'
        text=f'{speed}x PLAYBACK | Actual Isaac Sim recording | Simulated handoff'
        filters=f"setpts=PTS/{speed},fps=15,drawtext=fontfile={font}:text='{text}':fontsize=18:fontcolor=white:box=1:boxcolor=black@0.75:boxborderw=8:x=12:y=h-th-16"
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-i',str(original),'-vf',filters,'-an','-c:v','libx264','-preset','veryfast','-crf','24','-movflags','+faststart',str(target)],check=True,timeout=180)
        raw_seconds=duration(original);preview_seconds=duration(target)
        if abs(preview_seconds-raw_seconds/speed)>2:raise RuntimeError('unexpected_preview_duration')
        manifest.append({'label':label,'source':str(original.relative_to(ROOT)),'source_sha256':digest(original),'source_seconds':raw_seconds,'preview':target.name,'preview_seconds':preview_seconds,'playback_speed':speed,'preview_sha256':digest(target),'ffmpeg':ffmpeg,'editing':'Uniform playback acceleration, frame resampling, visible playback label. No generated scenes or replacement images. Original retained.'})
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps({'directory':str(directory),'previews':manifest},ensure_ascii=False))


if __name__=='__main__':main()
