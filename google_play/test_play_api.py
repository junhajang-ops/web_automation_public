#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 Google Play Developer API - 주문 조회 / 환불 여부 확인 테스트
================================================================================
 목적: 발급한 "서비스 계정(로봇 계정) + JSON 열쇠 파일"로 구글 플레이에서
       특정 주문(orderId)을 조회하고, 그 주문이 환불되었는지 확인합니다.
       (Phase 0 동작 확인 + 환불 여부 판정 방식 검증)

 이 스크립트는 "읽기(조회)"만 합니다. 실제 환불은 하지 않으니 안전합니다.

 ※ 환불 여부는 orders.get 응답의 'state' 값으로 판정합니다:
      PROCESSED          -> 정상 결제 (환불 아님)
      PENDING            -> 결제 대기/미확정
      CANCELED           -> 취소됨
      PENDING_REFUND     -> 환불 진행 중
      PARTIALLY_REFUNDED -> 부분 환불됨
      REFUNDED           -> 전액 환불됨
   (이미 환불된 건이면 REFUNDED / PARTIALLY_REFUNDED / PENDING_REFUND 로 나옵니다.)

--------------------------------------------------------------------------------
 [비개발자용] 실행 방법
--------------------------------------------------------------------------------
 1) 파이썬(Python) 설치: https://www.python.org (설치 시 'Add to PATH' 체크 권장)
 2) 부품 설치 (명령창에 입력):
        pip install google-api-python-client google-auth
 3) 실행:
        python test_play_api.py  <JSON키파일>  <패키지명>  <주문번호>

    예시)
        python test_play_api.py key.json com.mycompany.myapp GPA.1234-5678-9012-34567

    - <JSON키파일>: D단계에서 내려받은 .json 열쇠 파일 경로
    - <패키지명>  : 우리 앱 ID (예: com.회사.앱)
    - <주문번호>  : 환불 여부를 확인할 실제 orderId (예: GPA.xxxx-xxxx-xxxx-xxxxx)

 4) 화면에 주문 상태와 "환불됨/정상" 판정이 나오면 정상 작동입니다.

 (참고) 주문번호 없이 연결만 확인하고 싶으면 주문번호 자리를 비우고 실행하세요.
