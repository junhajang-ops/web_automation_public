# -*- coding: utf-8 -*-
"""
Console console leaderboard lookup.

Scope:
- Open the Console console
- Select the target project
- Open the leaderboard page from the side menu
- Search visible <keyword>_* leaderboards for each configured keyword (project별 env로 관리, 복수 지정 가능)
- Open each leaderboard from the on-screen list
- Read every player within rank <= 30 from the detail table (ties included)
- Enrich new accounts with receipt-verification / payment-history sums
  (--dc: skip payment-history lookup entirely; receipt-verification only counts
  purchased products, no price lookup — 게임B 프로젝트는 가격 등 조회 불가 정책)
- Save CSV and debug artifacts

By default this script is read-only. Actual blocking (mutation: new accounts whose total
payment sum is at or below the env-managed threshold — USER_BLOCK_MAX_TOTAL_PAYMENT,
default 200,000; --dc uses recent purchase count and USER_BLOCK_MAX_PURCHASE_COUNT instead
— are user-blocked, device ban included) only runs when EITHER of these is true:
  - --block-new-users is passed on the command line (manual runs), OR
  - env USER_BLOCK_NEW_USERS_ENABLED (or its {TITLE}_ override) is set to a truthy value
    (1/true/yes/on) — this is the only way scheduled runs (run_leaderboard_scheduled.ps1,
    which never passes --block-new-users) can enable real blocking, per project.
USER_BLOCK_MAX_TOTAL_PAYMENT/USER_BLOCK_MAX_PURCHASE_COUNT/ACCOUNT_NEW_HOURS/
USER_BLOCK_NEW_USERS_ENABLED all accept a {TITLE}_-prefixed per-project override
(e.g. GAMETITLE_USER_BLOCK_MAX_TOTAL_PAYMENT, DC_USER_BLOCK_NEW_USERS_ENABLED).
Without either activation path it merely prints the block candidates (dry-run).
"""

import argparse
import csv
import datetime
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from console_user_search import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    click_login_if_needed,
    ensure_sidebar_link_expanded,
    find_exact_text_match,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
    wait_for_visible,
)
from console_chart_lookup import PAYMENT_DOCS_DIR
from console_receipt_verification import (
    RECEIPT_IGNORE_PATTERNS,
    click_search_button,
    collect_result,
    fill_uuid_search,
    open_receipt_verification_menu,
    wait_for_receipt_page_render_stable,
)
from console_webshop_history import summarize_payitem_history as summarize_payitem_history_lookup
from console_user_block import (
    DEFAULT_BLOCK_PERIOD_DAYS,
    DEFAULT_BLOCK_REASON,
    DEFAULT_DEVICE_BAN_COUNT,
    run_user_block,
)
from console_slack_notify import get_slack_webhook_url, send_slack_message
from console_step_verify import (
    configure_console_output,
    display_width,
    get_retry_max_retries,
    pad_display,
    init_dump_dir,
    record_step_dump,
    retry_with_recovery,
    save_page_artifacts,
    step_and_verify_ui,
    wait_until,
)
from test_config import apply_title_profile


BASE_DIR = Path(__file__).resolve().parent

# cs 공용 GCP helper (build_logging_service, fetch_recent_log_entry)
_CS_DIR = BASE_DIR.parent / "cs"
if str(_CS_DIR) not in sys.path:
    sys.path.insert(0, str(_CS_DIR))
from cs_gcp_logging import (  # noqa: E402
    build_logging_service_from_credentials,
    fetch_recent_log_entry,
    load_logging_credentials,
)
from cs_bigquery_logging import (  # noqa: E402
    build_bigquery_client_from_credentials,
    fetch_min_date_for_user,
    load_bigquery_credentials,
)

def _parse_bool_env(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_OUTPUT = "dumps_console_leaderboard"
LEADERBOARD_OUT_DIR = PAYMENT_DOCS_DIR / "leaderboard"
DEFAULT_SEARCH_KEYWORDS = "PvPRank"  # 콤마 구분, 복수 지정 가능. --title/--gametitle 사용 시 {TITLE}_LEADERBOARD_KEYWORDS env로 대체 가능
MAX_RANK = 30
LIST_ROWS_PER_PAGE = 100
DETAIL_ROWS_PER_PAGE = 50
POLL_WAIT_MS = 1_000
GRID_SCROLL_STEP_PX = 900
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
# 아래 4개(ACCOUNT_NEW_HOURS/ACCOUNT_LOOKUP_LOOKBACK_HOURS/BLOCK_MAX_TOTAL_PAYMENT/
# BLOCK_MAX_PURCHASE_COUNT)는 모듈 로드 시 전역 env로 1차 로드하되, main()에서
# --title/--gametitle/--dc로 프로젝트가 정해진 뒤 {TITLE}_ 접두 env가 있으면 그 값으로
# 덮어쓴다(_resolve_title_int_env, 프로젝트별 분리 — 2026-07-03). 전역 env가 없으면
# 아래 하드코딩 기본값이 최종 fallback이다.
ACCOUNT_NEW_HOURS = int(os.environ.get("ACCOUNT_NEW_HOURS", "240"))  # 계정 생성 후 N시간 이내 → 신규
# 계정 생성일 조회 시 GCP 로그를 거슬러 볼 시간 범위(하한). 기본값은 신규 계정 기준(ACCOUNT_NEW_HOURS)과 동일.
# 근거: 진짜 "신규" 계정은 반드시 이 창 안에 로그가 있다(가입 자체가 창 안). 따라서 이 창만 조회해도
# 신규 판정에 필요한 로그는 전부 잡힌다. 창 안에 로그가 없으면 최근 활동이 없다는 뜻 → 신규일 수 없음 → 기존 유저.
# 범위를 좁히면 entries.list 스캔이 작아져 "빈 첫 페이지+nextPageToken" 문제도 사실상 사라진다(공식 권장 우회).
# 경계/수집지연이 우려되면 이 env만 키워 창을 넓히면 된다(판정 임계값 ACCOUNT_NEW_HOURS는 그대로).
ACCOUNT_LOOKUP_LOOKBACK_HOURS = max(1, int(os.environ.get("ACCOUNT_LOOKUP_LOOKBACK_HOURS", str(ACCOUNT_NEW_HOURS))))
RECENT_PAYMENT_LIMIT = 100  # 신규 유저 영수증검증 최근 결제 합계 대상 건수
GCP_QUERY_MAX_RETRIES = max(1, int(os.environ.get("GCP_QUERY_MAX_RETRIES", "10")))
GCP_QUERY_RETRY_WAIT_MS = max(0, int(os.environ.get("GCP_QUERY_RETRY_WAIT_MS", "1000")))
RETRY_MAX_RETRIES = get_retry_max_retries()  # console_user_block.py와 공용 env — 목록 재진입 재시도도 동일 개념이라 공용
# 신규 유저 차단 임계값(가격 기반, --dc 이외): 총 결제액(영수증검증+지급내역)이 이 값 "이하"면 차단 대상.
BLOCK_MAX_TOTAL_PAYMENT = max(0, int(os.environ.get("USER_BLOCK_MAX_TOTAL_PAYMENT", "200000")))
# 신규 유저 차단 임계값(구매 건수 기반, --dc 전용 — 가격 등 조회 불가 정책):
# 영수증검증 최근 RECENT_PAYMENT_LIMIT건 중 구매 제품 건수(recent_purchase_count)가 이 값 "이하"면 차단 대상.
BLOCK_MAX_PURCHASE_COUNT = max(0, int(os.environ.get("USER_BLOCK_MAX_PURCHASE_COUNT", "0")))
# 예약 실행(작업 스케줄러)은 --block-new-users CLI 플래그를 넘기지 않으므로, 프로젝트별로
# "실제 차단을 진행할지"를 env로만 켤 수 있게 한다(운영 실행 승인 게이트는 유지 — 기본값 False=드라이런).
# {TITLE}_USER_BLOCK_NEW_USERS_ENABLED가 있으면 그 값, 없으면 전역 USER_BLOCK_NEW_USERS_ENABLED(기본 false).
BLOCK_NEW_USERS_ENABLED = _parse_bool_env(os.environ.get("USER_BLOCK_NEW_USERS_ENABLED", ""))
RANK_COL_WIDTH = 4
UUID_COL_WIDTH = 36
ACCOUNT_TYPE_COL_WIDTH = 16  # 계정상태 고정폭 — 조회실패(...) 예외 메시지는 이 폭에서 말줄임 처리됨
NICKNAME_HEADER = "닉네임"
CREATE_DATE_HEADER = "계정생성일"
# 리더보드 목록/상세 화면 공통: 보상 우편 제목 위젯(FALLBACK/한국어 탭 포함 여부)과
# 순위 현황 위젯(즉시 초기화 버튼 + 과거 기록/현재 순위 탭 + 아이템/순위 구간/수량 컬럼)이
# 화면 조작과 무관하게 비동기로 깜빡이며 구조 지문을 오탐시킨다. 여러 화면/단계에서
# 재사용하므로 화면 국소가 아닌 위젯 단위 이름으로 둔다.
LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS = [
    r"button: .*FALLBACK\|type=button$",
    r"button: 한국어\|type=button$",
    r"button: 즉시 초기화\|type=button$",
    r"role: tab$",
    r"role: tablist$",
    r"role: tabpanel$",
    r"structural_text: label:보상 우편 제목(?: \(deprecated\))?$",
    r"structural_text: tab:(?:.*FALLBACK|한국어|과거 기록|현재 순위)$",
    r"structural_text: col:(?:수량|순위 구간|아이템)$",
]
# leaderboard_complete 전용: 이 스텝은 "마지막으로 처리한 보드"의 상세 페이지 상태를
# 찍는데, 그 보드가 순위 0명(빈 리더보드)이면 표시 개수(페이지네이션) 드롭다운 자체가
# 렌더되지 않아 role=combobox가 사라진다(실측). 실제 구조 변경이 아니라 마지막 보드의
# 데이터 유무에 따른 정상적 차이이므로 이 스텝에서만 국소적으로 무시한다 — 공용
# LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS에 넣지 않는 이유는 다른 스텝(목록 화면 등)에서는
# combobox 소실이 실제 문제일 수 있어 공용 범위를 넓히면 안 되기 때문(2026-06-29 원칙).
LEADERBOARD_COMPLETE_IGNORE_PATTERNS = LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS + [
    r"role: combobox$",
]



def _resolve_title_int_env(prefix: str, suffix: str, default_value: int) -> int:
    """{prefix}_{suffix} env가 있으면 그 값, 없으면 default_value(전역 fallback) 그대로."""
    if not prefix:
        return default_value
    raw = os.environ.get(f"{prefix}_{suffix}", "").strip()
    return int(raw) if raw else default_value


def _resolve_title_bool_env(prefix: str, suffix: str, default_value: bool) -> bool:
    """{prefix}_{suffix} env가 있으면 그 값(불리언 파싱), 없으면 default_value(전역 fallback) 그대로."""
    if not prefix:
        return default_value
    raw = os.environ.get(f"{prefix}_{suffix}", "").strip()
    return _parse_bool_env(raw) if raw else default_value


def _is_visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _leaderboard_nav_source(page) -> str:
    if _is_visible(page.locator("input[name='leaderboardName']")):
        return "list"
    if _is_visible(page.locator("input[name='value']")):
        return "detail"
    return "other"


def _leaderboard_nav_step_name(page, nav_context: str) -> str:
    # "return"(다음 키워드로 전환)도 "board_loop"와 동일하게 직전 화면이 목록/상세
    # 둘 다 될 수 있다 — 직전 키워드에서 보드를 하나라도 찾았으면 그 마지막 보드의
    # 상세 페이지에서 끝나고, 하나도 못 찾았으면(continue) 목록 페이지에서 끝난다.
    # 화면 종류를 구분하지 않고 이름 하나로 기록하면 서로 다른 화면의 지문이 같은
    # baseline으로 비교되어 매번 오탐이 발생한다(실측: leaderboard_nav_return_pre).
    if nav_context in ("board_loop", "return"):
        return f"leaderboard_nav_{nav_context}_{_leaderboard_nav_source(page)}_pre"
    return f"leaderboard_nav_{nav_context}_pre"


def _leaderboard_open_step_name() -> str:
    return "leaderboard_open_list_pre"


def parse_args():
    parser = argparse.ArgumentParser(description="Leaderboard PvPRank extractor")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--project-base", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument("--key", default="", help="GCP 서비스계정 JSON 키 경로 (직접 지정)")
    parser.add_argument("--gcp-project", default="", help="GCP 프로젝트 ID (직접 지정, Cloud Logging용)")
    parser.add_argument("--gcp-log", default="", help="GCP 로그 이름 (직접 지정, Cloud Logging용)")
    parser.add_argument(
        "--log-console",
        default="",
        help="계정 생성일 조회 백엔드: cloud_logging(기본) 또는 bigquery (직접 지정, 보통 --title로 env에서 자동 설정)",
    )
    parser.add_argument("--bq-project", default="", help="BigQuery GCP 프로젝트 ID (직접 지정)")
    parser.add_argument("--bq-dataset", default="", help="BigQuery 데이터셋명 (직접 지정)")
    parser.add_argument("--bq-table", default="", help="BigQuery 테이블명 (직접 지정)")
    parser.add_argument("--bq-user-col", default="", help="BigQuery 유저 식별 컬럼명 (직접 지정)")
    parser.add_argument("--bq-date-col", default="", help="BigQuery 계정생성일(가입일) 컬럼명 (직접 지정)")
    parser.add_argument(
        "--keywords",
        default=DEFAULT_SEARCH_KEYWORDS,
        help=(
            "검색할 리더보드 이름 키워드(콤마 구분, 복수 지정 가능). "
            "키워드마다 검색 후 나온 리더보드를 모두 조회한 뒤 다음 키워드로 넘어감 "
            f"(default: {DEFAULT_SEARCH_KEYWORDS})"
        ),
    )
    parser.add_argument(
        "--title",
        default="",
        metavar="NAME",
        help=(
            "타이틀 이름 (예: gametitle). "
            "env에서 {NAME}_KEY_FILE / {NAME}_GCP_PROJECT / {NAME}_LOGNAME / "
            "{NAME}_PROJECT_NAME / {NAME}_LEADERBOARD_KEYWORDS 를 일괄 적용. "
            "{NAME}_LOG_CONSOLE=bigquery 이면 GCP_PROJECT/LOGNAME 대신 "
            "{NAME}_BQ_PROJECT / _BQ_DATASET / _BQ_TABLE / _BQ_USER_COL / _BQ_DATE_COL 을 사용."
        ),
    )
    parser.add_argument("--gametitle", action="store_true", help="--title gametitle 단축키")
    parser.add_argument("--dc", action="store_true", help="--title dc 단축키 (게임B)")
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="예약 실행용: 오류 발생 시 Enter 입력 대기 없이 즉시 종료",
    )
    parser.add_argument(
        "--test-single-board",
        action="store_true",
        help=(
            "테스트용: 검색된 리더보드 중 1개만 조회한 뒤 나머지 키워드/리더보드는 "
            "건너뛰고 바로 다음 단계(신규유저 영수증검증 등)로 진행합니다."
        ),
    )
    # --- 신규 유저 차단(운영 실행) 옵션 ---
    parser.add_argument(
        "--block-new-users",
        action="store_true",
        help=(
            f"신규 유저 중 총 결제액이 임계값(기본 {BLOCK_MAX_TOTAL_PAYMENT:,}원, "
            "env USER_BLOCK_MAX_TOTAL_PAYMENT, {TITLE}_USER_BLOCK_MAX_TOTAL_PAYMENT로 프로젝트별 override 가능) "
            "이하인 대상을 실제로 user_block(디바이스밴 포함)합니다. --dc는 가격 대신 최근 구매 건수 "
            "(env USER_BLOCK_MAX_PURCHASE_COUNT/{TITLE}_USER_BLOCK_MAX_PURCHASE_COUNT) 기준입니다. "
            "지정하지 않아도 env USER_BLOCK_NEW_USERS_ENABLED(또는 {TITLE}_USER_BLOCK_NEW_USERS_ENABLED, "
            "값 1/true/yes/on) 가 켜져 있으면 동일하게 실제 차단이 진행됩니다 — 예약 실행(작업 스케줄러)은 "
            "이 CLI 플래그를 넘기지 않으므로, 프로젝트별로 실제 차단 여부를 정하는 유일한 경로입니다. "
            "둘 다 꺼져 있으면 후보 목록만 출력하는 드라이런으로 동작합니다(운영 실행 승인 게이트)."
        ),
    )
    parser.add_argument(
        "--reason",
        default=DEFAULT_BLOCK_REASON,
        help=f"차단 사유 (default: {DEFAULT_BLOCK_REASON})",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=DEFAULT_BLOCK_PERIOD_DAYS,
        help=f"차단 기간(일) (default: {DEFAULT_BLOCK_PERIOD_DAYS})",
    )
    parser.add_argument(
        "--skip-rank-delete",
        action="store_true",
        help="차단 시 리더보드 순위 삭제 체크를 하지 않습니다.",
    )
    parser.add_argument(
        "--skip-device-block",
        action="store_true",
        help="user_block 후 디바이스 차단 절차를 건너뜁니다(기본은 디바이스밴 포함).",
    )
    parser.add_argument(
        "--device-ban-count",
        type=int,
        default=DEFAULT_DEVICE_BAN_COUNT,
        help=f"디바이스 목록 중 최하단 몇 개를 차단할지 (default: {DEFAULT_DEVICE_BAN_COUNT})",
    )
    return parser.parse_args()


