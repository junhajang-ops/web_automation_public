# -*- coding: utf-8 -*-
"""
console_payment_error.py — 미지급(결제오류) 판정 (설계서 3-B)
================================================================

전제: Play API에 영수증 존재(결제 발생)는 호출자가 이미 확인(설계서 3-A).

★ 2026-07-07: run_receipt_verification이 영수증검증 UUID 입력 전 '유저' 탭에서 UUID
  존재 여부를 먼저 확인한다(ensure_uuid_registered, console_user_search.py). 존재하지
  않는(오탈자 등) UUID는 InvalidUuidError로 여기서 실패하므로, 아래 패턴1("기록 없음")은
  항상 "실존하는 UUID인데 기록이 없음"만을 의미한다 — 무효 UUID를 미지급으로 오판하지 않는다.

판정은 두 분기로 완전히 나뉘며 서로 의존하지 않는다(영수증검증 description 주축):
  - 분기A(패턴1·2, 미지급 확정): 기록 없음 / description=PurchaseCodeNull(또는 빈값).
    → 이미 미지급이 확정된 상태이므로 남은 목적은 "무엇을 재지급할지 상품만 특정"하는 것.
    Play `productId`(=StorePurchaseCode_AOS) → CSV로 Inapp 후보 집합 산출 → 결제 시각
    기준 이전 300초 이내(env `PAYMENT_ERROR_CLICK_WINDOW_SECONDS`) log_shop_click 중
    후보에 속하는 것만 필터 → 0/1/N건 그대로 나열(자동으로 1건을 확정하지 않음 — 2건
    이상이면 사람이 최종 선택).
  - 분기B(패턴3): description 정상(상품코드 있음).
    → 상품코드=description 그대로 사용, GCP 로그는 보지 않고 곧바로 ShopData Count 조회.
    - ShopData PurchaseCode 배열에 그 코드 자체가 없음(`PurchaseCodeNotFoundError`) → 구매
      시도 기록조차 없다는 뜻이라 **미지급 확정**(`pattern3_code_not_found`, 사람 승인 후 재지급).
    - 코드는 있음 → 상품표 CSV의 `Purchase_Limit_Type`/`Purchase_Limit_Count`로 자동 판정 여부를
      가른다(2026-07-07 Excel 확인, chart_Shop_248162 기준):
      - `Daily`/`Weekly`/`Monthly`(주기적으로 초기화) 또는 유형 미확인 → 현재 Count만으로 과거
        지급 여부를 단정할 수 없어 자동 판정 보류, 조회·표시만 하고 사람 확인(`pattern3_count_review`).
      - `None`/`Onetime`(초기화 없는 상품) & `Purchase_Limit_Count`==1(진짜 단일구매) → 누적
        Count 값 자체로 판정. Count=0 → **미지급 확정**(`pattern3_count_confirmed_missing`),
        Count≥1 → **이미 지급됨**(`pattern3_count_confirmed_granted`, 재지급 불필요).
      - `None`/`Onetime` & `Purchase_Limit_Count`==0(무제한) 또는 2 이상(다회구매가능) →
        **Count≥1이 곧 지급 확정을 의미하지 않는다**(2026-07-07 피드백). 영수증검증 전체 내역에서
        동일 Description 과거 구매 건수(attempts)와 ShopData Count를 대조해, Count가 attempts보다
        적으면 그 차이만큼 **미지급 확정**, 아니면 **이미 지급됨**으로 자동 판정한다.

코드 체계: 영수증검증 description = ShopData PurchaseCode = 상품표 Inapp_PurchaseCode
          = shop_click_id (출처별 컬럼명만 다른 같은 값). Play 영수증과 잇는 키만 StorePurchaseCode_AOS
          (1:N — 하나의 AOS 코드에 여러 Inapp이 매핑되는 사례 실측 확인. CSV 역매핑은 후보 집합만 좁힌다).

★ 읽기 전용: ShopData 편집 모드 진입·Count 변경 등 쓰기 동작 없음. 재지급 실행은 사람 승인 후 별도.
"""

import argparse
import csv as csv_mod
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# cs 모듈(브랜드→GCP 규칙, log_shop_click 조회) import 위해 경로 추가
_CS_DIR = BASE_DIR.parent / "cs"
if str(_CS_DIR) not in sys.path:
    sys.path.insert(0, str(_CS_DIR))

