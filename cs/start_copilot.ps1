# cs/ 폴더의 스크립트. 루트의 .venv·서비스계정 키를 사용한다.
# 실행: cs_payment\cs 에서  .\start_copilot.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

try {
    # 루트 .env에서 GOOGLE_KEY_FILE / COPILOT_STEP_WAIT_MS 읽기
    $keyFile = ""
    $copilotStepWaitMs = ""
    Get-Content "$root\.env" -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*GOOGLE_KEY_FILE\s*=\s*(.+)') {
            $keyFile = $Matches[1].Trim()
        }
        if ($_ -match '^\s*COPILOT_STEP_WAIT_MS\s*=\s*(.+)') {
            $copilotStepWaitMs = $Matches[1].Trim()
        }
    }
    if (-not $keyFile) {
        Write-Error ".env에 GOOGLE_KEY_FILE이 없습니다."
        exit 1
    }

    # 콘솔 판정 워커 전용 대기값(2026-07-11). console_leaderboard.py 등이 쓰는
    # CONSOLE_STEP_WAIT_MS/STEP_WAIT_MS와 분리된 이 프로세스만의 값이며, 이 줄은
    # 이 스크립트 프로세스와 그 자식(python.exe)에만 적용되고 다른 터미널/스크립트에는
    # 영향을 주지 않는다. .env에 없으면 기본값 2000ms로 동작한다.
    if ($copilotStepWaitMs) {
        $env:COPILOT_STEP_WAIT_MS = $copilotStepWaitMs
    } else {
        $env:COPILOT_STEP_WAIT_MS = "2000"
    }

    & "$root\.venv\Scripts\python.exe" "$scriptDir\cs_copilot.py" `
        --key "$root\$keyFile"
}
finally {
    # 오류로 창이 곧바로 닫혀 원인을 못 보는 것을 막기 위해 종료 전 대기한다.
    Write-Host ""
    Read-Host "종료하려면 Enter"
}
