# cs/ 폴더의 스크립트. 루트의 .venv·서비스계정 키를 사용한다.
# 실행: cs_payment\cs 에서  .\start_copilot.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
& "$root\.venv\Scripts\python.exe" "$scriptDir\cs_copilot.py" `
    --key "$root\<서비스계정키>.json"
