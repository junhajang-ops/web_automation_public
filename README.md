# 폴더 구조 / 주요 스크립트

```
cs_payment/
├─ README.md · requirements.txt · serviceaccount-*.json(키)
├─ cs/             cs 화면 자동화 (+pw_profile 세션)
├─ google_play/    Play Developer API 조회·환불
├─ gcp_logging/    Cloud Logging 조회
└─ console/        콘솔 화면 자동화 (+pw_profile_console 세션)
```

| 경로 | 용도 | 비고 |
|---|---|---|
| `cs/cs_field_dump.py` | cs 화면 필드 dump | 사람 직접 로그인, `pw_profile` |
| `cs/cs_parse.py` | dump→claim 추출 + 주문번호 정규화 | 오프라인·읽기 전용 |
| `cs/cs_copilot.py` | 실시간 보조(티켓 조회·표시) | 읽기 전용 |
| `cs/cs_gcp_logging.py` | Cloud Logging 조회 헬퍼 | 읽기 전용 |
| `cs/cs_bigquery_logging.py` | BigQuery 조회 헬퍼 | 읽기 전용 |
| `google_play/test_play_api.py` | Play `orders.get` 조회 | 읽기 전용 |
| `google_play/refund_play_order.py` | 실제 환불 실행 | ★위험: 기본 dry-run, `--execute` 필요 |
| `gcp_logging/test_logging_api.py` | Cloud Logging 읽기 권한 확인 | 읽기 전용 |
| `console/console_field_dump.py` | 콘솔 화면 dump | 읽기 전용, `pw_profile_console` |
| `console/console_user_search.py` | 콘솔 유저 검색 + 공용 헬퍼(접속·로그인·프로젝트 선택 등) | 읽기 전용 |
| `console/console_leaderboard.py` | 리더보드 조회 + 신규 유저 지급/영수증 합산 → CSV | 기본 읽기 전용 |
| `console/console_user_block.py` | 유저/디바이스 차단 | ★쓰기: 명시 플래그 필요 |
| `console/console_receipt_verification.py` | 영수증 검증 UUID 조회 | 읽기 전용 |
| `console/console_shopdata_lookup.py` | ShopData 조회 | 읽기 전용 |
| `console/console_chart_lookup.py` | 차트 조회 | 읽기 전용 |
| `console/console_payment_error.py` | 미지급(결제오류) 판정 | 읽기 전용 |
| `console/console_webshop_history.py` | 웹샵 지급 내역 조회 | 읽기 전용 |
| `console/console_post_register.py` · `console_post_bulk.py` | 우편 등록/대량 발송 | ★쓰기: 사람 승인 후 |