from console_step_verify import (
    configure_console_output,
    get_retry_max_retries,
    init_dump_dir,
    retry_with_recovery,
    save_page_artifacts,
)
from console_user_search import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    InvalidUuidError,
    hold_open_loop,
    load_playwright,
    prepare_console_project,
    select_target_page,
)
from console_chart_lookup import PAYMENT_DOCS_DIR
from console_receipt_verification import run_receipt_verification
from console_shopdata_lookup import (
    PurchaseCodeNotFoundError,
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
from cs_gcp_logging import build_logging_service, fetch_shop_click_candidates_in_window
from test_config import TEST_CHART_NAME, TEST_TABLE_NAME, TEST_UUID, apply_title_profile

DEFAULT_OUTPUT = "dumps_console_payment_error"
DEFAULT_UUID = TEST_UUID
DEFAULT_TABLE_NAME = TEST_TABLE_NAME
DEFAULT_BRAND = "Gametitle_Raid(en)"
PURCHASE_CODE_NULL = "PurchaseCodeNull"
RETRY_MAX_RETRIES = get_retry_max_retries()
# 분기A(패턴1·2) click 매칭 시각 윈도우 — 결제(orders.get createTime) 기준 이전 N초.
# 분석 근거: click→purchase 전환 최대 287초·99%가 60초 이내 → 300초면 여유있게 충분.
CLICK_MATCH_WINDOW_SECONDS = max(1, int(os.environ.get("PAYMENT_ERROR_CLICK_WINDOW_SECONDS", "300")))
# subprocess 호출자(cs co-pilot)가 결과를 회수할 때 쓰는 마커.
# cs_copilot.py 의 PAYMENT_ERROR_JSON_MARKER 와 반드시 같아야 한다.
PAYMENT_ERROR_JSON_MARKER = "===PAYMENT_ERROR_JSON==="

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
    """문의 주문번호(order_id)와 완전일치하는 행만 반환한다.

    주문번호가 없거나 일치하는 행이 없으면 무엇을 봐야 할지 모른다는 뜻이므로,
    다른 결제 건일 수 있는 첫 행을 임의로 집어 판정을 이어가지 않고 None을 반환한다
    (호출부가 verdict="inconclusive"로 사람 확인을 요청한다).
    """
    if not order_id:
        return None
    target = order_id.strip()
    for row in rows:
        if (row.get(ROW_ORDER_ID) or "").strip() == target:
            return row
    return None


# Purchase_Limit_Type 값 체계(2026-07-07 Excel 확인, chart_Shop_248162):
# None/Onetime = 초기화 없는 상품(누적 Count 그대로 신뢰 가능) vs Daily/Weekly/Monthly =
# 주기적으로 초기화되는 상품(현재 Count만으로 과거 지급 여부를 단정할 수 없어 사람 확인 필요).
#
# 다만 초기화가 없어도 Purchase_Limit_Count(구매 가능 횟수)가 1이 아니면(0=무제한, 2 이상=
# 다회 구매 가능) "Count>=1"이 곧 "이번 결제 지급 확정"을 의미하지 않는다(2026-07-07 사용자 피드백:
# 복수 결제 가능한 상품은 영수증검증 내역의 동일 Description 과거 구매 건수와 대조해 부족분이
# 있는지 확인해야 한다). Purchase_Limit_Count==1(진짜 단일구매)일 때만 Count 값 자체로 판정한다.
NO_RESET_PURCHASE_LIMIT_TYPES = {"None", "Onetime"}


def load_purchase_limit_info_map(chart_name=None):
    """web_docs/ 최신 chart_{chart_name}_*.csv에서
    {Inapp_PurchaseCode: {"type": Purchase_Limit_Type, "count": Purchase_Limit_Count(int|None)}} 매핑 생성.

    load_shop_aos_candidates()와 동일한 "최신 CSV = 파일명 정렬 마지막" 규칙을 따른다.
    """
    chart_name = chart_name or TEST_CHART_NAME
    csvs = sorted(PAYMENT_DOCS_DIR.glob(f"chart_{chart_name}_*.csv"))
    if not csvs:
        raise RuntimeError(f"web_docs/ 에 '{chart_name}' CSV 없음 — console_chart_lookup.py 먼저 실행 필요")
    csv_path = csvs[-1]

    for enc in ("utf-8-sig", "utf-8", "euc-kr"):
        try:
            mapping = {}
            with open(csv_path, encoding=enc, newline="") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    inapp = (row.get("Inapp_PurchaseCode") or "").strip()
                    if not inapp:
                        continue
                    limit_type = (row.get("Purchase_Limit_Type") or "").strip()
                    limit_count_raw = (row.get("Purchase_Limit_Count") or "").strip()
                    try:
                        limit_count = int(limit_count_raw)
                    except ValueError:
                        limit_count = None
                    mapping[inapp] = {"type": limit_type, "count": limit_count}
            return mapping
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"CSV 인코딩 판별 실패: {csv_path.name}")