================================================================================
"""

import sys

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[설치 필요] 먼저 아래 명령으로 부품을 설치하세요:")
    print("    pip install google-api-python-client google-auth")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]

# 주문 상태(state) -> 사람이 읽는 설명 + 환불 여부 라벨
STATE_INFO = {
    "PROCESSED":          ("정상 결제 완료",   "환불 아님"),
    "PENDING":            ("결제 대기/미확정", "환불 아님"),
    "CANCELED":           ("취소됨",           "환불/취소 처리됨"),
    "PENDING_REFUND":     ("환불 진행 중",     "환불 처리 중(이미 접수됨)"),
    "PARTIALLY_REFUNDED": ("부분 환불됨",       "환불됨(부분)"),
    "REFUNDED":           ("전액 환불됨",       "환불됨(전액)"),
    "STATE_UNSPECIFIED":  ("상태 불명",        "확인 불가"),
}
REFUNDED_STATES = {"REFUNDED", "PARTIALLY_REFUNDED", "PENDING_REFUND", "CANCELED"}


def fmt_money(m):
    """Money 객체(units/nanos/currencyCode)를 '1,200.00 KRW' 형태로."""
    if not m:
        return "(금액정보 없음)"
    units = int(m.get("units", 0))
    nanos = int(m.get("nanos", 0))
    amount = units + nanos / 1e9
    cur = m.get("currencyCode", "")
    return f"{amount:,.2f} {cur}".strip()


def main():
    if len(sys.argv) < 3:
        print("사용법: python test_play_api.py <JSON키파일> <패키지명> <주문번호>")
        print("예시 : python test_play_api.py key.json com.mycompany.myapp GPA.1234-5678-9012-34567")
        sys.exit(1)

    key_file = sys.argv[1]
    package_name = sys.argv[2]
    order_id = sys.argv[3] if len(sys.argv) >= 4 else None

    print("=" * 64)
    print(" Google Play - 주문 조회 / 환불 여부 확인")
    print("=" * 64)
    print(f"  열쇠 파일 : {key_file}")
    print(f"  패키지명  : {package_name}")
    print(f"  주문번호  : {order_id if order_id else '(없음 -> 연결 확인만 진행)'}")
    print("-" * 64)

    # ---- 1) 인증 -----------------------------------------------------------
    try:
        creds = service_account.Credentials.from_service_account_file(
            key_file, scopes=SCOPES
        )
        service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
        sa_email = getattr(creds, "service_account_email", "(확인불가)")
        print("[1/2] 인증: OK (열쇠 파일 형식 정상)")
        print(f"       서비스계정 이메일: {sa_email}")
        print("       ↑ 이 이메일이 Play Console '사용자 및 권한'에 초대돼 있어야 합니다.")
    except FileNotFoundError:
        print(f"[실패] 열쇠 파일을 찾을 수 없습니다: {key_file}")
        sys.exit(1)
    except Exception as e:
        print(f"[실패] 열쇠 파일이 올바르지 않습니다: {e}")
        print("       -> D단계에서 JSON 키를 다시 발급해 보세요.")
        sys.exit(1)

    # ---- 주문번호가 없으면: 연결만 확인하고 종료 ---------------------------
    if not order_id:
        print("[2/2] 주문번호가 없어 연결만 확인합니다(voidedpurchases.list) ...")
        try:
            resp = (service.purchases().voidedpurchases()
                    .list(packageName=package_name).execute())
            n = len(resp.get("voidedPurchases", []))
            print(f"       [OK] 연결 성공 - 환불(취소) 내역 {n}건 조회됨.")
            print("       (실제 환불 여부 판정은 주문번호를 넣고 다시 실행하세요.)")
        except HttpError as e:
            _explain_http_error(e)
            sys.exit(1)
        return

    # ---- 2) 주문 조회 + 환불 여부 판정 (orders.get) ------------------------
    print(f"[2/2] 주문 조회(orders.get) - orderId={order_id} ...")
    try:
        order = service.orders().get(
            packageName=package_name, orderId=order_id
        ).execute()
    except HttpError as e:
        _explain_http_error(e)
        sys.exit(1)

    state = order.get("state", "STATE_UNSPECIFIED")
    desc, refund_label = STATE_INFO.get(state, (state, "확인 불가"))
    is_refunded = state in REFUNDED_STATES

    print("       [OK] 주문 조회 성공!\n")
    print("-" * 64)
    print(f"  주문상태(state) : {state}  ->  {desc}")
    print(f"  결제 시각       : {order.get('createTime', '-')}")
    print(f"  최근 이벤트 시각: {order.get('lastEventTime', '-')}")
    print(f"  결제 총액       : {fmt_money(order.get('total'))}")
    print(f"  판매 채널       : {order.get('salesChannel', '-')}")

    # 구매 상품(라인아이템)
    for i, li in enumerate(order.get("lineItems", []), 1):
        print(f"  상품 {i}        : {li.get('productId','?')} "
              f"({li.get('productTitle','')}) / {fmt_money(li.get('total'))}")

    # 환불/취소 이벤트 상세 (있을 때만)
    hist = order.get("orderHistory", {}) or {}
    if hist.get("refundEvent"):
        rev = hist["refundEvent"]
        print(f"  - 전액환불 이벤트: {rev.get('eventTime','-')} "
              f"(사유: {rev.get('refundReason','-')})")
    for j, pr in enumerate(hist.get("partialRefundEvents", []) or [], 1):
        print(f"  - 부분환불 #{j}   : 생성 {pr.get('createTime','-')} / "
              f"처리 {pr.get('processTime','-')} / 상태 {pr.get('state','-')}")
    if hist.get("cancellationEvent"):
        print(f"  - 취소 이벤트   : {hist['cancellationEvent'].get('eventTime','-')}")
    print("-" * 64)

    # 최종 판정
    print()
    if is_refunded:
        print(f"  [환불] 판정: 이 주문은 [{refund_label}] 상태입니다.")
        print("         -> 이미 환불/취소 처리된 건이므로 재처리(중복환불) 금지 대상.")
    else:
        print(f"  [정상] 판정: 이 주문은 [{refund_label}] - 아직 환불되지 않았습니다.")
    print()
    print("=" * 64)
    print(" [OK] 연결/권한 정상 + 주문 조회로 환불 여부 판정 성공.")
    print("=" * 64)


def _explain_http_error(e, indent="       "):
    status = getattr(getattr(e, "resp", None), "status", None)
    detail = ""
    try:
        detail = e.error_details or e._get_reason()
    except Exception:
        detail = str(e)
    print(f"{indent}[오류] HTTP {status}")
    print(f"{indent}   구글 원본 메시지: {detail}")
    if status in (401, 403):
        print(f"{indent}-> 거의 대부분 '권한 문제'입니다(열쇠 파일 문제 아님).")
        print(f"{indent}   1) E단계: 위에 찍힌 '서비스계정 이메일'을 Play Console")
        print(f"{indent}      '사용자 및 권한'에 초대했는지, 이메일이 정확히 일치하는지 확인")
        print(f"{indent}   2) 권한 2가지(재무·주문 보기 / 주문 및 정기결제 관리)를 줬는지 확인")
        print(f"{indent}   3) 방금 줬다면 전파 지연 — 몇 분~최대 24시간 후 다시 시도")
        print(f"{indent}   4) 권한을 '특정 앱'에만 줬다면 그 앱(com.mycompany.myapp)이")
        print(f"{indent}      포함됐는지, 또는 계정 전체 권한인지 확인")
        print(f"{indent}   5) Play 개발자 계정이 여러 개면 '맞는 개발자 계정'에 초대했는지 확인")
    elif status == 404:
        print(f"{indent}-> 주문을 못 찾음. 주문번호(orderId)와 패키지명이 맞는지,")
        print(f"{indent}   그 주문이 이 앱 소속인지 확인하세요.")
    elif status == 400:
        print(f"{indent}-> 요청 형식 오류. 주문번호/패키지명 형식을 확인하세요.")
    else:
        print(f"{indent}-> 상세: {e}")


if __name__ == "__main__":
    main()
