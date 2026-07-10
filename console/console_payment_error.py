# -*- coding: utf-8 -*-
"""
console_payment_error.py — 미지급(결제오류) 판정 (설계서 3-B)
================================================================

전제: Play API에 영수증 존재(결제 발생)는 호출자가 이미 확인(설계서 3-A).

★ 2026-07-07: run_receipt_verification이 영수증검증 UUID 입력 전 '유저' 탭에서 UUID
  존재 여부를 먼저 확인한다(ensure_uuid_registered, console_user_search.py). 존재하지
  않는(오탈자 등) UUID는 InvalidUuidError로 여기서 실패하므로, 아래 "기록 없음" 분기는
  항상 "실존하는 UUID인데 기록이 없음"만을 의미한다 — 무효 UUID를 미지급으로 오판하지 않는다.

판정은 두 분기로 완전히 나뉘며 서로 의존하지 않는다(영수증검증 description 주축):
  - 분기A(주문번호 미기록 / 상품코드 비었음): 이 주문 건이 영수증검증에 없거나
    description=PurchaseCodeNull(또는 빈값)인 경우.
    주문번호 미기록(orders.get 결제 성공 + 영수증검증에 해당 주문번호 없음)은 상품 특정 후
    재구매 흔적을 대조해 `재지급` / `환불` / `미결정`으로 최종 분기한다. Play `productId`(=StorePurchaseCode_AOS)
    → CSV Inapp 후보 집합 → 결제 시각 이전 300초 이내(env `PAYMENT_ERROR_CLICK_WINDOW_SECONDS`)
    log_shop_click 후보를 본다. shop_click 후보 1건이면 그 상품으로 진행하고, shop_click 후보가
    0건이어도 CSV Inapp 후보가 1건이면 그 상품으로 진행한다. CSV 후보가 2건 이상인데
    shop_click 후보가 0건이면 `상품미특정 환불`, shop_click 후보가 다수이면 `미결정`으로 둔다.
    상품 확정 후 `None`/`Onetime`은 최근 영수증검증 100건, `Daily`/`Weekly`/`Monthly`는
    마지막 초기화 이후(KST Daily=매일 00시, Weekly=월요일 00시, Monthly=매달 1일 00시)
    같은 Code 존재 여부를 본다. 단일구매/한도도 찬 상태면 환불, 누락 상태면 재지급한다.
    분기A의 "이 주문 건이 없음"은 실제로 두 형태를 하나로 묶은 것이다
    (2026-07-07 사용자 지적 — 원인 차이는 즉시 출력 문구로만 구분):
    ① 그 UUID로 영수증검증을 조회했더니 행이 통째로 0건.
    ② 그 UUID는 다른 결제 행이 있지만(has_results=True), 이 주문번호(order_id)와
       일치하는 행만 없음 — UUID 유효성은 이미 위에서 확정했으므로(무효 UUID면
       InvalidUuidError로 먼저 걸러짐) "무효 UUID라 조회가 안 됨"과는 다르다. Google
       `orders.get`은 이 주문의 결제를 확인했는데 영수증검증에는 이 건만 안 잡힌
       것이므로 ①과 실질적으로 동일한 미지급 확정으로 취급한다(예전엔 이 경우 임의로
       다른 행을 골라 판정을 이어가는 버그가 있었음 — 지금은 그 행 선택 자체를 하지
       않고 곧바로 이 분기로 옴).
  - 분기B(description 정상): 상품코드가 정상적으로 있음.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# cs 모듈(브랜드→GCP 규칙, log_shop_click 조회) import 위해 경로 추가
_CS_DIR = BASE_DIR.parent / "cs"
if str(_CS_DIR) not in sys.path:
    sys.path.insert(0, str(_CS_DIR))

from console_step_verify import (
    configure_console_output,
    get_retry_max_retries,
    green,
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
# 분기A(주문번호 미기록/상품코드 비었음) click 매칭 시각 윈도우 — 결제(orders.get createTime) 기준 이전 N초.
# 분석 근거: click→purchase 전환 최대 287초·99%가 60초 이내 → 300초면 여유있게 충분.
CLICK_MATCH_WINDOW_SECONDS = max(1, int(os.environ.get("PAYMENT_ERROR_CLICK_WINDOW_SECONDS", "300")))
# subprocess 호출자(cs co-pilot)가 결과를 회수할 때 쓰는 마커.
# cs_copilot.py 의 PAYMENT_ERROR_JSON_MARKER 와 반드시 같아야 한다.
PAYMENT_ERROR_JSON_MARKER = "===PAYMENT_ERROR_JSON==="

# 영수증검증 결과 row 의 키는 한글 라벨(RECEIPT_FIELDS 의 두번째 값)
ROW_DESCRIPTION = "Description"
ROW_ORDER_ID = "주문 ID"
ROW_PURCHASE_TIME = "거래일시"
KST = timezone(timedelta(hours=9))


# ── 판정 헬퍼 ─────────────────────────────────────────────────────────────────

def classify_receipt_row(row: dict) -> str:
    """영수증검증 한 행을 description 기준으로 분류. 반환값은 내부 분기 키."""
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
RESET_PURCHASE_LIMIT_TYPES = {"Daily", "Weekly", "Monthly"}


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


def resolve_inapp_candidates_from_aos(product_id, chart_name=None):
    """Play productId(StorePurchaseCode_AOS)에 대응하는 Inapp 후보 목록을 반환한다."""
    if not product_id:
        return [], "Play productId(AOS) 없음 — 후보 조회 불가"
    try:
        aos_candidates = load_shop_aos_candidates(chart_name)
    except Exception as exc:  # noqa: BLE001
        return [], f"CSV 후보 로드 실패: {exc}"
    inapp_candidates = sorted(aos_candidates.get(product_id) or [])
    if not inapp_candidates:
        return [], f"CSV에서 AOS '{product_id}'에 대응하는 Inapp 후보 없음"
    return inapp_candidates, None


def get_purchase_limit_info(product_code):
    """상품코드의 Purchase_Limit_Type/Count를 CSV에서 읽는다."""
    info = load_purchase_limit_info_map().get(product_code)
    if not info:
        raise RuntimeError(f"CSV에서 Inapp_PurchaseCode '{product_code}'의 Purchase_Limit 정보 없음")
    return info.get("type"), info.get("count")


def _print_matched_limit_info(limit_type, limit_count):
    """상품이 매칭/확정된 직후 구매 제한 유형·횟수를 초록색으로 즉시 출력한다.

    사용자 요청(2026-07-08): ShopData Count 조회 이후가 아니라, GCP 로그든 영수증검증이든
    상품이 매칭되는 순간 유형(Onetime 등)·Purchase_Limit_Count를 초록색으로 보여준다.
    """
    if limit_type is None and limit_count is None:
        return
    type_disp = limit_type or "(미확인)"
    count_disp = limit_count if limit_count is not None else "(미확인)"
    print(green(
        f" [지급 상태 판정] 확인된 상품 구매제한 — 유형={type_disp}, "
        f"Purchase_Limit_Count={count_disp}"
    ))


# 첨부 결정 트리(2026-07-10 사용자 스크린샷)의 노드 텍스트를 그대로 상수화한다.
# judge_pattern1_missing_receipt가 어느 경로를 탔는지 이 노드들을 이어붙여 터미널에 찍어,
# 사용자가 화면 로그와 스크린샷을 1:1로 대조할 수 있게 한다.
_NODE_NO_RESET = "Onetime/None 상품"
_NODE_RESET = "Daily/Weekly/Monthly 상품"
_NODE_RECEIPT_CODE_YES = "최근 영수증검증 100건 Description 내 해당 Code 존재 O(유저 재구매)"
_NODE_RECEIPT_CODE_NO = "최근 영수증검증 100건 Description 내 해당 Code 존재 X"
_NODE_PERIOD_CODE_YES = "마지막 초기화 이후 영수증검증 Description 내 해당 Code 존재 O"
_NODE_PERIOD_CODE_NO = "마지막 초기화 이후 영수증검증 Description 내 해당 Code 존재 X"
_NODE_LIMIT_EQ1 = "Purchase_Limit_Count = 1"
_NODE_LIMIT_EQ0 = "Purchase_Limit_Count = 0"
_NODE_LIMIT_GT1 = "Purchase_Limit_Count > 1"
_NODE_SHOP_LT = "ShopData Count < Purchase_Limit_Count"
_NODE_SHOP_GE = "ShopData Count >= Purchase_Limit_Count"
_LEAF_REGRANT = "재지급"
_LEAF_REFUND = "환불"


def _branch_path(nodes, notes=None):
    """결정 트리에서 실제로 탄 노드들을 ` → `로 이어붙여 초록색으로 출력하고 그 문장을 반환한다.

    사용자 요청(2026-07-10): 스크린샷 분기 트리가 코드에 반영돼 있는지, 어느 분기를 탔는지
    터미널에서 바로 확인할 수 있게 판정 경로를 명시한다. 반환 문장은 notes에도 남겨
    print_result 요약과 JSON 결과에서도 동일 경로가 보이게 한다.
    """
    path = " → ".join(str(n) for n in nodes)
    print(green(f" [지급 상태 판정] 판정 경로: {path}"))
    sentence = f"판정 경로: {path}"
    if notes is not None:
        notes.append(sentence)
    return sentence


def _shop_node_for_action(action):
    """Count>1 분기에서 ShopData 대조 결과(action)를 스크린샷 노드 텍스트로 바꾼다."""
    if action == "regrant":
        return _NODE_SHOP_LT
    if action == "refund":
        return _NODE_SHOP_GE
    return "ShopData Count 확인 불가 → 미결정"


def _print_matched_limit_info_by_code(product_code):
    """product_code만 알 때 상품표 CSV에서 유형·횟수를 읽어 초록색으로 출력한다(실패 시 조용히 생략)."""
    if not product_code:
        return
    try:
        info = load_purchase_limit_info_map().get(product_code) or {}
    except Exception:  # noqa: BLE001 — 표시용 부가정보라 실패해도 판정은 계속한다
        return
    _print_matched_limit_info(info.get("type"), info.get("count"))


def resolve_product_candidates_via_gcp(logging_service, brand, uuid_value, product_id, order_create_time):
    """분기A(주문번호 미기록/상품코드 비었음) 상품 특정: AOS(product_id) → CSV 후보(Inapp) → 결제 시각 이전
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

    inapp_candidates, err = resolve_inapp_candidates_from_aos(product_id)
    if err:
        return [], err

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


