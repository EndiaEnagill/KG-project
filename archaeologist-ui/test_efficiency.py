import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pipeline import Client,run,write,digest

class EfficiencyTests(unittest.TestCase):
    def test_list_evidence_avoids_detail_calls_and_shared_ref_walks(self):
        date='2026-01-01T00:00:00Z';sha='a'*40;calls=[];events=[]
        issue={'id':1,'number':1,'title':'Example','body':'Example','labels':[],'state':'open','created_at':date,'updated_at':date,'html_url':'https://github.com/a/b/issues/1'}
        comments=[{'id':i,'body':'comment','created_at':date,'html_url':f'https://github.com/a/b/issues/1#issuecomment-{i}','url':f'https://api.github.com/repos/a/b/issues/comments/{i}'} for i in (101,102)]
        commit={'sha':sha,'html_url':'https://github.com/a/b/commit/'+sha,'commit':{'message':'initial','author':{'date':date},'committer':{'date':date}},'parents':[],'files':[]}
        def get(client,endpoint):
            client.check();calls.append(endpoint)
            if endpoint=='/repos/a/b':data={'full_name':'a/b','owner':{'login':'a'},'name':'b','html_url':'https://github.com/a/b','default_branch':'main'}
            elif endpoint.startswith('/repos/a/b/issues?'):data=[issue]
            elif endpoint.startswith('/repos/a/b/issues/1/comments?'):data=comments
            elif endpoint.startswith('/repos/a/b/commits?'):data=[{'sha':sha}]
            elif endpoint=='/repos/a/b/commits/'+sha:data=commit
            elif endpoint=='/repos/a/b/commits/main':data={'sha':sha}
            elif '/git/trees/' in endpoint:data={'tree':[],'truncated':False}
            elif endpoint.startswith('/repos/a/b/branches?'):data=[{'name':'main','commit':{'sha':sha}}]
            elif endpoint.startswith('/repos/a/b/tags?'):data=[{'name':str(i),'commit':{'sha':sha}} for i in range(20)]
            elif endpoint.startswith(('/repos/a/b/pulls?','/repos/a/b/releases?')):data=[]
            else:raise AssertionError('Unnecessary request: '+endpoint)
            record={'request_url':'https://api.github.com'+endpoint,'data':data,'fetched_at':date,'link':None}
            write(client.root/'raw/api'/(digest(record['request_url'])+'.json'),record)
            return record
        with tempfile.TemporaryDirectory() as td,patch.object(Client,'get',get):
            root=run('https://github.com/a/b','',td,{},lambda k,v:events.append((k,v)),threading.Event())
            quality=json.loads((root/'reports/quality.json').read_text(encoding='utf-8'))
            self.assertEqual(quality['errors'],[]);self.assertEqual(quality['node_counts']['Comment'],2)
            self.assertFalse(any('/issues/comments/' in c or '/commits?sha=' in c for c in calls))
            evidence=[json.loads(x) for x in (root/'processed/evidence.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(e['json_pointer']=='/data/1' for e in evidence))
            for e in evidence:self.assertTrue((root/e['raw_ref']).exists())
            progress=[v for k,v in events if k=='progress' and v['label']=='Issue与评论']
            self.assertEqual(progress[-1]['done'],1);self.assertEqual(progress[-1]['total'],1)

if __name__=='__main__':unittest.main()
