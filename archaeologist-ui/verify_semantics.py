"""Verify SOLVES candidates using the already extracted, quoted evidence only."""
import json
from concurrent.futures import ThreadPoolExecutor,as_completed
from second_stage import Stage,call,write,lines,now

def verify(stage,workers=8):
 stage.link_solutions()
 tasks=[]
 for e in stage.edges.values():
  if e['relation']!='SOLVES':continue
  source=stage.nodes[e['source_id']];target=stage.nodes[e['target_id']]
  evidence=[dict(relation=r['relation'],quote=r['evidence'],source=r['source_id']) for r in stage.edges.values() if r['target_id'] in (source['id'],target['id']) and r['relation'] in ('REPORTS','IMPLEMENTS')]
  tasks.append((e,dict(task_id='verify:'+e['relation_id'],entity_type='Verification',solution=source['properties'],problem=target['properties'],evidence=evidence)))
 with ThreadPoolExecutor(max_workers=workers) as pool:
  pending={pool.submit(call,t,stage.out):(e,t) for e,t in tasks}
  for future in as_completed(pending):
   e,t=pending[future]
   try:
    result=future.result()
    if result.get('supported') is True and isinstance(result.get('confidence'),(int,float)) and not isinstance(result['confidence'],bool) and .9<=result['confidence']<=1:
     e['status']='inferred';e['confidence']=min(e['confidence'],result['confidence']);e['properties']['verification_task']=t['task_id'];e['properties']['verification_reason']=str(result.get('reason',''));stage.metrics['solves_verified']+=1
    else:stage.metrics['solves_unconfirmed']+=1
   except Exception:stage.metrics['solves_verification_failed']+=1
 return len(tasks)
