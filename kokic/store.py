import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from cryptography.fernet import Fernet

ROOT=Path(os.environ.get('KOKIC_HOME',str(Path.home()/'Kokic Agent')))
LOCK=threading.RLock()

@contextmanager
def db():
    conn=sqlite3.connect(ROOT/'studio.db',timeout=30);conn.row_factory=sqlite3.Row
    try:
        with conn:yield conn
    finally:conn.close()

def init():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE,password TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,user TEXT,label TEXT,email TEXT DEFAULT '',status TEXT DEFAULT 'disconnected',model TEXT DEFAULT 'gemini-3.8-flash-high',models TEXT DEFAULT '[]',quota TEXT DEFAULT '{}',checked REAL DEFAULT 0,error TEXT DEFAULT '',cooldown REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,user TEXT,name TEXT,description TEXT,created REAL,autonomy TEXT DEFAULT 'autonomous',paused INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT,role TEXT,content TEXT,task TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project TEXT,parent TEXT,title TEXT,prompt TEXT,account TEXT,role TEXT,status TEXT DEFAULT 'queued',result TEXT DEFAULT '',branch TEXT DEFAULT '',path TEXT DEFAULT '',created REAL,updated REAL,attempt INTEGER DEFAULT 0,commit_hash TEXT DEFAULT '',error TEXT DEFAULT '',token TEXT DEFAULT '',policy TEXT DEFAULT 'sequential');
        CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,project TEXT,text TEXT,status TEXT DEFAULT 'active',created REAL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,user TEXT,project TEXT,task TEXT,channel TEXT,kind TEXT,text TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,user TEXT,name TEXT,description TEXT,instructions TEXT,model TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS integrations(id TEXT PRIMARY KEY,user TEXT,name TEXT,kind TEXT,config TEXT,enabled INTEGER DEFAULT 1,created REAL);
        ''')
        columns={row[1] for row in c.execute('PRAGMA table_info(accounts)')}
        for name,definition in [('quota_checked','REAL DEFAULT 0'),('quota_status',"TEXT DEFAULT 'unknown'"),('quota_error',"TEXT DEFAULT ''")]:
            if name not in columns:c.execute('ALTER TABLE accounts ADD COLUMN '+name+' '+definition)
        for table,fields in {'messages':[('thread',"TEXT DEFAULT 'main'"),('attachments',"TEXT DEFAULT '[]'")],'tasks':[('thread',"TEXT DEFAULT 'main'"),('mode',"TEXT DEFAULT 'build'"),('phase',"TEXT DEFAULT 'building'"),('plan',"TEXT DEFAULT '{}'"),('attachments',"TEXT DEFAULT '[]'"),('base_task',"TEXT DEFAULT ''")]}.items():
            columns={row[1] for row in c.execute('PRAGMA table_info('+table+')')}
            for name,definition in fields:
                if name not in columns:c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
        c.executescript('''
        CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,project TEXT,title TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,project TEXT,name TEXT,path TEXT,size INTEGER,extracted TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS preview_leases(token TEXT PRIMARY KEY,task TEXT,root TEXT,expires REAL);
        ''')

def rows(sql,args=()):
    with db() as c:return [dict(r) for r in c.execute(sql,args)]

def one(sql,args=()):
    result=rows(sql,args);return result[0] if result else None

def write(sql,args=()):
    with LOCK,db() as c:return c.execute(sql,args).lastrowid

def uid():return secrets.token_hex(10)

def emit(user,text,project='',task='',channel='',kind='info'):
    return write('INSERT INTO events(user,project,task,channel,kind,text,created) VALUES (?,?,?,?,?,?,?)',(user,project,task,channel,kind,text,time.time()))

def password_hash(password,salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()

def create_user(email,password):
    user=uid();write('INSERT INTO users VALUES (?,?,?,?)',(user,email.lower(),password_hash(password),time.time()))
    for name,description,instructions in [
      ('Coordinator','Plans the work and delegates to your team.','Break down the task, implement it, delegate independent work to available accounts through Kokic MCP, collect results, and verify before finishing.'),
      ('Builder','Implements features and fixes.','Implement the assigned task. Keep changes focused and verify the behavior with appropriate checks. Report changed files and results.'),
      ('Reviewer','Checks implementation and tests.','Review the assigned code, run relevant checks, and report concrete findings. Never claim tests passed without running them.')]:
        write('INSERT INTO agents VALUES (?,?,?,?,?,?)',(uid(),user,name,description,instructions,''))
    return user

def cipher():
    path=ROOT/'master.key'
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
    except FileExistsError:pass
    return Fernet(path.read_bytes())

def seal(value):return cipher().encrypt(json.dumps(value).encode()).decode()
def unseal(value):return json.loads(cipher().decrypt(value.encode()))

def safe_path(root,relative):
    root=Path(root).resolve();candidate=(root/relative).resolve()
    if not candidate.is_relative_to(root):raise ValueError('Path leaves this workspace')
    return candidate
