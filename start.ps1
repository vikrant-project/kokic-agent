param([int]$Port = 8787, [switch]$Setup)
$ErrorActionPreference = 'Stop'
$sourcePath = (& wsl -d Ubuntu -- wslpath -a $PSScriptRoot).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Ubuntu WSL is required.' }
$arguments = @('-d','Ubuntu','--cd',$sourcePath,'--','bash','./start.sh','--port',"$Port")
if ($Setup) { $arguments += '--setup' }
& wsl @arguments
exit $LASTEXITCODE
