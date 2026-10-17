"""Task-scoped stdio MCP connection for Antigravity workers."""
import json,os,sys
from urllib.request import Request,urlopen

TOOLS={
 'team':('List connected account availability',{}),
 'delegate':('Assign a child task to a different account. Work is isolated in a branch.',{'account':'string','title':'string','prompt':'string'}),
 'task_status':('Inspect a task in the current project',{'id':'string'}),
 'merge_child':('Merge a completed child branch into your own worktree',{'id':'string'})}

for line in sys.stdin:
    try:
        message=json.loads(line);id=message.get('id');method=message.get('method')
        if id is None:continue
        if method=='initialize':result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'kokic','version':'1.0'}}
        elif method=='ping':result={}
        elif method=='tools/list':result={'tools':[{'name':name,'description':description,'inputSchema':{'type':'object','properties':{k:{'type':v} for k,v in fields.items()},'required':list(fields)}} for name,(description,fields) in TOOLS.items()]}
        elif method=='tools/call':
            try:
                data=json.dumps(message['params']).encode();request=Request(os.environ.get('KOKIC_URL','http://127.0.0.1:8787')+'/api/mcp',data,{'Authorization':'Bearer '+os.environ['KOKIC_TASK_TOKEN'],'Content-Type':'application/json'})
                with urlopen(request,timeout=30) as response:value=json.load(response)
                result={'content':[{'type':'text','text':json.dumps(value)}]}
            except Exception as e:result={'isError':True,'content':[{'type':'text','text':str(e)}]}
        else:
            print(json.dumps({'jsonrpc':'2.0','id':id,'error':{'code':-32601,'message':'Unknown method'}}),flush=True);continue
        print(json.dumps({'jsonrpc':'2.0','id':id,'result':result}),flush=True)
    except Exception as e:print(str(e),file=sys.stderr)
