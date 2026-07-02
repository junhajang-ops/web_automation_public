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

# 화면 잠금(Win+L) 중에도 모니터/GPU 렌더링이 절전으로 내려가지 않도록
# 이 스크립트가 도는 동안만 Windows에 "화면·시스템을 켜둬라"라고 알려준다.
# 비밀번호 잠금 자체는 그대로 유지되고, 화면 전원/렌더링만 켜진 상태로 유지된다.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PowerHelper {
    [FlagsAttribute]
    public enum EXECUTION_STATE : uint {
        ES_CONTINUOUS = 0x80000000,
        ES_SYSTEM_REQUIRED = 0x00000001,
        ES_DISPLAY_REQUIRED = 0x00000002
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern EXECUTION_STATE SetThreadExecutionState(EXECUTION_STATE esFlags);
}
"@ -ErrorAction SilentlyContinue

$KeepAwake = ([PowerHelper+EXECUTION_STATE]::ES_CONTINUOUS -bor
             [PowerHelper+EXECUTION_STATE]::ES_SYSTEM_REQUIRED -bor
             [PowerHelper+EXECUTION_STATE]::ES_DISPLAY_REQUIRED)
[PowerHelper]::SetThreadExecutionState($KeepAwake) | Out-Null

try {
    & $Python $Script --gametitle --unattended *>> $LogFile
}
finally {
    # 실행이 끝나면 평소 전원 관리 설정으로 되돌린다.
    [PowerHelper]::SetThreadExecutionState([PowerHelper+EXECUTION_STATE]::ES_CONTINUOUS) | Out-Null
}

# 창이 뜨자마자 바로 닫혀 결과를 못 보는 것을 막기 위해 잠깐 유지한다.
Start-Sleep -Seconds 10