def _parse_receipt_time(value):
    """영수증검증 거래일시를 KST aware datetime으로 변환한다."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            numeric = float(text)
            if numeric > 10 ** 12:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc).astimezone(KST)
        except (OSError, OverflowError, ValueError):
            return None

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        dt = None
    if dt is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y.%m.%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y.%m.%d %H:%M",
            "%Y/%m/%d %H:%M",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def _last_reset_at(limit_type, now=None):
    """Daily/Weekly/Monthly 구매 제한의 마지막 KST 초기화 시각."""
    now = (now or datetime.now(KST)).astimezone(KST)
    if limit_type == "Daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if limit_type == "Weekly":
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return base - timedelta(days=base.weekday())
    if limit_type == "Monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def count_receipt_matches_since(rows, product_code, since_dt):
    """영수증검증 rows 중 since_dt 이후 동일 Description 행 수와 파싱 실패 수를 반환한다."""
    count = 0
    unparsed = 0
    for row in rows:
        if (row.get(ROW_DESCRIPTION) or "").strip() != product_code:
            continue
        row_dt = _parse_receipt_time(row.get(ROW_PURCHASE_TIME))
        if row_dt is None:
            unparsed += 1
            continue
        if row_dt >= since_dt:
            count += 1
    return count, unparsed


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
    """분기A(주문번호 미기록/상품코드 비었음) 공용: 후보 조회 결과를 product_code/product_candidates로 정리.

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


