"""Project-local cache of official USD assets with preserved relative references."""
import argparse,concurrent.futures, hashlib,json,sys,time,urllib.parse,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent/asset-inspection'))
from pxr import Sdf
ALLOWED={'omniverse-content-production.s3-us-west-2.amazonaws.com','omniverse-content-staging.s3.us-west-2.amazonaws.com'}
CACHE=ROOT/'assets/people-v2';CACHE.mkdir(parents=True,exist_ok=True)
def target(url):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.netloc not in ALLOWED:raise ValueError('asset_origin_not_allowed')
    path=(CACHE/parsed.netloc/urllib.parse.unquote(parsed.path).lstrip('/')).resolve()
    if CACHE.resolve() not in path.parents:raise ValueError('asset_cache_path_escape')
    return path
def download(url):
    p=target(url);p.parent.mkdir(parents=True,exist_ok=True)
    if not p.exists():
        with urllib.request.urlopen(url,timeout=30) as response:
            data=response.read(256*1024*1024+1)
        if len(data)>256*1024*1024:raise ValueError('asset_size_limit')
        temp=p.with_suffix(p.suffix+'.tmp');temp.write_bytes(data);temp.replace(p)
    dependencies=[]
    if p.suffix in ['.usd','.usda','.usdc']:
        layer=Sdf.Layer.FindOrOpen(str(p))
        if layer is None:raise ValueError('invalid_usd_asset:'+url)
        # Sdf's asset dependencies omit USD payload/reference arcs; both are required.
        references=set(layer.GetExternalAssetDependencies())|set(layer.GetExternalReferences())
        dependencies=[urllib.parse.urljoin(url,x) for x in references if not x.startswith('anon:')]
    return {'url':url,'local':str(p.relative_to(ROOT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size},dependencies
def main():
    parser=argparse.ArgumentParser();parser.add_argument('roots',nargs='*');parser.add_argument('--workers',type=int,default=16);parser.add_argument('--index-only',action='store_true')
    args=parser.parse_args()
    if not 1<=args.workers<=24:raise ValueError('worker_limit')
    roots=args.roots or ['https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/6.0/Isaac/People/MotionLibrary/HumanMotionLibrary.usd']
    if args.index_only:
        urls=set(roots)
        for url in roots:
            _,dependencies=download(url);urls.update(dependencies)
        rows=[{'url':url,'local':str(target(url).relative_to(ROOT))} for url in sorted(urls)]
        (CACHE/'download-index.json').write_text(json.dumps(rows,indent=2));print(json.dumps({'index_files':len(rows)}));return
    pending=set(roots);seen=set();rows=[];errors=[]
    def save(complete=False):
        (CACHE/'manifest.json').write_text(json.dumps({'roots':roots,'files':rows,'errors':errors,'pending':len(pending),'complete':complete,'wall_time':time.time()},indent=2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        while pending:
            batch=list(pending-seen);pending.clear()
            if not batch:break
            if len(seen)+len(batch)>1200:raise RuntimeError('asset_count_limit')
            seen.update(batch);futures={pool.submit(download,url):url for url in batch}
            for future in concurrent.futures.as_completed(futures):
                try:
                    row,deps=future.result();rows.append(row);pending.update(set(deps)-seen)
                except Exception as e:
                    errors.append({'url':futures[future],'error':str(e)})
                    print(json.dumps(errors[-1]),flush=True)
                if (len(rows)+len(errors))%8==0:
                    print(json.dumps({'downloaded':len(rows),'pending':len(pending),'errors':len(errors),'batch_remaining':sum(not f.done() for f in futures)}),flush=True);save()
            print(json.dumps({'downloaded':len(rows),'pending':len(pending),'errors':len(errors)}),flush=True)
            save()
    if errors:raise RuntimeError('asset_cache_incomplete')
    save(complete=True)
    print('ASSET_CACHE_READY',str(target(roots[0])),flush=True)
if __name__=='__main__':main()
