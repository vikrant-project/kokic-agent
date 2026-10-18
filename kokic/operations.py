"""Owner-scoped local workspace operations. No external credentials are bundled."""
import hashlib
import base64
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import zipfile
from fastapi import Depends,HTTPException
from fastapi.responses import StreamingResponse
from . import store as S,runtime as R,workflow as W
from cryptography.fernet import Fernet,InvalidToken

def archive_sources(u):
    from .app import workspace_files
    data=io.BytesIO();manifest=[];total=0
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
        for p in S.rows('SELECT * FROM projects WHERE user=?',(u['id'],)):
            root=R.project_path(p);memory=S.one('SELECT content FROM project_memory WHERE project=?',(p['id'],))
            manifest.append({'id':p['id'],'name':p['name'],'description':p['description'],'memory':memory['content'] if memory else ''})
            for f in workspace_files(root):
                if not f.is_file() or f.is_symlink() or not f.resolve().is_relative_to(root.resolve()):continue
                total+=f.stat().st_size
                if total>20_000_000:raise HTTPException(413,'Portable backup exceeds 20 MB')
                z.write(f,p['id']+'/'+str(f.relative_to(root)))
        z.writestr('manifest.json',json.dumps(manifest,indent=2))
    return data.getvalue()

def backup_cipher(password,salt):
    if not isinstance(password,str) or not 12<=len(password)<=1000:raise HTTPException(400,'Backup passphrase must contain 12–1000 characters')
    return Fernet(base64.urlsafe_b64encode(hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)))

def notice(project,task,text):
    S.write('INSERT INTO notices(id,user,project,task,text,created) VALUES (?,?,?,?,?,?)',(S.uid(),project['user'],project['id'],task['id'],text,time.time()))

def task_secrets(project):
    return {v['name']:S.unseal(v['value'])['value'] for v in S.rows('SELECT * FROM vault WHERE project=? AND user=?',(project['id'],project['user']))}

def checkpoint(project,task,account,reason):
    current=S.one('SELECT * FROM tasks WHERE id=?',(task['id'],)) or task
    summary='Previous attempt stopped: '+reason[:600]+'\n'
    if current.get('path') and Path(current['path']).is_dir():
        path=Path(current['path'])
        summary+='Working tree changes (preserved in place):\n'+R.git(path,'status','--short',check=False)[:5000]
        work=path/'WORK.md'
        if work.is_file() and not work.is_symlink():summary+='\nRecent work log:\n'+work.read_text(errors='replace')[-8000:]
    summary=S.redact(project['user'],summary)
    S.write('INSERT INTO checkpoints VALUES (?,?,?,?,?,?)',(S.uid(),task['id'],account['id'],S.redact(project['user'],reason[:500]),summary,time.time()))
    S.write('DELETE FROM checkpoints WHERE task=? AND id NOT IN (SELECT id FROM checkpoints WHERE task=? ORDER BY created DESC LIMIT 30)',(task['id'],task['id']))

