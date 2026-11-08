import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from git_collect import supplement, rows, parse_diff
from pipeline import TYPES, lines, write, Cancelled


@unittest.skipUnless(shutil.which('git'),'Git required')
class GitCollectionTests(unittest.TestCase):
    def test_history_merge_rename_binary_and_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); work=base/'work'; work.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',str(work),*args],stderr=subprocess.PIPE).decode().strip()
            git('init','-b','main');git('config','user.name','Test');git('config','user.email','test@example.com')
            (work/'old name.py').write_text('def f():\n    return 1\n',encoding='utf-8')
            git('add','.');git('commit','-m','root'); rootsha=git('rev-parse','HEAD')
            git('tag','-a','v1','-m','annotated')
            git('mv','old name.py','新名字.py');git('commit','-m','rename');renamesha=git('rev-parse','HEAD')
            git('checkout','-b','side')
            (work/'binary.dat').write_bytes(b'\0\xffhello')
            git('add','.');git('commit','-m','binary')
            git('checkout','main');(work/'README.md').write_text('main',encoding='utf-8')
            git('add','.');git('commit','-m','docs');git('merge','--no-ff','side','-m','merge')
            merge=git('rev-parse','HEAD')
            caches=base/'cache';caches.mkdir()
            subprocess.check_call(['git','clone','--bare',str(work),str(caches/'a__b.git')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            source=base/'source';source.mkdir()
            repository={'id':'repo:a/b','entity_type':'Repository','properties':{'owner':'a','name':'b','full_name':'a/b','url':'https://github.com/a/b','default_branch':'main','collected_at':'2026-01-01T00:00:00Z'}}
            pr={'id':'pr:a/b:1','entity_type':'PullRequest','properties':{'number':1,'title':'merge','state':'closed','merged':True,'base_branch':'main','created_at':'2026-01-01T00:00:00Z','updated_at':'2026-01-01T00:00:00Z','url':'https://github.com/a/b/pull/1'},'provenance':{'evidence_ids':['ev:pr']}}
            for kind,name in TYPES.items():lines(source/'processed/nodes'/(name+'.jsonl'),[n for n in [repository,pr] if n['entity_type']==kind])
            lines(source/'processed/relations.jsonl',[])
            lines(source/'processed/evidence.jsonl',[{'evidence_id':'ev:pr','raw_ref':'raw/api/pr.json','json_pointer':'/data'}])
            lines(source/'processed/file_changes.jsonl',[])
            write(source/'raw/api/pr.json',{'data':{'merge_commit_sha':merge}})
            write(source/'manifest.json',{'repo_url':'https://github.com/a/b','canonical_repo':'a/b','state':'ready','reference_sha':rootsha,'stages':{},'limitations':[]})
            write(source/'reports/quality.json',{'state':'ready','errors':[]})
            original=(source/'manifest.json').read_bytes()
            events=[];out=supplement(source,caches,lambda *x:events.append(x),offline=True)
            self.assertEqual((source/'manifest.json').read_bytes(),original)
            quality=json.loads((out/'reports/quality.json').read_text(encoding='utf-8'))
            self.assertEqual(quality['rejected_records'],0)
            self.assertEqual(quality['node_counts']['Commit'],5)
            edges=rows(out/'processed/relations.jsonl')
            self.assertEqual(sum(e['relation']=='PARENT_OF' for e in edges),5)
            self.assertEqual(sum(e['relation']=='RENAMED_TO' for e in edges),1)
            self.assertEqual(sum(e['relation']=='MERGED_AS' for e in edges),1)
            changes=rows(out/'processed/file_changes.jsonl')
            rename=next(c for c in changes if c['commit_sha']==renamesha)
            self.assertEqual(rename['old_path'],'old name.py');self.assertEqual(rename['path'],'新名字.py')
            self.assertTrue(any(c['binary'] and c['additions'] is None for c in changes))
            again=supplement(out,caches,offline=True)
            self.assertEqual(len(rows(again/'processed/relations.jsonl')),len(edges))
            self.assertEqual(len(rows(again/'processed/file_changes.jsonl')),len(changes))
            self.assertTrue(any(k=='progress' for k,v in events))
            # An API-known SHA absent locally must stay missing, not become a synthetic Commit.
            pr['properties']['merge_commit_sha']='f'*40
            lines(source/'processed/nodes/pull_requests.jsonl',[pr])
            missing=supplement(source,caches,offline=True)
            report=json.loads((missing/'reports/git_collection.json').read_text(encoding='utf-8'))
            self.assertEqual(report['missing_known_commits'],1)
            self.assertEqual(report['state'],'partial')
            self.assertFalse(any(e['relation']=='MERGED_AS' for e in rows(missing/'processed/relations.jsonl')))
            cancel=threading.Event();cancel.set()
            with self.assertRaises(Cancelled):supplement(source,caches,cancel=cancel,offline=True)
            self.assertFalse((caches/'a__b.lock').exists())

    def test_diff_paths_with_tabs_and_newlines(self):
        data=b':100644 100644 '+b'a'*40+b' '+b'b'*40+b' R100\0a\tx\0b\ny\0' + b'0\t0\t\0a\tx\0b\ny\0'
        change=parse_diff(data)[0]
        self.assertEqual(change['old_path'],'a\tx');self.assertEqual(change['path'],'b\ny')
        self.assertEqual(change['additions'],0)


if __name__=='__main__':unittest.main()
