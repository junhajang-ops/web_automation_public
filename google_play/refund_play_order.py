#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 Google Play - 실제 환불 처리 스크립트 (orders.refund)   ★★ 주의: 진짜 돈 환불 ★★
================================================================================
 이 스크립트는 Google Play Developer API로 **실제 환불을 실행**합니다.
 환불은 되돌릴 수 없습니다. 반드시 미리보기로 확인 후 실행하세요.

 안전장치:
   1) 기본은 '미리보기(dry-run)' 모드 — 주문 정보만 보여주고 환불하지 않습니다.
   2) 실제 환불은 --execute 플래그 + 주문번호 직접 입력 확인이 둘 다 있어야 진행됩니다.
   3) 이미 환불된 주문이면 자동 중단합니다(중복 환불 방지).

 필요 권한(Play Console): "주문 및 정기 결제 관리"(Manage orders and subscriptions).

--------------------------------------------------------------------------------
 사용법
--------------------------------------------------------------------------------
 # 부품 설치(최초 1회)
   pip install google-api-python-client google-auth

 # (1) 미리보기 — 환불 안 함, 주문 상태만 확인 (권장: 항상 먼저)
   python refund_play_order.py <JSON키> <패키지명> <주문번호>

 # (2) 실제 환불 실행 — 돈 환불됨
   python refund_play_order.py <JSON키> <패키지명> <주문번호> --execute

 # (3) 환불 + 지급된 아이템 회수(entitlement revoke)까지
   python refund_play_order.py <JSON키> <패키지명> <주문번호> --execute --revoke

 예시)
   python refund_play_order.py key.json com.mycompany.myapp GPA.1234-5678-9012-34567
   python refund_play_order.py key.json com.mycompany.myapp GPA.1234-5678-9012-34567 --execute
================================================================================
"""

import sys

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[설치 필요] pip install google-api-python-client google-auth")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]

STATE_DESC = {
    "PROCESSED": "정상 결제 완료 (환불 가능)",
    "PENDING": "결제 대기/미확정",
    "CANCELED": "취소됨",
    "PENDING_REFUND": "환불 진행 중",
    "PARTIALLY_REFUNDED": "부분 환불됨",
    "REFUNDED": "전액 환불됨",
    "STATE_UNSPECIFIED": "상태 불명",
}
# 이미 환불/취소/진행중이면 다시 환불하지 않는다
ALREADY_DONE = {"REFUNDED", "PARTIALLY_REFUNDED", "PENDING_REFUND", "CANCELED"}


def fmt_money(m):
    if not m:
        return "(금액정보 없음)"
    amount = int(m.get("units", 0)) + int(m.get("nanos", 0)) / 1e9
    return f"{amount:,.2f} {m.get('currencyCode', '')}".strip()


def main():
    args = sys.argv[1:]
    do_execute = "--execute" in args
    do_revoke = "--revoke" in args
    pos = [a for a in args if not a.startswith("--")]  # 위치 인자만

    if len(pos) < 3:
        print("사용법: python refund_play_order.py <JSON키> <패키지명> <주문번호> [--execute] [--revoke]")
        sys.exit(1)

    key_file, package_name, order_id = pos[0], pos[1], pos[2]

    print("=" * 66)
    print(" Google Play 환불 처리" + ("  ★실제 실행 모드★" if do_execute else "  (미리보기 모드)"))
    print("=" * 66)
    print(f"  열쇠 파일 : {key_file}")
    print(f"  패키지명  : {package_name}")
    print(f"  주문번호  : {order_id}")
    print(f"  아이템 회수(revoke): {'예' if do_revoke else '아니오'}")
    print("-" * 66)

    # --- 인증 ---
    try:
        creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
        service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
    except FileNotFoundError:
        print(f"[실패] 열쇠 파일 없음: {key_file}")
        sys.exit(1)
    except Exception as e:
        print(f"[실패] 열쇠 파일 오류: {e}")
        sys.exit(1)

    # --- 현재 주문 상태 확인 (환불 전 필수) ---
    try:
        order = service.orders().get(packageName=package_name, orderId=order_id).execute()
    except HttpError as e:
        _explain_http_error(e)
        sys.exit(1)

    state = order.get("state", "STATE_UNSPECIFIED")
    print(f"  현재 상태 : {state} ({STATE_DESC.get(state, '?')})")
    print(f"  결제 총액 : {fmt_money(order.get('total'))}")
    print(f"  결제 시각 : {order.get('createTime', '-')}")
    print("-" * 66)

    # --- 이미 환불/취소/진행중이면 중단 ---
    if state in ALREADY_DONE:
        print(f"[중단] 이 주문은 이미 '{STATE_DESC.get(state)}' 상태입니다. 환불하지 않습니다.")
        sys.exit(0)

    # --- 미리보기 모드: 여기서 끝 ---
    if not do_execute:
        print("[미리보기] 환불을 실행하지 않았습니다.")
        print("실제로 환불하려면 아래 명령을 실행하세요 (맨 끝에 --execute 추가):")
        revoke_part = " --revoke" if do_revoke else ""
        print(f"\n   python refund_play_order.py {key_file} {package_name} {order_id} --execute{revoke_part}\n")
        print("   ※ --revoke 를 붙이면 지급된 아이템 회수까지 함께 진행됩니다.")
        sys.exit(0)

    # --- 실제 실행 모드: 사람 직접 확인 ---
    print("★★ 경고: 지금부터 실제 환불을 실행합니다. 되돌릴 수 없습니다. ★★")
    print(f"   환불 금액: {fmt_money(order.get('total'))} / 아이템 회수: {'예' if do_revoke else '아니오'}")
    ans = input("   실제로 환불하시겠습니까? (y/n): ").strip().lower()
    if ans not in ("y", "yes"):
        print("[취소] 환불하지 않았습니다.")
        sys.exit(1)

    # --- 환불 실행 ---
    try:
        service.orders().refund(
            packageName=package_name, orderId=order_id, revoke=do_revoke
        ).execute()
    except HttpError as e:
        print("[환불 실패]")
        _explain_http_error(e)
        sys.exit(1)

    print("\n   [OK] 환불 요청을 전송했습니다.")

    # --- 결과 재조회 ---
    try:
        after = service.orders().get(packageName=package_name, orderId=order_id).execute()
        ns = after.get("state", "?")
        print(f"   환불 후 상태: {ns} ({STATE_DESC.get(ns, '?')})")
        print("   (PENDING_REFUND/REFUNDED 등으로 바뀌면 정상입니다. 반영에 약간 시간이 걸릴 수 있습니다.)")
    except HttpError:
        print("   (상태 재조회는 실패했지만 환불 요청 자체는 전송되었습니다.)")

    print("=" * 66)


def _explain_http_error(e, indent="   "):
    status = getattr(getattr(e, "resp", None), "status", None)
    detail = ""
    try:
        detail = e.error_details or e._get_reason()
    except Exception:
        detail = str(e)
    print(f"{indent}[오류] HTTP {status}")
    print(f"{indent}   구글 원본 메시지: {detail}")
    if status in (401, 403):
        print(f"{indent}-> 권한 문제. '주문 및 정기 결제 관리' 권한이 있는지 확인하세요.")
    elif status == 404:
        print(f"{indent}-> 주문/패키지명을 못 찾음. 값이 맞는지 확인하세요.")
    elif status == 400:
        print(f"{indent}-> 요청 형식 오류(또는 환불 불가 주문: 3년 초과 등).")
    else:
        print(f"{indent}-> 상세: {e}")


if __name__ == "__main__":
    main()
