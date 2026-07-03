# -*- coding: utf-8 -*-
# dc(게임B) 타이틀 리더보드 조회 예약 등록/갱신 — 우클릭 -> "PowerShell로 실행"
# 실제 로직은 register_leaderboard_schedule.ps1(타이틀별 공용 core)에 위임한다.
# 스케줄(요일/시각)은 .env의 dc_LEADERBOARD_SCHEDULE_DAYS / dc_LEADERBOARD_SCHEDULE_TIME 를 읽는다
# (현재 .env에는 예시값만 들어있으니 실제 운영 요일/시각으로 먼저 수정한 뒤 실행할 것).

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "register_leaderboard_schedule.ps1") -Title "dc"
