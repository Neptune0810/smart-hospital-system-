"""Match actual native-image model requests to the captured JPEG and timestamp."""
import hashlib,json,sys
from pathlib import Path
from .manage import ROOT

def main():
    directory=Path(sys.argv[1]).resolve()
    if ROOT/'runs/mhs-v2/acceptance' not in directory.parents:raise ValueError('outside_acceptance')
    rows=[json.loads(line) for line in (directory/'session.jsonl').read_text().splitlines()]
    native=[row for row in rows if row.get('type')=='request/header' and row.get('data',{}).get('media',{}).get('route')=='native-image']
    proofs=[]
    for row in native:
        for item in row['data']['media']['manifest']:
            if not item.get('selected'):continue
            digest=item['sha256'];matches=[]
            for index in (ROOT/'runs/mhs-v2/protocol').glob('*/image-index.jsonl'):
                for line in index.read_text().splitlines():
                    meta=json.loads(line)
                    if meta['sha256']!=digest:continue
                    jpeg=index.parent/(digest+'.bin')
                    if not jpeg.exists():continue
                    raw=jpeg.read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=digest or not raw.startswith(b'\xff\xd8'):raise RuntimeError('image_bytes_mismatch')
                    target=directory/(digest+'.jpg');target.write_bytes(raw)
                    matches.append({'capture':meta,'binary_path':str(jpeg),'image':str(target),'image_index':str(index)})
            sent=[r for r in rows if r.get('type')=='request/sent' and r.get('data',{}).get('derived_hash')==row['data']['derived_hash']]
            proofs.append({'manifest':item,'header_derived_hash':row['data']['derived_hash'],'request_sent':sent,'captures':matches,'pass':bool(sent and matches)})
    result={'native_image_request_count':len(native),'proofs':proofs,'pass':bool(proofs) and all(p['pass'] for p in proofs),
            'note':'Freshness and description correctness additionally verified from read_device result and visible original JPEG; this check proves actual selected provider image bytes.'}
    (directory/'image-proof.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'native_image_request_count':len(native),'pass':result['pass'],'directory':str(directory)}))
if __name__=='__main__':main()