def _pattern1_result(
    *,
    verdict,
    receipt,
    product_code,
    product_source,
    product_candidates,
    inapp_candidates,
    shopdata,
    notes,
    uuid_value,
    effective_uuid,
    recommended_action,
    decision_label,
):
    return {
        "verdict": verdict,
        "receipt": receipt,
        "matched_row": None,
        "product_code": product_code,
        "product_source": product_source,
        "product_candidates": product_candidates,
        "inapp_candidates": inapp_candidates,
        "shopdata": shopdata,
        "notes": notes,
        "submitted_uuid": uuid_value,
        "resolved_uuid": effective_uuid,
        "recommended_action": recommended_action,
        "decision_label": decision_label,
    }


def _resolve_pattern1_product_code(logging_service, brand, uuid_value, product_id, order_create_time, notes):
    """주문번호 미기록 분기의 상품 특정. 반환: (status, product_code, source, gcp_candidates, inapp_candidates)."""
    inapp_candidates, err = resolve_inapp_candidates_from_aos(product_id)
    if err:
        notes.append(f"상품 특정(CSV) 실패: {err}")
        return "lookup_failed", None, None, [], []

    notes.append(f"AOS '{product_id}' → Inapp 후보 {len(inapp_candidates)}건")
    if len(inapp_candidates) == 1:
        product_code = inapp_candidates[0]
        notes.append(f"Inapp 후보 1건 — shop_click 없이 상품 확정: {product_code}")
        return "resolved", product_code, "aos_single_candidate", [], inapp_candidates

    candidates, gcp_err = resolve_product_candidates_via_gcp(
        logging_service, brand, uuid_value, product_id, order_create_time
    )
    if gcp_err:
        notes.append(f"상품 특정(GCP 로그) 실패/불완전: {gcp_err}")
        return "lookup_failed", None, "gcp_log_candidates", candidates, inapp_candidates
    if len(candidates) == 0:
        notes.append(
            f"GCP 로그 후보 0건 — Inapp 후보 {len(inapp_candidates)}건 중 상품을 특정할 수 없음"
        )
        return "unspecified_refund", None, "gcp_log_candidates", [], inapp_candidates
    if len(candidates) > 1:
        notes.append(f"GCP 로그 후보 {len(candidates)}건 — 자동 확정 안 함")
        return "ambiguous", None, "gcp_log_candidates", candidates, inapp_candidates

    product_code = candidates[0].get("shop_click_id")
    if not product_code:
        notes.append("GCP 로그 후보 1건이나 shop_click_id가 비어 있음")
        return "lookup_failed", None, "gcp_log_candidates", candidates, inapp_candidates
    notes.append(f"GCP 로그 후보 1건 — 상품 확정: {product_code}")
    return "resolved", product_code, "gcp_log_candidates", candidates, inapp_candidates


