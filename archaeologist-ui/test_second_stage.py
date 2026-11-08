import unittest
from second_stage import Stage,symbols

class StageTests(unittest.TestCase):
 def make(self):
  s=Stage.__new__(Stage);s.repo='a/b';s.nodes={'issue:a/b:1':{'entity_type':'Issue'}};s.edges={};s.evidence={'ev':{'source_url':'https://example.org'}};s.manifest={'repo_url':'https://example.org'};s.review=[]
  s.docs={'d':{'document_id':'d','raw_text':'函数报错。修复建议。','evidence_ids':['ev']}}
  return s
 def task(self):return dict(task_id='t',entity_id='issue:a/b:1',entity_type='Issue',text='函数报错。修复建议。',source_spans=[{'document_id':'d'}],symbols=[],offset=0)
 def test_evidence_fabrication_rejected(self):
  s=self.make();s.accept(self.task(),{'claims':[dict(kind='Problem',name='x',quote='不存在',confidence=1)]});self.assertFalse(s.edges);self.assertEqual(len(s.nodes),1)
 def test_offsets_and_replay(self):
  s=self.make();r={'claims':[dict(kind='Problem',name='x',quote='函数报错',confidence=.9,problem_type='bug')]};s.accept(self.task(),r);s.accept(self.task(),r);self.assertEqual(len(s.edges),1);self.assertEqual(next(iter(s.edges.values()))['properties']['raw_start'],0)
 def test_bad_symbol_no_partial_mutation(self):
  s=self.make();s.accept(self.task(),{'claims':[dict(kind='Problem',name='x',quote='函数报错',confidence=1,symbol_ids=['fake'])]});self.assertEqual(len(s.nodes),1);self.assertFalse(s.edges)
 def test_issue_cannot_implement_solution(self):
  s=self.make();s.accept(self.task(),{'claims':[dict(kind='Solution',name='x',quote='函数报错',confidence=1)]});self.assertFalse(s.edges)
 def test_historical_deleted_and_decorated_symbol(self):
  r=symbols('@deco\ndef f():\n return 1\n');self.assertEqual(r[0]['start_line'],1);self.assertEqual(symbols(''),[]);self.assertIsNone(symbols('def broken('))
if __name__=='__main__':unittest.main()
