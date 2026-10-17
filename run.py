import argparse
import getpass
import os
from kokic import store

parser=argparse.ArgumentParser();parser.add_argument('--setup',action='store_true');parser.add_argument('--port',type=int,default=8787);args=parser.parse_args()
store.init()
if args.setup:
    email=input('Portal email: ').strip();password=getpass.getpass('Portal password: ')
    if len(password)<10:raise SystemExit('Use at least 10 characters')
    store.create_user(email,password);print('Portal account created')
else:
    import fcntl
    import uvicorn
    lock=(store.ROOT/'server.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Kokic Agent is already running for this runtime directory.')
    os.environ['KOKIC_URL']='http://127.0.0.1:'+str(args.port)
    uvicorn.run('kokic.app:app',host='127.0.0.1',port=args.port,log_level='warning',timeout_graceful_shutdown=3)