def load_shop_aos_candidates(chart_name=None):
    """web_docs/ 최신 chart_{chart_name}_*.csv에서 {StorePurchaseCode_AOS: set(Inapp_PurchaseCode)} 매핑 생성.

    AOS는 1:N(하나의 AOS 코드에 여러 Inapp이 매핑되는 사례 실측 확인)이므로 이 매핑은
    "후보 집합"일 뿐 그 자체로 상품을 확정하지 않는다. console_post_register.py의
    load_shop_table_id()와 동일한 "최신 CSV = 파일명 정렬 마지막"규칙을 그대로 따른다.
    """
    chart_name = chart_name or TEST_CHART_NAME
    csvs = sorted(PAYMENT_DOCS_DIR.glob(f"chart_{chart_name}_*.csv"))
    if not csvs:
        raise RuntimeError(f"web_docs/ 에 '{chart_name}' CSV 없음 — console_chart_lookup.py 먼저 실행 필요")
    csv_path = csvs[-1]

    for enc in ("utf-8-sig", "utf-8", "euc-kr"):
        try:
            mapping = {}
            with open(csv_path, encoding=enc, newline="") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    aos = (row.get("StorePurchaseCode_AOS") or "").strip()
                    inapp = (row.get("Inapp_PurchaseCode") or "").strip()
                    if not aos or not inapp:
                        continue
                    mapping.setdefault(aos, set()).add(inapp)
            return mapping
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"CSV 인코딩 판별 실패: {csv_path.name}")


def resolve_product_candidates_via_gcp(logging_service, brand, uuid_value, product_id, order_create_time):
    """분기A(패턴1·2) 상품 특정: AOS(product_id) → CSV 후보(Inapp) → 결제 시각 이전
    CLICK_MATCH_WINDOW_SECONDS초 이내 log_shop_click 중 후보에 속하는 것만 매칭.

    매칭된 후보를 전부 반환한다(자동으로 1건을 확정하지 않음 — 2건 이상이면 사람이
    최종 선택). 읽기 전용. 반환: (candidates: list[dict], error: str | None).
    """
    if logging_service is None:
        return [], "logging_service 없음(--key 미지정)"
    if not product_id:
        return [], "Play productId(AOS) 없음 — 후보 조회 불가"
    if not order_create_time:
        return [], "결제 시각(createTime) 없음 — 후보 조회 불가"

    project, log_name = resolve_brand_gcp_log(brand)
    if not (project and log_name):
        return [], f"브랜드 '{brand}' GCP 프로젝트/로그 규칙 없음(env 확인)"

    try:
        aos_candidates = load_shop_aos_candidates()
    except Exception as exc:  # noqa: BLE001
        return [], f"CSV 후보 로드 실패: {exc}"

    inapp_candidates = aos_candidates.get(product_id)
    if not inapp_candidates:
        return [], f"CSV에서 AOS '{product_id}'에 대응하는 Inapp 후보 없음"

    entries, err = fetch_shop_click_candidates_in_window(
        logging_service, project, log_name, uuid_value, order_create_time,
        window_seconds=CLICK_MATCH_WINDOW_SECONDS,
    )
    matched = [e for e in entries if e.get("shop_click_id") in inapp_candidates]
    return matched, err


