# -*- coding: utf-8 -*-
# register_leaderboard_schedule_<title>.ps1 로 등록한 작업 스케줄러 항목을 제거한다(타이틀별 공용 core, -Title 파라미터).
# 이 파일을 직접 실행하지 않는다 — unregister_leaderboard_schedule_gametitle.ps1 / _dc.ps1 처럼
# 타이틀별 래퍼를 우클릭 -> "PowerShell로 실행"한다.

param(
    [Parameter(Mandatory = $true)]
    [string]$Title
)

$ErrorActionPreference = "Stop"

$TaskName = "ConsoleLeaderboard_$Title"

try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    if (-not $existing) {
        Write-Host "등록된 작업이 없습니다: '$TaskName' (이미 삭제되었거나 등록된 적이 없습니다)"
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "삭제 완료: '$TaskName'"
    }
}
finally {
    # 창이 뜨자마자 바로 닫혀 결과를 못 보는 것을 막기 위해 잠깐 유지한다.
    Start-Sleep -Seconds 10
}