def open_leaderboard_page(page, nav_context: str = "initial"):
    print("[4] 사이드 메뉴에서 '리더보드' 페이지로 이동합니다.")
    link = page.locator("a#baseRank, a[href*='/baseRank']").first
    ensure_sidebar_link_expanded(page, link, "leaderboard_category_expand_pre")
    link.scroll_into_view_if_needed()
    ignore_patterns = (
        LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS
        if nav_context in ("return", "board_loop")
        else None
    )
    record_step_dump(
        page,
        _leaderboard_nav_step_name(page, nav_context),
        ignore_patterns=ignore_patterns,
    )
    link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("input[name='leaderboardName']").first.wait_for(
        state="visible",
        timeout=15_000,
    )


def search_pvp_rank(page, keyword: str):
    print(f"[5] 검색창에 '{keyword}'를 입력하고 검색합니다.")
    search_input = page.locator("input[name='leaderboardName']").first
    search_input.wait_for(state="visible", timeout=10_000)
    record_step_dump(page, "leaderboard_search_input_pre")
    search_input.fill("")
    search_input.fill(keyword)
    actual_value = search_input.input_value()
    if actual_value != keyword:
        raise RuntimeError(f"검색어 입력값이 반영되지 않았습니다: 입력='{keyword}' 실제='{actual_value}'")

    search_button = page.get_by_role("button", name="검색", exact=True).first
    search_button.wait_for(state="visible", timeout=10_000)
    search_button.scroll_into_view_if_needed()
    record_step_dump(page, "leaderboard_search_submit_pre")
    search_button.click()
    safe_wait_for_load(page, "networkidle", 10_000)


def get_data_rows(page):
    return page.locator("div.MuiDataGrid-row, [role='row'][data-id], tbody tr")


def wait_for_data_rows(page, timeout_ms: int = 15_000):
    rows = get_data_rows(page)
    rows.first.wait_for(state="visible", timeout=timeout_ms)
    return rows


def get_rows_per_page_dropdown(page, container=None, timeout_ms: int = 10_000):
    scope = container if container is not None else page
    selectors = [
        ".MuiTablePagination-select[role='combobox']",
        "[role='combobox']",
        "[aria-haspopup='listbox']",
    ]

    def _find_dropdown():
        visible_dropdowns = []
        for selector in selectors:
            dropdowns = scope.locator(selector)
            count = dropdowns.count()
            for index in range(count):
                dropdown = dropdowns.nth(index)
                try:
                    if not dropdown.is_visible():
                        continue
                    text = dropdown.inner_text().strip()
                except Exception:
                    continue
                if text == "100개씩 보기":
                    return dropdown
                if text.endswith("개씩 보기") or "보기" in text:
                    visible_dropdowns.append(dropdown)
            if visible_dropdowns:
                return visible_dropdowns[-1]
        return None

    # 검색/전환 직후 footer pagination 렌더가 늦을 수 있어 공용 반복 대기로 폴링한다.
    dropdown = wait_until(page, _find_dropdown, timeout_ms=timeout_ms)
    if dropdown is None:
        raise RuntimeError("표시 개수 드롭다운을 찾지 못했습니다.")
    return dropdown


def set_rows_per_page(page, target: int, label: str, verify_prefix: str = "", container=None, ignore_patterns=None):
    target_text = f"{target}개씩 보기"
    print(f"    {label}: {target_text}로 변경합니다.")

    # 검색/전환 직후 footer pagination이 재렌더되며 드롭다운 element가 DOM에서 떨어질 수 있다(stale).
    # 조회~열기를 재시도로 감싸 stale 시 재조회하고, 위치 조정은 click의 자동 스크롤에 맡긴다.
    dropdown = None
    option = None
    opened = False
    for _ in range(3):
        try:
            dropdown = get_rows_per_page_dropdown(page, container=container)
            current_text = dropdown.inner_text().strip()
            if current_text == target_text:
                print("    이미 설정되어 있습니다.")
                return
            record_step_dump(
                page,
                name=f"{verify_prefix}_dropdown_pre" if verify_prefix else "rows_dropdown_pre",
                ignore_patterns=ignore_patterns,
            )
            dropdown.click()
            if (dropdown.get_attribute("aria-expanded") or "").lower() != "true":
                continue
            # aria-expanded=true 확인 후 option 목록에 target_text가 실제로 보이는지 검증
            _opt = page.get_by_role("option", name=target_text, exact=True).first
            _opt.wait_for(state="visible", timeout=3_000)
            option = _opt
            opened = True
            break
        except Exception:
            page.wait_for_timeout(POLL_WAIT_MS)  # stale/옵션 미표시 → 안정 대기 후 재조회

    if not opened:
        raise RuntimeError(f"{label} 드롭다운을 열지 못했습니다.")

    record_step_dump(
        page,
        name=f"{verify_prefix}_option_pre" if verify_prefix else "rows_option_pre",
        ignore_patterns=ignore_patterns,
    )
    option.click()
    safe_wait_for_load(page, "networkidle", 5_000)

    def _rows_per_page_applied():
        try:
            return True if dropdown.inner_text().strip() == target_text else None
        except Exception:
            return None  # 전환 직후 재렌더로 stale → 재폴링

    if wait_until(page, _rows_per_page_applied, timeout_ms=10_000, wait_ms=POLL_WAIT_MS):
        return

    raise RuntimeError(f"{label} 전환 결과가 기대와 다릅니다: expected='{target_text}'")


