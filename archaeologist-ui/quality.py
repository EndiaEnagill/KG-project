"""Explicit graph contract checks; no third-party dependencies."""
from datetime import datetime

REQUIRED={
 'Repository':{'owner':str,'name':str,'full_name':str,'url':str,'default_branch':str,'collected_at':str},
 'Developer':{'login':str},
 'Issue':{'number':int,'title':str,'state':str,'created_at':str,'updated_at':str,'url':str},
 'PullRequest':{'number':int,'title':str,'state':str,'merged':bool,'base_branch':str,'created_at':str,'updated_at':str,'url':str},
 'Commit':{'sha':str,'short_sha':str,'message':str,'authored_at':str,'committed_at':str},
 'Release':{'tag_name':str,'draft':bool,'prerelease':bool,'url':str},
 'Comment':{'body':str,'created_at':str,'url':str,'comment_type':str},
 'File':{'path':str,'name':str},
 'CodeSymbol':{'name':str,'qualified_name':str,'kind':str},
 'Module':{'name':str,'path_prefixes':list,'definition_method':str},
 'Feature':{'name':str,'status':str,'definition_method':str},
 'Problem':{'name':str,'problem_type':str,'confidence':(int,float)},
 'Solution':{'name':str,'confidence':(int,float)}}
DOMAINS={'HAS_ISSUE':('Repository','Issue'),'HAS_PR':('Repository','PullRequest'),
 'HAS_COMMIT':('Repository','Commit'),'HAS_RELEASE':('Repository','Release'),
 'CREATED':('Developer','Issue'),'OPENED':('Developer','PullRequest'),
 'AUTHORED':('Developer','Commit'),'WROTE':('Developer','Comment'),
 'HAS_COMMENT':(('Issue','PullRequest'),'Comment'),'CONTAINS_COMMIT':('PullRequest','Commit'),
 'MERGED_AS':('PullRequest','Commit'),'REFERENCES':('PullRequest','Issue'),
 'MODIFIES':('Commit','File'),'PARENT_OF':('Commit','Commit'),'RENAMED_TO':('File','File'),
 'CONTAINS_MODULE':('Repository','Module'),'IMPLEMENTED_BY':('Feature','CodeSymbol')}
PAIRS={'CONTAINS':{('File','CodeSymbol'),('Module','File')}}
ENUMS={('Issue','state'):{'open','closed'},('PullRequest','state'):{'open','closed'},
 ('CodeSymbol','kind'):{'class','function','method'},('Comment','comment_type'):{'issue_comment','review_comment','review'}}
def validate(nodes,edges,evidence):
    good={}; rejected=[]
    for nid,n in nodes.items():
        kind=n.get('entity_type'); p=n.get('properties',{}); errors=[]
        if kind not in REQUIRED: errors.append('unsupported entity type')
        for field,t in REQUIRED.get(kind,{}).items():
            if field not in p or not isinstance(p[field],t) or (t is int and isinstance(p[field],bool)): errors.append('missing or invalid '+field)
        for field,value in p.items():
            if (kind,field) in ENUMS and value not in ENUMS[kind,field]: errors.append('invalid enum '+field)
            if value is not None and (field.endswith('_at')):
                try:
                    if datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is None: raise ValueError()
                except (ValueError,AttributeError,TypeError): errors.append('invalid datetime '+field)
        if kind=='Commit' and (not isinstance(p.get('sha'),str) or len(p.get('sha',''))!=40 or any(c not in '0123456789abcdef' for c in p.get('sha',''))): errors.append('invalid SHA')
        if any(e not in evidence for e in n.get('provenance',{}).get('evidence_ids',[])): errors.append('missing evidence')
        if errors: rejected.append({'record':n,'reasons':errors})
        else: good[nid]=n
    accepted={}
    for rid,e in edges.items():
        errors=[];a=good.get(e['source_id']);b=good.get(e['target_id']);domain=DOMAINS.get(e['relation'])
        if not a or not b: errors.append('missing or rejected endpoint')
        elif e['relation'] in PAIRS:
            if (a['entity_type'],b['entity_type']) not in PAIRS[e['relation']]:errors.append('domain/range mismatch')
        elif not domain or a['entity_type'] not in (domain[0] if isinstance(domain[0],tuple) else (domain[0],)) or b['entity_type']!=domain[1]: errors.append('domain/range mismatch')
        if not e.get('evidence_url') or not e.get('evidence_ids') or any(x not in evidence for x in e.get('evidence_ids',[])): errors.append('missing evidence')
        if not isinstance(e.get('confidence'),(float,int)) or not 0<=e['confidence']<=1: errors.append('invalid confidence')
        if errors: rejected.append({'record':e,'reasons':errors})
        else:accepted[rid]=e
    return good,accepted,rejected

def chunk_document(doc,size=1800):
    """Offsets refer to immutable raw Unicode code points, half-open intervals."""
    text=doc['raw_text']
    return [{'chunk_id':doc['document_id']+':'+str(start),'document_id':doc['document_id'],
        'text':text[start:start+size],'raw_start':start,'raw_end':min(start+size,len(text)),
        'offset_unit':'unicode_code_point','content_hash':doc['content_hash']} for start in range(0,len(text),size)]
