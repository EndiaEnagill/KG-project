"""Non-destructive manual Module/Feature enrichment of an existing handoff."""
import hashlib
import json
import shutil
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from pipeline import now,write,lines,TYPES,digest
from quality import validate

def read_rows(p):
    return [json.loads(line) for line in Path(p).read_text(encoding='utf-8').splitlines() if line.strip()]

def validate_config(config):
    if not isinstance(config,dict) or not isinstance(config.get('repository'),str):raise ValueError('配置缺少仓库名称')
    for group in ('modules','features'):
        if not isinstance(config.get(group,[]),list):raise ValueError('配置列表格式错误')
        names=set()
        for item in config.get(group,[]):
            if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not item['name'].strip():raise ValueError('模块与功能必须有名称')
            if item['name'] in names:raise ValueError('同类配置名称不能重复')
            names.add(item['name'])
            field='path_prefixes' if group=='modules' else 'seed_symbols'
            values=item.get(field,[])
            if not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values):raise ValueError(field+'必须是非空字符串列表')
            if group=='modules':
                if not values:raise ValueError('模块必须有至少一个路径')
                for p in values:
                    if '\\' in p or p.startswith('/') or ':' in p or '..' in PurePosixPath(p).parts:raise ValueError('模块路径须为仓库相对路径，使用正斜杠且不含..')
    return config

def matches(file,prefix):
    prefix=prefix.rstrip('/')
    return file==prefix or file.startswith(prefix+'/')

def enrich(source,config):
    config=validate_config(config);source=Path(source).resolve()
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if config['repository'].casefold()!=manifest['canonical_repo'].casefold():raise ValueError('配置仓库与当前结果不一致，请重新配置')
    nodes={n['id']:n for t in TYPES.values() for n in read_rows(source/'processed/nodes'/(t+'.jsonl'))}
    # Only replace records created by this manual configuration component.
    nodes={k:v for k,v in nodes.items() if not v.get('provenance',{}).get('configuration')}
    edges={e['relation_id']:e for e in read_rows(source/'processed/relations.jsonl') if not e.get('configuration')}
    evidence={e['evidence_id']:e for e in read_rows(source/'processed/evidence.jsonl') if not e['evidence_id'].startswith('ev:config:')}
    repo=manifest['canonical_repo'];rid='repo:'+repo
    if rid not in nodes:raise ValueError('当前结果没有Repository实体，无法配置')
    symbols={k for k,v in nodes.items() if v['entity_type']=='CodeSymbol'}
    for feature in config.get('features',[]):
        if any(s not in symbols for s in feature.get('seed_symbols',[])):raise ValueError('种子必须填写当前结果中的完整CodeSymbol ID；请从实体预览复制')
    eid='ev:config:'+digest(json.dumps(config,sort_keys=True,ensure_ascii=False))
    evidence[eid]={'evidence_id':eid,'source_url':manifest['repo_url'],'raw_ref':'config/project.json','observed_at':now(),'method':'manual','content_hash':eid.removeprefix('ev:config:')}
    warnings=[]
    def node(kind,item,props):
        nid=kind.lower()+':'+repo+':'+quote(item['name'].strip(),safe='')
        nodes[nid]={'id':nid,'entity_type':kind,'properties':props,'provenance':{'repo_id':rid,'observed_at':now(),'evidence_ids':[eid],'configuration':True,'schema_version':'1.0','contract_version':'desktop-3'}}
        return nid
    def edge(a,rel,b):
        key='rel:'+digest(a+'|'+rel+'|'+b)
        edges[key]={'relation_id':key,'source_id':a,'source_type':nodes[a]['entity_type'],'relation':rel,'target_id':b,'target_type':nodes[b]['entity_type'],'method':'manual','status':'asserted','confidence':1.0,'observed_at':now(),'evidence':'用户确认的模块路径或功能种子配置','evidence_url':manifest['repo_url'],'evidence_ids':[eid],'properties':{},'configuration':True}
    for item in config.get('modules',[]):
        mid=node('Module',item,{'name':item['name'],'description':item.get('description',''),'path_prefixes':item['path_prefixes'],'definition_method':'manual','aliases':[]});edge(rid,'CONTAINS_MODULE',mid)
        files=[n for n in list(nodes.values()) if n['entity_type']=='File' and any(matches(n['properties']['path'],p) for p in item['path_prefixes'])]
        for f in files:edge(mid,'CONTAINS',f['id'])
        if not files:warnings.append('模块 '+item['name']+' 在当前采集范围内没有匹配文件')
    for item in config.get('features',[]):
        fid=node('Feature',item,{'name':item['name'],'description':item.get('description',''),'status':'active','definition_method':'manual_confirmed','aliases':[]})
        for sid in item.get('seed_symbols',[]):edge(fid,'IMPLEMENTED_BY',sid)
        if not item.get('seed_symbols'):warnings.append('功能 '+item['name']+' 尚未配置实现符号')
    accepted,relations,rejected=validate(nodes,edges,evidence)
    if rejected:raise ValueError('配置后校验发现不合格记录；未修改原数据：'+str(rejected[0]['reasons']))
    target=source.with_name(source.name+'-configured-'+uuid.uuid4().hex[:6])
    shutil.copytree(source,target)
    write(target/'config/project.json',config)
    for kind,name in TYPES.items():lines(target/'processed/nodes'/(name+'.jsonl'),[n for n in accepted.values() if n['entity_type']==kind])
    lines(target/'processed/relations.jsonl',relations.values());lines(target/'processed/evidence.jsonl',evidence.values())
    quality=json.loads((target/'reports/quality.json').read_text(encoding='utf-8'))
    quality.update(node_counts={k:sum(n['entity_type']==k for n in accepted.values()) for k in TYPES},relation_counts={r:sum(e['relation']==r for e in relations.values()) for r in {e['relation'] for e in relations.values()}},configuration_warnings=warnings)
    write(target/'reports/quality.json',quality)
    manifest.update(run_id=target.name,contract_version='desktop-3',configured_at=now(),parent_run=source.name)
    manifest['stages']['module_feature_configuration']='configured'
    manifest['configuration_counts']={'modules':len(config.get('modules',[])),'features':len(config.get('features',[]))}
    manifest['limitations']=[s for s in manifest['limitations'] if not s.startswith('未配置Module/Feature')]
    manifest['checksums']={str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('processed','config') for p in (target/folder).rglob('*') if p.is_file()}
    write(target/'manifest.json',manifest)
    return target
