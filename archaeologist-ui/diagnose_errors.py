"""Read-only diagnosis; does not overwrite collection outputs."""
import json,os,re,subprocess,sys,urllib.request,urllib.error
from pathlib import Path
root=Path(sys.argv[1]);report=json.loads((root/'reports/quality.json').read_text(encoding='utf-8'))
shas=[e['reason'].rsplit('/',1)[-1] for e in report['errors']]
command=['rg','-l','-F']
for sha in shas:command+=['-e',sha]
command+=[str(root/'raw/api')]
found=subprocess.run(command,capture_output=True,text=True,encoding='utf-8').stdout.splitlines()
mapping={}
for filename in found:
    r=json.loads(Path(filename).read_text(encoding='utf-8'));data=r['data']
    for p in data if isinstance(data,list) else [data]:
        if isinstance(p,dict) and p.get('merge_commit_sha') in shas:
            mapping[p['merge_commit_sha']]={'pr':p.get('number'),'title':p.get('title'),'merged':p.get('merged'),'merged_at':p.get('merged_at'),'closed_at':p.get('closed_at'),'url':p.get('html_url'),'raw_ref':str(Path(filename).relative_to(root))}
rows=[]
for error,sha in zip(report['errors'],shas):
    url=error['reason'].split('https://',1)[-1];url='https://'+url
    headers={'Accept':'application/vnd.github+json','User-Agent':'repo-error-diagnosis'}
    if os.environ.get('GITHUB_TOKEN'):headers['Authorization']='Bearer '+os.environ['GITHUB_TOKEN']
    row={'sha':sha,'original_error':error,'pr':mapping.get(sha)}
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20) as response:
            data=json.load(response);row.update(status=response.status,message='Commit accessible on recheck',returned_sha=data.get('sha'))
    except urllib.error.HTTPError as e:
        try:body=json.loads(e.read().decode('utf-8'))
        except Exception:body={}
        row.update(status=e.code,message=body.get('message'),details=body.get('errors'),rate_remaining=e.headers.get('X-RateLimit-Remaining'))
    except Exception as e:row.update(recheck_error=str(e))
    rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
(root/'reports/error_diagnosis.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
