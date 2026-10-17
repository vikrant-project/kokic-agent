import codecs
from collections import deque
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from . import store as S

SOURCE=Path(__file__).resolve().parent
WORKER_TOOLS=['view_file','list_dir','find_by_name','grep_search','write_to_file','replace_file_content','multi_replace_file_content','run_command','command_status','send_command_input','call_mcp_tool','finish','read_url_content','search_web','generate_image']
AGY=Path(os.environ.get('KOKIC_AGY',str(Path.home()/'.local/bin/agy')))
PROFILES={};CONSOLES={};RUNNING={};MUTEX=threading.RLock();STOP=threading.Event()
ANSI=re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]')

def reported_reset(line,now=None):
    now=time.time() if now is None else now
    value=re.search(r'reset(?:s)?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2}))',line,re.I)
    if value:
        try:return datetime.fromisoformat(value[1].replace('Z','+00:00')).timestamp()
        except ValueError:return None
    value=re.search(r'reset(?:s)?\s+in\s+((?:\d+\s*(?:h(?:ours?)?|m(?:inutes?)?|s(?:econds?)?)\s*)+)',line,re.I)
    if value:
        seconds=sum(int(n)*{'h':3600,'m':60,'s':1}[unit.lower()[0]] for n,unit in re.findall(r'(\d+)\s*(h(?:ours?)?|m(?:inutes?)?|s(?:econds?)?)',value[1],re.I))
        return now+seconds
    return None

def profile_home(account):return S.ROOT/'profiles'/account['id']

def profile_env(account):
    home=profile_home(account);home.mkdir(parents=True,exist_ok=True,mode=0o700)
    ready=home/'bus.json'
    with MUTEX:
        record=PROFILES.get(account['id'])
        if not record or record.poll() is not None:
            if ready.exists():
                try:
                    previous=json.loads(ready.read_text());pid=int(previous['pid']);cmd=Path('/proc')/str(pid)/'cmdline'
                    if cmd.exists() and str(home).encode() in cmd.read_bytes() and b'profile_service.py' in cmd.read_bytes():os.kill(pid,signal.SIGTERM)
                except (OSError,ValueError,KeyError):pass
            ready.unlink(missing_ok=True)
            env=dict(os.environ);env['HOME']=str(home);env['KOKIC_PARENT_PID']=str(os.getpid());env.pop('DBUS_SESSION_BUS_ADDRESS',None)
            log=(home/'credential-service.log').open('ab')
            record=subprocess.Popen(['dbus-run-session','--',sys.executable,str(SOURCE/'profile_service.py'),str(home)],env=env,stdout=log,stderr=log,start_new_session=True)
            log.close();PROFILES[account['id']]=record
            for _ in range(100):
                if ready.exists():break
                if record.poll() is not None:raise RuntimeError('Private credential service failed to start; inspect account diagnostics')
                time.sleep(.05)
            else:raise RuntimeError('Private credential service startup timed out')
        bus=json.loads(ready.read_text())
    env=dict(os.environ)
    for name in list(env):
        if name.startswith(('GEMINI_','GOOGLE_','ANTHROPIC_','AGY_')) or name in ('DBUS_SESSION_BUS_ADDRESS','GNOME_KEYRING_CONTROL','SSH_AUTH_SOCK'):env.pop(name,None)
    env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'.config'),XDG_DATA_HOME=str(home/'.local/share'),XDG_CACHE_HOME=str(home/'.cache'),DBUS_SESSION_BUS_ADDRESS=bus['address'],TERM='xterm-256color',COLORTERM='truecolor',SSH_CONNECTION='127.0.0.1 1 127.0.0.1 1',PYTHONUNBUFFERED='1')
    return env

def terminate(proc):
    if proc.poll() is None:
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass

