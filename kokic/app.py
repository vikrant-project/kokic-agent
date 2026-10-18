import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
from pathlib import Path
import secrets
import subprocess
import threading
import time
import zipfile
from fastapi import FastAPI,Request,Response,HTTPException,Depends
from fastapi.responses import FileResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from . import store as S,runtime as R,workflow as W

WEB=Path(__file__).resolve().parent.parent/'web'
SKIP={'.git','node_modules','.venv','__pycache__','.env','.DS_Store'}

@asynccontextmanager
async def lifespan(app):
    S.init();R.start()
    yield
    R.shutdown()

app=FastAPI(title='Kokic Agent',lifespan=lifespan)

@app.middleware('http')
async def origin_guard(request,call_next):
    origin=request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin and origin!=str(request.base_url).rstrip('/'):
        return Response('Invalid origin',status_code=403)
    result=await call_next(request)
    result.headers['X-Content-Type-Options']='nosniff';result.headers['Referrer-Policy']='same-origin'
    if request.url.path.startswith('/api'):result.headers['Cache-Control']='no-store'
    return result

def user(request:Request):
    raw=request.cookies.get('kokic_session','');digest=hashlib.sha256(raw.encode()).hexdigest()
    session=S.one('SELECT s.user,u.email FROM sessions s JOIN users u ON u.id=s.user WHERE s.token=? AND s.expires>?',(digest,time.time()))
    if not session:raise HTTPException(401,'Please sign in')
    return {'id':session['user'],'email':session['email']}

def owned(table,id,u):
    if table not in ('accounts','projects','agents','integrations'):raise ValueError('Invalid collection')
    record=S.one('SELECT * FROM '+table+' WHERE id=? AND user=?',(id,u['id']))
    if not record:raise HTTPException(404,'Not found')
    return record

def project_task(id,u):
    task=S.one('SELECT * FROM tasks WHERE id=?',(id,))
    if not task:raise HTTPException(404,'Task not found')
    project=owned('projects',task['project'],u);return project,task

def text(body,key,maxlen=200000,default=None):
    value=body.get(key,default)
    if not isinstance(value,str) or not value.strip() or len(value)>maxlen:raise HTTPException(400,'Invalid '+key)
    return value.strip()

