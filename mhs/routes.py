"""Deterministic occupancy-grid candidates; the Agent chooses, Nav2 executes."""
import hashlib, heapq, json, math, re, time
from pathlib import Path
from .runtime import map_yaml,map_image,mode,SMART_MODE
from PIL import Image, ImageFilter
import numpy as np

class Routes:
    def __init__(self, root):
        self.root=Path(root)
        self.cfg=json.loads((self.root/'configs/hospital-v2.json').read_text(encoding='utf-8'))
        config=map_yaml(self.root).read_text(encoding='utf-8')
        self.resolution=float(re.search(r'resolution:\s*([^\s#]+)',config).group(1))
        origin=json.loads(re.search(r'origin:\s*(\[[^\]]+\])',config).group(1))
        if abs(origin[2])>1e-7:raise ValueError('rotated_map_not_supported')
        self.origin=tuple(origin[:2]);self.step=.5
        image=Image.open(map_image(self.root)).convert('L')
        self.image_size=image.size
        # Treat unknown as occupied, with 0.70 m clearance for Carter's footprint.
        mask=image.point(lambda p:255 if p>240 else 0).filter(ImageFilter.MinFilter(29))
        scale=self.step/self.resolution
        self.h=math.ceil(image.height/scale);self.w=math.ceil(image.width/scale)
        raster=np.array(mask);self.free=np.zeros((self.h,self.w),dtype=bool)
        for y in range(self.h):
            py=image.height-1-round((self.h-y-.5)*scale)
            for x in range(self.w):
                px=round((x+.5)*scale)
                if 0<=px<image.width and 0<=py<image.height:self.free[y,x]=raster[py,px]>240
        self.cache={};self.version=self.cfg['version']
        if mode(self.root)==SMART_MODE:self.version+=':smart:'+hashlib.sha256(map_image(self.root).read_bytes()).hexdigest()[:16]
    def cell(self,x,y):return (int((x-self.origin[0])/self.step),self.h-1-int((y-self.origin[1])/self.step))
    def point(self,c):return [self.origin[0]+(c[0]+.5)*self.step,self.origin[1]+(self.h-c[1]-.5)*self.step]
    def valid(self,c):return 0<=c[0]<self.w and 0<=c[1]<self.h and self.free[c[1],c[0]]
    def snap(self,p):
        c=self.cell(*p)
        if self.valid(c):return c
        nearby=[(math.dist(self.point((x,y)),p),(x,y)) for y in range(c[1]-3,c[1]+4) for x in range(c[0]-3,c[0]+4) if self.valid((x,y))]
        if not nearby or min(nearby)[0]>1:raise ValueError('route_endpoint_not_clear')
        return min(nearby)[1]
    def zone(self,p,z):
        x,y=p;return z['bounds'][0]<=x<=z['bounds'][2] and z['bounds'][1]<=y<=z['bounds'][3]
    def crowd(self):
        try:
            d=json.loads((self.root/'runs/crowd-v2.json').read_text())
            d['fresh']=0<=time.time()-d['wall_time']<3;return d
        except (OSError,ValueError,KeyError):return {'fresh':False,'people':[],'wall_time':None}
    def search(self,start,end,mode,crowd,closed,penalty):
        frontier=[(0,0,start)];cost={start:0};parent={};zones=self.cfg['zones']
        while frontier:
            _,g,c=heapq.heappop(frontier)
            if g!=cost.get(c):continue
            if c==end:
                path=[c]
                while c!=start:c=parent[c];path.append(c)
                return path[::-1]
            for dx,dy in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
                n=(c[0]+dx,c[1]+dy)
                if not self.valid(n) or (dx and dy and (not self.valid((c[0]+dx,c[1])) or not self.valid((c[0],c[1]+dy)))):continue
                p=self.point(n)
                if any(z['id'] in closed and self.zone(p,z) for z in zones):continue
                near=sum(max(0,3-math.dist(p,v['position'][:2]))/3 for v in crowd['people'])
                quiet=any(z['kind']=='quiet' and self.zone(p,z) for z in zones)
                weight=1+(near*4 if mode=='patient_friendly' else near*.5)+(quiet*2 if mode=='quiet_avoidance' else quiet*.4)+penalty.get(n,0)
                ng=g+math.hypot(dx,dy)*weight
                if ng<cost.get(n,float('inf')):
                    cost[n]=ng;parent[n]=c;heapq.heappush(frontier,(ng+math.dist(n,end),ng,n))
        return None
    def options(self,pose,station,closed=()):
        crowd=self.crowd()
        if not crowd['fresh']:raise ValueError('crowd_telemetry_stale')
        start=self.snap([pose['x'],pose['y']]);end=self.snap([station['x'],station['y']]);rows=[];seen=set();penalty={}
        for mode in ['shortest','patient_friendly','quiet_avoidance']:
            cells=self.search(start,end,mode,crowd,closed,{} if mode!='quiet_avoidance' else penalty)
            if not cells:continue
            # Only return meaningfully different corridors, not duplicate labels.
            signature=tuple(cells)
            if signature in seen:continue
            if rows and min(sum(c not in set(x['_cells']) for c in cells)/max(1,len(cells)) for x in rows)<.15:continue
            seen.add(signature);points=[self.point(c) for c in cells]
            actual_start=[pose['x'],pose['y']];actual_end=[station['x'],station['y']]
            if len(points)==1 and actual_start!=actual_end:points=[actual_start,actual_end]
            else:points[0]=actual_start;points[-1]=actual_end
            length=sum(math.dist(a,b) for a,b in zip(points,points[1:]));eta=0;exposure=0;quiet_m=0
            for a,b in zip(points,points[1:]):
                dist=math.dist(a,b);speed=self.speed_limit(b,crowd)
                eta+=dist/speed;exposure+=dist*sum(math.dist(b,v['position'][:2])<3 for v in crowd['people'])
                if any(z['kind']=='quiet' and self.zone(b,z) for z in self.cfg['zones']):quiet_m+=dist
            ident='R-'+hashlib.sha256(json.dumps([self.version,points,closed,time.time_ns()]).encode()).hexdigest()[:16]
            row={'route_id':ident,'kind':mode,'map_version':self.version,'destination':station['id'],'points':points,'length_m':round(length,2),'estimated_navigation_seconds':round(eta,1),'crowd_exposure_person_m':round(exposure,2),'quiet_zone_m':round(quiet_m,2),'crowd_capture_wall':crowd['wall_time'],'created_wall':time.time(),'validation':'occupancy_clearance_only','_cells':cells}
            self.cache[ident]=row;rows.append(row)
            for c in cells:
                for dx in range(-2,3):
                    for dy in range(-2,3):penalty[(c[0]+dx,c[1]+dy)]=4
        return rows
    def speed_limit(self,p,crowd=None):
        speed=.5
        for z in self.cfg['zones']:
            if self.zone(p,z):speed=min(speed,z.get('speed_limit',.5))
        if crowd and sum(math.dist(p,v['position'][:2])<3 for v in crowd.get('people',[]))>=2:speed=min(speed,.25)
        return speed
    def selected(self,ident,closed):
        r=self.cache.get(ident)
        if not r:raise ValueError('route_id_unknown_get_new_options')
        if any(z['id'] in closed and any(self.zone(p,z) for p in r['points']) for z in self.cfg['zones']):raise ValueError('route_closed_get_new_options')
        if r['validation']!='nav2_verified':raise ValueError('route_not_nav2_verified')
        return r
