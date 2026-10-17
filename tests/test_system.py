import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from fastapi.testclient import TestClient
from kokic import store as S,runtime as R
from kokic.app import app


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=patch.object(S,'ROOT',Path(self.temp.name));self.root.start();S.init()
        self.client=TestClient(app);self.other=TestClient(app)
        self.client.post('/api/register',json={'email':'one@example.test','password':'a-long-test-password'})
        self.other.post('/api/register',json={'email':'two@example.test','password':'another-long-password'})
        self.project=self.client.post('/api/projects',json={'name':'Test project','description':'A real Git repository'}).json()
    def tearDown(self):
        self.client.close();self.other.close();self.root.stop();self.temp.cleanup()
    def test_new_projects_default_to_autonomous_and_can_be_restricted(self):
        pid=self.project['id']
        self.assertEqual(self.project['autonomy'],'autonomous')
        self.assertEqual(S.one('SELECT autonomy FROM projects WHERE id=?',(pid,))['autonomy'],'autonomous')
        self.assertEqual(self.client.put('/api/projects/'+pid,json={'autonomy':'accept-edits'}).status_code,200)
        self.assertEqual(S.one('SELECT autonomy FROM projects WHERE id=?',(pid,))['autonomy'],'accept-edits')
    def test_auth_scope_and_logout(self):
        self.assertEqual(TestClient(app).get('/api/state').status_code,401)
        pid=self.project['id']
        for suffix in ('','/files','/download','/file?path=README.md'):
            self.assertEqual(self.other.get('/api/projects/'+pid+suffix).status_code,404)
        self.assertEqual(self.other.put('/api/projects/'+pid,json={'paused':True}).status_code,404)
        self.assertEqual(self.client.post('/api/logout').status_code,200)
        self.assertEqual(self.client.get('/api/me').status_code,401)
    def test_path_and_archive_boundaries(self):
        pid=self.project['id'];root=R.project_path(self.project)
        (root/'.env').write_text('private secret');(root/'node_modules').mkdir();(root/'node_modules/x').write_text('excluded')
        (root/'outside').symlink_to(Path(self.temp.name)/'studio.db')
        self.assertEqual(self.client.get(f'/api/projects/{pid}/file',params={'path':'../../studio.db'}).status_code,403)
        self.assertEqual(self.client.get(f'/api/projects/{pid}/file',params={'path':'outside'}).status_code,403)
        self.assertEqual(self.client.put(f'/api/projects/{pid}/file',json={'path':'.git/config','content':'bad'}).status_code,403)
        saved=self.client.put(f'/api/projects/{pid}/file',json={'path':'src/app.py','content':'print(42)\n'})
        self.assertEqual(saved.status_code,200)
        content=self.client.get(f'/api/projects/{pid}/download').content
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            self.assertEqual(z.read('src/app.py'),b'print(42)\n')
            self.assertFalse(any('.git/' in n or '.env'==n or 'node_modules/' in n or n=='outside' for n in z.namelist()))
    def test_account_and_integration_scope(self):
        a=self.client.post('/api/accounts',json={'label':'Account 1'}).json()
        self.assertEqual(a['status'],'disconnected');self.assertEqual(a['quota'],{})
        self.assertEqual(self.other.put('/api/accounts/'+a['id'],json={'model':'x'}).status_code,404)
        self.assertEqual(self.other.post('/api/accounts/'+a['id']+'/console',json={}).status_code,404)
        r=self.client.post('/api/integrations',json={'name':'private','kind':'mcp','config':{'server':{'command':'test','env':{'TOKEN':'test-private-token'}}}})
        record=S.one('SELECT * FROM integrations WHERE id=?',(r.json()['id'],))
        self.assertNotIn('test-private-token',record['config'])
        self.assertEqual(S.unseal(record['config'])['server']['env']['TOKEN'],'test-private-token')
        self.assertNotIn('test-private-token',self.client.get('/api/state').text)
        self.assertEqual(self.other.delete('/api/integrations/'+record['id']).status_code,404)
    def test_queued_without_authenticated_account(self):
        pid=self.project['id'];r=self.client.post(f'/api/projects/{pid}/chat',json={'prompt':'Build a page','policy':'fallback'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(S.one('SELECT status FROM tasks WHERE id=?',(r.json()['task'],))['status'],'queued')
        self.assertEqual(R.available_accounts(self.project['user']),[])
    def test_origin_guard(self):
        self.assertEqual(self.client.post('/api/accounts',json={'label':'x'},headers={'Origin':'https://evil.example'}).status_code,403)
    def test_real_worktree_execution_review_and_merge(self):
        a=self.client.post('/api/accounts',json={'label':'Test runner'}).json()
        S.write("UPDATE accounts SET status='connected' WHERE id=?",(a['id'],))
        fake=Path(self.temp.name)/'fake-agy'
        fake.write_text('#!/usr/bin/env python3\nimport sys,json,pathlib\nrequest=json.loads(sys.stdin.readline())\nassert request["event"]=="user"\npathlib.Path("hello.py").write_text("print(42)\\n")\nprint(json.dumps({"event":"result","result":{"status":"SUCCESS","response":"Created hello.py; synthetic runner integration test"}}))\n')
        fake.chmod(0o700)
        tid=R.create_task(self.project,'Test execution','Create hello.py',a['id'])
        S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,));task=S.one('SELECT * FROM tasks WHERE id=?',(tid,))
        with patch.object(R,'AGY',fake),patch.object(R,'profile_env',return_value=dict(os.environ)):
            R.run_task(task,a)
        updated=S.one('SELECT * FROM tasks WHERE id=?',(tid,))
        self.assertEqual(updated['status'],'review',updated['error'])
        self.assertFalse((R.project_path(self.project)/'hello.py').exists())
        self.assertIn('hello.py',self.client.get('/api/tasks/'+tid+'/diff').json()['diff'])
        self.assertEqual(self.other.post('/api/tasks/'+tid+'/merge').status_code,404)
        self.assertEqual(self.client.post('/api/tasks/'+tid+'/merge').status_code,200)
        self.assertEqual((R.project_path(self.project)/'hello.py').read_text(),'print(42)\n')
    def test_mcp_token_scope(self):
        import hashlib
        tid=R.create_task(self.project,'Coordinator','test');token='test-task-lease'
        S.write("UPDATE tasks SET status='running',token=? WHERE id=?",(hashlib.sha256(token.encode()).hexdigest(),tid))
        self.assertEqual(self.client.post('/api/mcp',json={'name':'team'}).status_code,401)
        headers={'Authorization':'Bearer '+token}
        self.assertEqual(self.client.post('/api/mcp',json={'name':'team'},headers=headers).status_code,200)
        self.assertEqual(self.client.post('/api/mcp',json={'name':'task_status','arguments':{'id':'other-project'}},headers=headers).status_code,404)
        S.write("UPDATE tasks SET status='completed',token='' WHERE id=?",(tid,))
        self.assertEqual(self.client.post('/api/mcp',json={'name':'team'},headers=headers).status_code,401)
    def test_reported_quota_only(self):
        a=self.client.post('/api/accounts',json={'label':'Quota test'}).json()
        c=R.Console.__new__(R.Console);c.account=a
        c.parse_usage('Gemini 3.8 Flash (High) 37% remaining resets in 2h 15m')
        quota=json.loads(S.one('SELECT quota FROM accounts WHERE id=?',(a['id'],))['quota'])
        self.assertEqual(next(iter(quota.values()))['remaining_pct'],37)
        self.assertAlmostEqual(next(iter(quota.values()))['reset_at']-time.time(),8100,delta=3)
        self.assertIsNone(R.reported_reset('reset tomorrow'))
    def test_native_shared_quota_groups_and_both_windows(self):
        a=self.client.post('/api/accounts',json={'label':'Native quota'}).json()
        c=R.Console.__new__(R.Console);c.account=a
        output='''GEMINI MODELS
  Models within this group: Gemini Flash, Gemini Pro
  Weekly Limit Remaining
[████████░░] 93.08%
Refreshes in 160h 45m
  Five Hour Limit Remaining
[████████░░] 94.33%
Refreshes in 2h 45m
CLAUDE AND GPT MODELS
  Models within this group: Claude Opus, Claude Sonnet, GPT-OSS
  Weekly Limit Remaining
[██████████] 99.70%
Refreshes in 163h 20m
  Five Hour Limit Remaining
[██████████] 99.40%
Refreshes in 20m
'''
        c.parse_usage(output)
        row=S.one('SELECT * FROM accounts WHERE id=?',(a['id'],));q=json.loads(row['quota'])
        self.assertEqual(q['gemini']['windows']['weekly']['remaining_pct'],93.08)
        self.assertEqual(q['gemini']['windows']['five_hour']['remaining_pct'],94.33)
        self.assertEqual(q['claude_gpt']['windows']['weekly']['remaining_pct'],99.70)
        self.assertAlmostEqual(q['claude_gpt']['windows']['five_hour']['reset_at']-time.time(),1200,delta=3)
        self.assertEqual(row['quota_status'],'ready');self.assertTrue(c.quota_complete)
        stamp=row['quota_checked'];c.parse_usage(output)
        self.assertEqual(S.one('SELECT quota_checked FROM accounts WHERE id=?',(a['id'],))['quota_checked'],stamp)
    def test_exhausted_selected_group_cannot_be_scheduled(self):
        account={'model':'gemini-3.8-flash-high','quota':json.dumps({'gemini':{'remaining_pct':0,'windows':{'weekly':{'remaining_pct':0,'reset_at':time.time()+3600}}},'claude_gpt':{'remaining_pct':90}})}
        self.assertFalse(R.quota_allows(account))
        account['model']='claude-sonnet-4-6';self.assertTrue(R.quota_allows(account))
    def test_account_scheduling_excludes_busy_limited_and_other_users(self):
        a=self.client.post('/api/accounts',json={'label':'Ready'}).json()
        b=self.client.post('/api/accounts',json={'label':'Limited'}).json()
        c=self.other.post('/api/accounts',json={'label':'Other owner'}).json()
        S.write("UPDATE accounts SET status='connected'")
        S.write("UPDATE accounts SET status='limited' WHERE id=?",(b['id'],))
        self.assertEqual([x['id'] for x in R.available_accounts(self.project['user'])],[a['id']])
        tid=R.create_task(self.project,'Busy','test',a['id'])
        S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,))
        self.assertEqual(R.available_accounts(self.project['user']),[])
    def test_removed_customization_is_removed_on_next_task(self):
        a=self.client.post('/api/accounts',json={'label':'Profile'}).json()
        item=self.client.post('/api/integrations',json={'name':'style','kind':'skill','config':{'content':'# Style\nKeep it clear'}}).json()
        tid=R.create_task(self.project,'Setup','test',a['id']);task=S.one('SELECT * FROM tasks WHERE id=?',(tid,))
        R.configure_profile(a,task,self.project)
        skill=R.profile_home(a)/'.gemini/config/skills/style/SKILL.md'
        self.assertTrue(skill.exists())
        self.client.delete('/api/integrations/'+item['id'])
        R.configure_profile(a,task,self.project)
        self.assertFalse(skill.exists())
    def test_quota_error_requeues_on_other_account(self):
        a=self.client.post('/api/accounts',json={'label':'Exhausted'}).json()
        S.write("UPDATE accounts SET status='connected' WHERE id=?",(a['id'],))
        fake=Path(self.temp.name)/'limited-cli'
        fake.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.readline()\nprint("429 RESOURCE_EXHAUSTED; resets in 2h",file=sys.stderr)\nsys.exit(1)\n');fake.chmod(0o700)
        tid=R.create_task(self.project,'Quota retry','test',a['id'],policy='fallback')
        S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,))
        with patch.object(R,'AGY',fake),patch.object(R,'profile_env',return_value=dict(os.environ)):
            R.run_task(S.one('SELECT * FROM tasks WHERE id=?',(tid,)),a)
        task=S.one('SELECT * FROM tasks WHERE id=?',(tid,));account=S.one('SELECT * FROM accounts WHERE id=?',(a['id'],))
        self.assertEqual(task['status'],'queued');self.assertEqual(task['account'],'')
        self.assertEqual(account['status'],'limited');self.assertAlmostEqual(account['cooldown']-time.time(),7200,delta=3)
    def test_cancellation_survives_runner_exception(self):
        a=self.client.post('/api/accounts',json={'label':'Cancelled'}).json()
        tid=R.create_task(self.project,'Cancel','test',a['id'])
        S.write("UPDATE tasks SET status='cancelled' WHERE id=?",(tid,))
        with patch.object(R,'profile_env',side_effect=RuntimeError('Cancelled before startup')):
            R.run_task(S.one('SELECT * FROM tasks WHERE id=?',(tid,)),a)
        self.assertEqual(S.one('SELECT status FROM tasks WHERE id=?',(tid,))['status'],'cancelled')
    def test_authorization_code_delivery_and_owner_scope(self):
        from types import SimpleNamespace
        a=self.client.post('/api/accounts',json={'label':'Login'}).json();sent=[]
        console=SimpleNamespace(user=self.project['user'],account=a,intent='login',text='https://accounts.google.com/auth?state=current_state',closed=False,send=sent.append)
        with patch.dict(R.CONSOLES,{'test-console':console}):
            self.assertEqual(self.other.get('/api/terminals/test-console').status_code,404)
            self.assertEqual(self.other.post('/api/terminals/test-console/authorize',json={'code':'test-code'}).status_code,404)
            self.assertEqual(self.client.post('/api/terminals/test-console/authorize',json={'code':'https://antigravity.google/oauth-callback?state=stale&code=test-code'}).status_code,400)
            self.assertEqual(sent,[])
            r=self.client.post('/api/terminals/test-console/authorize',json={'code':'https://antigravity.google/oauth-callback?state=current_state&code=test-code'})
            self.assertEqual(r.status_code,200);self.assertEqual(sent,['\x15test-code\r'])
            self.assertFalse(S.one("SELECT id FROM events WHERE text LIKE '%test-code%'"))
    def test_authorization_code_rejects_control_characters(self):
        for value in ('test\ncommand','https://evil.example/?code=test','', 'test\x1bcode'):
            with self.assertRaises(ValueError):R.authorization_code(value,'')
        self.assertEqual(R.authorization_code('  4/test-authorization-code  ',''),'4/test-authorization-code')