def collect_visible_board_names(page, keyword: str) -> list:
    print(f"[6] 현재 목록 페이지에서 '{keyword}_*' 리더보드 이름을 수집합니다.")
    # 뒤에 '_XXX' 접미사가 붙는 접두사 키워드(예: "AllyArena")와, 그 자체가 이미
    # 완전한 리더보드 이름인 키워드(예: --test-single-board 용 "AllyArena_GO_106")를
    # 둘 다 지원해야 한다. 접미사를 선택적으로 만들되, "AllyArena_GO_106"이
    # "AllyArena_GO_1067" 같은 더 긴 이름의 앞부분만 잘못 잘라 매치하지 않도록
    # 뒤에 단어문자가 이어지면 매치 실패로 처리하는 부정 전방탐색을 둔다.
    board_name_re = re.compile(rf"{re.escape(keyword)}(?:_[A-Za-z0-9_]+)?(?![A-Za-z0-9_])")
    wait_for_data_rows(page)
    rows = get_data_rows(page)
    count = rows.count()

    board_names = []
    seen = set()
    for index in range(count):
        try:
            row_text = rows.nth(index).inner_text().strip()
        except Exception:
            continue
        if not row_text:
            continue

        match = board_name_re.search(row_text)
        if match is None:
            continue

        name = match.group(0)
        if name in seen:
            continue
        board_names.append(name)
        seen.add(name)

    print(f"    {len(board_names)}개 리더보드 발견: {board_names}")
    return board_names


def open_leaderboard_list_and_search(page, keyword: str, nav_context: str = "initial"):
    open_leaderboard_page(page, nav_context=nav_context)
    search_pvp_rank(page, keyword)
    print(f"[7] 목록을 {LIST_ROWS_PER_PAGE}개씩 보기로 맞춥니다.")
    list_footer = page.locator(".MuiDataGrid-footerContainer").first
    set_rows_per_page(page, LIST_ROWS_PER_PAGE, "리더보드 목록 표시 개수", verify_prefix="list_rows", container=list_footer)


def open_leaderboard_list_and_collect_with_retry(page, keyword: str, start_url: str, project_name: str, nav_context: str = "initial") -> list:
    """목록 진입+검색+보드 이름 수집을 하나의 절차로 묶어 재시도한다.

    세션 자동 로그아웃뿐 아니라, 이전 동작에서 남은 MUI 메뉴/드롭다운의 보이지 않는
    backdrop이 클릭을 가로막아 사이드바 진입 자체가 반복 실패하는 경우도 있다
    (실측: is_login_page는 False인데도 클릭이 계속 막힘). 로그인 여부만 확인해서는
    이 상태를 벗어나지 못하므로, 기존에 쓰던 start_url로 재접속해 화면을 완전히
    리셋하고 프로젝트를 메뉴로 다시 선택한다(prepare_console_project 재사용).

    검색 결과 목록 읽기(collect_visible_board_names)까지 같은 action에 포함한다 —
    분리돼 있으면 결과 렌더 지연 같은 일시적 오류가 재시도 한 번 없이 곧장 예외로
    올라가 호출부의 재시도 범위를 벗어났다(2026-07-03 수정: action은 전체 절차를
    포함해야 한다는 원칙 위반 — 목록 조회 재시도 소진 여부와 무관하게 결과 읽기
    타임아웃은 재시도되지 않던 문제).
    """
    def _action():
        open_leaderboard_list_and_search(page, keyword, nav_context=nav_context)
        return collect_visible_board_names(page, keyword)

    return retry_with_recovery(
        action=_action,
        recovery=lambda: prepare_console_project(
            page=page,
            explicit_project_base="",
            start_url=start_url,
            project_name=project_name,
        ),
        label=f"검색어 '{keyword}' 목록 조회 재시도",
        recovery_desc=f"콘솔 초기화면({start_url})으로 재접속 후 재시도합니다.",
        max_retries=RETRY_MAX_RETRIES,
    )


def _click_board_from_list(page, board_name: str):
    open_step_name = _leaderboard_open_step_name()
    rows = get_data_rows(page)
    row = rows.filter(has_text=board_name).first
    if wait_for_visible(row, 2_000):
        row.scroll_into_view_if_needed()
        exact_label = find_exact_text_match(row.get_by_text(board_name, exact=True), board_name)
        if exact_label is not None and wait_for_visible(exact_label, 1_000):
            record_step_dump(page, open_step_name)
            exact_label.click()
        else:
            record_step_dump(page, open_step_name)
            row.click()
        return

    exact_text = find_exact_text_match(page.get_by_text(board_name, exact=True), board_name)
    if exact_text is None:
        raise RuntimeError(f"목록에서 '{board_name}'를 찾지 못했습니다.")

    exact_text.scroll_into_view_if_needed()
    record_step_dump(page, open_step_name)
    exact_text.click()


def enter_leaderboard_detail(page, board_name: str):
    print(f"[8] '{board_name}' 리더보드로 진입합니다.")
    before_url = page.url
    _click_board_from_list(page, board_name)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)

    title = page.get_by_role("heading", name=board_name, exact=True).first
    def _detail_opened():
        if wait_for_visible(title, 1_000):
            return True
        if page.url != before_url and wait_for_visible(get_data_rows(page).first, 1_000):
            return True
        return None

    if wait_until(page, _detail_opened, timeout_ms=15_000, wait_ms=POLL_WAIT_MS):
        return

    raise RuntimeError(f"'{board_name}' 상세 페이지 진입을 확인하지 못했습니다.")


def _row_cells(row_el) -> list:
    cells = row_el.locator("[role='gridcell'], [role='cell'], td")
    count = cells.count()
    if count > 0:
        values = []
        for index in range(count):
            try:
                values.append(cells.nth(index).inner_text().strip())
            except Exception:
                values.append("")
        return values
    return [part.strip() for part in row_el.inner_text().split("\t") if part.strip()]


def get_leaderboard_rank_grid(page):
    # 페이지에 DataGrid가 2개 존재: 보상 구조(rank/rewardItems) + 플레이어 순위(uuid/nickname)
    # uuid·nickname 필드가 있는 두 번째 그리드를 정확히 잡는다.
    return (
        page.locator("div.MuiDataGrid-root")
        .filter(has=page.locator("[data-field='uuid']"))
        .filter(has=page.locator("[data-field='nickname']"))
        .first
    )


def get_leaderboard_rank_rows(page):
    grid = get_leaderboard_rank_grid(page)
    return grid.locator("div.MuiDataGrid-row, [role='row'][data-id], tbody tr")


# 리더보드에 순위 데이터가 아예 없을 때 화면에 뜨는 문구(실제 덤프로 확인: 문장 끝에
# 마침표가 붙어 "현재 순위가 없습니다."로 표시됨) — 로딩 실패와 구분하는 판정 기준.
# 이 문구가 뜨면 "빈 결과"이지 "로딩 실패"가 아니므로 재시도 대상이 아니다.
# 부분 일치(exact=False)로 찾는다 — 마침표 유무 등 사소한 표기 차이에 흔들리지 않기 위함
# (이 문장 자체가 충분히 구체적이라 다른 텍스트와 오탐될 위험은 낮음).
LEADERBOARD_EMPTY_TEXT = "현재 순위가 없습니다"


def _leaderboard_rank_grid_state(page):
    """행이 로드됐는지, 빈 상태 문구가 떴는지 판정. "rows" | "empty" | None(아직 판정 불가)."""
    if _is_visible(get_leaderboard_rank_rows(page).first):
        return "rows"
    if _is_visible(page.get_by_text(LEADERBOARD_EMPTY_TEXT)):
        return "empty"
    return None


def wait_for_leaderboard_rank_rows(page, timeout_ms: int = 15_000):
    """행이 나타나거나 빈 상태 문구가 뜰 때까지 대기하고 상태를 구분해 반환한다.

    반환: ("rows", rows_locator) 또는 ("empty", None).
    시간 초과(둘 다 안 뜸)는 실제 로딩 실패이므로 예외를 던져 상위 재시도로 넘긴다.
    """
    state = wait_until(page, lambda: _leaderboard_rank_grid_state(page), timeout_ms=timeout_ms)
    if state == "rows":
        return "rows", get_leaderboard_rank_rows(page)
    if state == "empty":
        return "empty", None
    raise RuntimeError(
        f"리더보드 순위 목록 로딩을 확인하지 못했습니다(행도, '{LEADERBOARD_EMPTY_TEXT}' 문구도 나타나지 않음)."
    )


def _get_leaderboard_grid_scroll_state(page):
    grid = get_leaderboard_rank_grid(page)
    if not wait_for_visible(grid, 5_000):
        return None

    return grid.locator(".MuiDataGrid-virtualScroller").first.evaluate(
        """
        el => ({
          scrollTop: el.scrollTop,
          clientHeight: el.clientHeight,
          scrollHeight: el.scrollHeight
        })
        """
    )


def _scroll_leaderboard_grid_once(page):
    scroller = get_leaderboard_rank_grid(page).locator(".MuiDataGrid-virtualScroller").first
    if not wait_for_visible(scroller, 5_000):
        return False

    box = scroller.bounding_box()
    if not box:
        return False

    page.mouse.move(box["x"] + (box["width"] / 2), box["y"] + (box["height"] / 2))
    record_step_dump(
        page,
        "leaderboard_scroll_pre",
        ignore_patterns=LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS,
    )
    page.mouse.wheel(0, GRID_SCROLL_STEP_PX)
    page.wait_for_timeout(POLL_WAIT_MS)
    return True


def _extract_uuid_from_cells(cells: list, fallback_text: str) -> str:
    for cell in cells:
        match = UUID_RE.fullmatch(cell)
        if match:
            return match.group(0)

    match = UUID_RE.search(fallback_text)
    return match.group(0) if match else ""


def _read_row_field_text(row, field_names) -> str:
    for field_name in field_names:
        try:
            cell = row.locator(f"[data-field='{field_name}']").first
            value = cell.get_attribute("title")
            if value:
                return value.strip()
            text = cell.inner_text().strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def _extract_rank_from_row(row) -> int | None:
    # 1차: 순위 셀(data-field='rank'/'index')의 실제 값.
    #   공동순위(동점)는 이 값이 같은 숫자로 반복 표시되므로(예: 3위 4명 → "3" 4행),
    #   행 위치(data-rowindex)가 아니라 이 실제 순위값으로 판정해야 한다.
    rank_text = _read_row_field_text(row, ["rank", "index"])
    if rank_text and re.fullmatch(r"\d+", rank_text):
        return int(rank_text)
    # 2차: MuiDataGrid 행의 data-rowindex (0-based → 1-based) fallback.
    #   순위 셀을 못 읽을 때만 사용 — 공동순위 구분은 불가.
    rowindex_str = (row.get_attribute("data-rowindex") or "").strip()
    if rowindex_str.isdigit():
        return int(rowindex_str) + 1
    return None


def _is_retryable_gcp_error(err_text: str) -> bool:
    text = (err_text or "").strip()
    if not text:
        return False

    match = re.search(r"HTTP\s+(\d+)", text, re.I)
    if match:
        return int(match.group(1)) in {408, 429, 500, 502, 503, 504}

    lowered = text.lower()
    return any(
        token in lowered
        for token in [
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection reset",
            "remote end closed connection",
            "ssl",
            "tls",
        ]
    )


def _rfc3339_hours_before(now_utc, hours: int) -> str:
    """now_utc(naive UTC)에서 hours시간 전을 GCP 필터용 RFC3339(Z) 문자열로."""
    dt = now_utc - datetime.timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_recent_user_log_filter(project: str, log_name: str, uuid: str, since_iso: str = None) -> str:
    # SUB_CATEGORY 제한 없음 — 해당 유저의 로그 종류를 가리지 않고 가장 최근 생성된 것을 조회한다.
    # since_iso가 있으면 timestamp 하한을 걸어 스캔 범위를 신규 계정 기준 창으로 좁힌다(ACCOUNT_LOOKUP_LOOKBACK_HOURS).
    log_path = f"projects/{project}/logs/{log_name}"
    filt = f'logName="{log_path}" AND jsonPayload._user_id="{uuid}"'
    if since_iso:
        filt += f' AND timestamp >= "{since_iso}"'
    return filt