def signin(response,u):
    token=secrets.token_urlsafe(32)
    S.write('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u,time.time()+43200))
    response.set_cookie('kokic_session',token,max_age=43200,httponly=True,samesite='strict')

@app.get('/api/me')
def me(u=Depends(user)):return u

@app.post('/api/register')
def register(body:dict,response:Response):
    email=text(body,'email',200).lower();password=text(body,'password',200)
    if '@' not in email or len(password)<10:raise HTTPException(400,'Use a valid email and a password of at least 10 characters')
    if S.one('SELECT id FROM users WHERE email=?',(email,)):raise HTTPException(409,'This email is already registered')
    uid=S.create_user(email,password);signin(response,uid);return {'id':uid,'email':email}

@app.post('/api/login')
def login(body:dict,response:Response):
    record=S.one('SELECT * FROM users WHERE email=?',(str(body.get('email','')).lower(),))
    time.sleep(.2)
    if not record or not hmac.compare_digest(S.password_hash(str(body.get('password','')),record['password'].split(':')[0]),record['password']):raise HTTPException(401,'Invalid email or password')
    signin(response,record['id']);return {'id':record['id'],'email':record['email']}

@app.post('/api/logout')
def logout(request:Request,response:Response,u=Depends(user)):
    S.write('DELETE FROM sessions WHERE token=?',(hashlib.sha256(request.cookies.get('kokic_session','').encode()).hexdigest(),));response.delete_cookie('kokic_session');return {'ok':True}

def public_account(a):
    return {**a,'models':json.loads(a['models']),'quota':json.loads(a['quota']),'setup_ready':(R.profile_home(a)/'kokic-setup.json').is_file()}

@app.get('/api/state')
def state(u=Depends(user)):
    return {'accounts':[public_account(a) for a in S.rows('SELECT * FROM accounts WHERE user=?',(u['id'],))],
      'projects':S.rows('SELECT * FROM projects WHERE user=? ORDER BY created DESC',(u['id'],)),
      'agents':S.rows('SELECT * FROM agents WHERE user=?',(u['id'],)),
      'integrations':S.rows('SELECT id,user,name,kind,enabled,created FROM integrations WHERE user=?',(u['id'],))+[{'id':p.parent.name,'name':p.parent.name,'kind':'skill','enabled':1,'builtin':True,'content':p.read_text()} for p in (WEB.parent/'skills').glob('*/SKILL.md')],
      'consoles':[{'id':c.id,'account':c.account['id'],'intent':c.intent,'closed':c.closed} for c in R.CONSOLES.values() if c.user==u['id']],
      'running':S.rows("SELECT t.id,t.project,t.title,t.account FROM tasks t JOIN projects p ON p.id=t.project WHERE p.user=? AND t.status='running'",(u['id'],))}

@app.get('/api/events')
async def events(request:Request,after:int=0,u=Depends(user)):
    async def stream():
        cursor=after
        while not await request.is_disconnected():
            records=S.rows('SELECT * FROM events WHERE user=? AND id>? ORDER BY id LIMIT 250',(u['id'],cursor))
            for record in records:
                cursor=record['id'];yield 'id: '+str(cursor)+'\ndata: '+json.dumps(record)+'\n\n'
            if not records:yield ': keepalive\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

@app.post('/api/accounts')
def add_account(body:dict,u=Depends(user)):
    id=S.uid();S.write('INSERT INTO accounts(id,user,label) VALUES (?,?,?)',(id,u['id'],text(body,'label',80)))
    R.bootstrap_account(owned('accounts',id,u))
    return public_account(owned('accounts',id,u))

@app.put('/api/accounts/{id}')
def update_account(id:str,body:dict,u=Depends(user)):
    owned('accounts',id,u)
    if 'model' in body:S.write('UPDATE accounts SET model=? WHERE id=?',(text(body,'model',100),id))
    if 'label' in body:S.write('UPDATE accounts SET label=? WHERE id=?',(text(body,'label',80),id))
    return {'ok':True}

@app.post('/api/accounts/model-all')
def model_all(body:dict,u=Depends(user)):
    model=text(body,'model',100);S.write('UPDATE accounts SET model=? WHERE user=?',(model,u['id']));return {'ok':True}

@app.post('/api/accounts/{id}/console')
def account_console(id:str,body:dict,u=Depends(user)):
    account=owned('accounts',id,u);intent=body.get('intent','login')
    if intent not in ('login','usage','configure'):raise HTTPException(400,'Invalid terminal intent')
    active=next((c for c in R.CONSOLES.values() if c.account['id']==id and not c.closed),None)
    if active:return {'channel':active.id,'existing':True}
    if S.one("SELECT id FROM tasks WHERE account=? AND status='running'",(id,)):raise HTTPException(409,'This account has a running task')
    try:console=R.Console(u['id'],account,intent=intent)
    except Exception as e:raise HTTPException(400,str(e))
    return {'channel':console.id}

@app.post('/api/terminals/{id}/input')
def terminal_input(id:str,body:dict,u=Depends(user)):
    c=R.CONSOLES.get(id)
    if not c or c.user!=u['id']:raise HTTPException(404,'Terminal not found')
    try:c.send(str(body.get('data',''))[:16000])
    except Exception as e:raise HTTPException(409,str(e))
    return {'ok':True}

@app.get('/api/terminals/{id}')
def terminal_state(id:str,u=Depends(user)):
    c=R.CONSOLES.get(id)
    if not c or c.user!=u['id']:raise HTTPException(404,'Terminal not found')
    account=owned('accounts',c.account['id'],u)
    return {'text':c.text,'closed':c.closed,'status':account['status'],'email':account['email']}

@app.post('/api/terminals/{id}/authorize')
def terminal_authorize(id:str,body:dict,u=Depends(user)):
    c=R.CONSOLES.get(id)
    if not c or c.user!=u['id']:raise HTTPException(404,'Terminal not found')
    if c.intent not in ('login','configure','usage'):raise HTTPException(400,'Not a sign-in terminal')
    if not re.search(r'accounts\.google\.com',c.text):raise HTTPException(409,'Start Google OAuth in this terminal first')
    try:
        code=R.authorization_code(str(body.get('code','')),c.text)
        c.send('\x15'+code+'\r')
    except ValueError as exc:raise HTTPException(400,str(exc))
    except RuntimeError as exc:raise HTTPException(409,str(exc))
    return {'ok':True}

@app.post('/api/terminals/{id}/resize')
def terminal_resize(id:str,body:dict,u=Depends(user)):
    c=R.CONSOLES.get(id)
    if not c or c.user!=u['id']:raise HTTPException(404,'Terminal not found')
    c.resize(int(body.get('rows',30)),int(body.get('cols',100)));return {'ok':True}

@app.post('/api/terminals/{id}/close')
def terminal_close(id:str,u=Depends(user)):
    c=R.CONSOLES.get(id)
    if not c or c.user!=u['id']:raise HTTPException(404,'Terminal not found')
    c.close();return {'ok':True}

@app.post('/api/accounts/{id}/refresh')
def refresh_account(id:str,u=Depends(user)):
    account=owned('accounts',id,u)
    def refresh():
        try:
            env=R.profile_env(account);p=subprocess.run([str(R.AGY),'models'],env=env,cwd=str(R.profile_home(account)),capture_output=True,text=True,timeout=30)
            models=[{'id':line.split('\t',1)[0].strip(),'name':line.split('\t',1)[1].strip()} for line in p.stdout.splitlines() if '\t' in line and line.startswith(('gemini-','claude-','gpt-'))]
            if p.returncode or not models:raise RuntimeError('Sign in through the account terminal first. '+p.stderr[-300:])
            S.write("UPDATE accounts SET models=?,checked=?,error='' WHERE id=?",(json.dumps(models),time.time(),id))
            S.emit(u['id'],'Model catalog refreshed for '+account['label'],kind='account')
        except Exception as e:S.write('UPDATE accounts SET error=? WHERE id=?',(str(e)[:600],id))
    threading.Thread(target=refresh,daemon=True).start();return {'ok':True}

@app.post('/api/accounts/{id}/retry')
def retry_account(id:str,u=Depends(user)):
    account=owned('accounts',id,u)
    if not account['email']:raise HTTPException(400,'Complete Google sign-in first')
    if account['cooldown']>time.time():raise HTTPException(409,'The reported quota reset time has not arrived')
    S.write("UPDATE accounts SET status='connected',error='',cooldown=0 WHERE id=?",(id,))
    return {'ok':True}

@app.post('/api/accounts/{id}/quota')
def account_quota(id:str,u=Depends(user)):
    account=owned('accounts',id,u)
    if not account['email']:raise HTTPException(400,'Complete Google sign-in first')
    if not R.refresh_quota(account):raise HTTPException(409,'A quota check, terminal, or task is already using this account. Close its terminal or wait for it to finish.')
    return {'ok':True}

@app.post('/api/projects')
def create_project(body:dict,u=Depends(user)):
    from .templates import STARTERS
    template=body.get('template','blank')
    if not isinstance(template,str) or template not in STARTERS:raise HTTPException(400,'Unknown project template')
    project={'id':S.uid(),'user':u['id'],'name':text(body,'name',100),'description':str(body.get('description',''))[:3000],'created':time.time(),'autonomy':'autonomous'}
    R.init_project(project)
    S.write('INSERT INTO projects(id,user,name,description,created,autonomy) VALUES (?,?,?,?,?,?)',tuple(project[k] for k in ('id','user','name','description','created','autonomy')))
    if STARTERS[template]['files']:
        root=R.project_path(project)
        for name,content in STARTERS[template]['files'].items():(root/name).write_text(content)
        R.git(root,'add','.');R.git(root,'commit','-m','Add '+STARTERS[template]['name']+' starter')
    return project

@app.get('/api/projects/{id}')
def detail(id:str,thread:str='main',u=Depends(user)):
    project=owned('projects',id,u)
    tasks=S.rows('SELECT * FROM tasks WHERE project=? AND thread=? ORDER BY created',(id,thread))
    for task in tasks:
        task.pop('token',None);task['artifacts']=W.artifacts(Path(task['path'])) if task['path'] and Path(task['path']).exists() else []
    messages=S.rows('SELECT * FROM messages WHERE project=? AND thread=? ORDER BY id DESC LIMIT 300',(id,thread))[::-1]
    for message in messages:
        message['files']=[item for uid in json.loads(message['attachments']) if (item:=S.one('SELECT id,name,size FROM uploads WHERE id=? AND project=?',(uid,id)))]
    return {'project':project,'tasks':tasks,'threads':[{'id':'main','title':'Main chat'}]+S.rows('SELECT * FROM threads WHERE project=? ORDER BY created',(id,)),
      'messages':messages,
      'goals':S.rows('SELECT * FROM goals WHERE project=? ORDER BY created DESC',(id,))}

@app.post('/api/projects/{id}/threads')
def add_thread(id:str,body:dict,u=Depends(user)):
    owned('projects',id,u);tid=S.uid();S.write('INSERT INTO threads VALUES (?,?,?,?)',(tid,id,text(body,'title',80),time.time()));return {'id':tid}

@app.post('/api/projects/{id}/uploads')
def upload(id:str,body:dict,u=Depends(user)):
    owned('projects',id,u);name=Path(text(body,'name',250).replace('\\','/')).name
    if name in ('.','..','') or any(ord(c)<32 for c in name):raise HTTPException(400,'Invalid filename')
    encoded=body.get('data','')
    if not isinstance(encoded,str) or len(encoded)>28_000_000:raise HTTPException(413,'Attachment limit is 20 MB')
    try:content=base64.b64decode(encoded,validate=True)
    except ValueError:raise HTTPException(400,'Invalid attachment encoding')
    if len(content)>20_000_000:raise HTTPException(413,'Attachment limit is 20 MB')
    uid=S.uid();folder=S.ROOT/'uploads'/id/uid;folder.mkdir(parents=True);path=folder/name;path.write_bytes(content);extracted=''
    try:
        if path.suffix.lower()=='.pdf':
            from pypdf import PdfReader
            extracted='\n'.join((p.extract_text() or '')[:15000] for p in list(PdfReader(path).pages)[:30])[:100000]
        elif path.suffix.lower()=='.docx':
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(path) as doc:
                if doc.getinfo('word/document.xml').file_size>2_000_000:raise ValueError('Document XML too large')
                extracted=' '.join(ET.fromstring(doc.read('word/document.xml')).itertext())[:100000]
        elif path.suffix.lower() in ('.txt','.md','.json','.csv','.py','.js','.ts','.html','.css','.xml','.yaml','.yml','.log'):
            extracted=content[:300000].decode('utf-8',errors='replace')[:100000]
    except Exception as exc:extracted='[Text extraction unavailable: '+type(exc).__name__+'. Original file remains attached.]'
    S.write('INSERT INTO uploads VALUES (?,?,?,?,?,?,?)',(uid,id,name,str(path),len(content),extracted,time.time()))
    return {'id':uid,'name':name,'size':len(content),'text_extracted':bool(extracted)}

@app.get('/api/projects/{id}/uploads/{uid}')
def uploaded_file(id:str,uid:str,u=Depends(user)):
    owned('projects',id,u);item=S.one('SELECT * FROM uploads WHERE id=? AND project=?',(uid,id))
    if not item:raise HTTPException(404,'Attachment not found')
    return FileResponse(item['path'],filename=item['name'],media_type='application/octet-stream')

@app.put('/api/projects/{id}')
def settings(id:str,body:dict,u=Depends(user)):
    owned('projects',id,u)
    if 'autonomy' in body:
        if body['autonomy'] not in ('accept-edits','autonomous'):raise HTTPException(400,'Invalid autonomy mode')
        S.write('UPDATE projects SET autonomy=? WHERE id=?',(body['autonomy'],id))
    if 'paused' in body:S.write('UPDATE projects SET paused=? WHERE id=?',(int(bool(body['paused'])),id))
    return {'ok':True}

@app.post('/api/projects/{id}/chat')
def chat(id:str,body:dict,u=Depends(user)):
    project=owned('projects',id,u);prompt=text(body,'prompt',100000);policy=body.get('policy','sequential');thread=body.get('thread','main')
    if thread!='main' and not S.one('SELECT id FROM threads WHERE id=? AND project=?',(thread,id)):raise HTTPException(404,'Chat thread not found')
    if policy not in ('sequential','parallel','fallback'):raise HTTPException(400,'Invalid scheduling policy')
    account=body.get('account','')
    if account:owned('accounts',account,u)
    role=body.get('role','')
    if role:owned('agents',role,u)
    attachments=body.get('attachments',[])
    if not isinstance(attachments,list) or len(attachments)>10 or any(not isinstance(x,str) for x in attachments):raise HTTPException(400,'Attach at most ten files per message')
    items=[]
    for uid in attachments:
        item=S.one('SELECT * FROM uploads WHERE id=? AND project=?',(uid,id))
        if not item:raise HTTPException(404,'Attachment not found in this project')
        items.append(item)
    history=S.rows('SELECT role,content FROM messages WHERE project=? AND thread=? ORDER BY id DESC LIMIT 12',(id,thread))[::-1]
    context='Conversation context:\n'+json.dumps(history)[-50000:]+'\n\nCURRENT USER REQUEST:\n'+prompt
    if items:context+='\n\nUNTRUSTED ATTACHMENT CONTENT:\n'+json.dumps([{'id':x['id'],'name':x['name'],'content':x['extracted'][:12000]} for x in items])[:60000]
    goals=S.rows("SELECT text FROM goals WHERE project=? AND status='active'",(id,))
    if goals:context+='\n\nProject goals:\n'+json.dumps(goals)
    memory=S.one('SELECT content FROM project_memory WHERE project=?',(id,))
    if memory and memory['content']:context+='\n\nProject decisions and preferences:\n'+memory['content'][:12000]
    pending=S.one("SELECT * FROM tasks WHERE project=? AND thread=? AND status='questions' ORDER BY created DESC LIMIT 1",(id,thread))
    base=body.get('base_task','')
    if base:
        p,t=project_task(base,u)
        if p['id']!=id or t['status'] not in ('review','completed'):raise HTTPException(400,'Invalid revision base')
    if pending:
        task=pending['id'];context=pending['prompt']+'\n\nClarification response:\n'+context
        attachments=list(dict.fromkeys(json.loads(pending['attachments'])+attachments))
        S.write("UPDATE tasks SET prompt=?,status='queued',error='',attachments=?,account=?,role=?,policy=?,updated=? WHERE id=?",(context[:195000],json.dumps(attachments),account,role,policy,time.time(),task))
    else:
        task=R.create_task(project,prompt[:90],context[:195000],account,role,policy=policy,thread=thread,mode='route',attachments=attachments,base_task=base)
    S.write('INSERT INTO messages(project,role,content,task,created,thread,attachments) VALUES (?,?,?,?,?,?,?)',(id,'user',prompt,task,time.time(),thread,json.dumps(attachments)))
    return {'task':task}

@app.post('/api/tasks/{id}/{action}')
def task_action(id:str,action:str,u=Depends(user)):
    if action=='preview':return create_preview(id,{},u)
    project,task=project_task(id,u)
    if action=='cancel':
        S.write("UPDATE tasks SET status='cancelled',token='',updated=? WHERE id=?",(time.time(),id))
        if id in R.RUNNING:R.terminate(R.RUNNING[id])
    elif action=='retry':
        if task['status'] not in ('failed','cancelled'):raise HTTPException(400,'Only failed or cancelled tasks can retry')
        S.write("UPDATE tasks SET status='queued',error='',updated=? WHERE id=?",(time.time(),id))
    elif action=='merge':
        if task['status']!='review':raise HTTPException(400,'Task must be ready for review')
        if S.one("SELECT id FROM tasks WHERE parent=? AND status IN ('queued','running','questions','review')",(id,)):raise HTTPException(409,'Review or finish the child tasks before finalizing this result')
        with R.MUTEX:
            try:R.git(R.project_path(project),'merge','--no-ff','--no-edit',task['branch'])
            except Exception as exc:
                R.git(R.project_path(project),'merge','--abort',check=False);raise HTTPException(409,'Merge conflict. Inspect the task worktree and resolve before merging. '+str(exc)[:500])
        S.write("UPDATE tasks SET status='completed',phase='finalized',updated=? WHERE id=?",(time.time(),id))
    else:raise HTTPException(404,'Unknown action')
    S.emit(u['id'],action+': '+task['title'],project=project['id'],task=id,kind='task');return {'ok':True}

def code_root(project,task,u):
    if task:
        p,t=project_task(task,u)
        if p['id']!=project['id'] or not t['path']:raise HTTPException(404,'Worktree not ready')
        return Path(t['path'])
    return R.project_path(project)

def workspace_files(root):
    for directory,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in SKIP and not (Path(directory)/d).is_symlink())
        for name in sorted(names):
            p=Path(directory)/name
            if name not in SKIP and not name.startswith('.env.') and p.suffix.lower() not in ('.key','.pem','.p12','.db','.sqlite','.sqlite3') and not p.is_symlink() and p.is_file():yield p

