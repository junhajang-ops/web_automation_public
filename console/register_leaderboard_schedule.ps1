# -*- coding: utf-8 -*-
# console_leaderboard.py 를 Windows 작업 스케줄러에 등록/갱신한다.
# 요일/시각은 코드에 하드코딩하지 않고 프로젝트 루트 .env 의 아래 두 값을 읽어 사용한다.
#
#   LEADERBOARD_SCHEDULE_DAYS=Saturday,Sunday       (콤마 구분, 영문 요일명: Monday/Tuesday/Wednesday/Thursday/Friday/Saturday/Sunday)
#   LEADERBOARD_SCHEDULE_TIME=12:00,14:00,20:00     (콤마 구분, 24시간제 HH:mm. 여러 개 지정 가능)
#
# 위 예시는 "토요일·일요일 각각 12:00/14:00/20:00에 실행" = 지정한 요일 전체 x 지정한 시각 전체 조합으로 실행된다.
# 스케줄을 바꾸고 싶으면 .env 값만 수정한 뒤 이 스크립트를 다시 실행하면 된다(기존 등록을 덮어씀).
# 등록 취소: unregister_leaderboard_schedule.ps1 실행 (같은 방식으로 우클릭 -> "PowerShell로 실행")

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot ".env"
$WrapperScript = Join-Path $PSScriptRoot "run_leaderboard_scheduled.ps1"
$TaskName = "ConsoleLeaderboard"

try {
    if (-not (Test-Path $EnvFile)) {
        throw "[오류] .env 파일을 찾을 수 없습니다: $EnvFile"
    }

    $envMap = @{}
    foreach ($line in Get-Content -Path $EnvFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim("'").Trim('"')
        $envMap[$key] = $value
    }

    $Days = $envMap["LEADERBOARD_SCHEDULE_DAYS"]
    $Time = $envMap["LEADERBOARD_SCHEDULE_TIME"]

    if (-not $Days -or -not $Time) {
        throw "[오류] .env 에 LEADERBOARD_SCHEDULE_DAYS / LEADERBOARD_SCHEDULE_TIME 을 설정해 주세요. 예) LEADERBOARD_SCHEDULE_DAYS=Saturday,Sunday / LEADERBOARD_SCHEDULE_TIME=12:00,14:00,20:00"
    }

    $DaysOfWeek = $Days.Split(",") | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() }
    $Times = $Time.Split(",") | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() }

    foreach ($t in $Times) {
        if ($t -notmatch "^\d{1,2}:\d{2}$") {
            throw "[오류] LEADERBOARD_SCHEDULE_TIME 형식이 올바르지 않습니다: '$t' (예: 09:00)"
        }
    }

    Write-Host "등록할 스케줄: 매주 $($DaysOfWeek -join ', ') 요일의 $($Times -join ', ') 시각에 실행됩니다."

    # 예약 시각에 PC가 절전(Sleep) 상태여도 깨어나 실행되도록 하려면, 작업 자체의
    # WakeToRun 설정뿐 아니라 전원 설정의 "절전 모드 해제 타이머 허용"도 켜져 있어야 한다.
    # (관리자 권한 불필요 — 현재 전원 구성표에만 적용, 실행할 때마다 재확인해 항상 켜둔다.)
    powercfg -setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    powercfg -setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    powercfg -S SCHEME_CURRENT | Out-Null
    Write-Host "전원 설정: 절전 모드 해제 타이머 허용 켬(AC/DC) — WakeToRun이 실제로 동작하기 위한 전제조건"

    $Action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WrapperScript`""

    $Triggers = foreach ($t in $Times) {
        New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $t
    }

    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    # StartWhenAvailable: 재부팅(예: Windows 업데이트) 등으로 예약 시각을 놓쳐도 PC가 다시 켜지는 대로 실행.
    # WakeToRun: 예약 시각에 PC가 절전 상태여도 깨워서 실행(위 전원 설정과 함께 있어야 실제로 동작).
    # 단, 완전히 종료(전원 끔)된 상태에서는 소프트웨어적으로 깨울 방법이 없어 이 설정으로도 실행되지 않는다.
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)

    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
        -Principal $Principal -Settings $Settings -Force | Out-Null

    Write-Host "등록 완료: 작업 스케줄러 > '$TaskName' (요일=$($DaysOfWeek -join ', ') / 시각=$($Times -join ', '))"
}
finally {
    # 창이 뜨자마자 바로 닫혀 결과(성공/오류 메시지)를 못 보는 것을 막기 위해 잠깐 유지한다.
    Start-Sleep -Seconds 10
}
