# cs/ 폴더의 스크립트. 루트의 .venv·서비스계정 키를 사용한다.
# 실행: cs_payment\cs 에서  .\start_copilot.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

# 루트 .env에서 GOOGLE_KEY_FILE 읽기
$keyFile = ""
Get-Content "$root\.env" -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*GOOGLE_KEY_FILE\s*=\s*(.+)') {
        $keyFile = $Matches[1].Trim()
    }
}
if (-not $keyFile) {
    Write-Error ".env에 GOOGLE_KEY_FILE이 없습니다."
    exit 1
}

& "$root\.venv\Scripts\python.exe" "$scriptDir\cs_copilot.py" `
    --key "$root\$keyFile"
