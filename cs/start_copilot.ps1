# cs_payment 폴더에서 실행: .\start_copilot.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$scriptDir\.venv\Scripts\python.exe" "$scriptDir\cs_copilot.py" `
    --key "$scriptDir\<서비스계정키>.json"
