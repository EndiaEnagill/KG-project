import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from pipeline import Client, run, repository, export_zip, TYPES

class PipelineTests(unittest.TestCase):
    def test_url(self):
        self.assertEqual(repository('https://github.com/rasbt/mlxtend.git'),'rasbt/mlxtend')
        for url in ['http://github.com/a/b','https://evil.com/a/b','https://github.com/a/b/tree/main']:
            with self.assertRaises(ValueError): repository(url)

    def test_handoff_and_zip(self):
        # Deterministic API fixture: validates transformation without network/token.
        date='2026-01-01T00:00:00Z'
        meta={'full_name':'a/b','owner':{'login':'a'},'name':'b','html_url':'https://github.com/a/b','default_branch':'main'}
        issue={'number':1,'title':'Example','body':'hello\r\nworld','state':'open','created_at':date,'updated_at':date,'labels':[],'html_url':'https://github.com/a/b/issues/1','user':{'login':'alice','type':'User','html_url':'https://github.com/alice'}}
        def get(client,endpoint):
            client.check()
            data=meta if endpoint=='/repos/a/b' else issue if endpoint=='/repos/a/b/issues/1' else {'sha':'a'*40} if '/commits/main' in endpoint else {'tree':[],'truncated':False}
            return {'request_url':'https://api.github.com'+endpoint,'data':data,'fetched_at':date}
        def listing(client,endpoint,limit,predicate=None): return [issue] if '/issues?' in endpoint else []
        limits={'issues':1,'prs':1,'commits':1,'releases':1,'comments':1,'source_files':1}
        with tempfile.TemporaryDirectory() as td, patch.object(Client,'get',get),patch.object(Client,'listing',listing):
            root=run('https://github.com/a/b','secret-not-exported',td,limits,lambda *x:None,threading.Event())
            self.assertEqual(len(list((root/'processed/nodes').glob('*.jsonl'))),len(TYPES))
            docs=[json.loads(x) for x in (root/'processed/documents.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(docs[0]['clean_text'],'hello\nworld')
            nodes=[]
            for f in (root/'processed/nodes').glob('*.jsonl'): nodes.extend(json.loads(x) for x in f.read_text(encoding='utf-8').splitlines())
            ids={n['id'] for n in nodes}
            for row in (root/'processed/relations.jsonl').read_text(encoding='utf-8').splitlines():
                edge=json.loads(row);self.assertIn(edge['source_id'],ids);self.assertIn(edge['target_id'],ids)
            self.assertEqual(json.loads((root/'manifest.json').read_text(encoding='utf-8'))['stages']['semantic_extraction'],'not_run')
            z=export_zip(root)
            with zipfile.ZipFile(z) as archive:
                self.assertIn('manifest.json',archive.namelist())
                self.assertFalse(any(b'secret-not-exported' in archive.read(n) for n in archive.namelist()))

    def test_cancel_preserves_manifest(self):
        event=threading.Event();event.set()
        limits={'issues':1,'prs':1,'commits':1,'releases':1,'comments':1,'source_files':1}
        with tempfile.TemporaryDirectory() as td:
            root=run('https://github.com/a/b','',td,limits,lambda *x:None,event)
            self.assertEqual(json.loads((root/'manifest.json').read_text(encoding='utf-8'))['state'],'cancelled')

if __name__=='__main__':unittest.main()
