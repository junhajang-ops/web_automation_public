# -*- coding: utf-8 -*-
# gametitle 타이틀 리더보드 조회 예약 삭제 — 우클릭 -> "PowerShell로 실행"
# 실제 로직은 unregister_leaderboard_schedule.ps1(타이틀별 공용 core)에 위임한다.

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "unregister_leaderboard_schedule.ps1") -Title "gametitle"
