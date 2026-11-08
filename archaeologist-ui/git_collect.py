"""Supplement an API handoff with local Git facts; never mutate the source run."""
import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from pipeline import TYPES, Cancelled, digest, lines, now, repository, write
from quality import validate


def rows(path):
    if not path.exists(): return []
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]


class Git:
    def __init__(self, path, notify, cancel):
        self.path, self.notify, self.cancel = Path(path), notify, cancel
        self.exe = shutil.which('git')
        if not self.exe: raise RuntimeError('未找到 Git，请安装 Git for Windows 后重新打开工作台。')

    def run(self, *args, input_data=None):
        if self.cancel.is_set(): raise Cancelled('Git补采已取消；原数据未修改，可重新补采')
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_OPTIONAL_LOCKS='0')
        cmd = [self.exe, '-c', 'core.quotepath=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'gc.auto=0', '-c', 'http.version=HTTP/1.1',
               '-c', 'http.lowSpeedLimit=1', '-c', 'http.lowSpeedTime=90',
               '--git-dir='+str(self.path), *args]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result = queue.Queue()
        def communicate(): result.put(proc.communicate(input_data))
        threading.Thread(target=communicate, daemon=True).start()
        started = time.monotonic()
        while True:
            try: out, err = result.get(timeout=.2); break
            except queue.Empty:
                if self.cancel.is_set():
                    proc.kill(); proc.wait()
                    raise Cancelled('Git补采已取消；原数据未修改，可重新补采')
                if time.monotonic()-started >= 5:
                    self.notify('log', 'Git '+args[0]+' 仍在运行（下载期间暂无可计算的百分比）')
                    started = time.monotonic()
        if proc.returncode: raise RuntimeError('Git '+args[0]+' 失败：'+err.decode('utf-8', 'replace')[-1500:])
        return out


def parse_diff(data):
    """Parse --raw --numstat -z, including tabs/newlines in filenames and renames."""
    fields = data.decode('utf-8', 'surrogateescape').split('\0')
    changes = []; stats = {}; i = 0
    while i < len(fields):
        field = fields[i]; i += 1
        if not field: continue
        if field.startswith(':'):
            meta = field.split(); status = meta[-1]
            old = fields[i]; i += 1
            new = old
            if status[0] in 'RC': new = fields[i]; i += 1
            changes.append({'status':status, 'old_path':old, 'path':new,
                            'old_blob':meta[2], 'new_blob':meta[3]})
        else:
            added, deleted, path = field.split('\t', 2)
            if not path:
                old, path = fields[i:i+2]; i += 2
            stats[path] = {'additions':None if added=='-' else int(added),
                           'deletions':None if deleted=='-' else int(deleted), 'binary':added=='-'}
    for change in changes: change.update(stats.get(change['path'], {}))
    return changes