def _fetch_recent_user_log_with_retry(logging_service, project, log_name, uuid, since_iso=None):
    if not (project and log_name and uuid):
        return None, "project/log_name/uuid 부족"

    filter_expr = _build_recent_user_log_filter(project, log_name, uuid, since_iso)
    last_err = None

    for attempt in range(1, GCP_QUERY_MAX_RETRIES + 1):
        entry, err = fetch_recent_log_entry(logging_service, project, filter_expr)
        if not err:
            return entry, None

        last_err = err
        if not _is_retryable_gcp_error(err) or attempt >= GCP_QUERY_MAX_RETRIES:
            return None, err

        wait_seconds = (GCP_QUERY_RETRY_WAIT_MS * attempt) / 1000
        print(
            f"      [GCP retry] {uuid} {attempt}/{GCP_QUERY_MAX_RETRIES} 실패: {err} "
            f"-> {wait_seconds:.1f}초 후 재시도"
        )
        time.sleep(wait_seconds)

    return None, last_err


def query_account_creation_info(logging_service, project, log_name, uuid, now_utc) -> dict:
    """유저의 최근 로그(종류 무관, 신규 기준 창 안) 1건에서 계정 생성일(_create_account_date)만 읽는다.

    조회 창은 ACCOUNT_LOOKUP_LOOKBACK_HOURS(기본=ACCOUNT_NEW_HOURS)로 좁힌다. 창 안에 로그가 없으면
    최근 활동이 없다는 뜻이고, 그런 계정은 신규일 수 없으므로 "기존 유저"로 확정한다(과거엔 "로그없음"으로
    표기해 조회 실패처럼 보였으나, 실제로는 조회가 정상이고 단지 창 밖의 오래된 계정이라는 뜻).
    """
    since_iso = _rfc3339_hours_before(now_utc, ACCOUNT_LOOKUP_LOOKBACK_HOURS)
    entry, err = _fetch_recent_user_log_with_retry(logging_service, project, log_name, uuid, since_iso)

    if err:
        return {"account_type": f"조회실패({err})", "create_account_date": ""}
    if entry is None:
        # 신규 기준 창 안에 로그 없음 → 최근 활동 없음 → 신규 계정 아님 → 기존 유저.
        return {"account_type": "기존 유저", "create_account_date": ""}

    create_date_str = (entry.get("jsonPayload") or {}).get("_create_account_date", "")

    account_type = "기존 유저"
    if create_date_str:
        try:
            create_dt = datetime.datetime.fromisoformat(create_date_str.replace("Z", ""))
            hours_diff = (now_utc - create_dt).total_seconds() / 3600
            account_type = "계정 신규 생성" if hours_diff <= ACCOUNT_NEW_HOURS else "기존 유저"
        except Exception:
            account_type = "날짜파싱실패"
    else:
        account_type = "create_date없음"

    return {
        "account_type": account_type,
        "create_account_date": create_date_str,
    }


# GCP 로그 병렬 조회용. credentials는 1회 로드해 공유하고(thread-safe),
# httplib2/SSL을 품은 service(회선)만 스레드별로 build한다.
# 스레드풀은 전체 조회가 끝날 때까지 재사용 → service build가 총 max_workers회로 끝난다.
_gcp_tls = threading.local()
_gcp_executor = None


def _thread_logging_service(credentials):
    """현재 스레드 전용 logging_service. 스레드당 1회만 build(credentials는 공유)."""
    service = getattr(_gcp_tls, "service", None)
    if service is None:
        service = build_logging_service_from_credentials(credentials)
        _gcp_tls.service = service
    return service


def _get_gcp_executor():
    """GCP 로그 병렬 조회용 공용 스레드풀. 보드마다 새로 만들지 않고 재사용한다."""
    global _gcp_executor
    if _gcp_executor is None:
        _gcp_executor = ThreadPoolExecutor(max_workers=4)  # 동시 요청 burst 완화(429 감소)
    return _gcp_executor


def shutdown_gcp_executor():
    """공용 스레드풀 종료 (run 완료 후 main에서 호출)."""
    global _gcp_executor
    if _gcp_executor is not None:
        _gcp_executor.shutdown(wait=True)
        _gcp_executor = None


def enrich_board_with_gcp(board_rows, credentials, project, log_name, now_utc):
    """board_rows 각 행에 GCP 최근 로그(종류 무관) 1건 기준 계정 생성일을 in-place로 추가.
    credentials는 공유, service만 스레드별. 스레드풀은 보드 간 재사용한다."""
    if not credentials or not project or not log_name:
        for row in board_rows:
            row.update({"account_type": "GCP미설정", "create_account_date": ""})
        return
    print(f"    [GCP] {len(board_rows)}명 최근 로그 1건씩 계정 생성일 조회 중...")

    def _work(uuid_value):
        # 스레드별 service로 조회 → SSL 연결 공유 충돌 방지
        service = _thread_logging_service(credentials)
        if service is None:
            raise RuntimeError("logging service build 실패(credentials 확인)")
        return query_account_creation_info(service, project, log_name, uuid_value, now_utc)

    executor = _get_gcp_executor()
    futures = {executor.submit(_work, row["uuid"]): row for row in board_rows}
    for future in as_completed(futures):
        row = futures[future]
        try:
            row.update(future.result())
        except Exception as exc:
            row.update({"account_type": f"조회실패({exc})", "create_account_date": ""})


def _is_retryable_bq_error(err_text: str) -> bool:
    text = (err_text or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in [
            "429", "500", "502", "503", "504",
            "timeout", "timed out", "deadline exceeded",
            "temporarily unavailable", "connection reset",
            "rate limit", "console error",
        ]
    )


def _fetch_account_creation_date_with_retry(bq_client, bq_project, dataset, table, user_col, date_col, uuid):
    last_err = None
    for attempt in range(1, GCP_QUERY_MAX_RETRIES + 1):
        min_date, err = fetch_min_date_for_user(bq_client, bq_project, dataset, table, user_col, date_col, uuid)
        if not err:
            return min_date, None

        last_err = err
        if not _is_retryable_bq_error(err) or attempt >= GCP_QUERY_MAX_RETRIES:
            return None, err

        wait_seconds = (GCP_QUERY_RETRY_WAIT_MS * attempt) / 1000
        print(
            f"      [BQ retry] {uuid} {attempt}/{GCP_QUERY_MAX_RETRIES} 실패: {err} "
            f"-> {wait_seconds:.1f}초 후 재시도"
        )
        time.sleep(wait_seconds)

    return None, last_err


