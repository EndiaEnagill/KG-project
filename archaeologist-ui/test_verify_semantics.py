import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from verify_semantics import verify
class VerificationTests(unittest.TestCase):
 def stage(self):
  edge=dict(relation_id='r',source_id='s',target_id='p',relation='SOLVES',status='candidate',confidence=.95,properties={})
  return SimpleNamespace(link_solutions=lambda:None,edges={'r':edge},nodes={'s':{'id':'s','properties':{'name':'solution'}},'p':{'id':'p','properties':{'name':'problem'}}},out=Path('.'),metrics=Counter())
 def test_explicit_supported_result_promotes_inference(self):
  s=self.stage()
  with patch('verify_semantics.call',return_value={'supported':True,'confidence':.92,'reason':'supported by quotes'}):verify(s,1)
  self.assertEqual(s.edges['r']['status'],'inferred');self.assertEqual(s.edges['r']['confidence'],.92)
 def test_string_boolean_and_low_confidence_not_promoted(self):
  for result in ({'supported':'true','confidence':1},{'supported':True,'confidence':.8},{'supported':False,'confidence':1}):
   s=self.stage()
   with patch('verify_semantics.call',return_value=result):verify(s,1)
   self.assertEqual(s.edges['r']['status'],'candidate')
if __name__=='__main__':unittest.main()
