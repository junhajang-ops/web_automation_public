# -*- coding: utf-8 -*-
"""
console_payment_error.py — 미지급(결제오류) 판정 (설계서 3-B)
================================================================

전제: Play API에 영수증 존재(결제 발생)는 호출자가 이미 확인(설계서 3-A).

판정 흐름:
  1. 콘솔 '영수증 검증' 메뉴 조회.
  2. 분기(영수증검증 description 주축):
     - 기록 없음            → 패턴1: 미지급. 상품은 GCP log_shop_click으로 특정.
     - description=PurchaseCodeNull(또는 빈값) → 패턴2: 미지급. 상품은 로그로 특정.
     - description 정상     → 패턴3: 상품코드=description → ShopData Count 조회·표시
                               (Count 자동 미지급 판정은 주차/상품유형 복잡성으로 보류).

코드 체계: 영수증검증 description = ShopData PurchaseCode = 상품표 Inapp_PurchaseCode
          = shop_click_id (출처별 컬럼명만 다른 같은 값). Play 영수증과 잇는 키만 StorePurchaseCode_AOS.

★ 읽기 전용: ShopData 편집 모드 진입·Count 변경 등 쓰기 동작 없음. 재지급 실행은 사람 승인 후 별도.
"""

import argparse
import datetime
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

BASE_DIR = Path(__file__).resolve().parent

# cs 모듈(브랜드→GCP 규칙, log_shop_click 조회) import 위해 경로 추가
_CS_DIR = BASE_DIR.parent / "cs"
if str(_CS_DIR) not in sys.path:
    sys.path.insert(0, str(_CS_DIR))

from console_step_verify import init_dump_dir
from console_user_search_test import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    load_playwright,
    select_target_page,
)
from console_receipt_verification import run_receipt_verification
from console_shopdata_lookup import (
    click_shopdata_search_button,
    ensure_table_selected,
    fill_shopdata_uuid_filter,
    open_game_info_data_tab,
    open_game_info_menu,
    open_top_shopdata_detail,
    refresh_detail_popup_with_mouse_scroll,
    resolve_purchase_line_and_count,
    wait_for_shopdata_result_row,
)
from cs_parse import resolve_brand_gcp_log
from cs_copilot import fetch_recent_shop_click_log
from test_config import TEST_TABLE_NAME, TEST_UUID

DEFAULT_OUTPUT = "dumps_console_payment_error"
DEFAULT_UUID = TEST_UUID
DEFAULT_TABLE_NAME = TEST_TABLE_NAME
DEFAULT_BRAND = "Gametitle_Raid(en)"
PURCHASE_CODE_NULL = "PurchaseCodeNull"
LOGGING_SCOPE = "https://www.googleapis.com/auth/logging.read"

# 영수증검증 결과 row 의 키는 한글 라벨(RECEIPT_FIELDS 의 두번째 값)
ROW_DESCRIPTION = "Description"
ROW_ORDER_ID = "주문 ID"


# ── 판정 헬퍼 ─────────────────────────────────────────────────────────────────

def classify_receipt_row(row: dict) -> str:
    """영수증검증 한 행을 패턴으로 분류. description 주축."""
    desc = (row.get(ROW_DESCRIPTION) or "").strip()
    if desc == "" or desc == PURCHASE_CODE_NULL:
        return "pattern2"   # 기록은 있으나 PurchaseCodeNull/빈값 → 미지급
    return "pattern3"        # description 정상 → ShopData Count 검토


def _select_target_row(rows, order_id):
    """문의 주문번호(order_id)와 일치하는 행 우선, 없으면 첫 행."""
    if order_id:
        target = order_id.strip()
        for row in rows:
            if (row.get(ROW_ORDER_ID) or "").strip() == target:
                return row
    return rows[0] if rows else None