class Console:
    def __init__(self,user,account,command=None,intent='login'):
        self.id=S.uid();self.user=user;self.account=account;self.intent=intent;self.text='';self.closed=False;self.autosent=False
        env=profile_env(account);master,slave=pty.openpty();self.fd=master
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',50,140,0,0))
        self.proc=subprocess.Popen(command or [str(AGY)],env=env,cwd=str(profile_home(account)),stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave);CONSOLES[self.id]=self
        S.emit(user,'Terminal connected',channel=self.id,kind='terminal_status')
        threading.Thread(target=self.read,daemon=True).start()
    def read(self):
        decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while self.proc.poll() is None:
                if not select.select([self.fd],[],[],.5)[0]:continue
                try:chunk=os.read(self.fd,16384)
                except OSError:break
                if not chunk:break
                text=decoder.decode(chunk);self.text=(self.text+text)[-150000:]
                S.emit(self.user,text,channel=self.id,kind='terminal')
                clean=ANSI.sub('',self.text)
                email=re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',clean)
                if email and ('Gemini' in clean or re.search(r'authenticated successfully|signed in as',clean,re.I)):
                    S.write("UPDATE accounts SET email=?,status='connected',error='',checked=? WHERE id=?",(email[-1],time.time(),self.account['id']))
                    if self.intent=='usage' and not self.autosent:self.send('/usage\r');self.autosent=True
                if self.intent=='usage':self.parse_usage(clean)
        finally:
            self.closed=True
            try:os.close(self.fd)
            except OSError:pass
            S.emit(self.user,'Terminal exited',channel=self.id,kind='terminal_status')
    def parse_usage(self,text):
        quotas={}
        # CLI 1.4 reports shared model groups with two independent windows.
        clean=text.replace('\r','\n').replace('\b','')
        reports={}
        headings=[('gemini','GEMINI MODELS'),('claude_gpt','CLAUDE AND GPT MODELS')]
        for group,heading in headings:
            start=clean.rfind(heading)
            if start<0:continue
            section=clean[start+len(heading):]
            next_heading=re.search(r'\n(?:GEMINI MODELS|CLAUDE AND GPT MODELS)',section)
            if next_heading:section=section[:next_heading.start()]
            windows={}
            for key,label in [('weekly','Weekly'),('five_hour','Five Hour')]:
                match=re.search(label+r'\s+Limit Remaining\s*[^%]{0,250}?(\d{1,3}(?:\.\d+)?)%\s*Refreshes in\s+((?:\d+\s*[hms]\s*)+)',section,re.I)
                if match and 0<=float(match[1])<=100:
                    windows[key]={'remaining_pct':float(match[1]),'reset_at':reported_reset('resets in '+match[2]),'refreshes_in':match[2].strip()}
            if len(windows)==2:
                quotas[group]={'remaining_pct':min(w['remaining_pct'] for w in windows.values()),'windows':windows,'source':'CLI /usage','group':heading.title()}
                reports[group]={k:(v['remaining_pct'],v['refreshes_in']) for k,v in windows.items()}
        if not quotas:
            for line in text.splitlines():
                match=re.search(r'(Gemini[^\n]{0,75}?|Claude[^\n]{0,75}?|GPT[^\n]{0,75}?)\s+(\d{1,3}(?:\.\d+)?)\s*%',line,re.I)
                if match and 0<=float(match[2])<=100:
                    quotas[match[1].strip()]={'remaining_pct':float(match[2]),'reset_at':reported_reset(line),'source':'CLI /usage','reported_line':line.strip()}
            reports=quotas
        fingerprint=json.dumps(reports,sort_keys=True)
        if quotas and fingerprint!=getattr(self,'last_quota_report',''):
            self.last_quota_report=fingerprint
            self.quota_complete='gemini' in quotas and 'claude_gpt' in quotas
            S.write("UPDATE accounts SET quota=?,quota_checked=?,quota_status='ready',quota_error='' WHERE id=?",(json.dumps(quotas),time.time(),self.account['id']))
            S.emit(self.user if hasattr(self,'user') else self.account['user'],'Quota updated',kind='account')
    def send(self,text):
        if self.closed:raise RuntimeError('This terminal has exited')
        os.write(self.fd,text.encode())
    def resize(self,rows,cols):
        if not self.closed:fcntl.ioctl(self.fd,termios.TIOCSWINSZ,struct.pack('HHHH',max(8,min(rows,80)),max(30,min(cols,240)),0,0))
    def close(self):terminate(self.proc)