@app.get('/api/projects/{id}/files')
def files(id:str,task:str='',u=Depends(user)):
    root=code_root(owned('projects',id,u),task,u);result=[]
    for p in workspace_files(root):
        rel=p.relative_to(root)
        if p.is_symlink() or any(part in SKIP for part in rel.parts):continue
        if p.is_file():result.append({'path':str(rel),'size':p.stat().st_size})
        if len(result)>=3000:break
    return {'files':result}

@app.get('/api/projects/{id}/file')
def read_file(id:str,path:str,task:str='',u=Depends(user)):
    root=code_root(owned('projects',id,u),task,u)
    try:p=S.safe_path(root,path)
    except ValueError:raise HTTPException(403,'Path outside workspace')
    if any(part in SKIP for part in Path(path).parts) or any(part in SKIP for part in p.relative_to(root.resolve()).parts) or not p.is_file():raise HTTPException(404,'File not found')
    if p.stat().st_size>1_000_000:raise HTTPException(413,'File too large for the editor')
    return {'path':path,'content':p.read_text(errors='replace')}

@app.put('/api/projects/{id}/file')
def save_file(id:str,body:dict,u=Depends(user)):
    project=owned('projects',id,u);root=code_root(project,body.get('task',''),u)
    if body.get('task') and S.one('SELECT status FROM tasks WHERE id=?',(body['task'],))['status']=='running':raise HTTPException(409,'Wait for the agent to finish before editing its worktree')
    try:p=S.safe_path(root,text(body,'path',500))
    except ValueError:raise HTTPException(403,'Path outside workspace')
    if any(part in SKIP for part in p.relative_to(root).parts):raise HTTPException(403,'This file is excluded')
    content=body.get('content','')
    if not isinstance(content,str) or len(content)>1_000_000:raise HTTPException(413,'File too large')
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
    R.git(root,'add','--',str(p.relative_to(root)));R.git(root,'commit','-m','Edit '+str(p.relative_to(root)),check=False)
    return {'ok':True}

