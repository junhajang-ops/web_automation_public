# -*- coding: utf-8 -*-
# Windows 작업 스케줄러가 호출하는 실행 래퍼.
# console_leaderboard.py 를 --unattended 로 실행하고 출력을 로그 파일에 남긴다.
# 이 파일 자체를 수정할 필요는 없다 — 스케줄(요일/시각)은 register_leaderboard_schedule.ps1 이 .env 값으로 등록한다.

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Script = Join-Path $PSScriptRoot "console_leaderboard.py"
$LogDir = Join-Path $PSScriptRoot "logs_leaderboard"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "leaderboard_$Timestamp.log"

& $Python $Script --gametitle --unattended *>> $LogFile
