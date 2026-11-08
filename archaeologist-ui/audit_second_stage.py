"""Read-only audit of original package and layer-two outputs."""
import argparse,collections,hashlib,json
from pathlib import Path
from second_stage import rows,write

def audit(source,output):
 source=Path(source);out=Path(output)
 manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
 unchanged=all(hashlib.sha256((source/Path(p.replace('\\','/'))).read_bytes()).hexdigest()==h for p,h in manifest['checksums'].items())
 nodes={n['id']:n for f in (out/'processed/nodes').glob('*.jsonl') for n in rows(f)}
 edges=rows(out/'processed/relations.jsonl');documents={d['document_id']:d for d in rows(source/'processed/documents.jsonl')}
 original={e['relation_id'] for e in rows(source/'processed/relations.jsonl')}
 errors=[];outgoing=collections.defaultdict(list);release_counts=collections.Counter()
 for e in edges:
  outgoing[e['source_id']].append(e)
  if e['source_id'] not in nodes or e['target_id'] not in nodes:errors.append('dangling:'+e['relation_id'])
  if e['relation']=='FIRST_RELEASED_IN':release_counts[e['source_id']]+=1
  p=e.get('properties',{})
  if e['method']=='llm' and 'document_id' in p:
   d=documents.get(p['document_id'])
   if not d or d['raw_text'][p['raw_start']:p['raw_end']]!=e['evidence']:errors.append('quote:'+e['relation_id'])
 errors.extend('multiple_releases:'+k for k,v in release_counts.items() if v>1)
 structural=set();semantic=set()
 for e in edges:
  if e['relation']!='RESOLVES' or e['status']=='candidate':continue
  pr=e['source_id'];issue=e['target_id'];links=outgoing[pr]
  commits={x['target_id'] for x in links if x['relation'] in ('CONTAINS_COMMIT','MERGED_AS')}
  if any(any(x['relation']=='FIRST_RELEASED_IN' for x in outgoing[c]) and any(x['relation']=='CHANGES' for x in outgoing[c]) for c in commits):
   structural.add(issue)
   problems={x['target_id'] for x in outgoing[issue] if x['relation']=='REPORTS' and x['status']!='candidate'}
   solutions={x['target_id'] for x in links if x['relation']=='IMPLEMENTS' and x['status']!='candidate'}
   if any(x['relation']=='SOLVES' and x['target_id'] in problems and x['status']=='inferred' for sol in solutions for x in outgoing[sol]):semantic.add(issue)
 cache=[json.loads(f.read_text(encoding='utf-8')) for f in (out/'llm_cache').glob('*.json')]
 report=dict(original_checksums_unchanged=unchanged,original_relation_ids_preserved=original.issubset({e['relation_id'] for e in edges}),errors=errors,node_counts=dict(collections.Counter(n['entity_type'] for n in nodes.values())),relation_status_counts=dict(collections.Counter(e['relation']+':'+e['status'] for e in edges)),structural_issue_chains=len(structural),problem_solution_issue_chains=len(semantic),chain_definition='RESOLVES confirmed + same Commit CHANGES and FIRST_RELEASED_IN; semantic additionally REPORTS/IMPLEMENTS and verified inferred SOLVES; does not imply confirmed Feature mapping',cache_responses=len(cache),cache_total_tokens=sum((r.get('usage') or {}).get('total_tokens',0) for r in cache))
 write(out/'reports/final_audit.json',report)
 print(json.dumps(report,ensure_ascii=False))
 return report
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);a=p.parse_args();audit(a.source,a.output)