@app.get('/api/tasks/{id}/diff')
def diff(id:str,u=Depends(user)):
    project,task=project_task(id,u)
    return {'diff':R.git(task['path'],'diff','main...HEAD')[:250000] if task['path'] else ''}

@app.get('/api/tasks/{id}/artifact/{path:path}')
def artifact_file(id:str,path:str,inline:bool=False,u=Depends(user)):
    project,task=project_task(id,u)
    if not task['path']:raise HTTPException(404,'No generated files yet')
    root=Path(task['path']).resolve()
    try:p=S.safe_path(root,path)
    except ValueError:raise HTTPException(403,'Path outside workspace')
    if not p.is_file() or any(part in SKIP or part=='attachments' for part in p.relative_to(root).parts):raise HTTPException(404,'Artifact not found')
    if inline and p.suffix.lower() not in ('.pdf','.png','.jpg','.jpeg','.webp','.txt','.md'):inline=False
    response=FileResponse(p,filename=None if inline else p.name,content_disposition_type='inline' if inline else 'attachment')
    if p.suffix.lower()!='.pdf':response.headers['Content-Security-Policy']="sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return response

@app.post('/api/tasks/{id}/preview')
def create_preview(id:str,body:dict,u=Depends(user)):
    project,task=project_task(id,u)
    if not task['path']:raise HTTPException(404,'Worktree not ready')
    root=Path(task['path']).resolve()
    candidates=[root/'dist/index.html',root/'build/index.html',root/'out/index.html',root/'index.html']
    index=next((p for p in candidates if p.is_file() and p.resolve().is_relative_to(root) and not p.is_symlink()),None)
    if not index:raise HTTPException(404,'No built index.html yet. Ask the agent to produce a static preview build.')
    lease=secrets.token_urlsafe(32)
    S.write('INSERT INTO preview_leases VALUES (?,?,?,?)',(hashlib.sha256(lease.encode()).hexdigest(),id,str(index.parent),time.time()+3600))
    return {'url':'/preview/'+lease+'/index.html','expires_in':3600,'visibility':'local preview; anyone with this temporary link can view this build'}