def authorization_code(value,terminal_text):
    from urllib.parse import urlsplit,parse_qs
    value=value.strip()
    if value.startswith(('https://','http://')):
        url=urlsplit(value)
        if url.scheme!='https' or url.hostname!='antigravity.google' or url.path!='/oauth-callback':raise ValueError('Paste the code or the official Antigravity callback URL')
        params=parse_qs(url.query);value=params.get('code',[''])[0]
        returned_state=params.get('state',[''])[0]
        states=re.findall(r'(?:[?&])state=([a-zA-Z0-9_-]+)',terminal_text)
        if returned_state and (not states or returned_state!=states[-1]):raise ValueError('This callback belongs to a different sign-in attempt. Use the current Google link.')
    if not value or len(value)>8192 or any(ord(c)<33 or ord(c)>126 for c in value):raise ValueError('Enter a valid authorization code without spaces or line breaks')
    return value

def git(path,*args,check=True):
    p=subprocess.run(['git','-C',str(path),*args],capture_output=True,text=True,timeout=45)
    if check and p.returncode:raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p.stdout

def project_path(project):return S.ROOT/'projects'/project['id']/'source'

def init_project(project):
    root=project_path(project);root.mkdir(parents=True,exist_ok=True)
    git(root,'init','-b','main');git(root,'config','user.name','Kokic Agent');git(root,'config','user.email','agent@kokic.local')
    (root/'README.md').write_text('# '+project['name']+'\n\n'+project.get('description','')+'\n')
    (root/'.gitignore').write_text('node_modules/\n.venv/\n__pycache__/\n.env\n')
    git(root,'add','.');git(root,'commit','-m','Initialize project')
    return root

def available_accounts(user):
    busy={r['account'] for r in S.rows("SELECT account FROM tasks WHERE status='running'")}
    busy|={c.account['id'] for c in CONSOLES.values() if not c.closed}
    busy|=QUOTA_JOBS
    return [a for a in S.rows("SELECT * FROM accounts WHERE user=? AND status='connected' ORDER BY checked DESC",(user,)) if a['id'] not in busy and a['cooldown']<=time.time() and quota_allows(a)]

def quota_allows(account):
    group='gemini' if account['model'].startswith('gemini-') else 'claude_gpt'
    value=json.loads(account['quota']).get(group,{})
    if value.get('remaining_pct',1)>0:return True
    exhausted=[w for w in value.get('windows',{}).values() if w['remaining_pct']<=0]
    return bool(exhausted) and all(w.get('reset_at') is not None and w['reset_at']<=time.time() for w in exhausted)

