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
- Save CSV and debug artifacts

By default this script is read-only. Only when --block-new-users is passed does it
perform a mutation: new accounts whose total payment sum is at or below the env-managed
threshold (USER_BLOCK_MAX_TOTAL_PAYMENT, default 200,000) are user-blocked (device ban
included). Without the flag it merely prints the block candidates (dry-run).
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
# 신규 유저 차단 임계값: 총 결제액(영수증검증+지급내역)이 이 값 "이하"면 차단 대상. env로 관리.
BLOCK_MAX_TOTAL_PAYMENT = max(0, int(os.environ.get("USER_BLOCK_MAX_TOTAL_PAYMENT", "200000")))
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
    if nav_context == "board_loop":
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
            "env USER_BLOCK_MAX_TOTAL_PAYMENT) 이하인 대상을 실제로 user_block(디바이스밴 포함)합니다. "
            "지정하지 않으면 후보 목록만 출력하는 드라이런으로 동작합니다(운영 실행 승인 게이트)."
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
    board_name_re = re.compile(rf"{re.escape(keyword)}_[A-Za-z0-9_]+")
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


def open_leaderboard_list_and_search_with_retry(page, keyword: str, start_url: str, project_name: str, nav_context: str = "initial"):
    """목록 진입+검색 실패 시 콘솔 초기화면(start_url)으로 재접속해 재시도한다.

    세션 자동 로그아웃뿐 아니라, 이전 동작에서 남은 MUI 메뉴/드롭다운의 보이지 않는
    backdrop이 클릭을 가로막아 사이드바 진입 자체가 반복 실패하는 경우도 있다
    (실측: is_login_page는 False인데도 클릭이 계속 막힘). 로그인 여부만 확인해서는
    이 상태를 벗어나지 못하므로, 기존에 쓰던 start_url로 재접속해 화면을 완전히
    리셋하고 프로젝트를 메뉴로 다시 선택한다(prepare_console_project 재사용).
    """
    retry_with_recovery(
        action=lambda: open_leaderboard_list_and_search(page, keyword, nav_context=nav_context),
        recovery=lambda: prepare_console_project(
            page=page,
            explicit_project_base="",
            start_url=start_url,
            project_name=project_name,
        ),
        label="목록 진입 재시도",
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


def wait_for_leaderboard_rank_rows(page, timeout_ms: int = 15_000):
    rows = get_leaderboard_rank_rows(page)
    rows.first.wait_for(state="visible", timeout=timeout_ms)
    return rows


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
    record_step_dump(page, "leaderboard_scroll_pre")
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

    Cloud Logging 방식(query_account_creation_info)은 "최근 활동 없음=기존 유저"로 판단하지만,
    이 테이블은 유저당 레코드가 있는 마스터성 테이블이라 그 근거를 쓸 수 없다. 해당 uuid 행
    자체가 없으면 데이터 미반영/불일치일 수 있으므로 "기존 유저"로 단정하지 않고 별도 상태
    ("계정정보없음")로 남겨 신규 차단 후보 판정에서 조용히 누락되지 않게 한다.
    """
    create_dt, err = _fetch_account_creation_date_with_retry(
        bq_client, bq_project, dataset, table, user_col, date_col, uuid
    )
    if err:
        return {"account_type": f"조회실패({err})", "create_account_date": ""}
    if create_dt is None:
        return {"account_type": "계정정보없음", "create_account_date": ""}

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
                              limit=RECENT_PAYMENT_LIMIT, receipt_session=None):
    """콘솔 영수증검증 메뉴 조회 → 최근 limit건 결제액 합계. 읽기 전용.

    영수증검증은 이미 100개씩 보기로 최신순 표시되므로 rows 앞에서 limit건을 합산한다.
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
    total = sum(_parse_price_to_int(r.get("금액", "")) for r in recent)
    return {
        "recent_payment_count": len(recent),
        "recent_payment_sum": total,
    }


def enrich_new_users_with_payments(page, all_rows, start_url, project_name, timeout_error):
    """all_rows 중 '계정 신규 생성' 유저에 영수증검증 최근 결제 합계를 in-place 추가.

    같은 유저가 여러 보드에 중복될 수 있어 uuid 로 한 번만 조회하고 해당 행 전부에 반영.
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

    print(f"\n[10] 신규 유저 {len(new_uuids)}명 영수증검증 최근 {RECENT_PAYMENT_LIMIT}건 결제액 합계 조회...")
    summary_by_uuid = {}
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
                ),
                recovery=_recover_receipt_session,
                label=f"영수증검증 [{uuid_value}] 조회 재시도",
                recovery_desc=f"콘솔 초기화면({start_url})으로 재접속 후 재시도합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            print(f"    [{uuid_value}] {summary['recent_payment_count']}건 합계 {summary['recent_payment_sum']:,}")
        except Exception as exc:  # noqa: BLE001 — 재시도 소진 후에도 한 명 실패가 전체를 막지 않게
            print(f"    [{uuid_value}] 영수증검증 실패(재시도 소진) — 다음 UUID로 넘어갑니다: {exc}")
            summary = {"recent_payment_count": "", "recent_payment_sum": ""}
        summary_by_uuid[uuid_value] = summary

    for r in all_rows:
        if r["uuid"] in summary_by_uuid:
            r.update(summary_by_uuid[r["uuid"]])


def enrich_new_users_with_webshop_history(page, all_rows, start_url, project_name):
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
    summary_by_uuid = {}
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
        summary_by_uuid[uuid_value] = summary

    for row in all_rows:
        if row["uuid"] in summary_by_uuid:
            row.update(summary_by_uuid[row["uuid"]])


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


def select_block_candidates(all_rows, max_total_payment: int) -> list:
    """차단 대상 신규 유저 선정. UUID 기준 중복 제거.

    선정 조건:
    - account_type == '계정 신규 생성' (기존 유저는 제외).
    - total_payment_sum 이 정수로 존재 — compute_total_payment_sum은 영수증검증·지급내역이
      둘 다 정상 조회(정수)됐을 때만 이 값을 채운다. 하나라도 조회 실패면 키가 없으므로
      여기서 자연히 제외된다("조회했는데 0원"(total==0, 차단 대상)과 "조회 실패"(키 없음, 미진행)를 정확히 분리).
    - total_payment_sum <= max_total_payment (임계값 이하).
    """
    candidates = []
    seen = set()
    for row in all_rows:
        if row.get("account_type") != "계정 신규 생성":
            continue
        uuid_value = row["uuid"]
        if uuid_value in seen:
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


def block_low_payment_new_users(page, all_rows, start_url, project_name, args, project_key) -> dict:
    """신규 유저 중 총 결제액 임계값 이하 대상을 user_block(+디바이스밴)한다.

    운영 실행이므로 기본은 드라이런(후보만 출력)이고, --block-new-users 지정 시에만 실제 차단한다.
    각 UUID는 독립 재시도 예산을 갖고, 하나가 실패해도 다음 대상으로 계속 진행한다(배치 격리).
    user_block/디바이스밴은 '이미 등록됨'을 화면 문구로 정확히 판정하므로 멱등적이라 재시도 가능하다.
    """
    candidates = select_block_candidates(all_rows, BLOCK_MAX_TOTAL_PAYMENT)

    print(
        f"\n[12] 신규 유저 중 총 결제액 {BLOCK_MAX_TOTAL_PAYMENT:,}원 이하 차단 대상 선정 "
        "(기존 유저·조회 실패 제외)..."
    )
    if not candidates:
        print("    차단 대상 신규 유저가 없습니다.")
        return {"executed": bool(args.block_new_users), "candidates": [], "results": []}

    print(f"    차단 대상 {len(candidates)}명:")
    for c in candidates:
        print(f"      - {c['uuid']} ({c['nickname']}) 총결제 {c['total_payment_sum']:,}원")

    status_by_uuid = {}

    if not args.block_new_users:
        print(
            "    [드라이런] --block-new-users 미지정 — 실제 차단은 진행하지 않습니다. "
            "후보 목록만 출력하고 CSV에는 '차단대상(미실행)'으로 표기합니다."
        )
        for c in candidates:
            status_by_uuid[c["uuid"]] = "차단대상(미실행)"
        _annotate_block_status(all_rows, status_by_uuid)
        return {"executed": False, "candidates": candidates, "results": []}

    print(
        f"    [실행] --block-new-users 지정 — {len(candidates)}명 실제 차단"
        f"(디바이스밴 {'생략' if args.skip_device_block else '포함'})을 시작합니다."
    )
    results = []
    for idx, c in enumerate(candidates, start=1):
        uuid_value = c["uuid"]
        print(f"\n{'=' * 60}")
        print(f" [{idx}/{len(candidates)}] 차단: {uuid_value} (총결제 {c['total_payment_sum']:,}원)")
        print("=" * 60)

        attempt_counter = {"count": 0}

        def _run(uuid_value=uuid_value):
            attempt_counter["count"] += 1
            if attempt_counter["count"] > 1:
                print(
                    f"[재시도 {attempt_counter['count']}/{RETRY_MAX_RETRIES}] "
                    f"uuid={uuid_value} 차단 절차를 처음부터 다시 시작합니다."
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
        except Exception as exc:  # noqa: BLE001 — 한 명 실패가 전체 배치를 막지 않게
            print(f"    [{uuid_value}] 차단 실패(재시도 소진) — 다음 대상으로 넘어갑니다: {exc}")
            status_by_uuid[uuid_value] = f"차단실패({exc})"
            results.append({"uuid": uuid_value, "succeeded": False, "error": str(exc)})

    _annotate_block_status(all_rows, status_by_uuid)

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
    wait_for_leaderboard_rank_rows(page)

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
) -> tuple:
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
    for keyword in keywords:
        print(f"\n=== 검색어 '{keyword}' ===")
        if is_first_open:
            open_leaderboard_list_and_search_with_retry(page, keyword, start_url, project_name, nav_context="initial")
            is_first_open = False
        else:
            print(f"[7-retry] 다음 검색어('{keyword}')를 위해 목록 화면을 다시 엽니다.")
            open_leaderboard_list_and_search_with_retry(page, keyword, start_url, project_name, nav_context="return")

        board_names = collect_visible_board_names(page, keyword)
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
                # 키워드 전환용 "return"(직전 화면=목록 페이지)과는 실제 화면이 달라 지문 이름을 분리한다.
                open_leaderboard_list_and_search(page, keyword, nav_context="board_loop")
                enter_leaderboard_detail(page, board_name)
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

        if single_board_test:
            break

    if skipped_boards:
        print(f"\n[요약] 스킵된 리더보드 {len(skipped_boards)}건:")
        for skipped in skipped_boards:
            print(f"    - [{skipped['keyword']}] {skipped['leaderboard']}: {skipped['error']}")

    if not found_any_board:
        raise RuntimeError(f"다음 검색어로 리더보드를 찾지 못했습니다: {', '.join(keywords)}")

    step_and_verify_ui(page, "leaderboard_complete", ignore_patterns=LEADERBOARD_REWARD_MAIL_IGNORE_PATTERNS)

    # 신규 유저(계정 240시간 이내)만 영수증검증으로 최근 결제액 합계 점검
    enrich_new_users_with_payments(page, all_rows, start_url, project_name, timeout_error)
    enrich_new_users_with_webshop_history(page, all_rows, start_url, project_name)
    compute_total_payment_sum(all_rows)

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


def save_csv(rows: list, out_dir: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"leaderboard_pvprank_{ts}.csv"
    base_fields = ["leaderboard", "rank", "uuid", "nickname"]
    gcp_fields = ["account_type", "create_account_date"]
    payment_fields = ["recent_payment_count", "recent_payment_sum"]
    webshop_fields = ["payitem_match_count", "payitem_quantity_total", "payitem_item_value_sum"]
    total_fields = ["total_payment_sum"]
    block_fields = ["block_status"]
    has_gcp = any(row.get("account_type") for row in rows)
    has_payment = any("recent_payment_sum" in row for row in rows)
    has_webshop = any("payitem_item_value_sum" in row for row in rows)
    has_total = any("total_payment_sum" in row for row in rows)
    has_block = any("block_status" in row for row in rows)
    fieldnames = (
        base_fields
        + (gcp_fields if has_gcp else [])
        + (payment_fields if has_payment else [])
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
    if args.test_single_board:
        print("테스트   : --test-single-board 활성 — 리더보드 1개만 조회 후 다음 단계로 진행")
    if args.block_new_users:
        print(
            f"차단     : 활성 (실제 차단) — 신규 유저 총결제 {BLOCK_MAX_TOTAL_PAYMENT:,}원 이하, "
            f"디바이스밴 {'생략' if args.skip_device_block else '포함'}, 사유='{args.reason}'"
        )
    else:
        print(f"차단     : 드라이런 (--block-new-users 미지정) — 후보만 출력, 임계값 {BLOCK_MAX_TOTAL_PAYMENT:,}원 이하")

    succeeded = False
    all_rows = []
    skipped_boards = []
    error_message = ""
    block_had_failures = False
    page = None

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
            )

            # 조회 완료 후: 신규 유저 중 총결제 임계값 이하 대상 차단(운영 실행) 또는 후보 출력(드라이런).
            block_outcome = block_low_payment_new_users(
                page=page,
                all_rows=all_rows,
                start_url=args.start_url,
                project_name=args.project_name,
                args=args,
                project_key=project_key,
            )
            block_had_failures = block_outcome["executed"] and any(
                not r["succeeded"] for r in block_outcome["results"]
            )

            save_csv(all_rows, LEADERBOARD_OUT_DIR)

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

    if not succeeded:
        sys.exit(1)
    if skipped_boards or block_had_failures:
        # 실행 자체는 완료됐지만 일부 리더보드 스킵 또는 일부 차단 실패가 있었다는 사실을
        # 종료 코드로 숨기지 않는다.
        sys.exit(2)


if __name__ == "__main__":
    main()
