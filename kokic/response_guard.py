import json,sys
try:json.load(sys.stdin)
except ValueError:pass
print(json.dumps({'decision':'deny','reason':'Kokic conversation and planning mode: respond directly without tools, command execution, or file changes.'}))
