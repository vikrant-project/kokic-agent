import base64,json,time,unittest,os
from pathlib import Path
from unittest.mock import patch
import test_system
from kokic import store as S,runtime as R,workflow as W


class WorkflowTests(unittest.TestCase):
    setUp=test_system.PortalTests.setUp
    tearDown=test_system.PortalTests.tearDown
    # Reuse the isolated database/client fixture, not the base test methods.
    def test_plan_parser_handles_fences_prose_and_unrelated_braces(self):
        value={'intent':'task','reply':'Plan ready','enhanced_prompt':'Read the supplied goals.md text and plan the work','steps':['Read brief'],'acceptance':[],'questions':[]}
        response='A note {not JSON}.\n```json\n'+json.dumps(value)+'\n```\nAdditional note {also not JSON}'
        self.assertEqual(W.parse_plan(response)['intent'],'task')
        self.assertEqual(W.parse_plan(json.dumps({'intent':'chat','reply':'Hello'}))['reply'],'Hello')
        for bad in ['I will read the file.',json.dumps({'intent':'task','enhanced_prompt':'Build','questions':'none'}),json.dumps({'intent':'chat','reply':''})]:
            with self.assertRaises(W.PlanningFormatError):W.parse_plan(bad)

    def test_planning_format_repair_is_automatic_and_bounded(self):
        task={'id':'format-test','prompt':'Read goals.md'}
        good={'intent':'task','enhanced_prompt':'Read attached goals','steps':[],'questions':[]}
        with patch.object(W,'_response_once',side_effect=[W.PlanningFormatError('I need to read it'),good]) as run:
            self.assertEqual(W.response_turn(task,{},self.project),good)
            self.assertEqual(run.call_count,2);self.assertEqual(run.call_args.args[3],'I need to read it')
        with patch.object(W,'_response_once',side_effect=W.PlanningFormatError('plain text')) as run:
            with self.assertRaisesRegex(RuntimeError,'No build was started'):W.response_turn(task,{},self.project)
            self.assertEqual(run.call_count,2)

    def test_planner_uses_streamed_json_and_attachment_context_without_tools(self):
        a=self.client.post('/api/accounts',json={'label':'Stream planner'}).json()
        plan={'intent':'task','reply':'Plan ready','enhanced_prompt':'Summarize attached goals.md','steps':['Read supplied text'],'questions':[]}
        fake=Path(self.temp.name)/'stream-planner'
        fake.write_text('#!/usr/bin/env python3\nimport sys,json\np=json.loads(sys.stdin.readline())["message"]["content"]\nassert "The only goal is a local document" in p\nassert "do not call tools" in p\nprint(json.dumps({"event":"step_update","step_update":{"step_type":"agent_response","text_delta":'+repr(json.dumps(plan))+'}}))\nprint(json.dumps({"event":"result","result":{"status":"SUCCESS","response":""}}))\n');fake.chmod(0o700)
        task={'id':'stream-test','prompt':'Read goals.md. Extracted text: The only goal is a local document'}
        with patch.object(R,'AGY',fake),patch.object(R,'profile_env',return_value=dict(os.environ)):
            value=W.response_turn(task,a,self.project)
        self.assertEqual(value['enhanced_prompt'],plan['enhanced_prompt'])

    def test_chat_finishes_without_build_or_worktree(self):
        a=self.client.post('/api/accounts',json={'label':'Conversation'}).json()
        tid=self.client.post('/api/projects/'+self.project['id']+'/chat',json={'prompt':'hello'}).json()['task']
        S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,))
        with patch.object(W,'response_turn',return_value={'intent':'chat','reply':'Hello! I am Kokic Agent.','questions':[]}),patch.object(R,'profile_env') as env:
            R.run_task(S.one('SELECT * FROM tasks WHERE id=?',(tid,)),a)
            env.assert_not_called()
        row=S.one('SELECT * FROM tasks WHERE id=?',(tid,));self.assertEqual(row['phase'],'chat');self.assertEqual(row['status'],'completed');self.assertEqual(row['path'],'')
        self.assertIn('Kokic Agent',self.client.get('/api/projects/'+self.project['id']).json()['messages'][-1]['content'])
    def test_questions_resume_same_task_and_refine_context(self):
        a=self.client.post('/api/accounts',json={'label':'Planner'}).json();pid=self.project['id']
        tid=self.client.post('/api/projects/'+pid+'/chat',json={'prompt':'Build a business site'}).json()['task']
        S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,))
        answer={'intent':'task','reply':'I need one detail.','enhanced_prompt':'Build a business site','steps':['Clarify industry'],'questions':[{'question':'Which industry?','options':['Cafe','Consulting']} ]}
        with patch.object(W,'response_turn',return_value=answer):R.run_task(S.one('SELECT * FROM tasks WHERE id=?',(tid,)),a)
        self.assertEqual(S.one('SELECT status FROM tasks WHERE id=?',(tid,))['status'],'questions')
        reply=self.client.post('/api/projects/'+pid+'/chat',json={'prompt':'A cafe, with a menu and contact page'}).json()
        self.assertEqual(reply['task'],tid)
        row=S.one('SELECT * FROM tasks WHERE id=?',(tid,));self.assertEqual(row['status'],'queued');self.assertIn('A cafe',row['prompt'])
    def test_thread_context_and_upload_ownership(self):
        pid=self.project['id'];thread=self.client.post('/api/projects/'+pid+'/threads',json={'title':'Documents'}).json()['id']
        upload=self.client.post('/api/projects/'+pid+'/uploads',json={'name':'brief.md','data':base64.b64encode(b'Build a cafe menu with tea and coffee.').decode()})
        self.assertEqual(upload.status_code,200);uid=upload.json()['id']
        self.assertEqual(self.other.get('/api/projects/'+pid+'/uploads/'+uid).status_code,404)
        tid=self.client.post('/api/projects/'+pid+'/chat',json={'prompt':'Use the attached brief','thread':thread,'attachments':[uid]}).json()['task']
        row=S.one('SELECT * FROM tasks WHERE id=?',(tid,));self.assertIn('tea and coffee',row['prompt'])
        self.assertEqual(self.client.get('/api/projects/'+pid).json()['messages'],[])
        detail=self.client.get('/api/projects/'+pid,params={'thread':thread}).json();self.assertEqual(detail['messages'][0]['files'][0]['name'],'brief.md')
        self.assertEqual(self.client.post('/api/projects/'+pid+'/chat',json={'prompt':'x','attachments':['unknown']}).status_code,404)
    def test_preview_lease_boundaries_and_downloads(self):
        tid=R.create_task(self.project,'Preview','test');root=R.project_path(self.project);(root/'dist/assets').mkdir(parents=True)
        (root/'dist/index.html').write_text('<h1>Review me</h1><script type="module" src="/assets/app.js"></script>')
        (root/'dist/assets/app.js').write_text('document.body.dataset.ready="yes"')
        (root/'dist/.env').write_text('private')
        S.write("UPDATE tasks SET path=?,status='review' WHERE id=?",(str(root),tid))
        lease=self.client.post('/api/tasks/'+tid+'/preview',json={});self.assertEqual(lease.status_code,200)
        url=lease.json()['url'];page=self.other.get(url)
        self.assertEqual(page.status_code,200);self.assertIn('sandbox allow-scripts',page.headers['Content-Security-Policy']);self.assertIn(url.rsplit('/',1)[0]+'/assets/app.js',page.text)
        self.assertEqual(self.other.get(url.replace('index.html','.env')).status_code,404)
        self.assertEqual(self.other.get('/api/tasks/'+tid+'/artifact/dist/index.html').status_code,404)
        self.assertEqual(self.client.get('/api/tasks/'+tid+'/artifact/dist/index.html').status_code,200)
        S.write('UPDATE preview_leases SET expires=0');self.assertEqual(self.client.get(url).status_code,404)
    def test_plan_work_files_and_original_attachment_copy(self):
        pid=self.project['id'];item=self.client.post('/api/projects/'+pid+'/uploads',json={'name':'notes.txt','data':base64.b64encode(b'Test attachment').decode()}).json()
        task={'id':'task-files','project':pid,'prompt':'Build this','plan':json.dumps({'enhanced_prompt':'Build a menu','steps':['Implement','Verify'],'acceptance':['Menu works']}),'attachments':json.dumps([item['id']])}
        target=Path(self.temp.name)/'work';target.mkdir();W.work_files(target,task)
        self.assertIn('Build a menu',(target/'PLAN.md').read_text());self.assertTrue((target/'WORK.md').exists());self.assertEqual((target/'attachments'/item['id']/'notes.txt').read_text(),'Test attachment')
    def test_empty_permission_blocked_result_never_enters_review(self):
        a=self.client.post('/api/accounts',json={'label':'Blocked runner'}).json()
        fake=Path(self.temp.name)/'blocked-cli'
        fake.write_text('#!/usr/bin/env python3\nimport sys,json\nsys.stdin.readline()\nprint("no output produced: headless mode cannot prompt for mcp",file=sys.stderr)\nprint(json.dumps({"event":"result","result":{"status":"SUCCESS","response":""}}))\n');fake.chmod(0o700)
        tid=R.create_task(self.project,'Blocked','test',a['id']);S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,))
        with patch.object(R,'AGY',fake),patch.object(R,'profile_env',return_value=dict(os.environ)):
            R.run_task(S.one('SELECT * FROM tasks WHERE id=?',(tid,)),a)
        task=S.one('SELECT * FROM tasks WHERE id=?',(tid,));self.assertEqual(task['status'],'failed');self.assertIn('permission',task['error'])
        native=json.loads((R.profile_home(a)/'.gemini/antigravity-cli/settings.json').read_text())
        expected={'mcp(kokic/'+tool+')' for tool in ('team','delegate','task_status','merge_child')}
        expected.add('read_file('+str(S.ROOT/'projects'/self.project['id'])+')')
        self.assertEqual(set(native['permissions']['allow']),expected)
        agent=(R.profile_home(a)/'.gemini/config/agents/kokic-worker/agent.md').read_text()
        self.assertIn('"write_to_file"',agent);self.assertIn('tools:',agent)
    def test_pdf_text_attachment_and_inline_review(self):
        import io
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=PdfWriter();page=writer.add_blank_page(300,200)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 100 Td (Kokic PDF attachment) Tj ET');page[NameObject('/Contents')]=writer._add_object(stream)
        buffer=io.BytesIO();writer.write(buffer);data=buffer.getvalue();pid=self.project['id']
        uploaded=self.client.post('/api/projects/'+pid+'/uploads',json={'name':'brief.pdf','data':base64.b64encode(data).decode()}).json()
        self.assertTrue(uploaded['text_extracted']);self.assertIn('Kokic PDF attachment',S.one('SELECT extracted FROM uploads WHERE id=?',(uploaded['id'],))['extracted'])
        root=R.project_path(self.project);(root/'review.pdf').write_bytes(data);tid=R.create_task(self.project,'PDF','test')
        S.write('UPDATE tasks SET path=? WHERE id=?',(str(root),tid))
        response=self.client.get('/api/tasks/'+tid+'/artifact/review.pdf?inline=true')
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['Content-Type'],'application/pdf');self.assertEqual(response.content,data)
    def test_cli_proposed_files_are_stored_with_path_validation(self):
        root=R.project_path(self.project)
        saved,summary=W.materialize_response(root,json.dumps({'summary':'Website created','files':[{'path':'dist/index.html','content':'<h1>Review</h1>'}]}))
        self.assertEqual(saved,['dist/index.html']);self.assertEqual(summary,'Website created')
        self.assertEqual((root/'dist/index.html').read_text(),'<h1>Review</h1>')
        with self.assertRaises(ValueError):W.materialize_response(root,json.dumps({'files':[{'path':'okay.txt','content':'safe'},{'path':'../outside.txt','content':'no'}]}))
        self.assertFalse((root/'okay.txt').exists())
        with self.assertRaises(ValueError):W.materialize_response(root,json.dumps({'files':[{'path':'.git/config','content':'no'}]}))
    def test_named_fenced_files_are_materialized_only_when_missing(self):
        root=R.project_path(self.project)
        response='### `dist/index.html`\n\n```html\n<h1>Live review</h1>\n```\n'
        saved,_=W.materialize_response(root,response);self.assertEqual(saved,['dist/index.html'])
        saved,_=W.materialize_response(root,response);self.assertEqual(saved,[])
    def test_build_pipeline_saves_cli_file_proposals_before_review(self):
        a=self.client.post('/api/accounts',json={'label':'Proposal runner'}).json()
        proposal=json.dumps({'summary':'Ready for review','files':[{'path':'dist/index.html','content':'<h1>A real preview file</h1>'}]})
        fake=Path(self.temp.name)/'proposal-cli'
        fake.write_text('#!/usr/bin/env python3\nimport sys,json\nassert "--dangerously-skip-permissions" in sys.argv, "Autonomous worker missing native permission flag"\nsys.stdin.readline()\nprint(json.dumps({"event":"result","result":{"status":"SUCCESS","response":'+repr(proposal)+'}}))\n');fake.chmod(0o700)
        tid=R.create_task(self.project,'Build website','Create a page',a['id'],mode='route');S.write("UPDATE tasks SET status='running' WHERE id=?",(tid,))
        plan={'intent':'task','reply':'Building now','enhanced_prompt':'Create a page','deliverable':'website','steps':['Create HTML'],'questions':[]}
        with patch.object(W,'response_turn',return_value=plan),patch.object(R,'AGY',fake),patch.object(R,'profile_env',return_value=dict(os.environ)):
            R.run_task(S.one('SELECT * FROM tasks WHERE id=?',(tid,)),a)
        task=S.one('SELECT * FROM tasks WHERE id=?',(tid,));self.assertEqual(task['status'],'review',task['error'])
        self.assertEqual((Path(task['path'])/'dist/index.html').read_text(),'<h1>A real preview file</h1>')
        self.assertTrue((Path(task['path'])/'PLAN.md').exists());self.assertTrue((Path(task['path'])/'WORK.md').exists())



