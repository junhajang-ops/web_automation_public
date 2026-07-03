# -*- coding: utf-8 -*-
# Windows 작업 스케줄러가 호출하는 실행 래퍼(타이틀별 공용 core, -Title 파라미터).
# console_leaderboard.py 를 --title <Title> --unattended 로 실행하고 출력을 로그 파일에 남긴다.
# -Title 인자는 작업 스케줄러 등록 시 register_leaderboard_schedule.ps1이 액션 인자로 자동 넣어준다
# (예: -File run_leaderboard_scheduled.ps1 -Title gametitle). 이 파일을 직접 실행/수정할 필요는 없다 —
# 스케줄(요일/시각)은 register_leaderboard_schedule_<title>.ps1 이 .env의 {TITLE}_LEADERBOARD_SCHEDULE_*로 등록한다.

param(
    [Parameter(Mandatory = $true)]
    [string]$Title
)

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Script = Join-Path $PSScriptRoot "console_leaderboard.py"
$LogDir = Join-Path $PSScriptRoot "logs_leaderboard"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "leaderboard_${Title}_$Timestamp.log"

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
    # *>> 로 파일에만 리다이렉트하면 작업 스케줄러 창(있을 경우)에는 아무 출력도 안 보인다.
    # Tee-Object로 파일 저장과 동시에 터미널에도 그대로 흘려보내 평소 대화식 실행과 동일하게 보이게 한다.
    & $Python $Script --title $Title --unattended 2>&1 | Tee-Object -FilePath $LogFile -Append
}
finally {
    # 실행이 끝나면 평소 전원 관리 설정으로 되돌린다.
    [PowerHelper]::SetThreadExecutionState([PowerHelper+EXECUTION_STATE]::ES_CONTINUOUS) | Out-Null
}

# 창이 뜨자마자 바로 닫혀 결과를 못 보는 것을 막기 위해 잠깐 유지한다.
Start-Sleep -Seconds 10
