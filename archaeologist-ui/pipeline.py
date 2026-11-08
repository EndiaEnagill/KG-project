"""Bounded GitHub collection and graph handoff, standard library only."""
import ast
import base64
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from quality import validate, chunk_document

TYPES = {'Repository':'repositories','Developer':'developers','Issue':'issues',
 'PullRequest':'pull_requests','Commit':'commits','Release':'releases','Comment':'comments',
 'Module':'modules','File':'files','CodeSymbol':'code_symbols','Problem':'problems',
 'Solution':'solutions','Feature':'features'}
def now(): return datetime.now(timezone.utc).isoformat()
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def write(p, obj):
    p=Path(p); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
def lines(p, rows):
    p=Path(p); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
def repository(url):
    u=urllib.parse.urlparse(url.strip())
    if u.scheme!='https' or u.netloc!='github.com': raise ValueError('请输入 https://github.com/owner/repo 格式的链接')
    parts=u.path.strip('/').removesuffix('.git').split('/')
    if len(parts)!=2 or any(not re.fullmatch(r'[\w.-]+',p) or p in ('.','..') for p in parts): raise ValueError('仓库链接格式不正确')
    return '/'.join(parts)

class Cancelled(Exception): pass
class Client:
    def __init__(self, root, token, notify, cancel):
        self.root=root; self.token=token; self.notify=notify; self.cancel=cancel
        self.pages=[]; self.failures=[]; self.blocked=False
        self.origins={};self.network_requests=0;self.cache_hits=0;self.started=time.monotonic();self.last_metric=0
    def metrics(self,force=False):
        if not force and time.monotonic()-self.last_metric<1:return
        self.last_metric=time.monotonic()
        self.notify('telemetry',{'network':self.network_requests,'cache':self.cache_hits,'elapsed':int(time.monotonic()-self.started)})
    def origin(self,obj):return self.origins.get(id(obj))
    def check(self):
        if self.cancel.is_set(): raise Cancelled()
    def get(self, endpoint):
        self.check()
        url=endpoint if endpoint.startswith('https://') else 'https://api.github.com'+endpoint
        if urllib.parse.urlparse(url).netloc!='api.github.com': raise ValueError('不支持的API主机')
        cache=self.root/'raw/api'/(digest(url)+'.json')
        if cache.exists():
            self.cache_hits+=1;self.metrics();return json.loads(cache.read_text(encoding='utf-8'))
        if self.blocked: raise RuntimeError('接口已限制访问，请稍后使用新任务重试')
        self.notify('log','读取 '+urllib.parse.urlparse(url).path)
        headers={'Accept':'application/vnd.github+json','User-Agent':'Archaeologist-Desktop'}
        if self.token: headers['Authorization']='Bearer '+self.token
        for attempt in range(3):
            self.check()
            try:
                self.network_requests+=1;self.metrics()
                with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=25) as r:
                    record={'request_url':url,'fetched_at':now(),'status':r.status,'link':r.headers.get('Link'),'data':json.load(r)}
                write(cache,record); return record
            except urllib.error.HTTPError as e:
                if e.code in (401,403,429):
                    self.blocked=True
                    raise RuntimeError(f'HTTP {e.code}：权限或限流；请检查Token并稍后重试') from None
                if e.code<500 or attempt==2: raise RuntimeError(f'HTTP {e.code}：{url}') from None
            except (urllib.error.URLError,TimeoutError) as e:
                if attempt==2: raise RuntimeError('网络连接失败：'+str(e.reason if hasattr(e,'reason') else e)) from None
            if self.cancel.wait(2**attempt): raise Cancelled()
    def listing(self, endpoint, limit=None, predicate=None):
        out=[]; url=endpoint; pages=0
        while url and (limit is None or len(out)<limit):
            r=self.get(url); data=r['data']
            if not isinstance(data,list): raise ValueError('接口未返回列表')
            for index,obj in enumerate(data):
                if predicate is None or predicate(obj):
                    out.append(obj);self.origins[id(obj)]=(r,'/data/'+str(index))
            pages+=1
            m=re.search(r'<([^>]+)>; rel="next"',r.get('link') or '')
            url=m.group(1) if m else None
            self.notify('log',f'已读取 {pages} 页，累计 {len(out)} 条')
        self.pages.append({'endpoint':endpoint,'pages':pages,'available':len(out),'kept':len(out) if limit is None else min(len(out),limit),'complete':not url and (limit is None or len(out)<=limit)})
        return out[:limit]
    def safe(self, label, fn, default=None):
        try: return fn()
        except Cancelled: raise
        except Exception as e:
            self.failures.append({'stage':label,'reason':str(e),'observed_at':now()})
            self.notify('log',label+'：'+str(e)); return default