def query_account_creation_info_bigquery(bq_client, bq_project, dataset, table, user_col, date_col, uuid, now_utc) -> dict:
    """BigQuery 유저 마스터 테이블(예: dc_all)에서 유저의 가입일(date_col 최솟값)로 신규/기존을 판정한다.

    dc_all은 "유저당 한 행"인 마스터 테이블이 아니라 Firebase/GA4 스타일 이벤트 로그 테이블이다
    (라이브로 스키마 확인: event_name/event_timestamp 등 이벤트 컬럼 포함, 15억+ 행, 2026-07-03).
    즉 MIN(date_col)은 "그 uuid가 남긴 이벤트 중 가장 이른 시각"이며 가입일의 근사치일 뿐이다.
    Cloud Logging 방식(query_account_creation_info)은 "최근 활동 없음=기존 유저"로 판단하지만,
    여기서는 해당 uuid의 행 자체가 없는 경우가 "최근 활동 없음"과 "이 uuid는 이 테이블에 이벤트를
    한 번도 남긴 적이 없음"을 구분할 수 없어(클라이언트의 분석 수집 비활성화, 오래된 계정의 이벤트
    보존기간 만료 등 여러 원인 가능) "기존 유저"로 단정하지 않고 별도 상태("계정정보없음")로 남겨
    신규 차단 후보 판정에서 조용히 누락되지 않게 한다.
    """
    create_dt, err = _fetch_account_creation_date_with_retry(
        bq_client, bq_project, dataset, table, user_col, date_col, uuid
    )
    if err:
        return {"account_type": f"조회실패({err})", "create_account_date": ""}
    if create_dt is None:
        return {"account_type": "계정정보없음", "create_account_date": ""}

    if isinstance(create_dt, str):
        # dc_all의 in_date_UTC는 BigQuery 스키마상 STRING 컬럼(예: "2026-06-30 02:53:56")이라
        # TIMESTAMP/DATETIME으로 오지 않는다(라이브 조회로 확인, 2026-07-03). Cloud Logging 쪽
        # query_account_creation_info와 동일하게 파싱 실패는 별도 상태로 남긴다.
        try:
            create_dt = datetime.datetime.fromisoformat(create_dt.replace("Z", ""))
        except Exception:
            return {"account_type": "날짜파싱실패", "create_account_date": create_dt}

    if create_dt.tzinfo is not None:
        create_dt = create_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)

    hours_diff = (now_utc - create_dt).total_seconds() / 3600
    account_type = "계정 신규 생성" if hours_diff <= ACCOUNT_NEW_HOURS else "기존 유저"
    return {
        "account_type": account_type,
        "create_account_date": create_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _thread_bigquery_client(bq_credentials, bq_project):
    """현재 스레드 전용 BigQuery client. 스레드당 1회만 build(credentials는 공유)."""
    client = getattr(_gcp_tls, "bq_client", None)
    if client is None:
        client = build_bigquery_client_from_credentials(bq_credentials, bq_project)
        _gcp_tls.bq_client = client
    return client


def enrich_board_with_bigquery(board_rows, bq_credentials, bq_project, dataset, table, user_col, date_col, now_utc):
    """board_rows 각 행에 BigQuery 유저 마스터 테이블 기준 계정 신규 여부를 in-place로 추가.
    credentials는 공유, client만 스레드별. 스레드풀은 GCP Cloud Logging 조회와 공용(_get_gcp_executor)이다."""
    if not (bq_credentials and bq_project and dataset and table and user_col and date_col):
        for row in board_rows:
            row.update({"account_type": "BQ미설정", "create_account_date": ""})
        return
    print(f"    [BQ] {len(board_rows)}명 가입일(BigQuery) 조회 중...")

    def _work(uuid_value):
        client = _thread_bigquery_client(bq_credentials, bq_project)
        if client is None:
            raise RuntimeError("BigQuery client build 실패(credentials 확인)")
        return query_account_creation_info_bigquery(
            client, bq_project, dataset, table, user_col, date_col, uuid_value, now_utc
        )

    executor = _get_gcp_executor()
    futures = {executor.submit(_work, row["uuid"]): row for row in board_rows}
    for future in as_completed(futures):
        row = futures[future]
        try:
            row.update(future.result())
        except Exception as exc:
            row.update({"account_type": f"조회실패({exc})", "create_account_date": ""})


def _parse_price_to_int(text: str) -> int:
    """금액 셀 텍스트에서 숫자만 추출해 정수로. (예: '₩55,000' → 55000)"""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def prepare_receipt_verification_session(page, explicit_project_base, start_url, project_name):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    open_receipt_verification_menu(page)
    wait_for_receipt_page_render_stable(page)
    return {
        "initialized": True,
        "rows_per_page_applied": False,
    }


def summarize_recent_payments(page, uuid_value, start_url, project_name, timeout_error,
                              limit=RECENT_PAYMENT_LIMIT, receipt_session=None, dc_mode=False):
    """콘솔 영수증검증 메뉴 조회 → 최근 limit건 결제액 합계. 읽기 전용.

    영수증검증은 이미 100개씩 보기로 최신순 표시되므로 rows 앞에서 limit건을 합산한다.

    dc_mode=True(--dc)이면 가격("금액") 조회/합산을 하지 않고 최근 limit건 중
    구매 제품 건수만 센다(게임B 프로젝트는 가격 등 조회 불가 정책).
    """
    if receipt_session is None:
        receipt_session = prepare_receipt_verification_session(
            page,
            "",
            start_url,
            project_name,
        )
    elif not receipt_session.get("initialized"):
        receipt_session.update(
            prepare_receipt_verification_session(
                page,
                "",
                start_url,
                project_name,
            )
        )

    fill_uuid_search(page, uuid_value)
    click_search_button(page)
    step_and_verify_ui(page, "receipt_results", ignore_patterns=RECEIPT_IGNORE_PATTERNS)
    result = collect_result(
        page,
        uuid_value,
        timeout_error,
        ensure_rows_per_page=not receipt_session.get("rows_per_page_applied", False),
    )
    if result.get("has_results"):
        receipt_session["rows_per_page_applied"] = True

    rows = result.get("rows") or []
    recent = rows[:limit]
    if dc_mode:
        return {"recent_purchase_count": len(recent)}
    total = sum(_parse_price_to_int(r.get("금액", "")) for r in recent)
    return {
        "recent_payment_count": len(recent),
        "recent_payment_sum": total,
    }


def enrich_new_users_with_payments(page, all_rows, start_url, project_name, timeout_error, dc_mode=False, checkpoint=None):
    """all_rows 중 '계정 신규 생성' 유저에 영수증검증 최근 결제 합계를 in-place 추가.

    같은 유저가 여러 보드에 중복될 수 있어 uuid 로 한 번만 조회하고 해당 행 전부에 반영.

    dc_mode=True(--dc)이면 가격 합계 대신 최근 구매 제품 건수만 조회한다
    (게임B 프로젝트는 가격 등 조회 불가 정책).

    checkpoint(all_rows)가 주어지면 UUID 1명 처리(성공/실패 무관)할 때마다 호출해
    그 시점까지의 결과를 파일에 즉시 반영한다(전체가 끝나야 저장되는 문제 방지).
    """
    new_uuids = []
    seen = set()
    for r in all_rows:
        if r.get("account_type") == "계정 신규 생성" and r["uuid"] not in seen:
            seen.add(r["uuid"])
            new_uuids.append(r["uuid"])

    if not new_uuids:
        print("\n[10] 신규 유저(계정 240시간 이내) 없음 — 영수증검증 단계 생략.")
        return

    if dc_mode:
        print(f"\n[10] 신규 유저 {len(new_uuids)}명 영수증검증 최근 {RECENT_PAYMENT_LIMIT}건 중 구매 제품 건수 조회...")
    else:
        print(f"\n[10] 신규 유저 {len(new_uuids)}명 영수증검증 최근 {RECENT_PAYMENT_LIMIT}건 결제액 합계 조회...")
    receipt_session = {
        "initialized": False,
        "rows_per_page_applied": False,
    }

    def _recover_receipt_session():
        # 세션이 초기화된 상태에서 UUID 하나가 실패해도, 다음 시도가 여전히 초기화된
        # 것으로 착각해 fill_uuid_search를 바로 재시도하지 않도록 초기화 플래그를
        # 되돌리고 콘솔 초기화면부터 다시 준비한다(prepare_console_project 재사용).
        # rows_per_page_applied도 함께 되돌린다 — 메뉴를 새로 열면 페이지 크기가 기본값으로
        # 돌아가므로, 재시도 첫 검색은 첫 번째 검색처럼 다시 100개씩 보기를 선택해야 한다.
        receipt_session["initialized"] = False
        receipt_session["rows_per_page_applied"] = False
        prepare_console_project(
            page=page,
            explicit_project_base="",
            start_url=start_url,
            project_name=project_name,
        )

    for uuid_value in new_uuids:
        try:
            summary = retry_with_recovery(
                action=lambda uuid_value=uuid_value: summarize_recent_payments(
                    page,
                    uuid_value,
                    start_url,
                    project_name,
                    timeout_error,
                    receipt_session=receipt_session,
                    dc_mode=dc_mode,
                ),
                recovery=_recover_receipt_session,
                label=f"영수증검증 [{uuid_value}] 조회 재시도",
                recovery_desc=f"콘솔 초기화면({start_url})으로 재접속 후 재시도합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            if dc_mode:
                print(f"    [{uuid_value}] 구매 제품 {summary['recent_purchase_count']}건")
            else:
                print(f"    [{uuid_value}] {summary['recent_payment_count']}건 합계 {summary['recent_payment_sum']:,}")
        except Exception as exc:  # noqa: BLE001 — 재시도 소진 후에도 한 명 실패가 전체를 막지 않게
            print(f"    [{uuid_value}] 영수증검증 실패(재시도 소진) — 다음 UUID로 넘어갑니다: {exc}")
            summary = {"recent_purchase_count": ""} if dc_mode else {"recent_payment_count": "", "recent_payment_sum": ""}

        # 이 uuid에 해당하는 모든 행(여러 보드에 중복 등장 가능)에 즉시 반영하고,
        # 다음 uuid로 넘어가기 전에 체크포인트 저장 — 중간에 죽어도 이 uuid까지는 파일에 남는다.
        for r in all_rows:
            if r["uuid"] == uuid_value:
                r.update(summary)
        if checkpoint:
            checkpoint(all_rows)


def enrich_new_users_with_webshop_history(page, all_rows, start_url, project_name, checkpoint=None):
    """checkpoint(all_rows)가 주어지면 UUID 1명 처리할 때마다 호출해 즉시 파일에 반영한다."""
    new_uuids = []
    seen = set()
    for row in all_rows:
        if row.get("account_type") == "계정 신규 생성" and row["uuid"] not in seen:
            seen.add(row["uuid"])
            new_uuids.append(row["uuid"])

    if not new_uuids:
        print("\n[11] 신규 유저가 없어 지급 내역 합산을 생략합니다.")
        return

    print(f"\n[11] 신규 유저 {len(new_uuids)}명 지급 내역에서 payitem_* 합산을 조회합니다.")
    webshop_session = {
        "initialized": False,
        "rows_per_page_applied": False,
    }

    def _recover_webshop_session():
        # 세션이 초기화된 상태에서 UUID 하나가 실패해도, 다음 시도가 여전히 초기화된
        # 것으로 착각해 fill_webshop_uuid_search를 바로 재시도하지 않도록 초기화 플래그를
        # 되돌리고 콘솔 초기화면부터 다시 준비한다(prepare_console_project 재사용).
        # rows_per_page_applied도 함께 되돌린다 — 메뉴를 새로 열면 페이지 크기가 기본값으로
        # 돌아가므로, 재시도 첫 검색은 첫 번째 검색처럼 다시 100개씩 보기를 선택해야 한다.
        webshop_session["initialized"] = False
        webshop_session["rows_per_page_applied"] = False
        prepare_console_project(
            page=page,
            explicit_project_base="",
            start_url=start_url,
            project_name=project_name,
        )

    for uuid_value in new_uuids:
        try:
            summary = retry_with_recovery(
                action=lambda uuid_value=uuid_value: summarize_payitem_history_lookup(
                    page,
                    uuid_value,
                    start_url=start_url,
                    project_name=project_name,
                    session=webshop_session,
                ),
                recovery=_recover_webshop_session,
                label=f"지급내역 [{uuid_value}] 조회 재시도",
                recovery_desc=f"콘솔 초기화면({start_url})으로 재접속 후 재시도합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            print(
                f"    [{uuid_value}] "
                f"match={summary['payitem_match_count']} "
                f"qty={summary['payitem_quantity_total']} "
                f"sum={summary['payitem_item_value_sum']:,}"
            )
        except Exception as exc:  # noqa: BLE001 — 재시도 소진 후에도 한 명 실패가 전체를 막지 않게
            print(f"    [{uuid_value}] 지급 내역 조회 실패(재시도 소진) — 다음 UUID로 넘어갑니다: {exc}")
            summary = {
                "payitem_match_count": "",
                "payitem_quantity_total": "",
                "payitem_item_value_sum": "",
            }

        for row in all_rows:
            if row["uuid"] == uuid_value:
                row.update(summary)
        if checkpoint:
            checkpoint(all_rows)


def compute_total_payment_sum(all_rows):
    """recent_payment_sum(영수증검증) + payitem_item_value_sum(지급내역) 합산 → 총 결제액 파악.

    둘 다 정상 조회(정수)된 행에만 total_payment_sum을 채운다.
    조회 실패로 빈 문자열이 섞인 경우는 합산하지 않고 비워 둔다(잘못된 합계 노출 방지).
    """
    for row in all_rows:
        recent = row.get("recent_payment_sum")
        payitem = row.get("payitem_item_value_sum")
        if isinstance(recent, int) and isinstance(payitem, int):
            row["total_payment_sum"] = recent + payitem


def select_block_candidates(all_rows, max_total_payment: int, dc_mode: bool = False, max_purchase_count: int = 0) -> list:
    """차단 대상 신규 유저 선정. UUID 기준 중복 제거.

    공통 조건: account_type == '계정 신규 생성' (기존 유저는 제외).

    dc_mode=False(기본, 가격 기반):
    - total_payment_sum 이 정수로 존재 — compute_total_payment_sum은 영수증검증·지급내역이
      둘 다 정상 조회(정수)됐을 때만 이 값을 채운다. 하나라도 조회 실패면 키가 없으므로
      여기서 자연히 제외된다("조회했는데 0원"(total==0, 차단 대상)과 "조회 실패"(키 없음, 미진행)를 정확히 분리).
    - total_payment_sum <= max_total_payment (임계값 이하).

    dc_mode=True(--dc, 구매 건수 기반 — 가격 등 조회 불가 정책):
    - recent_purchase_count 가 정수로 존재(영수증검증 조회 실패 시 빈 문자열 → 자연히 제외).
    - recent_purchase_count <= max_purchase_count (임계값 이하).
    """
    candidates = []
    seen = set()
    for row in all_rows:
        if row.get("account_type") != "계정 신규 생성":
            continue
        uuid_value = row["uuid"]
        if uuid_value in seen:
            continue

        if dc_mode:
            count = row.get("recent_purchase_count")
            if not isinstance(count, int):
                continue  # 영수증검증 조회 실패 → 차단 미진행
            if count > max_purchase_count:
                continue
            seen.add(uuid_value)
            candidates.append(
                {
                    "uuid": uuid_value,
                    "nickname": row.get("nickname", ""),
                    "recent_purchase_count": count,
                }
            )
            continue

        total = row.get("total_payment_sum")
        if not isinstance(total, int):
            continue  # 영수증검증/지급내역 중 하나라도 조회 실패 → 차단 미진행
        if total > max_total_payment:
            continue
        seen.add(uuid_value)
        candidates.append(
            {
                "uuid": uuid_value,
                "nickname": row.get("nickname", ""),
                "total_payment_sum": total,
            }
        )
    return candidates


def _annotate_block_status(all_rows, status_by_uuid: dict):
    for row in all_rows:
        if row["uuid"] in status_by_uuid:
            row["block_status"] = status_by_uuid[row["uuid"]]


def _format_block_metric(candidate: dict, dc_mode: bool) -> str:
    if dc_mode:
        return f"최근 구매 {candidate['recent_purchase_count']}건"
    return f"총결제 {candidate['total_payment_sum']:,}원"


def block_low_payment_new_users(
    page, all_rows, start_url, project_name, args, project_key,
    dc_mode: bool = False, max_total_payment: int = 0, max_purchase_count: int = 0,
    checkpoint=None,
) -> dict:
    """신규 유저 중 차단 임계값 이하 대상을 user_block(+디바이스밴)한다.

    dc_mode=False(기본): 총 결제액(max_total_payment) 기준(가격 기반).
    dc_mode=True(--dc): 영수증검증 최근 구매 건수(max_purchase_count) 기준(가격 등 조회 불가 정책).

    운영 실행이므로 기본은 드라이런(후보만 출력)이고, --block-new-users 지정 시에만 실제 차단한다.
    각 UUID는 독립 재시도 예산을 갖고, 하나가 실패해도 다음 대상으로 계속 진행한다(배치 격리).
    user_block/디바이스밴은 '이미 등록됨'을 화면 문구로 정확히 판정하므로 멱등적이라 재시도 가능하다.

    checkpoint(all_rows)가 주어지면 대상 1명 처리할 때마다 호출해 block_status를
    즉시 파일에 반영한다(전체 대상이 끝나야만 CSV의 block_status가 채워지는 문제 방지).
    """
    candidates = select_block_candidates(all_rows, max_total_payment, dc_mode=dc_mode, max_purchase_count=max_purchase_count)

    if dc_mode:
        print(
            f"\n[12] 신규 유저 중 최근 구매 건수 {max_purchase_count}건 이하 차단 대상 선정 "
            "(기존 유저·조회 실패 제외)..."
        )
    else:
        print(
            f"\n[12] 신규 유저 중 총 결제액 {max_total_payment:,}원 이하 차단 대상 선정 "
            "(기존 유저·조회 실패 제외)..."
        )
    if not candidates:
        print("    차단 대상 신규 유저가 없습니다.")
        return {"executed": bool(args.block_new_users), "candidates": [], "results": []}

    print(f"    차단 대상 {len(candidates)}명:")
    for c in candidates:
        print(f"      - {c['uuid']} ({c['nickname']}) {_format_block_metric(c, dc_mode)}")

    status_by_uuid = {}

    if not args.block_new_users:
        print(
            "    [드라이런] --block-new-users 미지정 — 실제 차단은 진행하지 않습니다. "
            "후보 목록만 출력하고 CSV에는 '차단대상(미실행)'으로 표기합니다."
        )
        for c in candidates:
            status_by_uuid[c["uuid"]] = "차단대상(미실행)"
        _annotate_block_status(all_rows, status_by_uuid)
        if checkpoint:
            checkpoint(all_rows)
        return {"executed": False, "candidates": candidates, "results": []}

    print(
        f"    [실행] --block-new-users 지정 — {len(candidates)}명 실제 차단"
        f"(디바이스밴 {'생략' if args.skip_device_block else '포함'})을 시작합니다."
    )
    results = []
    previous_uuid_ok = False
    for idx, c in enumerate(candidates, start=1):
        uuid_value = c["uuid"]
        print(f"\n{'=' * 60}")
        print(f" [{idx}/{len(candidates)}] 차단: {uuid_value} ({_format_block_metric(c, dc_mode)})")
        print("=" * 60)

        attempt_counter = {"count": 0}

        def _run(uuid_value=uuid_value):
            attempt_counter["count"] += 1
            if attempt_counter["count"] > 1:
                print(
                    f"[재시도 {attempt_counter['count']}/{RETRY_MAX_RETRIES}] "
                    f"uuid={uuid_value} 차단 절차를 처음부터 다시 시작합니다."
                )
            # 이 uuid의 첫 시도이고 직전 uuid가 성공해 화면이 '접근 차단' 탭에 남아
            # 있다고 신뢰할 수 있을 때만 초기화면 재진입을 건너뛴다(console_user_block.py의
            # 배치 루프와 동일한 절약 — 재시도는 항상 전체 절차를 다시 밟음).
            skip_navigation = (
                attempt_counter["count"] == 1
                and idx > 1
                and previous_uuid_ok
            )
            return run_user_block(
                page=page,
                uuid_value=uuid_value,
                reason_text=args.reason,
                period_days=args.period_days,
                remove_rank=not args.skip_rank_delete,
                explicit_project_base="",
                start_url=start_url,
                project_name=project_name,
                project_key=project_key,
                skip_device_block=args.skip_device_block,
                device_ban_count=args.device_ban_count,
                skip_navigation=skip_navigation,
            )

        def _recover():
            prepare_console_project(
                page=page,
                explicit_project_base="",
                start_url=start_url,
                project_name=project_name,
            )

        try:
            summary = retry_with_recovery(
                action=_run,
                recovery=_recover,
                label=f"uuid={uuid_value} 차단 절차 재시도",
                recovery_desc=f"콘솔 초기화면({start_url})/프로젝트 선택부터 다시 준비합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            print(
                f"    [{uuid_value}] 차단 완료: status={summary['status']}, "
                f"device={summary['device_block_count']}"
            )
            status_by_uuid[uuid_value] = f"차단:{summary['status']}(device={summary['device_block_count']})"
            results.append({"uuid": uuid_value, "succeeded": True, "summary": summary})
            previous_uuid_ok = True
        except Exception as exc:  # noqa: BLE001 — 한 명 실패가 전체 배치를 막지 않게
            print(f"    [{uuid_value}] 차단 실패(재시도 소진) — 다음 대상으로 넘어갑니다: {exc}")
            status_by_uuid[uuid_value] = f"차단실패({exc})"
            results.append({"uuid": uuid_value, "succeeded": False, "error": str(exc)})
            previous_uuid_ok = False

        # 대상 1명 처리 직후 즉시 반영 — 다음 대상에서 재시도가 소진돼도 이 대상까지는 파일에 남는다.
        _annotate_block_status(all_rows, {uuid_value: status_by_uuid[uuid_value]})
        if checkpoint:
            checkpoint(all_rows)

    success_count = sum(1 for r in results if r["succeeded"])
    fail_count = len(results) - success_count
    print(f"\n[차단 요약] 성공 {success_count}건 / 실패(스킵) {fail_count}건")
    for r in results:
        mark = "성공" if r["succeeded"] else "실패(스킵)"
        line = f"    [{mark}] {r['uuid']}"
        if not r["succeeded"]:
            line += f" — {r.get('error', '')}"
        print(line)

    return {"executed": True, "candidates": candidates, "results": results}


def extract_top_ranks(page, board_name: str) -> list:
    print(f"[9] '{board_name}' 상세에서 {MAX_RANK}위 이내(공동순위 전원) 데이터를 읽습니다.")
    grid_state, _ = wait_for_leaderboard_rank_rows(page)
    if grid_state == "empty":
        print(f"    '{board_name}': '{LEADERBOARD_EMPTY_TEXT}' 확인 — 빈 리더보드로 처리하고 다음으로 넘어갑니다.")
        return []

    # 공동순위(동점)가 있으면 같은 순위에 여러 명이 있으므로, 순위가 아니라
    # uuid 를 키로 중복 제거한다. rank <= MAX_RANK 인 행은 인원 수와 무관하게 모두 수집.
    results_by_uuid = {}
    idle_rounds = 0
    saw_beyond_max = False

    while True:
        rows = get_leaderboard_rank_rows(page)
        count = rows.count()
        added_this_round = 0

        for index in range(count):
            row = rows.nth(index)

            rank = _extract_rank_from_row(row)
            if rank is None or rank < 1:
                continue
            if rank > MAX_RANK:
                saw_beyond_max = True  # 30위 초과가 보임 = 30위 이내는 모두 로드됨
                continue

            uuid_value = _read_row_field_text(row, ["uuid", "gamerId"])
            nickname = _read_row_field_text(row, ["nickname"])

            if not uuid_value or not nickname:
                continue
            if uuid_value in results_by_uuid:
                continue

            results_by_uuid[uuid_value] = {
                "leaderboard": board_name,
                "rank": rank,
                "uuid": uuid_value,
                "nickname": nickname,
            }
            added_this_round += 1

        if added_this_round == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        # 30위 초과 행을 본 순간 30위 이내는 전부 로드된 것 → 더 스크롤하지 않음
        if saw_beyond_max:
            break

        before_state = _get_leaderboard_grid_scroll_state(page)
        if before_state is None:
            break

        reached_bottom = (
            before_state["scrollTop"] + before_state["clientHeight"]
            >= before_state["scrollHeight"] - 4
        )
        if reached_bottom and idle_rounds > 0:
            break

        if not _scroll_leaderboard_grid_once(page):
            break

        after_state = _get_leaderboard_grid_scroll_state(page)
        if (
            after_state is not None
            and before_state["scrollTop"] == after_state["scrollTop"]
        ):
            idle_rounds += 1

        # 스크롤도 멈추고 새 행도 없으면 종료(무한 루프 방지)
        if idle_rounds >= 2:
            break

    # 같은 순위(공동순위)는 닉네임으로 보조 정렬해 출력 순서를 안정화
    results = list(results_by_uuid.values())
    results.sort(key=lambda row: (row["rank"], row["nickname"]))
    ranks_seen = {row["rank"] for row in results}
    print(f"    {len(results)}명 추출 완료 ({MAX_RANK}위 이내, 순위 {len(ranks_seen)}종 / 공동순위 포함).")
    return results


def _column_width_for(header: str, values) -> int:
    """헤더 라벨과 실제 값 중 가장 넓은 표시폭 — 잘림 없이 불필요한 여백만 줄인다."""
    return max([display_width(header)] + [display_width(v) for v in values])


def _format_board_row(rank, uuid_value, nickname, account_type, create_date, nickname_width, create_date_width) -> str:
    return "  ".join(
        [
            pad_display(rank, RANK_COL_WIDTH, align="right"),
            pad_display(uuid_value, UUID_COL_WIDTH),
            pad_display(nickname, nickname_width),
            pad_display(account_type, ACCOUNT_TYPE_COL_WIDTH),
            pad_display(create_date, create_date_width),
        ]
    )


def run(
    page,
    explicit_project_base,
    start_url,
    project_name,
    keywords,
    credentials=None,
    gcp_project="",
    gcp_log="",
    bq_credentials=None,
    bq_project="",
    bq_dataset="",
    bq_table="",
    bq_user_col="",
    bq_date_col="",
    timeout_error=Exception,
    single_board_test=False,
    dc_mode=False,
    checkpoint=None,
) -> tuple:
    """checkpoint(all_rows)가 주어지면 보드 1개를 다 읽을 때마다, 그리고 신규 유저
    보강(영수증검증/지급내역) 1명을 처리할 때마다 호출해 그 시점까지의 결과를 파일에
    즉시 반영한다 — 전체 키워드/보드가 끝나야만 CSV가 생기는 문제를 막기 위함."""
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )

    now_utc = datetime.datetime.utcnow()

    all_rows = []
    skipped_boards = []
    found_any_board = False
    is_first_open = True

    # 키워드마다: 검색 -> 검색된 리더보드 전부 조회 -> 다음 키워드로 넘어감.
    # 목록 진입+검색+결과 읽기 재시도가 소진돼도 이 키워드만 스킵하고 다음 키워드로
    # 넘어간다(원칙 5: 배치 중 하나의 실패가 전체를 막지 않게) — 기존에는 보드 단위
    # 실패만 이렇게 격리됐고, 키워드 단위 실패는 스크립트 전체를 중단시켰다(2026-07-03 수정).
    for keyword in keywords:
        print(f"\n=== 검색어 '{keyword}' ===")
        nav_context = "initial" if is_first_open else "return"
        if not is_first_open:
            print(f"[7-retry] 다음 검색어('{keyword}')를 위해 목록 화면을 다시 엽니다.")
        is_first_open = False

        try:
            board_names = open_leaderboard_list_and_collect_with_retry(
                page, keyword, start_url, project_name, nav_context=nav_context,
            )
        except Exception as exc:  # noqa: BLE001 — 키워드 하나의 최종 실패가 나머지 키워드를 막지 않게
            print(f"    [스킵] 검색어 '{keyword}' 리더보드 목록 조회 최종 실패 — 다음 검색어로 넘어갑니다: {exc}")
            skipped_boards.append({"keyword": keyword, "leaderboard": "(목록조회)", "error": str(exc)})
            continue

        if not board_names:
            print(f"    검색어 '{keyword}'로 리더보드를 찾지 못했습니다 — 건너뜁니다.")
            continue

        if single_board_test:
            print(f"    [테스트 모드] 리더보드 1개만 조회하고 다음 단계로 넘어갑니다: {board_names[0]}")
            board_names = board_names[:1]

        for board_name in board_names:
            def _process_board(board_name=board_name):
                # 매 시도(첫 시도 포함) 항상 이 키워드의 목록 화면부터 다시 연다.
                # 복구(초기화면 재접속) 뒤에는 항상 프로젝트 홈에 있으므로 "목록 화면이
                # 이미 열려 있다"고 가정하지 않는다 — 절차 전체를 하나의 단위로 재시도한다.
                # nav_context="board_loop": 이 호출 직전 화면은 항상 "직전 보드의 상세 페이지"이고,
                # 키워드 전환용 "return"과 화면이 다를 수 있어 지문 이름을 분리한다(둘 다
                # _leaderboard_nav_step_name()에서 실제 화면 종류로 세분화됨).
                open_leaderboard_list_and_search(page, keyword, nav_context="board_loop")
                enter_leaderboard_detail(page, board_name)

                # 순위가 아예 없는 리더보드는 페이지네이션(표시 개수) 드롭다운 자체가 렌더되지
                # 않아, 여기서 먼저 빈 상태("현재 순위가 없습니다")를 확인하지 않으면
                # set_rows_per_page()가 "드롭다운을 열지 못했습니다"로 실패해 로딩 실패로
                # 오판 -> 불필요한 전체 재시도로 이어진다(실측). extract_top_ranks() 진입 전에
                # 먼저 판정해서 빈 보드는 표시 개수 변경 자체를 건너뛴다.
                grid_state, _ = wait_for_leaderboard_rank_rows(page)
                if grid_state == "empty":
                    print(f"    '{board_name}': '{LEADERBOARD_EMPTY_TEXT}' 확인 — 표시 개수 변경 없이 빈 결과로 처리합니다.")
                    board_rows = []
                else:
                    set_rows_per_page(
                        page,
                        DETAIL_ROWS_PER_PAGE,
                        "리더보드 상세 표시 개수",
                        verify_prefix="detail_rows",
                        ignore_patterns=LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS,
                    )
                    board_rows = extract_top_ranks(page, board_name)
                # 각 보드 추출 직후 계정 생성일 보강 (Cloud Logging 또는 BigQuery, 프로젝트별 설정에 따름)
                if bq_credentials is not None:
                    enrich_board_with_bigquery(
                        board_rows, bq_credentials, bq_project, bq_dataset, bq_table,
                        bq_user_col, bq_date_col, now_utc,
                    )
                else:
                    enrich_board_with_gcp(board_rows, credentials, gcp_project, gcp_log, now_utc)
                return board_rows

            try:
                board_rows = retry_with_recovery(
                    action=_process_board,
                    recovery=lambda: prepare_console_project(
                        page=page,
                        explicit_project_base="",
                        start_url=start_url,
                        project_name=project_name,
                    ),
                    label=f"'{board_name}' 조회 재시도",
                    recovery_desc=f"콘솔 초기화면({start_url})으로 재접속 후 재시도합니다.",
                    max_retries=RETRY_MAX_RETRIES,
                )
            except Exception as exc:  # noqa: BLE001 — 개별 보드 최종 실패가 전체 실행을 막지 않게
                print(f"    [스킵] '{board_name}' 조회 최종 실패 — 다음 리더보드로 넘어갑니다: {exc}")
                skipped_boards.append({"keyword": keyword, "leaderboard": board_name, "error": str(exc)})
                continue

            # 보드별 통합 출력 — 닉네임/계정생성일은 실제 값 기준 최소폭으로 계산해 여백 낭비를 줄인다.
            nickname_width = _column_width_for(NICKNAME_HEADER, [r["nickname"] for r in board_rows])
            create_date_width = _column_width_for(
                CREATE_DATE_HEADER, [r.get("create_account_date", "") for r in board_rows]
            )
            print(f"\n  == {board_name} ==")
            print(
                "  "
                + _format_board_row(
                    "순위",
                    "UUID",
                    NICKNAME_HEADER,
                    "계정상태",
                    CREATE_DATE_HEADER,
                    nickname_width,
                    create_date_width,
                )
            )
            print(
                "  "
                + "  ".join(
                    [
                        "-" * RANK_COL_WIDTH,
                        "-" * UUID_COL_WIDTH,
                        "-" * nickname_width,
                        "-" * ACCOUNT_TYPE_COL_WIDTH,
                        "-" * create_date_width,
                    ]
                )
            )
            for r in board_rows:
                print(
                    "  "
                    + _format_board_row(
                        f"{r['rank']}위",
                        r["uuid"],
                        r["nickname"],
                        r.get("account_type", ""),
                        r.get("create_account_date", ""),
                        nickname_width,
                        create_date_width,
                    )
                )
            all_rows.extend(board_rows)
            found_any_board = True
            if checkpoint:
                checkpoint(all_rows)

        if single_board_test:
            break

    if skipped_boards:
        print(f"\n[요약] 스킵된 리더보드 {len(skipped_boards)}건:")
        for skipped in skipped_boards:
            print(f"    - [{skipped['keyword']}] {skipped['leaderboard']}: {skipped['error']}")

    if not found_any_board:
        raise RuntimeError(f"다음 검색어로 리더보드를 찾지 못했습니다: {', '.join(keywords)}")

    step_and_verify_ui(page, "leaderboard_complete", ignore_patterns=LEADERBOARD_COMPLETE_IGNORE_PATTERNS)

    # 신규 유저(계정 240시간 이내)만 영수증검증으로 최근 결제액 합계 점검
    # dc_mode(--dc, 게임B)는 가격 등 조회 불가 정책이라 지급 내역(웹샵) 조회는 생략하고
    # 영수증검증도 구매 제품 건수만 확인한다.
    enrich_new_users_with_payments(page, all_rows, start_url, project_name, timeout_error, dc_mode=dc_mode, checkpoint=checkpoint)
    if not dc_mode:
        enrich_new_users_with_webshop_history(page, all_rows, start_url, project_name, checkpoint=checkpoint)
    compute_total_payment_sum(all_rows)
    if checkpoint:
        checkpoint(all_rows)

    printed_uuids = set()
    for row in all_rows:
        uuid_value = row["uuid"]
        if "total_payment_sum" not in row or uuid_value in printed_uuids:
            continue
        printed_uuids.add(uuid_value)
        print(
            f"    [{uuid_value}] 총 결제액(영수증검증+지급내역) = {row['total_payment_sum']:,}"
        )

    return all_rows, skipped_boards


def save_csv(rows: list, out_dir: Path, project_key: str, csv_path: Path = None) -> Path:
    """rows를 CSV로 저장한다. csv_path를 넘기면 그 파일을 덮어써 같은 파일을 계속
    갱신하는 체크포인트 저장으로 쓸 수 있다(중간에 스크립트가 죽어도 그 시점까지의
    데이터가 파일에 남는다). csv_path가 없으면(기존 동작) 새 타임스탬프로 1회 저장한다."""
    if csv_path is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = out_dir / f"leaderboard_{project_key}_{ts}.csv"
    base_fields = ["leaderboard", "rank", "uuid", "nickname"]
    gcp_fields = ["account_type", "create_account_date"]
    payment_fields = ["recent_payment_count", "recent_payment_sum"]
    purchase_count_fields = ["recent_purchase_count"]  # --dc: 가격 대신 구매 제품 건수만
    webshop_fields = ["payitem_match_count", "payitem_quantity_total", "payitem_item_value_sum"]
    total_fields = ["total_payment_sum"]
    block_fields = ["block_status"]
    has_gcp = any(row.get("account_type") for row in rows)
    has_payment = any("recent_payment_sum" in row for row in rows)
    has_purchase_count = any("recent_purchase_count" in row for row in rows)
    has_webshop = any("payitem_item_value_sum" in row for row in rows)
    has_total = any("total_payment_sum" in row for row in rows)
    has_block = any("block_status" in row for row in rows)
    fieldnames = (
        base_fields
        + (gcp_fields if has_gcp else [])
        + (payment_fields if has_payment else [])
        + (purchase_count_fields if has_purchase_count else [])
        + (webshop_fields if has_webshop else [])
        + (total_fields if has_total else [])
        + (block_fields if has_block else [])
    )
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 저장: {csv_path.name} ({len(rows)}행, GCP컬럼={'포함' if has_gcp else '없음'})")
    return csv_path


def save_artifacts(page, out_dir: Path, succeeded: bool, rows: list, error_message: str, skipped_boards: list = None):
    unique_boards = sorted({row["leaderboard"] for row in rows})
    lines = [
        f"succeeded={succeeded}",
        f"url={page.url}",
        f"row_count={len(rows)}",
        f"board_count={len(unique_boards)}",
        f"boards={', '.join(unique_boards[:20])}",
    ]
    if skipped_boards:
        lines.append(f"skipped_count={len(skipped_boards)}")
        for skipped in skipped_boards:
            lines.append(f"skipped=[{skipped['keyword']}] {skipped['leaderboard']}: {skipped['error']}")
    if error_message:
        lines.append(f"error={error_message}")

    save_page_artifacts(page, out_dir, "console_leaderboard", lines)


def _slack_block_status(uuid_value: str, executed: bool, results: list) -> str:
    if not executed:
        return "드라이런(미실행)"
    for r in results:
        if r["uuid"] == uuid_value:
            return "차단 완료" if r["succeeded"] else f"차단 실패: {r.get('error', '')}"
    return "결과 확인 안됨"


def build_slack_summary(
    project_key: str,
    succeeded: bool,
    all_rows: list,
    skipped_boards: list,
    block_outcome: dict,
    dc_mode: bool,
    error_message: str,
) -> str:
    """예약 실행(--unattended) 완료 후 슬랙에 보낼 요약 텍스트.

    차단 대상은 없어도 "없음"을 명시한다 — 침묵으로 "0명"을 표현하지 않는다.
    """
    lines = [f"*[리더보드 예약 실행] {project_key}* — {'성공' if succeeded else '실패'}"]

    if not succeeded:
        lines.append(f"오류: {error_message}")
        return "\n".join(lines)

    board_count = len(set(row["leaderboard"] for row in all_rows))
    lines.append(f"조회: 리더보드 {board_count}개, 총 {len(all_rows)}행")
    if skipped_boards:
        lines.append(f"스킵된 리더보드: {len(skipped_boards)}건")

    if block_outcome is None:
        lines.append("차단 결과: 확인 불가(차단 단계 도달 전 종료)")
        return "\n".join(lines)

    candidates = block_outcome.get("candidates") or []
    if not candidates:
        lines.append("차단 대상: 없음")
        return "\n".join(lines)

    executed = block_outcome.get("executed", False)
    results = block_outcome.get("results") or []
    lines.append(f"차단 대상 {len(candidates)}명 ({'실행' if executed else '드라이런(미실행)'}):")
    for c in candidates:
        status = _slack_block_status(c["uuid"], executed, results)
        lines.append(f"  - {c.get('nickname', '')} ({c['uuid']}) {_format_block_metric(c, dc_mode)} — {status}")

    return "\n".join(lines)


def main():
    configure_console_output()
    args = parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
        include_key_file=True,
        include_gcp=True,
        include_bigquery=True,
        default_block_reason=DEFAULT_BLOCK_REASON,
        include_block_reason=True,
        default_leaderboard_keywords=DEFAULT_SEARCH_KEYWORDS,
        include_leaderboard_keywords=True,
    )
    # --dc(게임B)는 가격 등 조회 불가 정책 — 지급 내역(웹샵) 조회 생략, 영수증검증은 구매 제품 건수만 확인.
    # --title dc 로 직접 지정해도 동일하게 적용한다(apply_title_profile은 --dc일 때만 title을 자동 채움).
    dc_mode = bool(args.dc) or args.title.strip().lower() == "dc"
    # 차단 기록 CSV 파일명 접미사 — console_user_block.main()과 동일 규칙(title 우선, 없으면 프로젝트명 정규화).
    project_key = args.title.strip() if args.title.strip() else re.sub(r"[^\w가-힣]", "_", args.project_name).strip("_")
    keyword_list = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keyword_list:
        raise SystemExit("[오류] --keywords 값이 비어 있습니다.")
    sync_playwright, _timeout_error = load_playwright()
    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    LEADERBOARD_OUT_DIR.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    use_bigquery = args.log_console.strip().lower() == "bigquery"

    if args.title:
        prefix = args.title.upper()
        required = [("KEY_FILE", args.key), ("PROJECT_NAME", args.project_name)]
        if use_bigquery:
            required += [
                ("BQ_PROJECT", args.bq_project),
                ("BQ_DATASET", args.bq_dataset),
                ("BQ_TABLE", args.bq_table),
                ("BQ_USER_COL", args.bq_user_col),
                ("BQ_DATE_COL", args.bq_date_col),
            ]
        else:
            required += [("GCP_PROJECT", args.gcp_project), ("LOGNAME", args.gcp_log)]
        missing = [f"{prefix}_{k}" for k, v in required if not v]
        if missing:
            raise SystemExit(f"[오류] --title {args.title}: 다음 env가 비어 있습니다 — {', '.join(missing)}")

    # 프로젝트별 운영 파라미터 override(2026-07-03 분리): {TITLE}_ACCOUNT_NEW_HOURS 등이 있으면
    # 전역 기본값 대신 그 값을 쓴다. args.title은 require_project_name=True라 항상 채워져 있다.
    global ACCOUNT_NEW_HOURS, ACCOUNT_LOOKUP_LOOKBACK_HOURS, BLOCK_MAX_TOTAL_PAYMENT, BLOCK_MAX_PURCHASE_COUNT, BLOCK_NEW_USERS_ENABLED
    ACCOUNT_NEW_HOURS = _resolve_title_int_env(prefix, "ACCOUNT_NEW_HOURS", ACCOUNT_NEW_HOURS)
    ACCOUNT_LOOKUP_LOOKBACK_HOURS = max(
        1, _resolve_title_int_env(prefix, "ACCOUNT_LOOKUP_LOOKBACK_HOURS", ACCOUNT_NEW_HOURS)
    )
    BLOCK_MAX_TOTAL_PAYMENT = max(
        0, _resolve_title_int_env(prefix, "USER_BLOCK_MAX_TOTAL_PAYMENT", BLOCK_MAX_TOTAL_PAYMENT)
    )
    BLOCK_MAX_PURCHASE_COUNT = max(
        0, _resolve_title_int_env(prefix, "USER_BLOCK_MAX_PURCHASE_COUNT", BLOCK_MAX_PURCHASE_COUNT)
    )
    BLOCK_NEW_USERS_ENABLED = _resolve_title_bool_env(
        prefix, "USER_BLOCK_NEW_USERS_ENABLED", BLOCK_NEW_USERS_ENABLED
    )
    # --block-new-users(CLI, 수동 실행용)와 {TITLE}_USER_BLOCK_NEW_USERS_ENABLED(env, 예약 실행용)는
    # 둘 중 하나만 켜도 실제 차단을 진행한다 — 예약 실행은 CLI 플래그를 넘기지 않으므로 env가
    # 유일한 활성화 경로다. 어느 쪽으로 켜졌는지는 아래 로그에 남긴다.
    block_new_users_via_cli = bool(args.block_new_users)
    args.block_new_users = block_new_users_via_cli or BLOCK_NEW_USERS_ENABLED

    credentials = None
    bq_credentials = None
    if use_bigquery:
        if args.key:
            bq_credentials = load_bigquery_credentials(args.key)
            if bq_credentials is None:
                print("[경고] BigQuery 서비스계정 로드 실패. 계정 생성일 조회 없이 진행합니다.")
    elif args.key:
        credentials = load_logging_credentials(args.key)
        if credentials is None:
            print("[경고] GCP 서비스계정 로드 실패. 계정 생성일 로그 조회 없이 진행합니다.")

    print("=" * 55)
    print(" Leaderboard PvPRank extractor")
    print("=" * 55)
    print(f"프로필   : {profile_dir.name}")
    print(f"검색어   : {', '.join(keyword_list)}")
    print(f"출력     : {out_dir.name}")
    print(f"CSV 저장 : {LEADERBOARD_OUT_DIR}")
    print(f"덤프     : {out_dir} (30일 초과 자동 삭제)")
    if use_bigquery:
        bq_ready = bq_credentials and args.bq_project and args.bq_dataset and args.bq_table
        bq_desc = f"{args.bq_project}.{args.bq_dataset}.{args.bq_table}"
        print(f"BQ 조회  : {'활성 (' + bq_desc + ')' if bq_ready else '비활성 (--key / --bq-* 미지정)'}")
    else:
        gcp_ready = credentials and args.gcp_project and args.gcp_log
        print(f"GCP 조회 : {'활성 (' + args.gcp_project + ' / ' + args.gcp_log + ')' if gcp_ready else '비활성 (--key / --gcp-project / --gcp-log 미지정)'}")
    if dc_mode:
        print("DC모드   : 활성 — 지급 내역(웹샵) 조회 생략, 영수증검증은 구매 제품 건수만 확인(가격 등 조회 불가)")
    if args.test_single_board:
        print("테스트   : --test-single-board 활성 — 리더보드 1개만 조회 후 다음 단계로 진행")
    block_threshold_desc = (
        f"최근 구매 {BLOCK_MAX_PURCHASE_COUNT}건 이하" if dc_mode else f"총결제 {BLOCK_MAX_TOTAL_PAYMENT:,}원 이하"
    )
    if args.block_new_users:
        block_source = "--block-new-users" if block_new_users_via_cli else f"env {prefix}_USER_BLOCK_NEW_USERS_ENABLED"
        print(
            f"차단     : 활성 (실제 차단, {block_source}) — 신규 유저 {block_threshold_desc}, "
            f"디바이스밴 {'생략' if args.skip_device_block else '포함'}, 사유='{args.reason}'"
        )
    else:
        print(
            f"차단     : 드라이런 (--block-new-users 미지정, env {prefix}_USER_BLOCK_NEW_USERS_ENABLED도 비활성) "
            f"— 후보만 출력, 임계값 {block_threshold_desc}"
        )

    succeeded = False
    all_rows = []
    skipped_boards = []
    error_message = ""
    block_had_failures = False
    block_outcome = None
    page = None

    # 보드 1개, 신규 유저 보강 1명, 차단 대상 1명을 처리할 때마다 이 같은 파일을 계속
    # 덮어써 갱신한다(체크포인트). 전체 실행이 끝나야만 CSV가 생기던 기존 방식과 달리,
    # 중간에 스크립트가 죽어도 그 시점까지 처리된 데이터는 파일에 남는다.
    csv_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = LEADERBOARD_OUT_DIR / f"leaderboard_{project_key}_{csv_ts}.csv"

    def checkpoint(rows):
        # 사람이 이 CSV를 엑셀 등으로 열어서 조회 중이면 Windows가 파일을 배타적으로 잠가
        # 덮어쓰기(open(..., "w"))가 PermissionError로 실패할 수 있다. 이 저장 단계는
        # 재시도 로직으로 감싸여 있지 않으므로, 여기서 못 잡으면 예외가 그대로 올라가
        # 체크포인트 저장 실패 하나가 전체 배치 진행을 끊어버린다(중간 저장의 취지와 반대).
        # 실패해도 경고만 남기고 계속 진행 — 다음 체크포인트(다음 보드/uuid 처리 후)에서
        # 파일이 닫혀 있으면 그때의 전체 데이터로 다시 저장을 시도한다.
        try:
            save_csv(rows, LEADERBOARD_OUT_DIR, project_key, csv_path=csv_path)
        except OSError as exc:
            print(
                f"    [경고] 체크포인트 CSV 저장 실패(파일이 다른 프로그램(엑셀 등)에서 "
                f"열려 있을 수 있음) — 파일을 닫으면 다음 체크포인트에서 다시 저장됩니다: {exc}"
            )

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
            all_rows, skipped_boards = run(
                page=page,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
                keywords=keyword_list,
                credentials=credentials,
                gcp_project=args.gcp_project,
                gcp_log=args.gcp_log,
                bq_credentials=bq_credentials,
                bq_project=args.bq_project,
                bq_dataset=args.bq_dataset,
                bq_table=args.bq_table,
                bq_user_col=args.bq_user_col,
                bq_date_col=args.bq_date_col,
                timeout_error=_timeout_error,
                single_board_test=args.test_single_board,
                dc_mode=dc_mode,
                checkpoint=checkpoint,
            )

            # 조회 완료 후: 신규 유저 중 차단 임계값(가격 또는 --dc 구매 건수) 이하 대상 차단(운영 실행) 또는 후보 출력(드라이런).
            block_outcome = block_low_payment_new_users(
                page=page,
                all_rows=all_rows,
                start_url=args.start_url,
                project_name=args.project_name,
                args=args,
                project_key=project_key,
                dc_mode=dc_mode,
                max_total_payment=BLOCK_MAX_TOTAL_PAYMENT,
                max_purchase_count=BLOCK_MAX_PURCHASE_COUNT,
                checkpoint=checkpoint,
            )
            block_had_failures = block_outcome["executed"] and any(
                not r["succeeded"] for r in block_outcome["results"]
            )

            checkpoint(all_rows)

            board_count = len(set(row["leaderboard"] for row in all_rows))
            print(f"\n=== 완료: {board_count}개 리더보드, 총 {len(all_rows)}행 ===")
            if skipped_boards:
                print(f"    (스킵된 리더보드 {len(skipped_boards)}건 — 상세는 위 [요약]/artifacts 참고)")
            succeeded = True

            if args.hold_seconds > 0:
                print(f"{args.hold_seconds}초 대기 후 종료합니다.")
                page.wait_for_timeout(args.hold_seconds * 1_000)
        except Exception as exc:
            error_message = str(exc)
            print(f"\n[오류] {exc}")
            if args.unattended:
                print("(--unattended) Enter 대기 없이 종료합니다.")
            else:
                print("브라우저를 열어둡니다. 확인 후 Enter 를 눌러 종료합니다.")
                input()
        finally:
            try:
                if page is not None:
                    page = select_target_page(context, page)
                    save_artifacts(
                        page=page,
                        out_dir=out_dir,
                        succeeded=succeeded,
                        rows=all_rows,
                        error_message=error_message,
                        skipped_boards=skipped_boards,
                    )
            finally:
                context.close()

    shutdown_gcp_executor()

    if args.unattended:
        webhook_url = get_slack_webhook_url()
        if webhook_url:
            send_slack_message(
                webhook_url,
                build_slack_summary(
                    project_key, succeeded, all_rows, skipped_boards, block_outcome, dc_mode, error_message,
                ),
            )
        else:
            print("    [안내] SLACK_WEBHOOK_URL 미설정 — 슬랙 알림 생략.")

    if not succeeded:
        sys.exit(1)
    if skipped_boards or block_had_failures:
        # 실행 자체는 완료됐지만 일부 리더보드 스킵 또는 일부 차단 실패가 있었다는 사실을
        # 종료 코드로 숨기지 않는다.
        sys.exit(2)


if __name__ == "__main__":
    main()
