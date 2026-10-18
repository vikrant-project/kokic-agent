import base64,io,json,time,unittest,zipfile
from pathlib import Path
import test_system
from kokic import store as S,runtime as R,operations as O

class OperationsTests(unittest.TestCase):
    setUp=test_system.PortalTests.setUp
    tearDown=test_system.PortalTests.tearDown

    def test_memory_is_scoped_and_bounded(self):
        path='/api/ops/projects/'+self.project['id']
        self.assertEqual(self.other.get(path).status_code,404)
        self.assertEqual(self.client.put(path+'/memory',json={'content':'Use accessible purple controls.'}).status_code,200)
        self.assertEqual(self.client.get(path).json()['memory'],'Use accessible purple controls.')
        self.assertEqual(self.client.put(path+'/memory',json={'content':'a'*12001}).status_code,400)

    def test_secrets_encrypted_redacted_and_environment_guarded(self):
        pid=self.project['id'];url='/api/ops/projects/'+pid
        value='synthetic-secret-value-1234'
        self.assertEqual(self.client.post(url+'/secrets',json={'name':'SERVICE_API_KEY','value':value}).status_code,200)
        entry=S.one('SELECT * FROM vault WHERE project=?',(pid,))
        self.assertNotIn(value,entry['value'])
        self.assertNotIn(value,self.client.get(url).text)
        self.assertEqual(O.task_secrets(self.project)['SERVICE_API_KEY'],value)
        S.emit(self.project['user'],'API key '+value)
        self.assertNotIn(value,S.one('SELECT text FROM events ORDER BY id DESC LIMIT 1')['text'])
        for name in ['PATH','LD_PRELOAD','KOKIC_URL','BASH_ENV','PYTHONPATH','GIT_CONFIG_COUNT']:
            self.assertEqual(self.client.post(url+'/secrets',json={'name':name,'value':value}).status_code,400)
        self.assertEqual(self.other.delete(url+'/secrets/'+entry['id']).status_code,404)

    def test_schedule_exactly_once_and_owner_scope(self):
        pid=self.project['id'];url='/api/ops/projects/'+pid+'/schedule'
        self.assertEqual(self.other.post(url,json={'prompt':'x','due':time.time()+60}).status_code,404)
        result=self.client.post(url,json={'prompt':'Scheduled test','due':time.time()+60});self.assertEqual(result.status_code,200)
        sid=result.json()['id'];S.write('UPDATE schedules SET due=? WHERE id=?',(time.time()-1,sid))
        O.dispatch_schedules();first=S.one('SELECT * FROM schedules WHERE id=?',(sid,));O.dispatch_schedules()
        self.assertEqual(first['status'],'dispatched');self.assertEqual(S.one('SELECT task FROM schedules WHERE id=?',(sid,))['task'],first['task'])
        self.assertEqual(len(S.rows('SELECT id FROM tasks WHERE id=?',(first['task'],))),1)

    def test_search_and_notices_do_not_cross_users(self):
        tid=R.create_task(self.project,'Confidential synthetic plan','test')
        O.notice(self.project,{'id':tid},'Review is ready')
        self.assertEqual(len(self.client.get('/api/ops/search?q=Confidential').json()['results']),1)
        self.assertEqual(self.other.get('/api/ops/search?q=Confidential').json()['results'],[])
        self.assertEqual(self.other.get('/api/ops/dashboard').json()['notices'],[])

    def test_backup_excludes_secret_files(self):
        root=R.project_path(self.project)
        for name in ('.env.production','private.key','state.db'):(root/name).write_text('private')
        (root/'app.txt').write_text('public source')
        result=self.client.get('/api/ops/backup');self.assertEqual(result.status_code,200)
        with zipfile.ZipFile(io.BytesIO(result.content)) as z:
            names=z.namelist();self.assertTrue(any(n.endswith('/app.txt') for n in names))
            self.assertFalse(any(n.endswith(('.key','.db','.production')) for n in names))

    def test_account_bootstrap_is_private_and_idempotent(self):
        a=self.client.post('/api/accounts',json={'label':'Fresh profile'}).json();root=R.profile_home(a)
        self.assertTrue((root/'kokic-setup.json').is_file())
        settings=root/'.gemini/antigravity-cli/settings.json';settings.write_text(json.dumps({'permissions':{'allow':['command(my-safe-tool)']}}))
        R.bootstrap_account(a)
        self.assertEqual(json.loads(settings.read_text())['permissions']['allow'],['command(my-safe-tool)'])
        self.assertTrue((root/'.gemini/config/skills/kokic-intent/SKILL.md').is_file())

    def test_version_restore_keeps_history(self):
        root=R.project_path(self.project);original=R.git(root,'rev-parse','HEAD').strip()
        (root/'new.txt').write_text('revision');R.git(root,'add','.');R.git(root,'commit','-m','Add new file')
        before=R.git(root,'rev-parse','HEAD').strip()
        result=self.client.post('/api/ops/projects/'+self.project['id']+'/restore-version',json={'commit':original})
        self.assertEqual(result.status_code,200);self.assertFalse((root/'new.txt').exists())
        self.assertIn(before,R.git(root,'rev-list','main').splitlines())

    def test_encrypted_backup_roundtrip_and_wrong_passphrase(self):
        root=R.project_path(self.project);(root/'hello.txt').write_text('Backup content')
        password='test-backup-only-passphrase'
        response=self.client.post('/api/ops/backup/encrypted',json={'passphrase':password})
        self.assertEqual(response.status_code,200);self.assertNotIn(b'Backup content',response.content)
        data=base64.b64encode(response.content).decode()
        self.assertEqual(self.client.post('/api/ops/backup/restore',json={'passphrase':'wrong-long-passphrase','data':data}).status_code,400)
        restored=self.client.post('/api/ops/backup/restore',json={'passphrase':password,'data':data})
        self.assertEqual(restored.status_code,200)
        p=S.one('SELECT * FROM projects WHERE id=?',(restored.json()['projects'][0],))
        self.assertEqual(p['user'],self.project['user']);self.assertEqual(p['autonomy'],'accept-edits')
        self.assertEqual((R.project_path(p)/'hello.txt').read_text(),'Backup content')

    def test_template_creation_and_validation(self):
        response=self.client.post('/api/projects',json={'name':'Starter','template':'website'})
        self.assertEqual(response.status_code,200);root=R.project_path(response.json())
        self.assertTrue((root/'index.html').is_file());self.assertTrue((root/'app.js').is_file())
        self.assertEqual(self.client.post('/api/projects',json={'name':'Bad','template':'../x'}).status_code,400)