def dispatch_schedules():
    # Scheduler holds the process-wide mutex; claim and task creation commit together.
    with S.LOCK,S.db() as c:
        for job in c.execute("SELECT s.* FROM schedules s JOIN projects p ON p.id=s.project WHERE s.status='pending' AND s.due<=? AND p.paused=0",(time.time(),)).fetchall():
            tid=S.uid();now=time.time()
            c.execute("INSERT INTO tasks(id,project,parent,title,prompt,account,role,created,updated,policy,mode,phase) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(tid,job['project'],'',job['prompt'][:150],job['prompt'],'','',now,now,'fallback','route','understanding'))
            c.execute("UPDATE schedules SET status='dispatched',task=? WHERE id=?",(tid,job['id']))

def register(app,user,owned,project_task):
    @app.get('/api/ops/dashboard')
    def dashboard(u=Depends(user)):
        tasks=S.rows('SELECT t.id,t.title,t.project,t.status,t.phase,t.account,t.created,t.updated,t.attempt FROM tasks t JOIN projects p ON p.id=t.project WHERE p.user=? ORDER BY t.created DESC LIMIT 500',(u['id'],))
        counts={s:sum(t['status']==s for t in tasks) for s in ('queued','running','questions','review','completed','failed','cancelled')}
        return {'tasks':tasks,'counts':counts,'notices':S.rows('SELECT * FROM notices WHERE user=? ORDER BY created DESC LIMIT 50',(u['id'],)),
          'schedules':S.rows('SELECT * FROM schedules WHERE user=? ORDER BY due DESC LIMIT 100',(u['id'],)),
          'capabilities':{'docker_installed':bool(shutil.which('docker')),'git':bool(shutil.which('git')),'cli':R.AGY.is_file()},
          'usage_note':'Durations and attempts are local observations. Provider token counts and monetary costs are not inferred.'}

    @app.get('/api/ops/search')
    def search(q:str='',u=Depends(user)):
        q=q.strip()[:150]
        if len(q)<2:return {'results':[]}
        # Bound results; escape LIKE metacharacters for literal search.
        term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        results=S.rows("SELECT t.id,t.project,t.title AS text,'task' AS type FROM tasks t JOIN projects p ON p.id=t.project WHERE p.user=? AND (t.title LIKE ? ESCAPE '\\' OR t.prompt LIKE ? ESCAPE '\\') LIMIT 40",(u['id'],term,term))
        results+=S.rows("SELECT m.id,m.project,substr(m.content,1,300) AS text,'message' AS type FROM messages m JOIN projects p ON p.id=m.project WHERE p.user=? AND m.content LIKE ? ESCAPE '\\' LIMIT 40",(u['id'],term))
        from .app import workspace_files
        scanned=0
        for p in S.rows('SELECT * FROM projects WHERE user=?',(u['id'],)):
            root=R.project_path(p)
            for f in workspace_files(root):
                scanned+=1
                if scanned>1000:break
                if f.stat().st_size>500000:continue
                try:content=f.read_text()
                except (UnicodeError,OSError):continue
                index=content.lower().find(q.lower())
                if index>=0:results.append({'id':str(f.relative_to(root)),'project':p['id'],'type':'file','text':str(f.relative_to(root))+': '+content[max(0,index-50):index+200]})
                if len(results)>=100:break
            if scanned>1000 or len(results)>=100:break
        return {'results':results}

    @app.post('/api/ops/notices/read')
    def read_notices(body:dict,u=Depends(user)):
        S.write('UPDATE notices SET read=1 WHERE user=?',(u['id'],));return {'ok':True}

    @app.get('/api/ops/projects/{id}')
    def project_ops(id:str,u=Depends(user)):
        p=owned('projects',id,u);memory=S.one('SELECT content FROM project_memory WHERE project=?',(id,))
        history=R.git(R.project_path(p),'log','-30','--format=%H%x09%ct%x09%s').strip().splitlines()
        return {'memory':memory['content'] if memory else '', 'history':[dict(zip(('commit','created','subject'),x.split('\t',2))) for x in history],
          'secrets':S.rows('SELECT id,name,created FROM vault WHERE user=? AND project=?',(u['id'],id)),
          'checkpoints':S.rows('SELECT c.* FROM checkpoints c JOIN tasks t ON t.id=c.task WHERE t.project=? ORDER BY c.created DESC LIMIT 20',(id,)),
          'artifacts':[{'task':t['id'],'title':t['title'],**a} for t in S.rows("SELECT * FROM tasks WHERE project=? AND status IN ('review','completed')",(id,)) if t['path'] for a in W.artifacts(Path(t['path']))]}

    @app.put('/api/ops/projects/{id}/memory')
    def memory(id:str,body:dict,u=Depends(user)):
        owned('projects',id,u);value=body.get('content','')
        if not isinstance(value,str) or len(value)>12000:raise HTTPException(400,'Memory limit: 12000 characters')
        S.write('INSERT INTO project_memory VALUES (?,?) ON CONFLICT(project) DO UPDATE SET content=excluded.content',(id,value));return {'ok':True}

    @app.post('/api/ops/projects/{id}/secrets')
    def secret(id:str,body:dict,u=Depends(user)):
        owned('projects',id,u);name=body.get('name','');value=body.get('value','')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,79}',name) or name.startswith(('KOKIC_','LD_','PYTHON','GIT_','XDG_','DBUS_')) or name in ('HOME','PATH','SHELL','BASH_ENV','ENV','NODE_OPTIONS'):raise HTTPException(400,'Use a service-specific uppercase environment variable name')
        if not isinstance(value,str) or not 8<=len(value)<=10000:raise HTTPException(400,'Secret must be 8–10000 characters')
        S.write('INSERT INTO vault VALUES (?,?,?,?,?,?) ON CONFLICT(project,name) DO UPDATE SET value=excluded.value',(S.uid(),u['id'],id,name,S.seal({'value':value}),time.time()))
        return {'ok':True}

    @app.delete('/api/ops/projects/{id}/secrets/{sid}')
    def delete_secret(id:str,sid:str,u=Depends(user)):
        owned('projects',id,u);S.write('DELETE FROM vault WHERE id=? AND project=? AND user=?',(sid,id,u['id']));return {'ok':True}

    @app.post('/api/ops/projects/{id}/schedule')
    def schedule(id:str,body:dict,u=Depends(user)):
        owned('projects',id,u);prompt=body.get('prompt','')
        try:due=float(body.get('due',0))
        except (TypeError,ValueError):raise HTTPException(400,'Invalid due time')
        if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=10000 or not time.time()<due<time.time()+366*86400:raise HTTPException(400,'Enter a prompt and a future time within one year')
        sid=S.uid();S.write('INSERT INTO schedules(id,user,project,prompt,due) VALUES (?,?,?,?,?)',(sid,u['id'],id,prompt,due));return {'id':sid}

    @app.post('/api/ops/schedules/{id}/cancel')
    def cancel_schedule(id:str,body:dict,u=Depends(user)):
        S.write("UPDATE schedules SET status='cancelled' WHERE id=? AND user=? AND status='pending'",(id,u['id']));return {'ok':True}

    @app.post('/api/ops/projects/{id}/restore-version')
    def restore(id:str,body:dict,u=Depends(user)):
        p=owned('projects',id,u);commit=body.get('commit','')
        if not isinstance(commit,str) or not re.fullmatch('[0-9a-f]{40}',commit):raise HTTPException(400,'Select a full commit identifier')
        with R.MUTEX:
            if S.one("SELECT id FROM tasks WHERE project=? AND status='running'",(id,)):raise HTTPException(409,'Wait for running tasks before restoring a version')
            root=R.project_path(p)
            if R.git(root,'status','--porcelain').strip():raise HTTPException(409,'Commit or discard main-branch edits first')
            ancestors=R.git(root,'rev-list','main').splitlines()
            if commit not in ancestors:raise HTTPException(400,'Commit is not in this project main history')
            R.git(root,'restore','--source='+commit,'--staged','--worktree','--','.')
            R.git(root,'commit','--allow-empty','-m','Restore project version '+commit[:12])
        return {'ok':True,'commit':R.git(root,'rev-parse','HEAD').strip()}

    @app.post('/api/ops/tasks/{id}/review')
    def review(id:str,body:dict,u=Depends(user)):
        p,t=project_task(id,u)
        if t['status'] not in ('review','completed'):raise HTTPException(409,'Only finished work can be reviewed')
        choices=[a for a in R.available_accounts(u['id']) if a['id']!=t['account']]
        if not choices:raise HTTPException(409,'Connect a second eligible account for an independent review')
        role=S.one("SELECT id FROM agents WHERE user=? AND name='Reviewer'",(u['id'],))
        tid=R.create_task(p,'Independent review: '+t['title'],'Review this project branch. Run relevant checks, report specific findings and limitations in REVIEW.md. Do not implement unrelated features.',choices[0]['id'],role['id'] if role else '',base_task=id)
        return {'task':tid}

    @app.post('/api/ops/projects/{id}/checks')
    def checks(id:str,body:dict,u=Depends(user)):
        p=owned('projects',id,u)
        task=body.get('task','');root=R.project_path(p)
        if task:
            _,t=project_task(task,u)
            if t['project']!=id:raise HTTPException(404,'Task not in project')
            if t['status']=='running':raise HTTPException(409,'Wait for build completion')
            if not t['path']:raise HTTPException(409,'Task has no files')
            root=Path(t['path'])
        kind=body.get('kind','syntax');output=[]
        if kind not in ('syntax','tests'):raise HTTPException(400,'Choose syntax or tests')
        # Explicit button action may run project code; no shell string or arbitrary command endpoint.
        command=['python3','-m','unittest','discover','-s','tests','-q'] if kind=='tests' else None
        candidates=[]
        if kind=='syntax':
            for directory,dirs,files in os.walk(root,followlinks=False):
                dirs[:]=[d for d in dirs if d not in ('.git','node_modules','.venv','attachments') and not (Path(directory)/d).is_symlink()]
                for name in files:
                    path=Path(directory)/name
                    if path.suffix=='.js' and not path.is_symlink() and 'vendor' not in path.parts:candidates.append(path)
            if not shutil.which('node'):raise HTTPException(409,'Node.js is not installed in the Linux execution environment')
            commands=[['node','--check',str(path)] for path in candidates[:100]]
        else:
            if not (root/'tests').is_dir():raise HTTPException(409,'No Python tests directory in this workspace')
            commands=[command]
        for command in commands:
            try:r=subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=30,env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','HOME')})
            except subprocess.TimeoutExpired:raise HTTPException(408,'Check timed out after 30 seconds')
            output.append({'file':command[-1] if kind=='syntax' else 'tests','passed':r.returncode==0,'output':(r.stdout+r.stderr)[-5000:]})
        return {'checks':output,'passed':bool(output) and all(x['passed'] for x in output),'note':'No compatible files found' if not output else 'Actual executed checks; not a browser or visual test'}

    @app.get('/api/ops/backup')
    def backup(u=Depends(user)):
        return StreamingResponse(iter([archive_sources(u)]),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="kokic-source-backup.zip"'})

    @app.post('/api/ops/backup/encrypted')
    def encrypted_backup(body:dict,u=Depends(user)):
        salt=os.urandom(16);cipher=backup_cipher(body.get('passphrase'),salt)
        data=b'KOKIC1'+salt+cipher.encrypt(archive_sources(u))
        return StreamingResponse(iter([data]),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="kokic-source-backup.enc"'})

    @app.post('/api/ops/backup/restore')
    def restore_backup(body:dict,u=Depends(user)):
        value=body.get('data','')
        if not isinstance(value,str) or len(value)>40_000_000:raise HTTPException(413,'Encrypted backup exceeds size limit')
        try:
            raw=base64.b64decode(value,validate=True)
            if raw[:6]!=b'KOKIC1' or len(raw)<40:raise ValueError('Invalid backup header')
            data=backup_cipher(body.get('passphrase'),raw[6:22]).decrypt(raw[22:])
            z=zipfile.ZipFile(io.BytesIO(data));items=z.infolist()
            if len(items)>2000 or sum(i.file_size for i in items)>20_000_000 or len({i.filename for i in items})!=len(items):raise ValueError('Backup archive limits exceeded')
            manifest=json.loads(z.read('manifest.json'))
            if not isinstance(manifest,list) or len(manifest)>100:raise ValueError('Invalid project manifest')
            known=set()
            for p in manifest:
                if not isinstance(p,dict) or not re.fullmatch('[0-9a-f]{20}',p.get('id','')) or p['id'] in known or not isinstance(p.get('name'),str) or len(p['name'])>100:raise ValueError('Invalid project metadata')
                known.add(p['id'])
            for item in items:
                parts=Path(item.filename).parts
                if item.filename=='manifest.json':continue
                if len(parts)<2 or parts[0] not in known or Path(item.filename).is_absolute() or any(x in ('..','.git','.env') or x.startswith('.env.') for x in parts) or (item.external_attr>>16)&0o170000==0o120000:raise ValueError('Unsafe backup path')
        except (InvalidToken,ValueError,KeyError,TypeError,zipfile.BadZipFile,json.JSONDecodeError):raise HTTPException(400,'Invalid backup or incorrect passphrase')
        created=[]
        for item in manifest:
            p={'id':S.uid(),'user':u['id'],'name':('Restored: '+item['name'])[:100],'description':str(item.get('description',''))[:3000],'created':time.time()}
            R.init_project(p);root=R.project_path(p)
            try:
                for f in items:
                    if not f.filename.startswith(item['id']+'/') or f.is_dir():continue
                    target=S.safe_path(root,f.filename.split('/',1)[1]);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(f))
                R.git(root,'add','.');R.git(root,'commit','--allow-empty','-m','Restore encrypted source backup')
                with S.LOCK,S.db() as c:
                    c.execute('INSERT INTO projects(id,user,name,description,created,autonomy) VALUES (?,?,?,?,?,?)',tuple(p[k] for k in ('id','user','name','description','created'))+('accept-edits',))
                    c.execute('INSERT INTO project_memory VALUES (?,?)',(p['id'],str(item.get('memory',''))[:12000]))
                created.append(p['id'])
            except Exception:
                if root.parent.resolve().parent==(S.ROOT/'projects').resolve():shutil.rmtree(root.parent)
                raise
        z.close();return {'projects':created,'note':'Restored into new projects with command review enabled. Provider accounts, secrets, sessions and Git history were not included.'}
