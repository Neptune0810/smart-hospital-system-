"""Bounded camera motion check, separate from Agent delivery acceptance."""
import asyncio, hashlib, json, math, time
from .manage import ROOT, api
from .hub_client import HubClient
from .test_navigation_live import stopped

async def main():
    directory=ROOT/'runs/mhs-v1/acceptance/first-person-ui-20261011/motion'
    directory.mkdir(parents=True,exist_ok=False)
    frames=[];result={'pass':False};done=asyncio.Event()
    async def capture():
        while not done.is_set():
            try:
                meta=await asyncio.to_thread(api,'/api/frame?view=first_person')
                raw=(ROOT/'runs/first-person.jpg').read_bytes()
                if (not frames or meta['capture_wall_time']!=frames[-1]['capture_wall_time']) and hashlib.sha256(raw).hexdigest()==meta['sha256']:
                    name=f'{len(frames):04d}.jpg';(directory/name).write_bytes(raw)
                    frames.append(dict(meta,file=name))
                state=await asyncio.to_thread(api)
                with (directory/'state.jsonl').open('a') as f:f.write(json.dumps(state)+'\n')
            except Exception:pass
            await asyncio.sleep(.5)
    task=asyncio.create_task(capture())
    try:
        await stopped()
        before=api('/api/frame?view=first_person');start_heading=heading(before)
        async with HubClient(record=directory/'hub.jsonl') as hub:
            started=await hub.navigate('station_a')
            if not started.get('accepted'):raise RuntimeError('navigation_not_accepted')
            deadline=time.monotonic()+35;changed=False;moving=False
            while time.monotonic()<deadline:
                s=await asyncio.to_thread(api);meta=api('/api/frame?view=first_person')
                moving=moving or abs(s['pose']['speed'])>.03
                delta=abs(math.atan2(math.sin(heading(meta)-start_heading),math.cos(heading(meta)-start_heading)))
                if delta>.35:
                    changed=True;break
                await asyncio.sleep(.5)
            await hub.rpc('hub/stop',{'device':'hospital-robot-01'})
            after=await stopped();canceled=await hub.finish(started['job'])
            returned=await hub.navigate('depot')
            if not returned.get('accepted'):raise RuntimeError('return_not_accepted')
            completion=await asyncio.wait_for(hub.finish(returned['job']),120)
            final=await stopped()
            result=dict(pass_=changed and moving and after['stopped_confirmed'] and completion['status']=='done',
                        camera_heading_changed=changed,physical_motion_seen=moving,heading_change_radians=delta,
                        before=before,after_stop=after,canceled_job=canceled,return_job=completion,final=final)
            result['pass']=result.pop('pass_')
    except Exception as error:
        result['error']=str(error)
        await asyncio.to_thread(api,'/api/mhs/stop',{'reason':'first_person_validation_error'})
    finally:
        done.set();await task
        (directory/'frames.jsonl').write_text(''.join(json.dumps(frame)+'\n' for frame in frames))
        (directory/'report.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({'directory':str(directory),'pass':result['pass'],'frames':len(frames),'error':result.get('error')}))

def heading(frame):
    eye=frame['camera']['eye_world'];target=frame['camera']['look_at_world']
    return math.atan2(target[1]-eye[1],target[0]-eye[0])

if __name__=='__main__':asyncio.run(main())