def lookup_count_readonly(page, uuid_value, table_name, purchase_code, timeout_error):
    """ShopData 조회 → 상세 팝업 → PurchaseCode 줄/Count (읽기 전용, 편집 진입 없음).

    같은 세션(이미 prepare_console_project 완료) 전제 — 게임정보 메뉴 이동부터.
    console_shopdata_lookup 의 조회 함수만 재사용하며, 편집(click_detail_edit_button,
    edit_count_line_to_zero_in_edit_mode)·Ace 쓰기는 호출하지 않는다.

    Count 자동/보류 판정은 여기서 내리지 않는다 — Purchase_Limit_Count>=2(다회구매가능)일 때는
    영수증검증 전체 내역과 대조해야 하므로, 그 정보를 가진 judge_nonpayment 쪽 resolve_count_judgment()가
    최종 판정을 담당한다.
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

    purchase_limit_type = None
    purchase_limit_count = None
    try:
        limit_info = load_purchase_limit_info_map().get(purchase_code) or {}
        purchase_limit_type = limit_info.get("type")
        purchase_limit_count = limit_info.get("count")
    except Exception as exc:  # noqa: BLE001 — 조회 실패 시 기존처럼 보류(HELD)로 안전하게 처리
        print(f"    (Purchase_Limit_Type/Count 조회 실패 — 보류 판정 유지: {exc})")

    return {
        "purchase_line_number": purchase_line_number,
        "purchase_count": purchase_count,
        "purchase_limit_type": purchase_limit_type,
        "purchase_limit_count": purchase_limit_count,
    }


def count_receipt_matches(rows, product_code):
    """영수증검증 전체 내역 중 동일 Description(=product_code)인 행 수(과거 구매 시도 건수)."""
    return sum(1 for row in rows if (row.get(ROW_DESCRIPTION) or "").strip() == product_code)


def resolve_count_judgment(shopdata, rows, product_code):
    """Purchase_Limit_Type/Count 기반 Count 자동/보류 판정.

    - Daily/Weekly/Monthly 또는 유형 미확인 → 주기적 초기화로 현재 Count만으론 단정 불가 → HELD.
    - None/Onetime(초기화 없음) & Purchase_Limit_Count==1(진짜 단일구매) → Count 값 자체로 판정
      (Count=0 → 미지급 확정, Count>=1 → 지급 확정).
    - None/Onetime & Purchase_Limit_Count==0(무제한) 또는 2 이상(다회구매가능) → Count>=1만으로는
      "이번 결제가 지급됐다"를 보장하지 못한다(2026-07-07 피드백). 영수증검증 전체 내역에서 동일
      Description 과거 구매 건수(attempts)와 ShopData Count를 대조해, Count가 attempts보다 적으면
      그 차이만큼 미지급으로 확정한다.

    반환: (count_judgment: "HELD"|"CONFIRMED_MISSING"|"CONFIRMED_GRANTED", notes: list[str])
    """
    notes = []
    limit_type = shopdata.get("purchase_limit_type")
    limit_count = shopdata.get("purchase_limit_count")
    purchase_count = shopdata.get("purchase_count")

    if limit_type not in NO_RESET_PURCHASE_LIMIT_TYPES:
        notes.append("Count 자동 판정 보류(주차/상품유형) — 사람 확인 필요")
        return "HELD", notes

    if limit_count == 1:
        if purchase_count == 0:
            notes.append(f"Purchase_Limit_Type={limit_type}(단일구매, 초기화 없음) → Count=0 → 미지급 확정")
            return "CONFIRMED_MISSING", notes
        notes.append(f"Purchase_Limit_Type={limit_type}(단일구매, 초기화 없음) → Count={purchase_count} → 이미 지급됨(재지급 불필요)")
        return "CONFIRMED_GRANTED", notes

    attempts = count_receipt_matches(rows, product_code)
    missing = attempts - purchase_count
    if missing > 0:
        notes.append(
            f"Purchase_Limit_Type={limit_type}(다회구매가능, limit_count={limit_count}) → "
            f"영수증검증 동일상품 구매기록 {attempts}건 vs ShopData Count {purchase_count}건 → {missing}건 미지급 확정"
        )
        return "CONFIRMED_MISSING", notes
    notes.append(
        f"Purchase_Limit_Type={limit_type}(다회구매가능, limit_count={limit_count}) → "
        f"영수증검증 동일상품 구매기록 {attempts}건 vs ShopData Count {purchase_count}건 → 전체 지급 확인(재지급 불필요)"
    )
    return "CONFIRMED_GRANTED", notes


def _resolve_gcp_candidates_result(logging_service, brand, uuid_value, product_id, order_create_time, notes):
    """분기A(패턴1·2) 공용: 후보 조회 결과를 product_code/product_candidates로 정리.

    후보 0건 → product_code=None. 1건 → 그 shop_click_id로 확정. 2건 이상 →
    product_code=None, product_candidates에 전부 담아 사람이 최종 선택하게 한다.
    """
    candidates, err = resolve_product_candidates_via_gcp(
        logging_service, brand, uuid_value, product_id, order_create_time
    )
    if err:
        notes.append(f"상품 특정(GCP 로그) 실패/불완전: {err}")
    elif len(candidates) == 0:
        # err가 없는 0건은 "조회를 안 함"이 아니라 "조회는 됐지만 매칭 없음"이다.
        # 이 note가 없으면 화면상 (미특정)만 보여 두 경우를 구분할 수 없다(2026-07-10 사용자 확인).
        notes.append(
            f"GCP 로그 후보 0건 — 결제시각 이전 {CLICK_MATCH_WINDOW_SECONDS}초 내 매칭 "
            "log_shop_click 없음(조회 자체는 정상 수행됨)"
        )
    elif len(candidates) > 1:
        notes.append(f"후보 {len(candidates)}건 — 자동 확정 안 함, product_candidates 참조해 사람 확인")
    product_code = candidates[0].get("shop_click_id") if len(candidates) == 1 else None
    return product_code, candidates


def judge_nonpayment(
    page,
    uuid_value,
    brand,
    *,
    order_id=None,
    product_id=None,
    order_create_time=None,
    nickname=None,
    nickname_source=None,
    table_name="ShopData",
    logging_service=None,
    start_url=DEFAULT_START_URL,
    project_name=DEFAULT_PROJECT_NAME,
    timeout_error=RuntimeError,
):
    """미지급 판정. (전제) Play 영수증 존재는 호출자가 확인.

    product_id(Play productId=StorePurchaseCode_AOS)·order_create_time(Play createTime)은
    분기A(패턴1·2)의 GCP 로그 후보 조회에만 쓰인다. 분기B(패턴3)는 description만 쓰므로 불필요.

    nickname(티켓 제출 닉네임, 선택)은 uuid_value가 '유저' 탭에서 존재하지 않을 때만 쓰인다
    (2026-07-08 사용자 지시): '닉네임'으로 재검색해 나온 후보와 uuid_value의 편집거리가
    2 이하(env USER_UUID_NICKNAME_MAX_DISTANCE)인 후보가 정확히 1개면 동일인의 오탈자로
    판정하고, 그 콘솔 확정 UUID로 유저 존재를 확정해 이후 절차(영수증검증/ShopData/GCP
    로그 조회)를 그 UUID로 진행한다 — 자세한 내용은 console_user_search.ensure_uuid_registered.
    nickname_source는 참고용 출처 표기("custom_field" | "sender_display_name")이며 판정
    로직·신뢰도에는 영향을 주지 않는다(오qupie '보낸 사람' 표시명이 실제 닉네임이 오도록
    설정돼 있음을 사용자가 확인, 2026-07-08).

    반환: verdict / receipt / matched_row / product_code / product_source /
          product_candidates(list|None) / shopdata{purchase_line_number, purchase_count,
          purchase_limit_type, purchase_limit_count, count_judgment} | None / notes[] /
          submitted_uuid(티켓 제출 UUID 원본) / resolved_uuid(실제 조회에 쓴 확정 UUID —
          닉네임 대조로 오탈자 보정이 없었으면 submitted_uuid와 동일)
    """
    notes = []
    try:
        receipt = run_receipt_verification(
            page, uuid_value, "", start_url, project_name, timeout_error,
            nickname=nickname, nickname_source=nickname_source,
        )
    except InvalidUuidError as exc:
        # '유저' 탭에서 존재하지 않는다고 이미 확정된 UUID — 오탈자 등 결정론적 문제라
        # 재시도해도 결과가 바뀌지 않는다. main()의 retry_with_recovery(절차 전체 재시도)까지
        # 전파시키지 않고 여기서 바로 정상 반환해 재시도로 시간을 낭비하지 않는다
        # (2026-07-08 사용자 제보: 무효 UUID 판정 실패까지 시간이 너무 걸림). 닉네임 대조
        # 결과(있었다면)도 이미 이 예외 메시지에 포함돼 있다(ensure_uuid_registered 참고).
        print(f" [미지급 판정] '{uuid_value}' — '유저' 탭에서 존재하지 않는 UUID로 확인됨(재시도 없이 즉시 종료)")
        return {
            "verdict": "invalid_uuid",
            "receipt": None,
            "matched_row": None,
            "product_code": None,
            "product_source": None,
            "product_candidates": None,
            "shopdata": None,
            "notes": [str(exc)],
            "submitted_uuid": uuid_value,
            "resolved_uuid": None,
        }

    effective_uuid = receipt.get("resolved_uuid") or uuid_value
    if effective_uuid != uuid_value:
        notes.append(
            f"닉네임 대조로 UUID 오탈자 확정 — 제출값={uuid_value} → 확정값={effective_uuid} "
            f"(이후 조회는 확정값 기준, nickname_source={nickname_source})"
        )
        print(f" [미지급 판정] 닉네임 대조로 UUID 오탈자 확정 → 이후 조회는 '{effective_uuid}' 기준으로 진행")

    # 분기A 패턴1: 영수증검증 기록 없음 → 미지급 확정. 상품은 로그 후보로 나열.
    if not receipt.get("has_results"):
        print(" [미지급 판정] 영수증검증 기록 없음 → 패턴1(미지급 확정) — GCP 로그 후보 조회로 상품 특정")
        product_code, candidates = _resolve_gcp_candidates_result(
            logging_service, brand, effective_uuid, product_id, order_create_time, notes
        )
        return {
            "verdict": "pattern1_no_receipt_record",
            "receipt": receipt,
            "matched_row": None,
            "product_code": product_code,
            "product_source": "gcp_log_candidates",
            "product_candidates": candidates,
            "shopdata": None,
            "notes": notes,
            "submitted_uuid": uuid_value,
            "resolved_uuid": effective_uuid,
        }

    rows = receipt.get("rows") or []
    matched = _select_target_row(rows, order_id)
    if matched is None:
        if not order_id:
            notes.append("결과 행은 있으나 주문번호가 없어 대상 행을 특정하지 못함")
        else:
            notes.append(f"결과 행은 있으나 주문번호 '{order_id}'와 일치하는 행이 없음")
        return {
            "verdict": "inconclusive",
            "receipt": receipt,
            "matched_row": None,
            "product_code": None,
            "product_source": None,
            "product_candidates": None,
            "shopdata": None,
            "notes": notes,
            "submitted_uuid": uuid_value,
            "resolved_uuid": effective_uuid,
        }

    pattern = classify_receipt_row(matched)
    matched_desc = matched.get(ROW_DESCRIPTION) or "(빈값)"
    print(f" [미지급 판정] 매칭 행 발견 — description='{matched_desc}' → {pattern}")

    # 분기A 패턴2: description = PurchaseCodeNull/빈값 → 미지급 확정. 상품은 로그 후보로 나열.
    if pattern == "pattern2":
        print(" [미지급 판정] description=PurchaseCodeNull/빈값 → 패턴2(미지급 확정) — GCP 로그 후보 조회로 상품 특정")
        product_code, candidates = _resolve_gcp_candidates_result(
            logging_service, brand, effective_uuid, product_id, order_create_time, notes
        )
        return {
            "verdict": "pattern2_purchase_code_null",
            "receipt": receipt,
            "matched_row": matched,
            "product_code": product_code,
            "product_source": "gcp_log_candidates",
            "product_candidates": candidates,
            "shopdata": None,
            "notes": notes,
            "submitted_uuid": uuid_value,
            "resolved_uuid": effective_uuid,
        }
    # 분기B 패턴3: description 정상 → 상품코드 = description → 로그는 안 보고 곧바로
    # ShopData Count 조회(판정 보류). 분기A와 완전히 분리 — product_id/order_create_time 불필요.
    # classify_receipt_row가 이미 pattern2(빈값/PurchaseCodeNull)를 걸러냈으므로 matched_desc는 실값이다.
    product_code = matched_desc.strip()
    print(f" [미지급 판정] description 정상('{product_code}') → 패턴3(로그 미사용) — ShopData Count 조회로 이동")
    shopdata = None
    try:
        shopdata = lookup_count_readonly(
            page, effective_uuid, table_name, product_code, timeout_error
        )
    except PurchaseCodeNotFoundError as exc:
        # ShopData PurchaseCode 배열 자체에 해당 코드가 없음 = 구매 시도 기록조차 없음 → 미지급 확정
        # (Count 값 판정 보류와는 다르다 — 여기는 코드 존재 여부라 주차/상품유형 애매성이 없다).
        notes.append(f"ShopData PurchaseCode에 해당 코드 자체가 없음 → 미지급 확정: {exc}")
        print(f" [미지급 판정] ShopData에 '{product_code}' 코드 자체가 없음 → 미지급 확정")
        return {
            "verdict": "pattern3_code_not_found",
            "receipt": receipt,
            "matched_row": matched,
            "product_code": product_code,
            "product_source": "description",
            "product_candidates": None,
            "shopdata": None,
            "notes": notes,
            "submitted_uuid": uuid_value,
            "resolved_uuid": effective_uuid,
        }
    except Exception as exc:  # noqa: BLE001 — 그 외 조회 실패는 기록만 하고 결과 반환
        notes.append(f"ShopData Count 조회 실패: {exc}")

    if shopdata is None:
        count_judgment = "HELD"
        notes.append("Count 자동 판정 보류(ShopData 조회 실패) — 사람 확인 필요")
    else:
        count_judgment, judgment_notes = resolve_count_judgment(shopdata, rows, product_code)
        shopdata["count_judgment"] = count_judgment
        notes.extend(judgment_notes)

    if count_judgment == "CONFIRMED_MISSING":
        verdict = "pattern3_count_confirmed_missing"
    elif count_judgment == "CONFIRMED_GRANTED":
        verdict = "pattern3_count_confirmed_granted"
    else:
        verdict = "pattern3_count_review"

    return {
        "verdict": verdict,
        "receipt": receipt,
        "matched_row": matched,
        "product_code": product_code,
        "product_source": "description",
        "product_candidates": None,
        "shopdata": shopdata,
        "notes": notes,
        "submitted_uuid": uuid_value,
        "resolved_uuid": effective_uuid,
    }


# ── 출력·아티팩트 ─────────────────────────────────────────────────────────────

_SEP = "=" * 60


def print_result(result):
    print()
    print(_SEP)
    print(f" 미지급 판정: {result.get('verdict')}")
    print(_SEP)
    submitted_uuid = result.get("submitted_uuid")
    resolved_uuid = result.get("resolved_uuid")
    if submitted_uuid and resolved_uuid and submitted_uuid != resolved_uuid:
        print(f" UUID            : 제출값={submitted_uuid} → 닉네임 대조로 확정={resolved_uuid}")
    receipt = result.get("receipt") or {}
    print(f" 영수증검증 결과 : has_results={receipt.get('has_results')} row_count={receipt.get('row_count')}")
    print(f" 상품코드        : {result.get('product_code') or '(미특정)'} (source={result.get('product_source')})")
    candidates = result.get("product_candidates")
    if candidates:
        print(f" GCP 로그 후보 {len(candidates)}건(자동 미확정 — 사람 확인):")
        for c in candidates:
            print(f"   - {c.get('shop_click_id', '?')} @ {c.get('update_date', '?')} "
                  f"(price={c.get('shop_click_price', '?')}, category={c.get('shop_click_category', '?')})")
    shopdata = result.get("shopdata")
    if shopdata:
        print(f" ShopData Count  : line={shopdata.get('purchase_line_number')} "
              f"count={shopdata.get('purchase_count')} "
              f"limit_type={shopdata.get('purchase_limit_type')} "
              f"limit_count={shopdata.get('purchase_limit_count')} "
              f"judgment={shopdata.get('count_judgment')}")
    for note in result.get("notes", []):
        print(f" - {note}")
    print(_SEP)


def emit_json_result(result, succeeded, error_message):
    """subprocess 호출자(co-pilot)가 회수하도록 결과 요약을 JSON 한 줄로 출력.

    PII가 많은 receipt.rows 는 제외하고 판정 핵심만 담는다.
    """
    payload = {
        "success": succeeded,
        "verdict": (result or {}).get("verdict"),
        "submitted_uuid": (result or {}).get("submitted_uuid"),
        "resolved_uuid": (result or {}).get("resolved_uuid"),
        "product_code": (result or {}).get("product_code"),
        "product_source": (result or {}).get("product_source"),
        "product_candidates": (result or {}).get("product_candidates"),
        "shopdata": (result or {}).get("shopdata"),
        "notes": (result or {}).get("notes", []),
        "error": error_message or None,
    }
    print(PAYMENT_ERROR_JSON_MARKER)
    print(json.dumps(payload, ensure_ascii=False))


def save_artifacts(page, out_dir, uuid_value, succeeded, result=None, error_message=""):
    lines = [f"success={succeeded}", f"uuid={uuid_value}", f"url={page.url}"]
    if result:
        lines.append(f"verdict={result.get('verdict')}")
        lines.append(f"submitted_uuid={result.get('submitted_uuid')}")
        lines.append(f"resolved_uuid={result.get('resolved_uuid')}")
        lines.append(f"product_code={result.get('product_code')}")
        lines.append(f"product_source={result.get('product_source')}")
        candidates = result.get("product_candidates") or []
        lines.append(f"product_candidates_count={len(candidates)}")
        for c in candidates:
            lines.append(f"product_candidate={c.get('shop_click_id')}@{c.get('update_date')}")
        shopdata = result.get("shopdata") or {}
        lines.append(f"purchase_line_number={shopdata.get('purchase_line_number')}")
        lines.append(f"purchase_count={shopdata.get('purchase_count')}")
        lines.append(f"purchase_limit_type={shopdata.get('purchase_limit_type')}")
        lines.append(f"purchase_limit_count={shopdata.get('purchase_limit_count')}")
        lines.append(f"count_judgment={shopdata.get('count_judgment')}")
        for note in result.get("notes", []):
            lines.append(f"note={note}")
    if error_message:
        lines.append(f"error={error_message}")
    save_page_artifacts(page, out_dir, "console_payment_error", lines)


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return
    print(f"현재 화면을 {hold_seconds}초 동안 유지합니다.")
    hold_open_loop(page, hold_seconds)


def parse_args():
    parser = argparse.ArgumentParser(description="미지급(결제오류) 판정 (읽기 전용)")
    parser.add_argument("--uuid", default=DEFAULT_UUID, help="대상 유저 UUID")
    parser.add_argument("--brand", default=DEFAULT_BRAND, help="cs 브랜드(패키지/GCP 규칙 키)")
    parser.add_argument("--order-id", dest="order_id", default="", help="문의 주문번호(영수증검증 행 매칭용, 선택)")
    parser.add_argument(
        "--nickname",
        default="",
        help=(
            "티켓 제출 닉네임. UUID가 '유저' 탭에서 무효로 판정될 때만 '닉네임'으로 재검색해 "
            "오탈자 여부를 대조하는 데 쓰인다(선택, 미지정 시 대조 없이 바로 실패)"
        ),
    )
    parser.add_argument(
        "--nickname-source",
        dest="nickname_source",
        default="",
        choices=["", "custom_field", "sender_display_name"],
        help="--nickname 값의 출처(선택, 참고용 표기일 뿐 판정에는 영향 없음)",
    )
    parser.add_argument("--product-id", dest="product_id", default="",
                         help="Play productId(=StorePurchaseCode_AOS). 분기A(패턴1·2) 후보 조회용, 선택")
    parser.add_argument("--order-time", dest="order_create_time", default="",
                         help="Play orders.get createTime(RFC3339). 분기A(패턴1·2) 후보 조회용, 선택")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME, help="ShopData 테이블명")
    parser.add_argument("--key", default="", help="GCP 서비스계정 JSON 키(패턴1·2 로그 후보조회용, 선택)")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument(
        "--title",
        default="",
        metavar="NAME",
        help="Title env profile to apply (example: gametitle)",
    )
    parser.add_argument("--gametitle", action="store_true", help="Shortcut for --title gametitle")
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument("--emit-json", action="store_true",
                        help="결과 요약을 stdout에 JSON 한 줄로 출력(co-pilot subprocess 연계용)")
    return parser.parse_args()


def main():
    configure_console_output()
    args = parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
        include_key_file=True,
    )
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
    print(f"닉네임     : {args.nickname or '(미지정)'}")
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
            result = retry_with_recovery(
                action=lambda: judge_nonpayment(
                    page,
                    args.uuid,
                    args.brand,
                    order_id=args.order_id or None,
                    product_id=args.product_id or None,
                    order_create_time=args.order_create_time or None,
                    nickname=args.nickname or None,
                    nickname_source=args.nickname_source or None,
                    table_name=args.table_name,
                    logging_service=logging_service,
                    start_url=args.start_url,
                    project_name=args.project_name,
                    timeout_error=timeout_error,
                ),
                recovery=lambda: prepare_console_project(
                    page=page,
                    explicit_project_base="",
                    start_url=args.start_url,
                    project_name=args.project_name,
                ),
                label=f"미지급 판정 UUID {args.uuid} 재시도",
                recovery_desc=f"콘솔 초기화면({args.start_url})/프로젝트 선택부터 다시 준비합니다.",
                max_retries=RETRY_MAX_RETRIES,
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

    if args.emit_json:
        emit_json_result(result, succeeded, error_message)

    if not succeeded:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n사용자 요청으로 종료했습니다.")
        sys.exit(130)
