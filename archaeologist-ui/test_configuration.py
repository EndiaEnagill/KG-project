import json
import tempfile
import unittest
from pathlib import Path
from configuration import enrich,matches,validate_config,read_rows
from pipeline import TYPES,write,lines

class ConfigurationTests(unittest.TestCase):
    def test_prefix_boundaries(self):
        self.assertTrue(matches('pkg/a/f.py','pkg/a/'))
        self.assertFalse(matches('pkg/ab/f.py','pkg/a'))
    def test_path_traversal_rejected(self):
        for p in ['../secret','/tmp/x','C:/x','pkg\\x']:
            with self.assertRaises(ValueError):validate_config({'repository':'a/b','modules':[{'name':'m','path_prefixes':[p]}]})
    def test_enrichment_is_non_destructive_and_repeatable(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'original';root.mkdir()
            records=[{'id':'repo:a/b','entity_type':'Repository','properties':{'owner':'a','name':'b','full_name':'a/b','url':'https://github.com/a/b','default_branch':'main','collected_at':'2026-01-01T00:00:00Z'}},
             {'id':'file:a/b:pkg/a.py','entity_type':'File','properties':{'path':'pkg/a.py','name':'a.py'}},
             {'id':'symbol:a/b:pkg/a.py:f','entity_type':'CodeSymbol','properties':{'name':'f','qualified_name':'f','kind':'function'}}]
            for kind,name in TYPES.items():lines(root/'processed/nodes'/(name+'.jsonl'),[n for n in records if n['entity_type']==kind])
            lines(root/'processed/relations.jsonl',[]);lines(root/'processed/evidence.jsonl',[])
            write(root/'manifest.json',{'canonical_repo':'a/b','repo_url':'https://github.com/a/b','stages':{},'limitations':[]})
            write(root/'reports/quality.json',{'state':'partial'})
            config={'repository':'a/b','modules':[{'name':'算法','path_prefixes':['pkg/']}],'features':[{'name':'计算','seed_symbols':['symbol:a/b:pkg/a.py:f']}]}
            original=(root/'processed/nodes/modules.jsonl').read_bytes()
            new=enrich(root,config)
            self.assertEqual((root/'processed/nodes/modules.jsonl').read_bytes(),original)
            relations=read_rows(new/'processed/relations.jsonl')
            self.assertEqual({e['relation'] for e in relations},{'CONTAINS_MODULE','CONTAINS','IMPLEMENTED_BY'})
            again=enrich(new,config)
            self.assertEqual(len(read_rows(again/'processed/relations.jsonl')),3)
            bad={**config,'features':[{'name':'错误','seed_symbols':['missing']} ]}
            with self.assertRaises(ValueError):enrich(root,bad)

if __name__=='__main__':unittest.main()
