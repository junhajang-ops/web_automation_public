# -*- coding: utf-8 -*-
# console_leaderboard.py 를 Windows 작업 스케줄러에 등록/갱신한다.
# 요일/시각은 코드에 하드코딩하지 않고 프로젝트 루트 .env 의 아래 두 값을 읽어 사용한다.
#
#   LEADERBOARD_SCHEDULE_DAYS=Monday,Thursday   (콤마 구분, 영문 요일명: Monday/Tuesday/Wednesday/Thursday/Friday/Saturday/Sunday)
#   LEADERBOARD_SCHEDULE_TIME=09:00             (24시간제 HH:mm)
#
# 스케줄을 바꾸고 싶으면 .env 값만 수정한 뒤 이 스크립트를 다시 실행하면 된다(기존 등록을 덮어씀).
# 등록 취소: Unregister-ScheduledTask -TaskName "ConsoleLeaderboard" -Confirm:$false

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot ".env"
$WrapperScript = Join-Path $PSScriptRoot "run_leaderboard_scheduled.ps1"
$TaskName = "ConsoleLeaderboard"

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
    throw "[오류] .env 에 LEADERBOARD_SCHEDULE_DAYS / LEADERBOARD_SCHEDULE_TIME 을 설정해 주세요. 예) LEADERBOARD_SCHEDULE_DAYS=Monday,Thursday / LEADERBOARD_SCHEDULE_TIME=09:00"
}

$DaysOfWeek = $Days.Split(",") | ForEach-Object { $_.Trim() }

Write-Host "등록할 스케줄: $($DaysOfWeek -join ', ') / $Time"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WrapperScript`""

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $Time

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings -Force | Out-Null

Write-Host "등록 완료: 작업 스케줄러 > '$TaskName' ($($DaysOfWeek -join ', ') $Time)"
