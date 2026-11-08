import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline import Client
from quality import validate,chunk_document

class ContractTests(unittest.TestCase):
    def test_unlimited_pagination_reads_every_page(self):
        pages=[{'data':[{'number':i}], 'link':f'<https://api.github.com/repos/a/b/issues?page={i+2}>; rel="next"' if i<11 else None} for i in range(12)]
        with tempfile.TemporaryDirectory() as td:
            client=Client(Path(td),'',lambda *x:None,threading.Event())
            with patch.object(client,'get',side_effect=pages):rows=client.listing('/repos/a/b/issues')
            self.assertEqual(len(rows),12);self.assertTrue(client.pages[-1]['complete'])
    def test_issue_filter_continues_after_pr_only_page(self):
        pages=[{'data':[{'number':1,'pull_request':{}}],'link':'<https://api.github.com/repos/a/b/issues?page=2>; rel="next"'}, {'data':[{'number':2}], 'link':None}]
        with tempfile.TemporaryDirectory() as td:
            client=Client(Path(td),'',lambda *x:None,threading.Event())
            with patch.object(client,'get',side_effect=pages):
                self.assertEqual(client.listing('/repos/a/b/issues',1,predicate=lambda x:'pull_request' not in x),[{'number':2}])
            self.assertEqual(client.pages[-1]['pages'],2)
    def test_chunks_reconstruct_raw_unicode(self):
        raw='甲😀\r\n乙\x00XYZ'
        chunks=chunk_document({'raw_text':raw,'document_id':'d','content_hash':'h'},size=3)
        self.assertEqual(''.join(c['text'] for c in chunks),raw)
        for c in chunks:self.assertEqual(raw[c['raw_start']:c['raw_end']],c['text'])
    def test_bad_node_and_its_edge_are_quarantined(self):
        nodes={'a':{'entity_type':'Developer','properties':{'login':'x'}},'b':{'entity_type':'Issue','properties':{'number':True}}}
        edge={'source_id':'a','target_id':'b','relation':'CREATED','confidence':1,'evidence_url':'https://github.com/a/b','evidence_ids':['e']}
        good,edges,bad=validate(nodes,{'edge':edge},{'e':{}})
        self.assertEqual(set(good),{'a'});self.assertFalse(edges);self.assertEqual(len(bad),2)

if __name__=='__main__':unittest.main()
