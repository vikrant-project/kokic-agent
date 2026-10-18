"""Small, reviewable starters; no provider calls or external asset dependencies."""
STARTERS={
 'blank':{'name':'Blank workspace','files':{}},
 'website':{'name':'Static website','files':{
   'index.html':'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="style.css"><title>My project</title><main><h1>Your next idea starts here.</h1><p>A local starter, ready for your agent.</p><button id="hello">Try it</button><p id="result" role="status"></p></main><script src="app.js"></script></html>\n',
   'style.css':'body{font:18px/1.7 system-ui;background:#17121f;color:#eee3f5;margin:0;padding:12vh 8vw}main{max-width:720px;margin:auto}button{padding:12px 20px;background:#c4a0ef;color:#21142d;border:0;border-radius:8px;cursor:pointer}\n',
   'app.js':'document.querySelector("#hello").addEventListener("click",()=>{document.querySelector("#result").textContent="Your starter is working.";});\n'}},
 'python-api':{'name':'Python JSON API','files':{
   'app.py':'from fastapi import FastAPI\napp = FastAPI(title="My API")\n\n@app.get("/health")\ndef health():\n    return {"status": "ok"}\n',
   'requirements.txt':'fastapi\nuvicorn\n',
   'README.md':'# Python API starter\n\nCreate a project virtual environment, install requirements.txt, then run `uvicorn app:app --host 127.0.0.1 --port 8000`.\n'}},
 'document':{'name':'Document brief','files':{
   'BRIEF.md':'# Document brief\n\n## Audience\nWho will read this?\n\n## Purpose\nWhat should the reader understand or do?\n\n## Sections\n- Introduction\n- Main content\n- Next steps\n\n## Deliverables\nState the requested format and review criteria.\n'}}
}
