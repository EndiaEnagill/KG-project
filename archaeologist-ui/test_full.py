import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline import Client,run

class FullCollectionTests(unittest.TestCase):
    def test_full_scope_has_no_numeric_limits(self):
        seen=[]
        def get(client,endpoint):
            client.check()
            if endpoint=='/repos/a/b':data={'full_name':'a/b','owner':{'login':'a'},'name':'b','html_url':'https://github.com/a/b','default_branch':'main'}
            elif '/commits/main' in endpoint:data={'sha':'a'*40}
            else:data={'tree':[],'truncated':False}
            return {'request_url':'https://api.github.com'+endpoint,'data':data,'fetched_at':'2026-01-01T00:00:00Z'}
        def listing(client,endpoint,limit=None,predicate=None):seen.append(limit);return []
        with tempfile.TemporaryDirectory() as td,patch.object(Client,'get',get),patch.object(Client,'listing',listing):
            root=run('https://github.com/a/b','',td,{},lambda *x:None,threading.Event())
            manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['mode'],'full');self.assertTrue(all(v is None for v in manifest['scope'].values()))
            self.assertTrue(all(v is None for v in seen));self.assertTrue((root/'processed/refs.json').exists())
if __name__=='__main__':unittest.main()
