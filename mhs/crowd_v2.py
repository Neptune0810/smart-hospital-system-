"""Four real IRA walking characters added to the existing hospital stage."""
import asyncio, json, time
from pathlib import Path
if __package__:
    from .runtime import data_dir
else:
    # Isaac launches hospital_sim.py as a script and imports this module from
    # its directory. Keep that existing entry point as well as package imports.
    from runtime import data_dir

class HospitalCrowd:
    def __init__(self,root,stage,seed=101):
        self.root=Path(root);self.stage=stage;self.seed=seed;self.last_publish=0;self.command_id=None;self.mode='normal';self.targets=[]
        self.data_dir=data_dir(self.root)
        # Normal traffic is distributed across the hospital. The west corridor
        # retains a walker, with another in the ward corridor instead of both
        # repeatedly occupying the B handoff point. Congestion mode is separate.
        self.normal=[[[-24,11.8,0],[-17,11.8,0]],[[-40,26,0],[-40,30,0]],[[14,10,0],[20,10,0]],[[18,18,0],[18,23,0]]]
        self.crowded=[[[-3,-1,0],[0,-1,0]],[[-3,2.5,0],[-1,2.5,0]],[[-2,0,0],[1,0,0]],[[-3,3.5,0],[-1,3.5,0]]]
        self.crossing=[[[-18,9.5,0],[-18,11.6,0]],*self.normal[1:]]
        self.setup_task=asyncio.ensure_future(self.setup())
    async def setup(self):
        import omni.kit.app, omni.kit.commands, NavSchema
        from pxr import UsdGeom,Gf
        from isaacsim.replicator.agent.core.configuration.models.character import CharacterConfig
        from isaacsim.replicator.agent.core.scene_assembly.character_loader import CharacterLoader
        from isaacsim.replicator.agent.core.randomizer import Randomizer
        # Only this stage/session layer is changed; no shared extension settings are edited.
        volume=NavSchema.NavMeshVolume.Define(self.stage,'/World/HospitalCrowdNavMesh')
        volume.GetNavVolumeTypeAttr().Set('Include')
        UsdGeom.Boundable(volume.GetPrim()).GetExtentAttr().Set([Gf.Vec3f(-49,-4,-.15),Gf.Vec3f(27,38,2.2)])
        NavSchema.NavMeshAreaAPI.Apply(volume.GetPrim())
        # The volume is a navigation guide, never a rendered LiDAR obstacle.
        UsdGeom.Imageable(volume.GetPrim()).CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        UsdGeom.Imageable(volume.GetPrim()).CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
        diagnostic={'path':str(volume.GetPath()),'type':volume.GetPrim().GetTypeName(),'attributes':{a.GetName():str(a.Get()) for a in volume.GetPrim().GetAttributes()}}
        folder=self.data_dir/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
        (folder/('navmesh-volume-'+time.strftime('%Y%m%d-%H%M%S')+'.json')).write_text(json.dumps(diagnostic,indent=2))
        import omni.anim.navigation.core as nav
        navigation=nav.acquire_interface()
        if navigation.get_navmesh() is None:
            if not navigation.start_navmesh_baking():raise RuntimeError('hospital_navmesh_bake_rejected')
            deadline=time.monotonic()+180
            while navigation.get_navmesh() is None:
                if time.monotonic()>deadline:raise RuntimeError('hospital_navmesh_bake_timeout')
                await omni.kit.app.get_app().next_update_async()
                await asyncio.sleep(.02)
        self.targets=[];groups={}
        for i,path in enumerate(self.normal):
            targets=[]
            for j,point in enumerate(path):
                prim=UsdGeom.Xform.Define(self.stage,f'/World/HospitalPeopleTargets/P{i}_{j}')
                prim.AddTranslateOp().Set(Gf.Vec3d(*point));targets.append(str(prim.GetPath()))
            self.targets.append(targets)
            groups[f'person{i}']={'num':1,'routines':[{'type':'patrol','target_prims':targets,'speed_range':[.45,.6]}],'colliders':[{'cylinder':{'name':'body_collider','radius':.28}}]}
        library=self.root/'assets/people-v2/omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/6.0/Isaac/People/MotionLibrary/HumanMotionLibrary.usd'
        if not library.exists():raise RuntimeError('project_motion_library_cache_missing')
        config=CharacterConfig.model_validate({'root_prim_path':'/World/HospitalPeople','motion_library_path':str(library),'groups':groups})
        positions=[path[0] for path in self.normal]
        class SpawnRandomizer(Randomizer):
            def __init__(self,seed):super().__init__(seed);self.index=0
            def get_random_position(self,*args,**kwargs):
                import carb
                point=positions[self.index%len(positions)];self.index+=1;return carb.Float3(*point)
        self.loader=CharacterLoader(config,SpawnRandomizer(self.seed),self.stage)
        count=await self.loader.load()
        if count!=4:raise RuntimeError(f'crowd_character_load_failed:{count}/4')
        self.paths=[str(p.GetPath()) for p in self.stage.Traverse() if p.GetTypeName()=='SkelRoot' and str(p.GetPath()).startswith('/World/HospitalPeople/')]
        print('HOSPITAL_CROWD_READY',json.dumps({'count':count,'seed':self.seed,'paths':self.paths}),flush=True)
    def ready(self):
        if not self.setup_task.done():return False
        self.setup_task.result();return True
    def tick(self,sim_time):
        if not self.ready():return
        from pxr import UsdGeom,Gf
        from omni.metropolis.pipeline.agent import AgentsManager
        command=self.root/'runs/crowd-command-v2.json'
        if command.exists():
            d=json.loads(command.read_text())
            if d['id']!=self.command_id:
                self.command_id=d['id'];self.mode=d['mode'];paths=getattr(self,self.mode)
                for target_paths,points in zip(self.targets,paths):
                    for target,point in zip(target_paths,points):UsdGeom.Xformable(self.stage.GetPrimAtPath(target)).GetOrderedXformOps()[0].Set(Gf.Vec3d(*point))
                print('CROWD_MODE',self.mode,flush=True)
        if time.monotonic()-self.last_publish<.2:return
        people=[]
        for actor in AgentsManager.get_instance().get_runtime_agent_instances():
            if str(actor.prim.GetPath()).startswith('/World/HospitalPeople/'):
                position=actor.get_world_position()
                if position is not None:people.append({'id':str(actor.prim.GetPath()),'position':list(position),'speed_mps':actor.get_speed(),'radius_m':.28})
        if len(people)!=4:return
        self.last_publish=time.monotonic();data={'wall_time':time.time(),'sim_time':sim_time,'seed':self.seed,'mode':self.mode,'people':people,'source':'IRA_actual_character_runtime','simulated':True}
        p=self.root/'runs/crowd-v2.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data));tmp.replace(p)
        folder=self.data_dir/'crowd';folder.mkdir(parents=True,exist_ok=True)
        with (folder/'positions.jsonl').open('a') as f:f.write(json.dumps(data)+'\n')