def create_task(project,title,prompt,account='',role='',parent='',policy='sequential',thread='main',mode='build',attachments=None,base_task=''):
    if len(prompt)>200000:raise ValueError('Task instructions exceed 200000 characters')
    tid=S.uid();now=time.time()
    S.write('INSERT INTO tasks(id,project,parent,title,prompt,account,role,created,updated,policy,thread,mode,phase,attachments,base_task) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(tid,project['id'],parent,title[:150],prompt,account,role,now,now,policy,thread,mode,'understanding' if mode=='route' else 'building',json.dumps(attachments or []),base_task))
    S.emit(project['user'],'Queued: '+title,project=project['id'],task=tid,kind='task')
    return tid

def configure_profile(account,task,project):
    from .workflow import IDENTITY
    home=profile_home(account);config=home/'.gemini/config';config.mkdir(parents=True,exist_ok=True)
    settings=home/'.gemini/antigravity-cli/settings.json';settings.parent.mkdir(parents=True,exist_ok=True)
    native=json.loads(settings.read_text()) if settings.exists() else {}
    permissions=native.setdefault('permissions',{});allow=permissions.setdefault('allow',[])
    permission_manifest=home/'kokic-permissions.json'
    previous=json.loads(permission_manifest.read_text()) if permission_manifest.exists() else []
    allow[:]=[rule for rule in allow if rule not in previous]
    scoped=['mcp(kokic/'+tool+')' for tool in ('team','delegate','task_status','merge_child')]
    scoped.append('read_file('+str(S.ROOT/'projects'/project['id'])+')')
    for rule in scoped:
        if rule not in allow:allow.append(rule)
    permission_manifest.write_text(json.dumps(scoped))
    settings.write_text(json.dumps(native,indent=2));settings.chmod(0o600)
    manifest=config/'kokic-managed.json'
    for relative in json.loads(manifest.read_text()) if manifest.exists() else []:
        S.safe_path(config,relative).unlink(missing_ok=True)
    managed=[]
    role=S.one('SELECT * FROM agents WHERE id=? AND user=?',(task['role'],project['user']))
    instructions=role['instructions'] if role else 'Complete the assigned task, implement the necessary code, and verify your changes.'
    agent=config/'agents/kokic-worker/agent.md';agent.parent.mkdir(parents=True,exist_ok=True)
    bundled=SOURCE.parent/'skills'
    workflow='\n\n'.join(p.read_text() for p in sorted(bundled.glob('*/SKILL.md')))
    agent.write_text('---\nname: kokic-worker\ndescription: Kokic project worker\nmainAgent: true\nsubagent: true\ninheritCustomizations: true\nexcludeDefaultComponents: true\ntools: '+json.dumps(WORKER_TOOLS)+'\n---\n\n'+IDENTITY+'\n'+instructions+'\n'+workflow)
    rules=config/'rules/kokic-workflow.md';rules.parent.mkdir(parents=True,exist_ok=True)
    rules.write_text(IDENTITY+'\n\n'+instructions+'\n\n'+workflow)
    for skill in bundled.glob('*/SKILL.md'):
        target=config/'skills'/skill.parent.name/'SKILL.md';target.parent.mkdir(parents=True,exist_ok=True);target.write_text(skill.read_text())
        managed.append(str(target.relative_to(config)))
    mcp={'mcpServers':{'kokic':{'command':sys.executable,'args':[str(SOURCE/'mcp_server.py')]}}}
    if len(S.rows("SELECT id FROM accounts WHERE user=? AND status='connected'",(project['user'],)))<2:mcp['mcpServers'].clear()
    for integration in S.rows('SELECT * FROM integrations WHERE user=? AND enabled=1',(project['user'],)):
        value=S.unseal(integration['config']);name=re.sub(r'[^a-zA-Z0-9_-]','-',integration['name'])[:60]
        if integration['kind'] in ('mcp','connector') and value.get('server'):mcp['mcpServers'][name]=value['server']
        elif integration['kind']=='skill':
            path=config/'skills'/name/'SKILL.md';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value.get('content',''))
            managed.append(str(path.relative_to(config)))
        elif integration['kind']=='rule':
            path=config/'rules'/(name+'.md');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value.get('content',''))
            managed.append(str(path.relative_to(config)))
    manifest.write_text(json.dumps(managed))
    (config/'mcp_config.json').write_text(json.dumps(mcp,indent=2));(config/'mcp_config.json').chmod(0o600)
    return role['model'] if role and role['model'] else account['model']