@app.get('/preview/{lease}/{path:path}')
def preview_file(lease:str,path:str):
    record=S.one('SELECT * FROM preview_leases WHERE token=? AND expires>?',(hashlib.sha256(lease.encode()).hexdigest(),time.time()))
    if not record:raise HTTPException(404,'Preview link expired or invalid')
    root=Path(record['root']).resolve()
    try:p=S.safe_path(root,path or 'index.html')
    except ValueError:raise HTTPException(403,'Outside preview')
    if p.is_dir():
        p=(p/'index.html').resolve()
        if not p.is_relative_to(root):raise HTTPException(403,'Outside preview')
    if not p.is_file() or any(part in SKIP for part in p.relative_to(root).parts) or p.stat().st_size>50_000_000:raise HTTPException(404,'Preview asset not found')
    if p.suffix in ('.html','.css','.js') and p.stat().st_size<5_000_000:
        prefix='/preview/'+lease+'/'
        content=p.read_text(errors='replace')
        if p.suffix=='.html':content=re.sub(r'((?:src|href|poster)=["\'])/(?!/)',lambda m:m[1]+prefix,content)
        content=content.replace('"/assets/','"'+prefix+'assets/').replace("'/assets/","'"+prefix+'assets/')
        if p.suffix=='.css':content=re.sub(r'(url\(["\']?)/(?!/)',lambda m:m[1]+prefix,content)
        response=Response(content,media_type=mimetypes.guess_type(str(p))[0] or 'text/plain')
    else:response=FileResponse(p)
    response.headers['Access-Control-Allow-Origin']='*'
    response.headers['Content-Security-Policy']="sandbox allow-scripts; default-src * data: blob:; script-src * 'unsafe-inline' 'unsafe-eval'; style-src * 'unsafe-inline'; frame-ancestors 'self'"
    response.headers['Referrer-Policy']='no-referrer'
    return response