def build_logging_service(key_path):
    """GCP Cloud Logging v2 service(읽기 전용 scope). 실패 시 None."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=[LOGGING_SCOPE]
        )
        return build("logging", "v2", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def resolve_product_via_gcp(logging_service, brand, uuid_value):
    """패턴1·2 보조: 브랜드 → GCP project/log → 직전 log_shop_click 의 shop_click_id.

    반환: (shop_click_id | None, error | None). 읽기 전용.
    """
    if logging_service is None:
        return None, "logging_service 없음(--key 미지정)"
    project, log_name = resolve_brand_gcp_log(brand)
    if not (project and log_name):
        return None, f"브랜드 '{brand}' GCP 프로젝트/로그 규칙 없음(env 확인)"
    entry, err = fetch_recent_shop_click_log(logging_service, project, log_name, uuid_value)
    if err or not entry:
        return None, err or "log_shop_click 로그 없음"
    payload = entry.get("jsonPayload", {}) or {}
    return payload.get("shop_click_id"), None


def lookup_count_readonly(page, uuid_value, table_name, purchase_code, timeout_error):
    """ShopData 조회 → 상세 팝업 → PurchaseCode 줄/Count (읽기 전용, 편집 진입 없음).

    같은 세션(이미 prepare_console_project 완료) 전제 — 게임정보 메뉴 이동부터.
    console_shopdata_lookup 의 조회 함수만 재사용하며, 편집(click_detail_edit_button,
    edit_count_line_to_zero_in_edit_mode)·Ace 쓰기는 호출하지 않는다.
    """
    open_game_info_menu(page)
    open_game_info_data_tab(page)
    fill_shopdata_uuid_filter(page, uuid_value)
    ensure_table_selected(page, table_name)
    click_shopdata_search_button(page)

    result_row = wait_for_shopdata_result_row(page, uuid_value, timeout_error)
    dialog = open_top_shopdata_detail(page, result_row, uuid_value, timeout_error)
    refresh_detail_popup_with_mouse_scroll(page, dialog)
    purchase_line_number, purchase_count = resolve_purchase_line_and_count(
        page, dialog, purchase_code
    )
    return {
        "purchase_line_number": purchase_line_number,
        "purchase_count": purchase_count,
        "count_judgment": "HELD",  # 주차/상품유형 복잡 → 자동 미지급 판정 보류(사람 확인)
    }


def judge_nonpayment(
    page,
    uuid_value,
    brand,
    *,
    order_id=None,
    table_name="ShopData",
    logging_service=None,
    start_url=DEFAULT_START_URL,
    project_name=DEFAULT_PROJECT_NAME,
    timeout_error=RuntimeError,
):
    """미지급 판정. (전제) Play 영수증 존재는 호출자가 확인.

    반환: verdict / receipt / matched_row / product_code / product_source /
          shopdata{purchase_line_number, purchase_count, count_judgment} | None / notes[]
    """
    notes = []
    receipt = run_receipt_verification(
        page, uuid_value, "", start_url, project_name, timeout_error
    )

    # 패턴1: 영수증검증 기록 없음 → 미지급. 상품은 로그로 특정.
    if not receipt.get("has_results"):
        product_code, err = resolve_product_via_gcp(logging_service, brand, uuid_value)
        if err:
            notes.append(f"상품 특정(GCP 로그) 실패: {err}")
        return {
            "verdict": "pattern1_no_receipt_record",
            "receipt": receipt,
            "matched_row": None,
            "product_code": product_code,
            "product_source": "gcp_log",
            "shopdata": None,
            "notes": notes,
        }

    rows = receipt.get("rows") or []
    matched = _select_target_row(rows, order_id)
    if matched is None:
        notes.append("결과 행은 있으나 대상 행을 고르지 못함")
        return {
            "verdict": "inconclusive",
            "receipt": receipt,
            "matched_row": None,
            "product_code": None,
            "product_source": None,
            "shopdata": None,
            "notes": notes,
        }

    pattern = classify_receipt_row(matched)

    # 패턴2: description = PurchaseCodeNull/빈값 → 미지급. 상품은 로그로 특정.
    if pattern == "pattern2":
        product_code, err = resolve_product_via_gcp(logging_service, brand, uuid_value)
        if err:
            notes.append(f"상품 특정(GCP 로그) 실패: {err}")
        return {
            "verdict": "pattern2_purchase_code_null",
            "receipt": receipt,
            "matched_row": matched,
            "product_code": product_code,
            "product_source": "gcp_log",
            "shopdata": None,
            "notes": notes,
        }

    # 패턴3: description 정상 → 상품코드 = description → ShopData Count 조회(판정 보류)
    product_code = (matched.get(ROW_DESCRIPTION) or "").strip()
    shopdata = None
    try:
        shopdata = lookup_count_readonly(
            page, uuid_value, table_name, product_code, timeout_error
        )
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 기록만 하고 결과 반환
        notes.append(f"ShopData Count 조회 실패: {exc}")
    notes.append("Count 자동 판정 보류(주차/상품유형) — 사람 확인 필요")
    return {
        "verdict": "pattern3_count_review",
        "receipt": receipt,
        "matched_row": matched,
        "product_code": product_code,
        "product_source": "description",
        "shopdata": shopdata,
        "notes": notes,
    }


# ── 출력·아티팩트 ─────────────────────────────────────────────────────────────

_SEP = "=" * 60


def print_result(result):
    print()
    print(_SEP)
    print(f" 미지급 판정: {result.get('verdict')}")
    print(_SEP)
    receipt = result.get("receipt") or {}
    print(f" 영수증검증 결과 : has_results={receipt.get('has_results')} row_count={receipt.get('row_count')}")
    print(f" 상품코드        : {result.get('product_code') or '(미특정)'} (source={result.get('product_source')})")
    shopdata = result.get("shopdata")
    if shopdata:
        print(f" ShopData Count  : line={shopdata.get('purchase_line_number')} "
              f"count={shopdata.get('purchase_count')} judgment={shopdata.get('count_judgment')}")
    for note in result.get("notes", []):
        print(f" - {note}")
    print(_SEP)


def save_artifacts(page, out_dir, uuid_value, succeeded, result=None, error_message=""):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_payment_error_{ts}"
    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as exc:
        print(f"  (스크린샷 저장 실패: {exc})")
    try:
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML 저장 실패: {exc})")

    lines = [f"success={succeeded}", f"uuid={uuid_value}", f"url={page.url}"]
    if result:
        lines.append(f"verdict={result.get('verdict')}")
        lines.append(f"product_code={result.get('product_code')}")
        lines.append(f"product_source={result.get('product_source')}")
        shopdata = result.get("shopdata") or {}
        lines.append(f"purchase_line_number={shopdata.get('purchase_line_number')}")
        lines.append(f"purchase_count={shopdata.get('purchase_count')}")
        lines.append(f"count_judgment={shopdata.get('count_judgment')}")
        for note in result.get("notes", []):
            lines.append(f"note={note}")
    if error_message:
        lines.append(f"error={error_message}")
    try:
        Path(f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        print(f"  (요약 저장 실패: {exc})")
    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        page.wait_for_timeout(1_000)


def parse_args():
    parser = argparse.ArgumentParser(description="미지급(결제오류) 판정 (읽기 전용)")
    parser.add_argument("--uuid", default=DEFAULT_UUID, help="대상 유저 UUID")
    parser.add_argument("--brand", default=DEFAULT_BRAND, help="cs 브랜드(패키지/GCP 규칙 키)")
    parser.add_argument("--order-id", dest="order_id", default="", help="문의 주문번호(영수증검증 행 매칭용, 선택)")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME, help="ShopData 테이블명")
    parser.add_argument("--key", default="", help="GCP 서비스계정 JSON 키(패턴1·2 로그 상품특정용, 선택)")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    return parser.parse_args()


def main():
    args = parse_args()
    sync_playwright, timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    logging_service = build_logging_service(args.key) if args.key else None
    if args.key and logging_service is None:
        print("[경고] GCP logging service 생성 실패 — 패턴1/2 상품 특정(로그)이 불가합니다.")

    print("=" * 60)
    print(" Console 미지급(결제오류) 판정 — 읽기 전용")
    print("=" * 60)
    print(f"대상 UUID  : {args.uuid}")
    print(f"브랜드     : {args.brand}")
    print(f"주문번호   : {args.order_id or '(미지정)'}")

    succeeded = False
    error_message = ""
    result = None

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page = select_target_page(context, page)
            result = judge_nonpayment(
                page,
                args.uuid,
                args.brand,
                order_id=args.order_id or None,
                table_name=args.table_name,
                logging_service=logging_service,
                start_url=args.start_url,
                project_name=args.project_name,
                timeout_error=timeout_error,
            )
            succeeded = True
            print_result(result)
            hold_browser_open(page, args.hold_seconds)
        except Exception as exc:
            error_message = str(exc)
            print(f"\n[오류] {error_message}")
        finally:
            try:
                page = select_target_page(context, page)
                save_artifacts(page, out_dir, args.uuid, succeeded, result, error_message)
            finally:
                context.close()

    if not succeeded:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n사용자 요청으로 종료했습니다.")
        sys.exit(130)