def run_task(task,account):
    project=S.one('SELECT * FROM projects WHERE id=?',(task['project'],));proc=None;stderr_worker=None;timer=None;last_output='';result='';success=False;errors=deque(maxlen=40);response_parts=[]
    try:
        from . import workflow as W
        if W.prepare(task,account,project):return
        env=profile_env(account);model=configure_profile(account,task,project)
        path=S.ROOT/'projects'/project['id']/'worktrees'/task['id'];branch='task/'+task['id']
        with MUTEX:
            base=S.one('SELECT branch FROM tasks WHERE id=? AND project=?',(task.get('base_task',''),project['id']))
            if not path.exists():git(project_path(project),'worktree','add','-b',branch,str(path),base['branch'] if base and base['branch'] else 'main')
        if task.get('mode')=='route':W.work_files(path,task)
        lease=secrets.token_urlsafe(32)
        S.write('UPDATE tasks SET path=?,branch=?,token=? WHERE id=?',(str(path),branch,hashlib.sha256(lease.encode()).hexdigest(),task['id']))
        env['KOKIC_TASK_TOKEN']=lease;env['KOKIC_URL']=os.environ.get('KOKIC_URL','http://127.0.0.1:8787')
        command=[str(AGY),'--agent','default','--model',model,'--input-format','stream-json','--output-format','stream-json']
        if project['autonomy']=='autonomous':command+=['--dangerously-skip-permissions']
        else:command+=['--mode','accept-edits']
        team=[{'id':a['id'],'label':a['label'],'model':a['model']} for a in S.rows("SELECT * FROM accounts WHERE user=? AND status='connected'",(project['user'],))]
        prompt=(W.IDENTITY+' You are working inside an isolated Git worktree for Kokic Agent. Implement the requested result here. '
          'Planning has already finished. You are now the builder: create and edit the requested project files. This request is NOT a native background task. Do not use manage_task, poll yourself, or search runtime storage for a task identifier. '
          'Use list_dir for directories, view_file for existing files, and write_to_file to create missing files. A missing output file is expected before creation; do not repeatedly try to read it. '
          'Do not inspect CLI settings, brain storage, parent directories, or your own task status. Kokic handles runtime configuration and Git commits. '
          'The team list below is authoritative. If it contains only your account, build locally without MCP coordination calls. '
          'Save files using native file tools. If file-writing bindings are unavailable (not permission-denied), return JSON {"summary":"...","files":[{"path":"relative/path","content":"complete file text"}]} so Kokic can store the proposed files for review. Never claim files are saved merely because you printed code. '
          'Read PLAN.md and WORK.md when present. Improve the plan as necessary; update WORK.md with actual work and checks. For websites produce a runnable build and an index.html in dist/ when possible. For documents produce the requested real file, with a PDF review copy where tools permit. For APKs compile a real APK when Android tooling is installed; report missing tooling rather than renaming another file to .apk. '
          'The portal serves local previews and download links from files you actually create. Do not claim public deployment. Stop at review; the user must approve before finalization. '
          'Do not modify other projects or account credentials. Kokic will commit the completed changes for review. '
          'You may use Kokic MCP tools to delegate independent subtasks to OTHER accounts, inspect their results, and merge their completed branches into this worktree. '
          'Do not delegate to your own account while you are waiting for a child task. If only one account is connected, do the task yourself. '
          'Verify the result and summarize actual checks and limitations.\nYour account (only for excluding yourself from delegation): '+account['id']+'\nConnected account list: '+json.dumps(team)+'\n\nIMPLEMENT THIS REQUEST:\n'+task['prompt'])
        proc=subprocess.Popen(command,cwd=str(path),env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',start_new_session=True)
        RUNNING[task['id']]=proc
        def stderr():
            for line in proc.stderr:
                errors.append(line[-2000:]);S.emit(project['user'],line,project=project['id'],task=task['id'],kind='stderr')
        stderr_worker=threading.Thread(target=stderr,daemon=True);stderr_worker.start()
        timer=threading.Timer(1800,lambda:terminate(proc));timer.daemon=True;timer.start()
        proc.stdin.write(json.dumps({'event':'user','message':{'content':prompt}})+'\n');proc.stdin.close()
        for line in proc.stdout:
            last_output=(last_output+line)[-20000:]
            try:event=json.loads(line)
            except ValueError:S.emit(project['user'],line,project=project['id'],task=task['id'],kind='stdout');continue
            if event.get('event')=='step_update':
                step=event.get('step_update',{});kind=step.get('step_type','step')
                if kind=='agent_response':text=step.get('text_delta','');response_parts.append(text)
                elif kind=='tool':text=json.dumps({'tool':step.get('tool_name'),'state':step.get('state'),**step.get('tool_info',{})},ensure_ascii=False)
                else:text=''
                if text:S.emit(project['user'],text,project=project['id'],task=task['id'],kind=kind)
            elif event.get('event')=='result':
                final=event.get('result',{});success=final.get('status')=='SUCCESS';result=final.get('response','') or str(final.get('error',''))
            elif event.get('event')=='init':S.emit(project['user'],json.dumps(event,ensure_ascii=False),project=project['id'],task=task['id'],kind='init')
        code=proc.wait(timeout=10);timer.cancel();success=success and code==0
        stderr_worker.join(timeout=2)
        if S.one('SELECT status FROM tasks WHERE id=?',(task['id'],))['status']=='cancelled':return
        result=result or ''.join(response_parts).strip()
        if re.search(r'no output produced|headless mode cannot prompt', ''.join(errors),re.I):raise RuntimeError('CLI was blocked by a tool permission. '+''.join(errors)[-1800:])
        if success and not result:raise RuntimeError('CLI exited without a final response; inspect the terminal before retrying. No result was marked ready for review.')
        if not success:raise RuntimeError(result or ''.join(errors)[-3000:] or last_output[-2000:] or 'CLI exited without a successful result')
        if task.get('mode')=='route':
            saved,summary=W.materialize_response(path,result)
            if saved:
                result=(summary or 'Your files are ready for review.')+'\n\nSaved files: '+', '.join(saved)+'.'
                S.emit(project['user'],'Stored CLI-proposed files: '+', '.join(saved),project=project['id'],task=task['id'],kind='task')
        if task.get('mode')=='route' and json.loads(task.get('plan') or '{}').get('deliverable')=='website' and not any((path/p).is_file() for p in ('dist/index.html','build/index.html','out/index.html','index.html')):
            raise RuntimeError('The CLI returned text but did not create a website preview build. File contents in a chat reply are not saved deliverables. Inspect the terminal and retry.')
        if task.get('mode')=='route':
            with (path/'WORK.md').open('a') as log:log.write('\n\n## Agent report for review\n\n'+result+'\n\nStatus: awaiting user review.\n')
        git(path,'add','-A');git(path,'commit','--allow-empty','-m','Kokic task '+task['id'])
        commit=git(path,'rev-parse','HEAD').strip()
        S.write("UPDATE tasks SET status='review',phase='review',result=?,commit_hash=?,updated=?,token='' WHERE id=?",(result,commit,time.time(),task['id']))
        S.write('INSERT INTO messages(project,role,content,task,created,thread) VALUES (?,?,?,?,?,?)',(project['id'],'assistant',result,task['id'],time.time(),task.get('thread','main')))
        S.emit(project['user'],'Ready for review: '+task['title'],project=project['id'],task=task['id'],kind='task')
    except Exception as exc:
        if S.one('SELECT status FROM tasks WHERE id=?',(task['id'],))['status']=='cancelled':return
        error=str(exc)[:3000];limited=bool(re.search(r'RESOURCE_EXHAUSTED|quota.{0,30}exceed|rate.limit|429',error,re.I))
        if limited:
            reset=reported_reset(error)
            S.write("UPDATE accounts SET status='limited',error=?,cooldown=? WHERE id=?",(error,reset or 0,account['id']))
        state='queued' if limited and task['policy']=='fallback' else 'failed'
        S.write('UPDATE tasks SET status=?,phase=?,error=?,updated=?,token=?,account=? WHERE id=?',(state,'understanding' if state=='queued' else 'failed',error,time.time(),'', '' if state=='queued' else account['id'],task['id']))
        S.emit(project['user'],error,project=project['id'],task=task['id'],kind='error')
    finally:
        if timer:timer.cancel()
        if proc:
            terminate(proc)
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            if stderr_worker:stderr_worker.join(timeout=2)
            for stream in (proc.stdin,proc.stdout,proc.stderr):
                if stream and not stream.closed:stream.close()
        RUNNING.pop(task['id'],None)

def scheduler():
    while not STOP.wait(1):
        try:
            with MUTEX:
                S.write("UPDATE accounts SET status='connected',cooldown=0,error='' WHERE status='limited' AND cooldown>0 AND cooldown<=?",(time.time(),))
                for task in S.rows("SELECT t.* FROM tasks t JOIN projects p ON p.id=t.project WHERE t.status='queued' AND p.paused=0 ORDER BY t.created"):
                    project=S.one('SELECT * FROM projects WHERE id=?',(task['project'],))
                    if task['policy']!='parallel' and S.one("SELECT id FROM tasks WHERE project=? AND status='running' AND parent=''",(project['id'],)) and not task['parent']:continue
                    accounts=available_accounts(project['user'])
                    chosen=next((a for a in accounts if not task['account'] or a['id']==task['account']),None)
                    if not chosen:continue
                    S.write("UPDATE tasks SET status='running',account=?,attempt=attempt+1,updated=?,error='' WHERE id=?",(chosen['id'],time.time(),task['id']))
                    task['account']=chosen['id']
                    threading.Thread(target=run_task,args=(task,chosen),daemon=True).start()
        except Exception as exc:print('Scheduler:',type(exc).__name__,str(exc),flush=True)

def start():
    STOP.clear()
    S.write("UPDATE tasks SET status='failed',token='',error='Server restarted during execution; retry explicitly' WHERE status='running'")
    threading.Thread(target=scheduler,daemon=True).start()
    threading.Thread(target=quota_scheduler,daemon=True).start()

QUOTA_JOBS=set()

def refresh_quota(account):
    with MUTEX:
        if account['id'] in QUOTA_JOBS:return False
        if S.one("SELECT id FROM tasks WHERE account=? AND status='running'",(account['id'],)) or any(c.account['id']==account['id'] and not c.closed for c in CONSOLES.values()):return False
        QUOTA_JOBS.add(account['id'])
    def worker():
        console=None
        S.write("UPDATE accounts SET quota_status='loading',quota_error='' WHERE id=?",(account['id'],))
        try:
            console=Console(account['user'],account,intent='usage')
            deadline=time.time()+60
            while time.time()<deadline and not STOP.wait(.25):
                if getattr(console,'quota_complete',False):return
                if console.closed:break
            raise RuntimeError('CLI quota check did not return both limit windows. Retry when this account is idle.')
        except Exception as exc:
            S.write("UPDATE accounts SET quota_status='error',quota_error=?,quota_checked=? WHERE id=?",(str(exc)[:500],time.time(),account['id']))
        finally:
            if console:console.close()
            with MUTEX:QUOTA_JOBS.discard(account['id'])
    threading.Thread(target=worker,daemon=True).start();return True

def quota_scheduler():
    while not STOP.wait(5):
        for account in S.rows("SELECT * FROM accounts WHERE status IN ('connected','limited') AND quota_checked<?",(time.time()-300,)):
            refresh_quota(account)

def shutdown():
    STOP.set()
    for proc in list(RUNNING.values()):terminate(proc)
    for console in list(CONSOLES.values()):console.close()
    for proc in list(PROFILES.values()):terminate(proc)