class CredentialTests(unittest.TestCase):
    def test_private_service_with_standard_client(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(S,'ROOT',Path(directory)):
                env=R.profile_env({'id':'credential-test'})
                script='''
import os,secretstorage
bus=secretstorage.dbus_init()
collection=secretstorage.get_default_collection(bus)
item=collection.create_item('test',{'service':'kokic-test'},b'test-only-secret',replace=True)
assert item.get_secret()==b'test-only-secret'
assert len(list(collection.search_items({'service':'kokic-test'})))==1
item.delete()
assert not list(collection.search_items({'service':'kokic-test'}))
'''
                try:
                    p=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=15)
                    self.assertEqual(p.returncode,0,p.stderr)
                    self.assertNotIn(b'test-only-secret',(R.profile_home({'id':'credential-test'})/'credentials.enc').read_bytes())
                    env2=R.profile_env({'id':'credential-other'})
                    self.assertNotEqual(env['DBUS_SESSION_BUS_ADDRESS'],env2['DBUS_SESSION_BUS_ADDRESS'])
                    self.assertNotEqual(env['HOME'],env2['HOME'])
                finally:
                    for key in ('credential-test','credential-other'):
                        proc=R.PROFILES.pop(key,None)
                        if proc:R.terminate(proc);proc.wait(timeout=5)


if __name__=='__main__':unittest.main()
