# -*- coding: utf-8 -*-
# register_leaderboard_schedule.ps1 로 등록한 작업 스케줄러 항목을 제거한다.
# 실행: 이 파일을 우클릭 -> "PowerShell로 실행"

$ErrorActionPreference = "Stop"

$TaskName = "ConsoleLeaderboard"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $existing) {
    Write-Host "등록된 작업이 없습니다: '$TaskName' (이미 삭제되었거나 등록된 적이 없습니다)"
} else {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "삭제 완료: '$TaskName'"
}