def supplement(source, cache_root=None, notify=lambda *a: None, cancel=None, offline=False):
    source = Path(source).resolve(); cancel = cancel or threading.Event()
    manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    repo = repository(manifest['repo_url'])
    if repo.casefold() != manifest['canonical_repo'].casefold(): raise ValueError('仓库名称与链接不一致')
    repo = manifest['canonical_repo']
    cache_root = Path(cache_root or Path(__file__).parent/'cache/git').resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root/(repo.replace('/', '__')+'.git')
    git = Git(cache, notify, cancel)
    lock = cache.with_suffix('.lock')
    try: lockfile = lock.open('x')
    except FileExistsError: raise RuntimeError('该仓库正在补采，或上次异常退出留下锁文件：'+str(lock)) from None
    target = None
    started = time.monotonic()
    try:
        with lockfile: lockfile.write(str(os.getpid()))
        notify('stage', 'Git 1/4 下载或更新仓库缓存')
        if not cache.exists():
            if offline: raise ValueError('没有本地仓库缓存，无法离线补采')
            git.run('init', '--bare', str(cache))
        if git.run('rev-parse', '--is-shallow-repository').strip() != b'false':
            raise ValueError('缓存为浅克隆，不能声明完整历史，请使用新的缓存目录')
        if not offline:
            for attempt in range(3):
                try:
                    git.run('fetch', '--progress', '--prune', '--no-recurse-submodules', 'https://github.com/'+repo+'.git',
                            '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*')
                    break
                except RuntimeError:
                    if attempt==2: raise
                    notify('log','Git下载连接失败，准备重试（'+str(attempt+1)+'/2）')
                    if cancel.wait(2): raise Cancelled('Git补采已取消')
        notify('stage', 'Git 2/4 冻结分支和标签、读取提交')
        refs = []
        for line in git.run('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags').decode().splitlines():
            name, obj = line.split(' ', 1)
            try: commit = git.run('rev-parse', '--verify', name+'^{commit}').decode().strip()
            except RuntimeError: commit = None
            refs.append({'name':name, 'object_sha':obj, 'commit_sha':commit})
        nodes = {n['id']:n for name in TYPES.values() for n in rows(source/'processed/nodes'/(name+'.jsonl'))}
        edges = {e['relation_id']:e for e in rows(source/'processed/relations.jsonl')}
        evidence = {e['evidence_id']:e for e in rows(source/'processed/evidence.jsonl')}
        # Older desktop versions omitted merge_commit_sha from PR nodes, but archived it in API evidence.
        for n in nodes.values():
            if n['entity_type']!='PullRequest' or 'merge_commit_sha' in n['properties']: continue
            for eid in n.get('provenance',{}).get('evidence_ids',[]):
                record = evidence.get(eid,{})
                raw = (source/record.get('raw_ref','')).resolve()
                if not raw.is_relative_to(source) or not raw.is_file(): continue
                try:
                    payload = json.loads(raw.read_text(encoding='utf-8'))
                    for part in record.get('json_pointer','/data').strip('/').split('/'):
                        part = part.replace('~1','/').replace('~0','~')
                        payload = payload[int(part)] if isinstance(payload,list) else payload[part]
                    if isinstance(payload,dict) and 'merge_commit_sha' in payload:
                        n['properties']['merge_commit_sha']=payload['merge_commit_sha']; break
                except (ValueError,KeyError,TypeError): continue
        rid = 'repo:'+repo
        if rid not in nodes: raise ValueError('数据包缺少仓库实体')
        available = {r['commit_sha'] for r in refs if r['commit_sha']}
        checks = []
        # Probe known API commit SHAs too: PR commits may not be reachable from branches/tags.
        wanted = {n['properties']['sha'] for n in nodes.values() if n['entity_type']=='Commit'}
        for n in nodes.values():
            if n['entity_type']=='PullRequest' and n['properties'].get('merge_commit_sha'):
                wanted.add(n['properties']['merge_commit_sha'])
        reference = manifest.get('reference_sha')
        if reference: wanted.add(reference)
        wanted = sorted(sha for sha in wanted if re.fullmatch('[0-9a-f]{40}',sha))
        if wanted:
            probe = git.run('cat-file','--batch-check=%(objectname) %(objecttype)',
                            input_data=('\n'.join(wanted)+'\n').encode()).decode().splitlines()
            for sha, result in zip(wanted,probe):
                found = result.endswith(' commit')
                if found: available.add(sha)
                checks.append({'sha':sha,'found':found,**({} if found else {'reason':'not_in_local_objects'})})
        commits = []
        if available:
            log = git.run('log', '--topo-order', '--reverse', '--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B%x00',
                          '--stdin', input_data=('\n'.join(sorted(available))+'\n').encode())
            parts = log.decode('utf-8', 'replace').split('\0')
            for i in range(0, len(parts)-1, 9):
                sha, parents, an, ae, at, cn, ce, ct, message = parts[i:i+9]
                commits.append({'sha':sha.strip(), 'parents':parents.split(), 'author_name':an, 'author_email':ae,
                                'authored_at':at, 'committer_name':cn, 'committer_email':ce,
                                'committed_at':ct, 'message':message.rstrip('\n')})
        target = source.with_name(source.name+'-git-'+uuid.uuid4().hex[:6])
        notify('stage', 'Git 3/4 复制原数据并解析文件变化')
        shutil.copytree(source, target)
        # A cancelled/failed target must never look like a completed handoff.
        pending = dict(manifest, state='partial', run_id=target.name)
        pending['stages'] = dict(manifest.get('stages',{}), local_git='running')
        write(target/'manifest.json', pending)
        write(target/'raw/git/refs.json', {'observed_at':now(), 'refs':refs, 'offline':offline})
        lines(target/'raw/git/commits.jsonl', commits)
        def ev(raw, url, suffix):
            eid = 'ev:git:'+digest(repo+'|'+suffix)
            evidence[eid] = {'evidence_id':eid,'source_url':url,'raw_ref':raw,'observed_at':now(),'method':'git'}
            return eid
        def node(kind, key, props, eid):
            nid = kind.lower()+':'+repo+':'+key
            old = nodes.get(nid, {})
            provenance = dict(old.get('provenance', {}))
            provenance.update(repo_id=rid, observed_at=now(), schema_version='1.0', contract_version='desktop-5',
                              evidence_ids=list(dict.fromkeys(provenance.get('evidence_ids',[])+[eid])))
            nodes[nid] = {'id':nid,'entity_type':kind,'properties':{**old.get('properties',{}),**props},'provenance':provenance}
            return nid
        def edge(a, rel, b, eid, url, props=None):
            context = '|'+props['commit_sha'] if rel=='RENAMED_TO' else ''
            key = 'rel:'+digest(a+'|'+rel+'|'+b+context); old = edges.get(key,{})
            edges[key] = {'relation_id':key,'source_id':a,'source_type':nodes[a]['entity_type'],
                          'relation':rel,'target_id':b,'target_type':nodes[b]['entity_type'],
                          'method':'git','status':'asserted','confidence':1.0,'observed_at':now(),
                          'evidence':'本地Git对象与版本比较','evidence_url':url,
                          'evidence_ids':list(dict.fromkeys(old.get('evidence_ids',[])+[eid])),
                          'properties':{**old.get('properties',{}),**(props or {})}}
        changes = []; failures = []; commit_ids = {}
        for c in commits:
            sha = c['sha']; url = 'https://github.com/'+repo+'/commit/'+sha
            eid = ev('raw/git/commits.jsonl', url, 'commit:'+sha)
            commit_ids[sha] = node('Commit', sha, {**c,'short_sha':sha[:12], 'url':url,'is_merge_commit':len(c['parents'])>1}, eid)
        for index, c in enumerate(commits, 1):
            sha = c['sha']; cid = commit_ids[sha]; url = nodes[cid]['properties']['url']
            eid = ev('raw/git/commits.jsonl', url, 'commit:'+sha)
            edge(rid, 'HAS_COMMIT', cid, eid, url)
            for parent in c['parents']:
                if parent in commit_ids: edge(commit_ids[parent], 'PARENT_OF', cid, eid, url)
            try:
                args = ['diff-tree','--no-commit-id','--no-abbrev','-r','-M','--raw','--numstat','-z','--no-ext-diff','--no-textconv']
                args += [c['parents'][0], sha] if c['parents'] else ['--root',sha]
                parsed = parse_diff(git.run(*args, '--'))
                raw = 'raw/git/diffs/'+sha+'.json'
                write(target/raw, {'commit_sha':sha,'base_sha':c['parents'][0] if c['parents'] else None,
                                  'comparison':'first_parent' if c['parents'] else 'empty_tree','changes':parsed})
                did = ev(raw, url, 'diff:'+sha)
                for change in parsed:
                    path = change['path']; status = change['status'][0]
                    kind = {'A':'added','D':'deleted','M':'modified','R':'renamed','C':'copied','T':'type_changed'}.get(status,status)
                    fid = node('File', path, {'path':path,'name':path.rsplit('/',1)[-1],'extension':Path(path).suffix,'is_test':'test' in path.lower()},did)
                    props = {**change,'change_type':kind,'comparison_base':c['parents'][0] if c['parents'] else None}
                    edge(cid,'MODIFIES',fid,did,url,props)
                    if status=='R':
                        oldpath = change['old_path']
                        oldid = node('File',oldpath,{'path':oldpath,'name':oldpath.rsplit('/',1)[-1]},did)
                        edge(oldid,'RENAMED_TO',fid,did,url,{'commit_sha':sha,'detection':'git_similarity'})
                    changes.append({'commit_sha':sha,**props,'method':'git','evidence_ids':[did],
                                    'patch_available':False,'patch_note':'完整补丁可从共享Git缓存读取；本文件保存差异元数据'})
            except Cancelled: raise
            except Exception as error: failures.append({'stage':'git_diff','sha':sha,'reason':str(error)})
            if index==len(commits) or index%10==0:
                notify('progress',{'label':'Git提交与文件变化','done':index,'total':len(commits)})
        for n in list(nodes.values()):
            if n['entity_type']=='PullRequest' and n['properties'].get('merged'):
                sha = n['properties'].get('merge_commit_sha')
                if sha in commit_ids:
                    eid = 'ev:git:'+digest(repo+'|commit:'+sha)
                    edge(n['id'],'MERGED_AS',commit_ids[sha],eid,nodes[commit_ids[sha]]['properties']['url'])
        notify('stage', 'Git 4/4 合并校验并保存新交接包')
        nodes, edges, rejected = validate(nodes,edges,evidence)
        for kind, name in TYPES.items(): lines(target/'processed/nodes'/(name+'.jsonl'),(n for n in nodes.values() if n['entity_type']==kind))
        lines(target/'processed/relations.jsonl', edges.values())
        lines(target/'processed/evidence.jsonl', evidence.values())
        # Keep API changes separately to avoid counting the same commit/file twice.
        original = target/'processed/file_changes_api.jsonl'
        if not original.exists() and (target/'processed/file_changes.jsonl').exists():
            shutil.copy2(target/'processed/file_changes.jsonl',original)
        git_shas = {c['sha'] for c in commits} - {f['sha'] for f in failures}
        fallback = [r for r in rows(original) if r.get('commit_sha') not in git_shas]
        lines(target/'processed/file_changes.jsonl', changes+fallback)
        write(target/'processed/git_refs.json', refs)
        old_rejected = rows(target/'review_queue/items.jsonl')
        lines(target/'review_queue/items.jsonl', old_rejected+rejected)
        report = {'state':'partial' if failures or rejected or any(not x['found'] for x in checks) else 'completed','observed_at':now(),
                  'cache_path':str(cache),'offline':offline,'commits':len(commits),'file_changes':len(changes),
                  'refs':len(refs),'elapsed_seconds':round(time.monotonic()-started,1), 'errors':failures,
                  'known_commit_checks':checks,'missing_known_commits':sum(not x['found'] for x in checks),
                  'diff_policy':'root versus empty tree; others versus first parent; rename similarity detection',
                  'scope':'冻结的分支和标签可达历史，以及本地存在的API已知提交及其祖先；不主动抓取不可达SHA',
                  'limitations':['未执行历史函数映射','未推断首次发布版本','Git身份未按邮箱自动合并到GitHub账号','不下载LFS外部对象和子模块内容']}
        write(target/'reports/git_collection.json', report)
        quality = json.loads((target/'reports/quality.json').read_text(encoding='utf-8'))
        quality.update(node_counts=dict(Counter(n['entity_type'] for n in nodes.values())),
                       relation_counts=dict(Counter(e['relation'] for e in edges.values())),
                       dangling_edges=0,rejected_records=len(old_rejected)+len(rejected),git_collection=report)
        quality['node_counts'] = {kind:quality['node_counts'].get(kind,0) for kind in TYPES}
        quality['errors'] = quality.get('errors',[])+failures
        state = 'partial' if manifest.get('state')!='ready' or failures or rejected or report['missing_known_commits'] else 'ready'
        quality['state'] = state
        write(target/'reports/quality.json',quality)
        manifest.update(run_id=target.name,parent_run=source.name,contract_version='desktop-5',state=state,git_collected_at=now())
        manifest.setdefault('stages',{})['local_git'] = report['state']
        manifest['git_cache'] = str(cache)
        manifest['checksums'] = {str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (target/'processed').rglob('*') if p.is_file()}
        write(target/'manifest.json',manifest)
        write(target/'state/progress.json',{'state':state,'stage':'local_git','observed_at':now()})
        readme=target/'README.md'
        previous=readme.read_text(encoding='utf-8') if readme.exists() else ''
        readme.write_text('> 本目录已增加本地Git补采。新增文件和处理规则见 `本地Git补采说明.md`；'
                          '最新统计以 `reports/quality.json` 和 `reports/git_collection.json` 为准。'
                          '下方保留的原任务说明可能包含补采前的数量。\n\n'+previous,encoding='utf-8')
        (target/'本地Git补采说明.md').write_text('# 本地 Git 补采结果\n\n原 API 数据未重新请求，原任务未修改。\n\n'
            '详见 reports/git_collection.json。raw/git 保存冻结的引用、提交和逐提交文件差异；'
            'processed 已合并实体、关系和证据。file_changes_api.jsonl 保留原 API 文件变化。\n\n'
            '共享仓库缓存未包含在交接包中；完整历史源码和补丁需要访问 manifest 中 git_cache 指向的缓存。'
            '没有执行历史函数映射或首次发布版本推断。历史 API 错误仍保留，补查结果单独记录。\n',encoding='utf-8')
        notify('log','Git补采完成：'+str(target))
        return target
    except BaseException:
        if target and target.exists():
            pending = json.loads((target/'manifest.json').read_text(encoding='utf-8'))
            pending['state']='cancelled' if cancel.is_set() else 'partial'
            pending.setdefault('stages',{})['local_git']='cancelled' if cancel.is_set() else 'failed'
            write(target/'manifest.json',pending)
        raise
    finally: lock.unlink(missing_ok=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description='复用API结果，补采本地Git历史')
    parser.add_argument('--source',required=True,help='包含manifest.json的原任务目录')
    parser.add_argument('--cache-root'); parser.add_argument('--offline',action='store_true')
    args = parser.parse_args()
    print(supplement(args.source,args.cache_root,lambda k,v:print(k,v,flush=True),offline=args.offline))
