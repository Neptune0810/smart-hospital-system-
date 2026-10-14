"""Validate actual recorded messages against the pinned official MHS schema."""
import collections,hashlib,json,time
from agnes_mhs.schema import REQUEST_PARAMS,RESULTS,NOTIFICATIONS,NERVE,SCHEMA_PATH,errors
from .manage import ROOT

def main():
    captures=sorted((ROOT/'runs/mhs-v2/protocol').iterdir())
    checked=collections.Counter();unknown=collections.Counter();failures=[];binary_failures=[];binaries=0;requests={};partial=0
    for folder in captures:
        path=folder/'wire.jsonl'
        if not path.exists():continue
        for line_number,line in enumerate(path.read_text().splitlines(),1):
            try:row=json.loads(line)
            except json.JSONDecodeError:partial+=1;continue
            if 'binary' in row:
                b=row['binary'];p=folder/b['file'];binaries+=1
                if not p.is_file() or len(p.read_bytes())!=b['size'] or hashlib.sha256(p.read_bytes()).hexdigest()!=b['sha256']:binary_failures.append({'capture':folder.name,'line':line_number,'file':b['file']})
                continue
            message=row['message'];method=message.get('method');key=(folder.name,row['channel'],message.get('id'))
            definition=None;value=None
            if method in REQUEST_PARAMS:
                requests[key]=method;definition=REQUEST_PARAMS[method];value=message.get('params',{})
            elif method in NOTIFICATIONS:definition=NOTIFICATIONS[method];value=message.get('params',{})
            elif 'result' in message and requests.get(key) in RESULTS:definition=RESULTS[requests[key]];value=message['result']
            elif message.get('type') in NERVE:definition=NERVE[message['type']];value=message
            else:unknown[(row['channel'],method or message.get('type') or 'response_without_known_method')]+=1
            if definition:
                checked[definition]+=1
                found=errors(definition,value)
                if found:failures.append({'capture':folder.name,'line':line_number,'direction':row['direction'],'definition':definition,'errors':found})
    report={'wall_time':time.time(),'official_schema_sha256':hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),'captures':[p.name for p in captures],
            'validated_messages':dict(checked),'schema_failures':failures,'unvalidated_messages':{str(k):v for k,v in unknown.items()},'partial_json_lines':partial,
            'binary_integrity_count':binaries,'binary_integrity_failures':binary_failures,'binary_frame_schema_validation':None,
            'scope':'Recorded JSON schema and binary file integrity only; not movement, delivery or official conformance acceptance. Unknown message types and incomplete lines remain unvalidated.'}
    directory=ROOT/'runs/mhs-v2/acceptance'/('wire-schema-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'directory':str(directory),'validated_count':sum(checked.values()),'schema_failures':len(failures),'unvalidated_count':sum(unknown.values()),'partial_lines':partial,'binary_integrity_failures':len(binary_failures)}))

if __name__=='__main__':main()