def run(url, token, output, limits, notify, cancel, resume=None):
    repo=repository(url)
    full=all(limits.get(k) is None for k in ('issues','prs','commits','releases','comments','source_files'))
    if full: limits={k:None for k in ('issues','prs','commits','releases','comments','source_files')}
    root=Path(resume) if resume else Path(output)/(repo.replace('/','__'))/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+digest(now())[:6])
    root.mkdir(parents=True,exist_ok=True)
    api=Client(root,token,notify,cancel); nodes={}; edges={}; docs={}; evidence={}; versions=[]; changes=[]; processed_commits={}; budget_skips=set()
    state='ready'; sha=None; canonical=repo; source_state='not_run'
    write(root/'job.json',{'url':url,'limits':limits,'mode':'full' if full else 'bounded','created_at':now()})
    def track(items,label):
        total=len(items);last=0
        def update(done):
            event={'label':label,'done':done,'total':total}
            notify('progress',event)
            write(root/'state/progress.json',{**event,'observed_at':now(),'network_requests':api.network_requests,'cache_hits':api.cache_hits})
            api.metrics(True)
        update(0)
        for index,item in enumerate(items,1):
            api.check();yield item
            if time.monotonic()-last>=1 or index==total:update(index);last=time.monotonic()
    def node(kind,key,props,ev=None):
        prefix={'Repository':'repo','PullRequest':'pr','CodeSymbol':'symbol'}.get(kind,kind.lower())
        nid=key if kind=='Developer' else f'{prefix}:{canonical}'+(':'+str(key) if key else '')
        old=nodes.get(nid,{})
        merged={**old.get('properties',{}),**{k:v for k,v in props.items() if v is not None}}
        eids=list(dict.fromkeys(old.get('provenance',{}).get('evidence_ids',[])+([ev] if ev else [])))
        nodes[nid]={'id':nid,'entity_type':kind,'properties':merged,'provenance':{'repo_id':f'repo:{canonical}','observed_at':now(),'evidence_ids':eids,'schema_version':'1.0','contract_version':'desktop-4' if full else 'desktop-2'}}
        return nid
    def ev(record, pointer=''):
        key='ev:'+digest(record['request_url']+pointer)
        evidence[key]={'evidence_id':key,'source_url':record['request_url'],'raw_ref':'raw/api/'+digest(record['request_url'])+'.json','json_pointer':pointer,'observed_at':record['fetched_at']}
        return key
    def edge(a, rel, b, eid, url, props=None):
        key='rel:'+digest(a+'|'+rel+'|'+b)
        edges[key]={'relation_id':key,'source_id':a,'source_type':nodes[a]['entity_type'],'relation':rel,'target_id':b,'target_type':nodes[b]['entity_type'],'evidence':'API或AST结构记录','evidence_url':url,'evidence_ids':[eid],'method':'ast' if rel=='CONTAINS' else 'api','confidence':1.0,'observed_at':now(),'status':'asserted','properties':props or {}}
    def author(user,eid):
        if not user or not user.get('login'): return None
        return node('Developer','developer:github:'+user['login'],{k:v for k,v in {'login':user['login'],'name':user.get('name'),'url':user.get('html_url'),'account_type':user.get('type')}.items() if v is not None},eid)
    def document(nid,obj,eid,kind):
        raw=obj.get('body') or ''; did=nid+':body'
        docs[did]={'document_id':did,'source_entity_id':nid,'kind':kind,'raw_text':raw,'clean_text':raw.replace('\r\n','\n').replace('\x00','').strip(),'content_hash':digest(raw),'source_url':obj.get('html_url'),'evidence_ids':[eid],'created_at':obj.get('created_at'),'updated_at':obj.get('updated_at')}
    def comments(nid, endpoint, kind):
        for c in api.listing(endpoint,limits['comments']):
            origin=api.origin(c)
            if origin:r,pointer=origin
            else:
                if not c.get('url'):continue
                r=api.get(c['url']);pointer='/data'
            eid=ev(r,pointer); created=c.get('created_at') or c.get('submitted_at')
            if not created: continue
            cid=node('Comment',kind+':'+str(c['id']),{'body':c.get('body') or '', 'created_at':created,'updated_at':c.get('updated_at'),'url':c.get('html_url'),'comment_type':kind},eid)
            edge(nid,'HAS_COMMENT',cid,eid,c.get('html_url')); a=author(c.get('user'),eid)
            if a: edge(a,'WROTE',cid,eid,c.get('html_url'))
            document(cid,c,eid,kind)
    def commit(c,rid):
        if c['sha'] in processed_commits: return processed_commits[c['sha']]
        if limits['commits'] is not None and len(processed_commits)>=limits['commits']:
            budget_skips.add(c['sha']); return None
        r=api.get(f'/repos/{canonical}/commits/'+c['sha']); c=r['data']; eid=ev(r,'/data')
        cid=node('Commit',c['sha'],{'sha':c['sha'],'short_sha':c['sha'][:12],'message':c['commit']['message'],'authored_at':c['commit']['author']['date'],'committed_at':c['commit']['committer']['date'],'url':c['html_url'],'additions':c.get('stats',{}).get('additions'),'deletions':c.get('stats',{}).get('deletions'),'is_merge_commit':len(c.get('parents',[]))>1},eid)
        edge(rid,'HAS_COMMIT',cid,eid,c['html_url']); a=author(c.get('author'),eid)
        if a: edge(a,'AUTHORED',cid,eid,c['html_url'])
        all_files=list(c.get('files',[])); more=re.search(r'<([^>]+)>; rel="next"',r.get('link') or '')
        pages=1
        while more and (full or pages<10):
            page=api.get(more.group(1)); all_files.extend(page['data'].get('files',[]));pages+=1
            more=re.search(r'<([^>]+)>; rel="next"',page.get('link') or '')
        api.pages.append({'endpoint':r['request_url'],'complete':not bool(more),'pages':pages})
        if len(all_files)>=3000: api.pages.append({'endpoint':r['request_url'],'complete':False,'reason':'文件列表可能达到GitHub端点硬上限，需用Git补齐'})
        for f in all_files:
            fid=node('File',f['filename'],{'path':f['filename'],'name':Path(f['filename']).name,'extension':Path(f['filename']).suffix,'is_test':'test' in f['filename'].lower()},eid)
            props={'change_type':{'removed':'deleted'}.get(f['status'],f['status']),'old_path':f.get('previous_filename',f['filename']),'new_path':f['filename'],'additions':f.get('additions',0),'deletions':f.get('deletions',0)}
            edge(cid,'MODIFIES',fid,eid,c['html_url'],props); changes.append({'commit_sha':c['sha'],'path':f['filename'],'patch':f.get('patch'),'patch_available':'patch' in f})
        processed_commits[c['sha']]=cid
        return cid
    try:
        notify('stage','1/4 获取仓库与活动数据')
        r=api.get('/repos/'+repo); meta=r['data']; canonical=meta['full_name']; eid=ev(r,'/data'); base='/repos/'+canonical
        rid=node('Repository','',{'owner':meta['owner']['login'],'name':meta['name'],'full_name':canonical,'url':meta['html_url'],'default_branch':meta['default_branch'],'primary_language':meta.get('language'),'description':meta.get('description'),'collected_at':now()},eid)
        issue_list=api.listing(base+'/issues?state=all&sort=updated&direction=desc&per_page=100',limits['issues'],predicate=lambda x:'pull_request' not in x)
        for item in track(issue_list,'Issue与评论'):
            api.check()
            if 'pull_request' in item: continue
            if limits['issues'] is not None and sum(n['entity_type']=='Issue' for n in nodes.values())>=limits['issues']: break
            origin=api.origin(item)
            if origin:r,pointer=origin;i=item
            else:r=api.get(base+'/issues/'+str(item['number']));pointer='/data';i=r['data']
            eid=ev(r,pointer)
            nid=node('Issue',i['number'],{k:i.get(k) for k in ['number','title','body','state','state_reason','created_at','updated_at','closed_at']}|{'labels':[x['name'] for x in i['labels']],'url':i['html_url']},eid)
            edge(rid,'HAS_ISSUE',nid,eid,i['html_url']); a=author(i.get('user'),eid)
            if a: edge(a,'CREATED',nid,eid,i['html_url'])
            document(nid,i,eid,'issue'); api.safe('Issue评论',lambda:comments(nid,base+f'/issues/{i["number"]}/comments?per_page=100','issue_comment'))
        pr_list=api.listing(base+'/pulls?state=all&sort=updated&direction=desc&per_page=100',limits['prs'])
        for p in track(pr_list,'PR、审查与关联提交'):
            r=api.get(base+'/pulls/'+str(p['number'])); p=r['data']; eid=ev(r,'/data')
            props={k:p.get(k) for k in ['number','title','body','state','merged','draft','created_at','updated_at','merged_at','closed_at','merge_commit_sha']}
            props.update(base_branch=p['base']['ref'],head_branch=p['head']['ref'],url=p['html_url'])
            nid=node('PullRequest',p['number'],props,eid); edge(rid,'HAS_PR',nid,eid,p['html_url']); a=author(p.get('user'),eid)
            if a: edge(a,'OPENED',nid,eid,p['html_url'])
            document(nid,p,eid,'pr')
            for suffix,kind in [(f'/issues/{p["number"]}/comments','issue_comment'),(f'/pulls/{p["number"]}/comments','review_comment'),(f'/pulls/{p["number"]}/reviews','review')]:
                api.safe('PR讨论',lambda s=suffix,k=kind:comments(nid,base+s+'?per_page=100',k))
            pr_commits=api.listing(base+f'/pulls/{p["number"]}/commits?per_page=100',limits['commits'])
            if full and p.get('commits',0)>len(pr_commits):api.pages.append({'endpoint':f'PR #{p["number"]} commits','complete':False,'reason':'PR提交接口数量不足，可能达到平台上限'})
            for c in pr_commits:
                cid=api.safe('PR提交',lambda c=c:commit(c,rid))
                if cid: edge(nid,'CONTAINS_COMMIT',cid,eid,p['html_url'])
            if p.get('merged') and p.get('merge_commit_sha'):
                cid=api.safe('合并结果',lambda:commit({'sha':p['merge_commit_sha']},rid))
                if cid: edge(nid,'MERGED_AS',cid,eid,p['html_url'])
        notify('stage','2/4 获取提交和版本')
        default_commits=api.listing(base+'/commits?per_page=100',limits['commits'])
        for c in track(default_commits,'默认分支提交'): api.safe('提交详情',lambda c=c:commit(c,rid))
        history_seen={c['sha'] for c in default_commits} if full else set()
        for rel in track(api.listing(base+'/releases?per_page=100',limits['releases']),'发布记录'):
            origin=api.origin(rel)
            if origin:r,pointer=origin
            else:r=api.get(base+'/releases/'+str(rel['id']));pointer='/data';rel=r['data']
            eid=ev(r,pointer)
            nid=node('Release',rel['tag_name'],{k:rel.get(k) for k in ['tag_name','name','body','draft','prerelease','created_at','published_at']}|{'url':rel['html_url']},eid)
            edge(rid,'HAS_RELEASE',nid,eid,rel['html_url']); document(nid,rel,eid,'release')
        if full:
            branches=api.listing(base+'/branches?per_page=100')
            tags=api.listing(base+'/tags?per_page=100')
            write(root/'processed/refs.json',{'branches':branches,'tags':tags,'observed_at':now()})
            for ref in track(branches+tags,'分支/tag增量历史'):
                ref_sha=ref['commit']['sha']
                def collect_ref():
                    pending=[ref_sha]
                    while pending:
                        current=pending.pop()
                        if current in history_seen:continue
                        record=api.get(base+'/commits/'+current)
                        cid=commit(record['data'],rid)
                        if cid:
                            history_seen.add(current)
                            pending.extend(p['sha'] for p in record['data'].get('parents',[]) if p['sha'] not in history_seen)
                api.safe('分支/tag历史 '+ref['name'],collect_ref)
        notify('stage','3/4 解析冻结版本的Python源码')
        sha=api.get(base+'/commits/'+urllib.parse.quote(meta['default_branch'],safe=''))['data']['sha']
        tree_record=api.get(base+'/git/trees/'+sha+'?recursive=1'); tree=tree_record['data']; source_state='completed'
        if tree.get('truncated') and full:
            entries=[];pending=[(sha,'')]
            while pending:
                tree_sha,prefix=pending.pop();part=api.get(base+'/git/trees/'+tree_sha)['data']
                if part.get('truncated'):api.pages.append({'endpoint':'tree '+tree_sha,'complete':False})
                for item in part['tree']:
                    entry={**item,'path':prefix+item['path']};entries.append(entry)
                    if item['type']=='tree':pending.append((item['sha'],entry['path']+'/'))
            tree['tree']=entries
        elif tree.get('truncated'): api.pages.append({'endpoint':'git/tree','complete':False})
        write(root/'processed/source_tree.json',{'revision_sha':sha,'entries':tree['tree']})
        if full:
            for f in track([x for x in tree['tree'] if x['type']=='blob'],'源码文件归档'):
                def archive_blob():
                    record=api.get(base+'/git/blobs/'+f['sha']);eid=ev(record,'/data')
                    node('File',f['path'],{'path':f['path'],'name':Path(f['path']).name,'extension':Path(f['path']).suffix,'is_test':'test' in f['path'].lower(),'status':'active'},eid)
                api.safe('源码归档 '+f['path'],archive_blob)
        sources=[x for x in tree['tree'] if x['type']=='blob' and x['path'].endswith('.py') and (full or (0<x.get('size',1)<500000 and not any(p in x['path'].split('/') for p in ('.venv','venv','build','dist','__pycache__'))))]
        package=meta['name'].replace('-','_')+'/'
        sources.sort(key=lambda f:(not f['path'].startswith(package), 'test' in f['path'].lower(),f['path'].endswith('__init__.py'),f['path']))
        if limits['source_files'] is not None and len(sources)>limits['source_files']: source_state='partial'
        if source_state=='partial': api.pages.append({'endpoint':'source_files','complete':False,'available':len(sources),'kept':limits['source_files']})
        docfiles=[x for x in tree['tree'] if x['type']=='blob' and x['path'].lower().endswith(('.md','.rst','.txt')) and (full or (len(x['path'].split('/'))==1 and x.get('size',0)<200000))][:(None if full else 5)]
        for f in track(docfiles,'文档提取'):
            def source_document():
                record=api.get(base+'/git/blobs/'+f['sha']); eid=ev(record,'/data'); text=base64.b64decode(record['data']['content']).decode('utf-8-sig')
                did=f'doc:{canonical}:{f["path"]}:{sha}'
                docs[did]={'document_id':did,'source_entity_id':rid,'kind':'repository_document','raw_text':text,'clean_text':text.replace('\r\n','\n').replace('\x00','').strip(),'content_hash':digest(text),'source_url':meta['html_url']+'/blob/'+sha+'/'+f['path'],'evidence_ids':[eid]}
            api.safe('项目文档 '+f['path'],source_document)
        for f in track(sources[:limits['source_files']],'Python代码解析'):
            api.check()
            def load_source():
                record=api.get(base+'/git/blobs/'+f['sha']);return record,base64.b64decode(record['data']['content']).decode('utf-8-sig')
            loaded=api.safe('源码读取 '+f['path'],load_source)
            if loaded is None:source_state='partial';continue
            r,text=loaded;eid=ev(r,'/data')
            source_ref='raw/source/'+f['sha']+'.py'; (root/'raw/source').mkdir(parents=True,exist_ok=True); (root/source_ref).write_text(text,encoding='utf-8')
            fid=node('File',f['path'],{'path':f['path'],'name':Path(f['path']).name,'extension':'.py','language':'Python','is_test':'test' in f['path'].lower(),'status':'active'},eid)
            def parse():
                module=ast.parse(text)
                def walk(parent,prefix='',in_class=False):
                    for child in ast.iter_child_nodes(parent):
                        if isinstance(child,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                            q=prefix+child.name; kind='class' if isinstance(child,ast.ClassDef) else 'method' if in_class else 'function'
                            props={'name':child.name,'qualified_name':q,'kind':kind,'signature':child.name+'('+ast.unparse(child.args)+')' if hasattr(child,'args') else child.name,'docstring':ast.get_docstring(child),'start_line':child.lineno,'end_line':child.end_lineno,'status':'active'}
                            sid=node('CodeSymbol',f['path']+':'+q,props,eid); edge(fid,'CONTAINS',sid,eid,meta['html_url']+'/blob/'+sha+'/'+f['path'],{'revision_sha':sha})
                            versions.append({'symbol_id':sid,'revision_sha':sha,'path':f['path'],'source_ref':source_ref,**props}); walk(child,q+'.',kind=='class')
                        else: walk(child,prefix,in_class)
                walk(module)
            api.safe('AST '+f['path'],parse)
        # References require real Issue endpoints; they are not resolutions.
        for n in list(nodes.values()):
            if n['entity_type']!='PullRequest': continue
            for number in set(re.findall(r'(?<![\w/])#(\d+)\b',n['properties'].get('body') or '')):
                target=f'issue:{canonical}:{number}'
                if target in nodes: edge(n['id'],'REFERENCES',target,n['provenance']['evidence_ids'][0],n['properties']['url'])
    except Cancelled: state='cancelled'; notify('log','已取消，正在保留已采数据')
    except Exception as e: state='partial'; api.failures.append({'stage':'pipeline','reason':str(e)}); notify('log',str(e))
    notify('stage','4/4 校验并保存交接包')
    raw_count=len(nodes); nodes,edges,rejected=validate(nodes,edges,evidence)
    dangling=[e for e in edges.values() if e['source_id'] not in nodes or e['target_id'] not in nodes]
    if budget_skips: api.pages.append({'endpoint':'commit_budget','complete':False,'skipped_unique_shas':len(budget_skips)})
    if rejected and state!='cancelled':state='partial'
    if api.failures or any(not p['complete'] for p in api.pages): state='partial' if state!='cancelled' else state
    for kind,filename in TYPES.items(): lines(root/'processed/nodes'/(filename+'.jsonl'),[n for n in nodes.values() if n['entity_type']==kind])
    lines(root/'processed/relations.jsonl',edges.values()); lines(root/'processed/documents.jsonl',docs.values()); lines(root/'processed/evidence.jsonl',evidence.values()); lines(root/'processed/code_versions.jsonl',versions); lines(root/'processed/file_changes.jsonl',changes)
    semantic=[{'task_id':'semantic:'+digest(d['document_id']),'document_id':d['document_id'],'source_entity_id':d['source_entity_id'],'text':d['clean_text'],'evidence_ids':d['evidence_ids']} for d in docs.values()]
    lines(root/'semantic/inputs.jsonl',semantic)
    lines(root/'semantic/candidates.jsonl',[]);lines(root/'review_queue/items.jsonl',rejected)
    chunks=[c for d in docs.values() for c in chunk_document(d)];lines(root/'processed/chunks.jsonl',chunks)
    quality={'state':state,'node_counts':{t:sum(n['entity_type']==t for n in nodes.values()) for t in TYPES},'relation_counts':dict(Counter(e['relation'] for e in edges.values())),'dangling_edges':len(dangling),'rejected_records':len(rejected),'input_nodes':raw_count,'documents':len(docs),'chunks':len(chunks),'errors':api.failures,'pagination':api.pages,'validation_scope':'13类必填字段与基础类型、日期、已实现关系Domain/Range和证据引用；非全部JSON Schema约束'}
    write(root/'reports/quality.json',quality)
    quality['performance']={'network_requests':api.network_requests,'cache_hits':api.cache_hits,'elapsed_seconds':int(time.monotonic()-api.started),'comment_evidence':'list_page_json_pointer','ref_history':'deduplicated_parent_walk'}
    write(root/'reports/quality.json',quality)
    manifest={'repo_url':url,'canonical_repo':canonical,'run_id':root.name,'schema_version':'1.0','contract_version':'desktop-2','created_at':now(),'reference_sha':sha,'scope':limits,'state':state,'stages':{'source_parsing':source_state,'semantic_extraction':'not_run','module_feature_configuration':'not_configured','historical_symbol_mapping':'not_run','release_membership':'not_run'},'limitations':['仅有界采集','不自动生成RESOLVES或FIRST_RELEASED_IN','无Git历史AST映射','未配置Module/Feature；语义类型文件为空','仅采集根目录前5份Markdown/RST文档','完整Schema校验待实现']}
    if full:
        manifest.update(mode='full',contract_version='desktop-4',collection_scope='API可访问的全部Issue/PR/评论/Release；默认分支、分支和tag可达提交及PR原提交；默认分支冻结快照的全部blob归档、Python解析和Markdown/RST/TXT文本')
        manifest['limitations']=['受Token权限、API硬上限和网络影响，缺失会记录','不包含Discussion、Actions、Release附件、Wiki、LFS外部对象、子模块内容及不可达已删除提交','不保存所有历史版本的全部源码快照','不自动生成RESOLVES或FIRST_RELEASED_IN','未运行语义抽取及历史函数映射','完整Schema校验待实现']
    manifest['checksums']={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'processed').rglob('*.jsonl')}
    write(root/'manifest.json',manifest);write(root/'state/progress.json',{'state':state,'observed_at':now(),'network_requests':api.network_requests,'cache_hits':api.cache_hits}); notify('done',str(root)); return root

def export_zip(root):
    root=Path(root); target=root.with_suffix('.zip')
    temporary=target.with_suffix('.zip.part')
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
            for p in root.rglob('*'):
                if p.is_file(): z.write(p,p.relative_to(root))
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target