def _compare_shopdata_limit(page, effective_uuid, table_name, product_code, limit_count, timeout_error, notes):
    try:
        shopdata = lookup_count_readonly(page, effective_uuid, table_name, product_code, timeout_error)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"ShopData Count 조회 실패: {exc}")
        return "review", None
    purchase_count = shopdata.get("purchase_count")
    if purchase_count is None:
        shopdata["count_judgment"] = "HELD"
        notes.append("ShopData Count 값 없음 — 사람 확인 필요")
        return "review", shopdata
    if purchase_count < limit_count:
        shopdata["count_judgment"] = "LIMIT_NOT_REACHED_REGRANT"
        notes.append(f"ShopData Count {purchase_count} < Purchase_Limit_Count {limit_count} → 재지급")
        return "regrant", shopdata
    shopdata["count_judgment"] = "LIMIT_REACHED_REFUND"
    notes.append(f"ShopData Count {purchase_count} >= Purchase_Limit_Count {limit_count} → 환불")
    return "refund", shopdata


def judge_pattern1_missing_receipt(
    page,
    *,
    receipt,
    rows,
    uuid_value,
    effective_uuid,
    brand,
    product_id,
    order_create_time,
    table_name,
    logging_service,
    timeout_error,
    notes,
):
    """orders.get 결제 성공이나 영수증검증에 해당 주문번호가 없을 때의 최종 분기(상품 특정·재구매 흔적 대조)."""
    status, product_code, product_source, gcp_candidates, inapp_candidates = _resolve_pattern1_product_code(
        logging_service, brand, effective_uuid, product_id, order_create_time, notes
    )
    if status == "lookup_failed":
        return _pattern1_result(
            verdict="pattern1_product_lookup_review",
            receipt=receipt,
            product_code=None,
            product_source=product_source,
            product_candidates=gcp_candidates,
            inapp_candidates=inapp_candidates,
            shopdata=None,
            notes=notes,
            uuid_value=uuid_value,
            effective_uuid=effective_uuid,
            recommended_action="review",
            decision_label="상품조회실패 미결정",
        )
    if status == "ambiguous":
        return _pattern1_result(
            verdict="pattern1_product_ambiguous_review",
            receipt=receipt,
            product_code=None,
            product_source=product_source,
            product_candidates=gcp_candidates,
            inapp_candidates=inapp_candidates,
            shopdata=None,
            notes=notes,
            uuid_value=uuid_value,
            effective_uuid=effective_uuid,
            recommended_action="review",
            decision_label="상품후보다수 미결정",
        )
    if status == "unspecified_refund":
        return _pattern1_result(
            verdict="pattern1_product_unspecified_refund",
            receipt=receipt,
            product_code=None,
            product_source=product_source,
            product_candidates=None,
            inapp_candidates=inapp_candidates,
            shopdata=None,
            notes=notes,
            uuid_value=uuid_value,
            effective_uuid=effective_uuid,
            recommended_action="refund",
            decision_label="상품미특정 환불",
        )

    try:
        limit_type, limit_count = get_purchase_limit_info(product_code)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Purchase_Limit 정보 조회 실패: {exc}")
        return _pattern1_result(
            verdict="pattern1_limit_info_review",
            receipt=receipt,
            product_code=product_code,
            product_source=product_source,
            product_candidates=gcp_candidates,
            inapp_candidates=inapp_candidates,
            shopdata=None,
            notes=notes,
            uuid_value=uuid_value,
            effective_uuid=effective_uuid,
            recommended_action="review",
            decision_label="상품제한정보 미결정",
        )

    notes.append(f"Purchase_Limit_Type={limit_type}, Purchase_Limit_Count={limit_count}")
    # 상품 특정(CSV 단일 후보 또는 GCP 로그) 완료 → 유형·횟수를 즉시 초록색으로 표시.
    _print_matched_limit_info(limit_type, limit_count)
    matching_count = count_receipt_matches(rows, product_code)
    if limit_type in NO_RESET_PURCHASE_LIMIT_TYPES:
        if matching_count == 0:
            notes.append("최근 영수증검증 100건 Description 내 해당 Code 없음 → 재지급")
            _branch_path([_NODE_NO_RESET, _NODE_RECEIPT_CODE_NO, _LEAF_REGRANT], notes)
            return _pattern1_result(
                verdict="pattern1_regrant_no_receipt_code",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=None,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action="regrant",
                decision_label="재지급",
            )
        notes.append(f"최근 영수증검증 100건 Description 내 해당 Code {matching_count}건 존재")
        if limit_count == 1:
            _branch_path([_NODE_NO_RESET, _NODE_RECEIPT_CODE_YES, _NODE_LIMIT_EQ1, _LEAF_REFUND], notes)
            return _pattern1_result(
                verdict="pattern1_refund_repurchase_detected",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=None,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action="refund",
                decision_label="환불",
            )
        if limit_count == 0:
            notes.append("무제한 구매 예외 상품(Purchase_Limit_Count=0) → ShopData Count 없이 재지급")
            _branch_path([_NODE_NO_RESET, _NODE_RECEIPT_CODE_YES, _NODE_LIMIT_EQ0, _LEAF_REGRANT], notes)
            return _pattern1_result(
                verdict="pattern1_regrant_unlimited",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=None,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action="regrant",
                decision_label="재지급",
            )
        if isinstance(limit_count, int) and limit_count > 1:
            action, shopdata = _compare_shopdata_limit(
                page, effective_uuid, table_name, product_code, limit_count, timeout_error, notes
            )
            verdict = "pattern1_regrant_limit_not_reached" if action == "regrant" else "pattern1_refund_limit_reached"
            _branch_path(
                [_NODE_NO_RESET, _NODE_RECEIPT_CODE_YES, _NODE_LIMIT_GT1, _shop_node_for_action(action),
                 _LEAF_REGRANT if action == "regrant" else (_LEAF_REFUND if action == "refund" else "미결정")],
                notes,
            )
            return _pattern1_result(
                verdict=verdict if action != "review" else "pattern1_count_review",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=shopdata,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action=action,
                decision_label="재지급" if action == "regrant" else ("환불" if action == "refund" else "미결정"),
            )

    if limit_type in RESET_PURCHASE_LIMIT_TYPES:
        reset_at = _last_reset_at(limit_type)
        period_count, unparsed_count = count_receipt_matches_since(rows, product_code, reset_at)
        notes.append(
            f"{limit_type} 마지막 초기화(KST)={reset_at.isoformat()} 이후 "
            f"영수증검증 Description 해당 Code {period_count}건"
        )
        if period_count == 0:
            if unparsed_count:
                notes.append(f"해당 Code 행 {unparsed_count}건의 거래일시 파싱 실패 — 사람 확인 필요")
                _branch_path(
                    [_NODE_RESET, _NODE_PERIOD_CODE_NO,
                     "단, 해당 Code 행 거래일시 파싱 실패 → 트리 이탈, 미결정(사람 확인)"],
                    notes,
                )
                return _pattern1_result(
                    verdict="pattern1_period_time_review",
                    receipt=receipt,
                    product_code=product_code,
                    product_source=product_source,
                    product_candidates=gcp_candidates,
                    inapp_candidates=inapp_candidates,
                    shopdata=None,
                    notes=notes,
                    uuid_value=uuid_value,
                    effective_uuid=effective_uuid,
                    recommended_action="review",
                    decision_label="미결정",
                )
            _branch_path([_NODE_RESET, _NODE_PERIOD_CODE_NO, _LEAF_REGRANT], notes)
            return _pattern1_result(
                verdict="pattern1_regrant_no_period_receipt_code",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=None,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action="regrant",
                decision_label="재지급",
            )
        if limit_count == 1:
            _branch_path([_NODE_RESET, _NODE_PERIOD_CODE_YES, _NODE_LIMIT_EQ1, _LEAF_REFUND], notes)
            return _pattern1_result(
                verdict="pattern1_refund_period_repurchase_detected",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=None,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action="refund",
                decision_label="환불",
            )
        if isinstance(limit_count, int) and limit_count > 1:
            action, shopdata = _compare_shopdata_limit(
                page, effective_uuid, table_name, product_code, limit_count, timeout_error, notes
            )
            verdict = "pattern1_regrant_period_limit_not_reached" if action == "regrant" else "pattern1_refund_period_limit_reached"
            _branch_path(
                [_NODE_RESET, _NODE_PERIOD_CODE_YES, _NODE_LIMIT_GT1, _shop_node_for_action(action),
                 _LEAF_REGRANT if action == "regrant" else (_LEAF_REFUND if action == "refund" else "미결정")],
                notes,
            )
            return _pattern1_result(
                verdict=verdict if action != "review" else "pattern1_period_count_review",
                receipt=receipt,
                product_code=product_code,
                product_source=product_source,
                product_candidates=gcp_candidates,
                inapp_candidates=inapp_candidates,
                shopdata=shopdata,
                notes=notes,
                uuid_value=uuid_value,
                effective_uuid=effective_uuid,
                recommended_action=action,
                decision_label="재지급" if action == "regrant" else ("환불" if action == "refund" else "미결정"),
            )
        notes.append(f"{limit_type} 상품의 Purchase_Limit_Count={limit_count} — 예상 밖 값, 사람 확인 필요")
        _branch_path(
            [_NODE_RESET, _NODE_PERIOD_CODE_YES,
             f"Purchase_Limit_Count={limit_count}(스크린샷 트리에 없는 값) → 미결정(사람 확인)"],
            notes,
        )
    else:
        _branch_path(
            [f"Purchase_Limit_Type={limit_type}, Purchase_Limit_Count={limit_count}"
             "(스크린샷 트리의 Onetime/None·Daily/Weekly/Monthly 어디에도 속하지 않음) → 미결정(사람 확인)"],
            notes,
        )

    return _pattern1_result(
        verdict="pattern1_limit_type_review",
        receipt=receipt,
        product_code=product_code,
        product_source=product_source,
        product_candidates=gcp_candidates,
        inapp_candidates=inapp_candidates,
        shopdata=None,
        notes=notes,
        uuid_value=uuid_value,
        effective_uuid=effective_uuid,
        recommended_action="review",
        decision_label="미결정",
    )


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
    분기A(주문번호 미기록/상품코드 비었음)의 GCP 로그 후보 조회에만 쓰인다. 분기B(description 정상)는 description만 쓰므로 불필요.

    nickname(티켓 제출 닉네임, 선택)은 uuid_value가 '유저' 탭에서 존재하지 않을 때만 쓰인다
    (2026-07-08 사용자 지시): '닉네임'으로 재검색해 나온 후보와 uuid_value의 편집거리가
    2 이하(env USER_UUID_NICKNAME_MAX_DISTANCE)인 후보가 정확히 1개면 동일인의 오탈자로
    판정하고, 그 콘솔 확정 UUID로 유저 존재를 확정해 이후 절차(영수증검증/ShopData/GCP
    로그 조회)를 그 UUID로 진행한다 — 자세한 내용은 console_user_search.ensure_uuid_registered.
    nickname_source는 참고용 출처 표기("custom_field" | "sender_display_name")이며 판정
    로직·신뢰도에는 영향을 주지 않는다(cs '보낸 사람' 표시명이 실제 닉네임이 오도록
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
        print(f" [지급 상태 판정] '{uuid_value}' — '유저' 탭에서 존재하지 않는 UUID로 확인됨(재시도 없이 즉시 종료)")
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
        print(f" [지급 상태 판정] 닉네임 대조로 UUID 오탈자 확정 → 이후 조회는 '{effective_uuid}' 기준으로 진행")

    # 분기A(주문번호 미기록): 이 주문 건이 영수증검증에 없음 → 미지급 확정. 상품은 로그 후보로 나열.
    # "없음"의 실제 형태는 두 가지이나(① UUID 전체가 0건 ② 다른 구매 행은 있으나 이
    # 주문번호만 없음) 재지급 판단 기준으로는 동일한 조건이라 verdict를 나누지 않는다
    # (2026-07-07 사용자 지적 — 애초에 둘 다 "Google엔 있는데 콘솔 영수증검증엔 이 건이
    # 없다"는 하나의 사실이며, UUID 유효성은 이미 위에서 확정됐으므로 무효 UUID와도 무관).
    # 원인 차이는 아래 "[지급 상태 판정]" 즉시 출력 문구로만 남긴다 — notes에도 같은 문구를
    # 중복으로 넣으면 co-pilot 최종 요약(cs_copilot._print_payment_error)에서 방금 본
    # 문장이 그대로 한 번 더 찍혀 중복이었다(2026-07-08 사용자 지적).
    rows = receipt.get("rows") or []
    if not receipt.get("has_results"):
        matched = None
        no_record_reason = "그 UUID의 영수증검증 결과가 통째로 0건"
    else:
        matched = _select_target_row(rows, order_id)
        no_record_reason = (
            "주문번호가 없어 대상 행을 특정하지 못함" if not order_id
            else f"주문번호 '{order_id}'와 일치하는 행 없음"
        )

    if matched is None:
        if receipt.get("has_results") and not order_id:
            # 실전 경로(cs_copilot)는 order_id 없는 티켓을 이 함수 호출 전에 이미
            # 걸러내므로(order_missing 플래그) 여기 도달하는 건 CLI 단독 테스트 등뿐이다.
            # 어떤 주문을 찾아야 할지 자체를 모르므로 자동 판정하지 않는다.
            notes.append(f"결과 행은 있으나 {no_record_reason}")
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
        print(f" [지급 상태 판정] {no_record_reason} → 상품 특정·재구매 흔적 확인으로 진행")
        return judge_pattern1_missing_receipt(
            page,
            receipt=receipt,
            rows=rows,
            uuid_value=uuid_value,
            effective_uuid=effective_uuid,
            brand=brand,
            product_id=product_id,
            order_create_time=order_create_time,
            table_name=table_name,
            logging_service=logging_service,
            timeout_error=timeout_error,
            notes=notes,
        )

    pattern = classify_receipt_row(matched)
    matched_desc = matched.get(ROW_DESCRIPTION) or "(빈값)"

    # 분기A(상품코드 비었음): description = PurchaseCodeNull/빈값 → 미지급 확정. 상품은 로그 후보로 나열.
    if pattern == "pattern2":
        print(green(f" [지급 상태 판정] 매칭 행 발견 — description='{matched_desc}'"))
        # 실제 GCP 로그 조회 전에 CSV(AOS→Inapp 매핑)로 추려지는 후보 폭만 먼저 안내한다
        # (아래 _resolve_gcp_candidates_result가 이 중 결제 시각과 맞는 로그만 다시 골라낸다).
        preview_candidates, _preview_err = resolve_inapp_candidates_from_aos(product_id)
        print(f" 미지급 확정 - GCP 로그 상품 후보 {len(preview_candidates)}건 조회")
        product_code, candidates = _resolve_gcp_candidates_result(
            logging_service, brand, effective_uuid, product_id, order_create_time, notes
        )
        # 구매제한(유형/횟수)은 후보 상품마다 다르므로 여기서 단일 표시하지 않고,
        # 최종 요약(cs_copilot._print_payment_error)에서 후보별로 표시한다.
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
    # 분기B(description 정상): 상품코드 = description → 로그는 안 보고 곧바로
    # ShopData Count 조회(판정 보류). 분기A와 완전히 분리 — product_id/order_create_time 불필요.
    # classify_receipt_row가 이미 빈값/PurchaseCodeNull을 걸러냈으므로 matched_desc는 실값이다.
    print(green(
        f" [지급 상태 판정] 매칭 행 발견 — description='{matched_desc}' → "
        "description 정상 → 로그 미사용, ShopData Count 조회로 이동"
    ))
    product_code = matched_desc.strip()
    # 영수증검증 description으로 상품이 확정됨 → ShopData 조회 전에 유형·횟수를 초록색으로 표시.
    _print_matched_limit_info_by_code(product_code)
    shopdata = None
    try:
        shopdata = lookup_count_readonly(
            page, effective_uuid, table_name, product_code, timeout_error
        )
    except PurchaseCodeNotFoundError as exc:
        # ShopData PurchaseCode 배열 자체에 해당 코드가 없음 = 구매 시도 기록조차 없음 → 미지급 확정
        # (Count 값 판정 보류와는 다르다 — 여기는 코드 존재 여부라 주차/상품유형 애매성이 없다).
        notes.append(f"ShopData PurchaseCode에 해당 코드 자체가 없음 → 미지급 확정: {exc}")
        print(f" [지급 상태 판정] ShopData에 '{product_code}' 코드 자체가 없음 → 미지급 확정")
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

# 내부 verdict 코드(분기 로직용 식별자)를 사람이 읽는 자연어 문구로 옮긴다.
# 코드 값 자체는 두 스크립트가 공유하는 분기 키라 그대로 두고, 화면 출력에서만 이 표를 쓴다.
_VERDICT_DESCRIPTIONS = {
    "invalid_uuid": "존재하지 않는 UUID (유저 탭에서 확인)",
    "inconclusive": "판정 보류 — 사람 확인 필요",
    # 주문번호 미기록 분기 — 상품 특정 후 재구매 흔적 대조
    "pattern1_product_lookup_review": "상품 조회 실패 — 미결정(사람 확인 필요)",
    "pattern1_product_ambiguous_review": "상품 후보 다수 — 미결정(사람 확인 필요)",
    "pattern1_product_unspecified_refund": "상품 미특정 — 환불 후보",
    "pattern1_limit_info_review": "상품 제한 정보 조회 실패 — 미결정(사람 확인 필요)",
    "pattern1_regrant_no_receipt_code": "재지급 — 같은 상품의 정상 영수증 기록 없음",
    "pattern1_refund_repurchase_detected": "환불 — 재구매 흔적 확인됨",
    "pattern1_regrant_unlimited": "재지급 — 무제한 구매 예외 상품",
    "pattern1_regrant_limit_not_reached": "재지급 — 구매 한도 미도달(누락분 있음)",
    "pattern1_refund_limit_reached": "환불 — 구매 한도까지 이미 채워짐",
    "pattern1_count_review": "미결정 — ShopData Count로 판정 불가(사람 확인 필요)",
    "pattern1_period_time_review": "미결정 — 주기 초기화 시각 확인 불가(사람 확인 필요)",
    "pattern1_regrant_no_period_receipt_code": "재지급 — 주기 초기화 이후 같은 상품 기록 없음",
    "pattern1_refund_period_repurchase_detected": "환불 — 주기 내 재구매 흔적 확인됨",
    "pattern1_regrant_period_limit_not_reached": "재지급 — 주기형 상품 한도 미도달(누락분 있음)",
    "pattern1_refund_period_limit_reached": "환불 — 주기형 상품 한도까지 이미 채워짐",
    "pattern1_period_count_review": "미결정 — 주기형 Count로 판정 불가(사람 확인 필요)",
    "pattern1_limit_type_review": "미결정 — 구매 제한 유형 확인 불가(사람 확인 필요)",
    # 주문번호 기록의 상품코드가 비었음 → 로그 후보로 상품 특정
    "pattern2_purchase_code_null": "미지급 — 영수증검증 PurchaseCodeNull(로그 후보로 상품 특정)",
    # description 정상 → ShopData Count 대조
    "pattern3_count_confirmed_missing": "미지급 확정 — ShopData Count 부족",
    "pattern3_count_confirmed_granted": "이미 지급됨 — ShopData Count 충족(재지급 불필요)",
    "pattern3_count_review": "미결정 — ShopData Count 자동 판정 보류(사람 확인 필요)",
    "pattern3_code_not_found": "미지급 확정 — ShopData에 상품코드 자체가 없음(구매 시도 기록 없음)",
}


def describe_verdict(verdict):
    """내부 verdict 코드를 사람이 읽는 자연어 문구로 바꾼다. 미등록 코드는 원본 그대로."""
    if not verdict:
        return "(판정 없음)"
    return _VERDICT_DESCRIPTIONS.get(verdict, verdict)


def describe_decision(result):
    """recommended_action/decision_label을 터미널용 자연어 처리분기로 바꾼다."""
    if not result:
        return None
    action = result.get("recommended_action")
    label = result.get("decision_label")
    verdict_text = describe_verdict(result.get("verdict"))
    if action == "regrant":
        if label and label != "재지급":
            return label
        return "재지급 가능"
    if action == "refund":
        if label and label != "환불":
            return label if "후보" in label else f"{label} 후보"
        return verdict_text
    if action == "review":
        return label or verdict_text
    return label


def print_result(result):
    print()
    print(_SEP)
    print(f" 지급 상태 판정: {describe_verdict(result.get('verdict'))}")
    print(_SEP)
    submitted_uuid = result.get("submitted_uuid")
    resolved_uuid = result.get("resolved_uuid")
    if submitted_uuid and resolved_uuid and submitted_uuid != resolved_uuid:
        print(f" UUID            : 제출값={submitted_uuid} → 닉네임 대조로 확정={resolved_uuid}")
    receipt = result.get("receipt") or {}
    print(f" 영수증검증 결과 : has_results={receipt.get('has_results')} row_count={receipt.get('row_count')}")
    decision_text = describe_decision(result)
    if decision_text:
        print(f" 처리분기        : {decision_text}")
    print(f" 상품코드        : {result.get('product_code') or '(미특정)'} (source={result.get('product_source')})")
    inapp_candidates = result.get("inapp_candidates")
    if inapp_candidates:
        print(f" Inapp 후보      : {len(inapp_candidates)}건")
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
        "inapp_candidates": (result or {}).get("inapp_candidates"),
        "shopdata": (result or {}).get("shopdata"),
        "recommended_action": (result or {}).get("recommended_action"),
        "decision_label": (result or {}).get("decision_label"),
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
        lines.append(f"recommended_action={result.get('recommended_action')}")
        lines.append(f"decision_label={result.get('decision_label')}")
        lines.append(f"product_code={result.get('product_code')}")
        lines.append(f"product_source={result.get('product_source')}")
        inapp_candidates = result.get("inapp_candidates") or []
        lines.append(f"inapp_candidates_count={len(inapp_candidates)}")
        for code in inapp_candidates:
            lines.append(f"inapp_candidate={code}")
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
                         help="Play productId(=StorePurchaseCode_AOS). 분기A(주문번호 미기록/상품코드 비었음) 후보 조회용, 선택")
    parser.add_argument("--order-time", dest="order_create_time", default="",
                         help="Play orders.get createTime(RFC3339). 분기A(주문번호 미기록/상품코드 비었음) 후보 조회용, 선택")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME, help="ShopData 테이블명")
    parser.add_argument("--key", default="", help="GCP 서비스계정 JSON 키(분기A 로그 후보 조회용, 선택)")
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
        print("[경고] GCP logging service 생성 실패 — 로그 후보로 상품을 특정하는 분기가 동작하지 않습니다.")

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
                label=f"지급 상태 판정 UUID {args.uuid} 재시도",
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