@app.get('/api/projects/{id}/download')
def download(id:str,task:str='',u=Depends(user)):
    project=owned('projects',id,u);root=code_root(project,task,u);buffer=io.BytesIO();total=0
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for p in workspace_files(root):
            rel=p.relative_to(root)
            if p.is_symlink() or not p.is_file() or any(part in SKIP for part in rel.parts):continue
            if not p.resolve().is_relative_to(root.resolve()):continue
            total+=p.stat().st_size
            if total>100_000_000:raise HTTPException(413,'Archive exceeds 100 MB')
            archive.write(p,str(rel))
    buffer.seek(0);return StreamingResponse(buffer,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="kokic-project.zip"'})

@app.post('/api/projects/{id}/goals')
def goal(id:str,body:dict,u=Depends(user)):
    owned('projects',id,u);gid=S.uid();S.write('INSERT INTO goals(id,project,text,created) VALUES (?,?,?,?)',(gid,id,text(body,'text',4000),time.time()));return {'id':gid}

@app.put('/api/goals/{id}')
def update_goal(id:str,body:dict,u=Depends(user)):
    goal=S.one('SELECT * FROM goals WHERE id=?',(id,))
    if not goal:raise HTTPException(404,'Goal not found')
    owned('projects',goal['project'],u)
    if body.get('status') not in ('active','completed','paused'):raise HTTPException(400,'Invalid status')
    S.write('UPDATE goals SET status=? WHERE id=?',(body['status'],id));return {'ok':True}

@app.post('/api/agents')
def agent(body:dict,u=Depends(user)):
    id=body.get('id') or S.uid()
    if body.get('id'):owned('agents',id,u)
    S.write('INSERT OR REPLACE INTO agents VALUES (?,?,?,?,?,?)',(id,u['id'],text(body,'name',80),str(body.get('description',''))[:500],text(body,'instructions',20000),str(body.get('model',''))[:100]));return {'id':id}

@app.post('/api/integrations')
def integration(body:dict,u=Depends(user)):
    kind=body.get('kind')
    if kind not in ('skill','rule','mcp','connector','plugin'):raise HTTPException(400,'Invalid integration kind')
    config=body.get('config',{})
    if not isinstance(config,dict) or len(json.dumps(config))>100000:raise HTTPException(400,'Invalid configuration')
    id=body.get('id') or S.uid()
    if body.get('id'):owned('integrations',id,u)
    S.write('INSERT OR REPLACE INTO integrations VALUES (?,?,?,?,?,?,?)',(id,u['id'],text(body,'name',80),kind,S.seal(config),int(body.get('enabled',True)),time.time()));return {'id':id}

@app.delete('/api/integrations/{id}')
def delete_integration(id:str,u=Depends(user)):
    owned('integrations',id,u);S.write('DELETE FROM integrations WHERE id=?',(id,));return {'ok':True}

@app.post('/api/accounts/{id}/install-plugin')
def plugin(id:str,body:dict,u=Depends(user)):
    account=owned('accounts',id,u);source=text(body,'source',500)
    if S.one("SELECT id FROM tasks WHERE account=? AND status='running'",(id,)) or any(c.account['id']==id and not c.closed for c in R.CONSOLES.values()):raise HTTPException(409,'Close the active terminal or wait for this account task to finish')
    if source.startswith('-'):raise HTTPException(400,'Invalid plugin source')
    console=R.Console(u['id'],account,command=[str(R.AGY),'plugin','install',source],intent='plugin');return {'channel':console.id}

@app.post('/api/mcp')
def mcp_call(body:dict,request:Request):
    token=request.headers.get('Authorization','').removeprefix('Bearer ')
    task=S.one("SELECT * FROM tasks WHERE token=? AND status='running'",(hashlib.sha256(token.encode()).hexdigest(),))
    if not task or not token:raise HTTPException(401,'Task token invalid or expired')
    project=S.one('SELECT * FROM projects WHERE id=?',(task['project'],));name=body.get('name');a=body.get('arguments',{})
    if name=='team':return {'accounts':[{k:x[k] for k in ('id','label','model','status')} for x in S.rows('SELECT * FROM accounts WHERE user=?',(project['user'],))]}
    if name=='delegate':
        account=S.one('SELECT * FROM accounts WHERE id=? AND user=?',(a.get('account'),project['user']))
        if not account or account['status']!='connected' or account['id']==task['account']:raise HTTPException(400,'Delegate to a different connected account')
        return {'id':R.create_task(project,text(a,'title',150),text(a,'prompt'),account['id'],parent=task['id'],policy='parallel',thread=task.get('thread','main'))}
    target=S.one('SELECT * FROM tasks WHERE id=? AND project=?',(a.get('id'),project['id']))
    if not target:raise HTTPException(404,'Task not found in this project')
    if name=='task_status':return {k:target[k] for k in ('id','title','status','result','error','branch')}
    if name=='merge_child':
        if target['parent']!=task['id'] or target['status']!='review':raise HTTPException(400,'Only a completed child worktree can be merged')
        try:R.git(task['path'],'merge','--no-ff','--no-edit',target['branch'])
        except Exception as exc:R.git(task['path'],'merge','--abort',check=False);raise HTTPException(409,str(exc))
        S.write("UPDATE tasks SET status='completed',updated=? WHERE id=?",(time.time(),target['id']))
        S.emit(project['user'],'Merged child work into coordinator: '+target['title'],project=project['id'],task=task['id'],kind='task');return {'ok':True}
    raise HTTPException(400,'Unsupported MCP tool')

@app.get('/')
def index():return FileResponse(WEB/'index.html')

from .operations import register as register_operations
register_operations(app,user,owned,project_task)
app.mount('/assets',StaticFiles(directory=WEB),name='assets')
