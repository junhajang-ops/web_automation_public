# -*- coding: utf-8 -*-
"""
Console console leaderboard lookup.

Scope:
- Open the Console console
- Select the target project
- Open the leaderboard page from the side menu
- Search visible PvPRank_* leaderboards
- Open each leaderboard from the on-screen list
- Read every player within rank <= 30 from the detail table (ties included)
- Save CSV and debug artifacts

This script is intentionally read-only. It does not click any mutation action.
"""

import argparse
import csv
import datetime
import os
import re
import sys
import time
from pathlib import Path

from console_user_search_test import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    click_login_if_needed,
    find_exact_text_match,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
    wait_for_visible,
)
from console_chart_lookup import PAYMENT_DOCS_DIR
from console_receipt_verification import run_receipt_verification
from console_step_verify import (
    configure_console_output,
    pad_display,
    init_dump_dir,
    record_step_dump,
    step_and_verify_ui,
    wait_until,
)
from test_config import apply_title_profile


BASE_DIR = Path(__file__).resolve().parent

# cs 공용 GCP helper (build_logging_service, fetch_pvp_match_logs)
_CS_DIR = BASE_DIR.parent / "cs"
if str(_CS_DIR) not in sys.path:
    sys.path.insert(0, str(_CS_DIR))
from cs_gcp_logging import build_logging_service, fetch_pvp_match_logs  # noqa: E402

DEFAULT_OUTPUT = "dumps_console_leaderboard"
LEADERBOARD_OUT_DIR = PAYMENT_DOCS_DIR / "leaderboard"
SEARCH_KEYWORD = "PvPRank"
MAX_RANK = 30
LIST_ROWS_PER_PAGE = 100
DETAIL_ROWS_PER_PAGE = 50
POLL_WAIT_MS = 1_000
GRID_SCROLL_STEP_PX = 900
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
BOARD_NAME_RE = re.compile(rf"{SEARCH_KEYWORD}_[A-Za-z0-9_]+")
ACCOUNT_NEW_HOURS = int(os.environ.get("ACCOUNT_NEW_HOURS", "240"))  # 계정 생성 후 N시간 이내 → 신규
RECENT_PAYMENT_LIMIT = 100  # 신규 유저 영수증검증 최근 결제 합계 대상 건수
RANK_COL_WIDTH = 4
UUID_COL_WIDTH = 36
NICKNAME_COL_WIDTH = 24
ACCOUNT_TYPE_COL_WIDTH = 16
MAX_TICKET_COL_WIDTH = 8
LOG_COUNT_COL_WIDTH = 6
LEADERBOARD_NAV_RETURN_IGNORE_PATTERNS = [
    r"button: .*FALLBACK\|type=button$",
    r"button: 한국어\|type=button$",
    r"role: tabpanel$",
    r"structural_text: label:보상 우편 제목(?: \(deprecated\))?$",
    r"structural_text: tab:(?:.*FALLBACK|한국어)$",
]



def parse_args():
    parser = argparse.ArgumentParser(description="Leaderboard PvPRank extractor")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--project-base", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument("--key", default="", help="GCP 서비스계정 JSON 키 경로 (직접 지정)")
    parser.add_argument("--gcp-project", default="", help="GCP 프로젝트 ID (직접 지정)")
    parser.add_argument("--gcp-log", default="", help="GCP 로그 이름 (직접 지정)")
    parser.add_argument(
        "--title",
        default="",
        metavar="NAME",
        help=(
            "타이틀 이름 (예: gametitle). "
            "env에서 {NAME}_KEY_FILE / {NAME}_GCP_PROJECT / {NAME}_LOGNAME / {NAME}_PROJECT_NAME 을 일괄 적용."
        ),
    )
    parser.add_argument("--gametitle", action="store_true", help="--title gametitle 단축키")
    return parser.parse_args()


def open_leaderboard_page(page, nav_context: str = "initial"):
    print("[4] 사이드 메뉴에서 '리더보드' 페이지로 이동합니다.")
    link = page.locator("a#baseRank, a[href*='/baseRank']").first
    link.wait_for(state="visible", timeout=15_000)
    link.scroll_into_view_if_needed()
    ignore_patterns = (
        LEADERBOARD_NAV_RETURN_IGNORE_PATTERNS
        if nav_context == "return"
        else None
    )
    record_step_dump(
        page,
        f"leaderboard_nav_{nav_context}_pre",
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


def search_pvp_rank(page):
    print(f"[5] 검색창에 '{SEARCH_KEYWORD}'를 입력하고 검색합니다.")
    search_input = page.locator("input[name='leaderboardName']").first
    search_input.wait_for(state="visible", timeout=10_000)
    record_step_dump(page, "leaderboard_search_input_pre")
    search_input.fill("")
    search_input.fill(SEARCH_KEYWORD)

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


def set_rows_per_page(page, target: int, label: str, verify_prefix: str = "", container=None):
    target_text = f"{target}개씩 보기"
    print(f"    {label}: {target_text}로 변경합니다.")
    dropdown = get_rows_per_page_dropdown(page, container=container)
    current_text = dropdown.inner_text().strip()
    if current_text == target_text:
        print("    이미 설정되어 있습니다.")
        return

    dropdown.scroll_into_view_if_needed()

    opened = False
    option = None
    for _ in range(3):
        record_step_dump(page, name=f"{verify_prefix}_dropdown_pre" if verify_prefix else "rows_dropdown_pre")
        dropdown.click()
        expanded = (dropdown.get_attribute("aria-expanded") or "").lower()
        if expanded != "true":
            continue
        # aria-expanded=true 확인 후 option 목록에 target_text가 실제로 보이는지 검증
        _opt = page.get_by_role("option", name=target_text, exact=True).first
        try:
            _opt.wait_for(state="visible", timeout=3_000)
            option = _opt
            opened = True
            break
        except Exception:
            pass  # 열렸지만 옵션 미표시 → 재시도

    if not opened:
        raise RuntimeError(f"{label} 드롭다운을 열지 못했습니다.")

    option.scroll_into_view_if_needed()
    record_step_dump(page, name=f"{verify_prefix}_option_pre" if verify_prefix else "rows_option_pre")
    option.click()
    safe_wait_for_load(page, "networkidle", 5_000)

    def _rows_per_page_applied():
        current_text = dropdown.inner_text().strip()
        if current_text == target_text:
            return True
        return None

    if wait_until(page, _rows_per_page_applied, timeout_ms=10_000, wait_ms=POLL_WAIT_MS):
        return

    raise RuntimeError(
        f"{label} 전환 결과가 기대와 다릅니다: expected='{target_text}', actual='{dropdown.inner_text().strip()}'"
    )


def collect_visible_board_names(page) -> list:
    print("[6] 현재 목록 페이지에서 PvPRank_* 리더보드 이름을 수집합니다.")
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

        match = BOARD_NAME_RE.search(row_text)
        if match is None:
            continue

        name = match.group(0)
        if name in seen:
            continue
        board_names.append(name)
        seen.add(name)

    print(f"    {len(board_names)}개 리더보드 발견: {board_names}")
    return board_names


def open_leaderboard_list_and_search(page):
    open_leaderboard_page(page, nav_context="initial")
    search_pvp_rank(page)
    print(f"[7] 목록을 {LIST_ROWS_PER_PAGE}개씩 보기로 맞춥니다.")
    list_footer = page.locator(".MuiDataGrid-footerContainer").first
    set_rows_per_page(page, LIST_ROWS_PER_PAGE, "리더보드 목록 표시 개수", verify_prefix="list_rows", container=list_footer)


def _click_board_from_list(page, board_name: str):
    rows = get_data_rows(page)
    row = rows.filter(has_text=board_name).first
    if wait_for_visible(row, 2_000):
        row.scroll_into_view_if_needed()
        exact_label = find_exact_text_match(row.get_by_text(board_name, exact=True), board_name)
        if exact_label is not None and wait_for_visible(exact_label, 1_000):
            record_step_dump(page, f"leaderboard_open_{board_name}_pre")
            exact_label.click()
        else:
            record_step_dump(page, f"leaderboard_open_{board_name}_pre")
            row.click()
        return

    exact_text = find_exact_text_match(page.get_by_text(board_name, exact=True), board_name)
    if exact_text is None:
        raise RuntimeError(f"목록에서 '{board_name}'를 찾지 못했습니다.")

    exact_text.scroll_into_view_if_needed()
    record_step_dump(page, f"leaderboard_open_{board_name}_pre")
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


def get_week_start_utc() -> datetime.datetime:
    """이번 주 월요일 05:00 KST → UTC naive datetime.

    월요일 00:00~04:59 KST는 시즌 전환 시간대 → RuntimeError.
    그 외(월 05:00 ~ 일 23:59)는 해당 주 월요일 05:00 KST를 반환.
    """
    KST = datetime.timezone(datetime.timedelta(hours=9))
    now_kst = datetime.datetime.now(KST)
    if now_kst.weekday() == 0 and now_kst.hour < 5:
        raise RuntimeError(
            f"월요일 00:00~04:59 KST는 시즌 전환 시간대입니다. "
            f"현재 시각: {now_kst.strftime('%H:%M')} KST — 05:00 이후 재시도해 주세요."
        )
    days_since_monday = now_kst.weekday()
    monday_kst = (now_kst - datetime.timedelta(days=days_since_monday)).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    return monday_kst.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def query_pvp_stats(logging_service, project, log_name, uuid, week_start_utc, now_utc) -> dict:
    """GCP log_pvp_match 조회 → account_type / max_pvp_ticket 반환."""
    since_iso = week_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    entries, err = fetch_pvp_match_logs(logging_service, project, log_name, uuid, since_iso, until_iso)

    if err and not entries:
        return {
            "account_type": f"조회실패({err})",
            "max_pvp_ticket": "",
            "create_account_date": "",
            "pvp_log_count": "",
        }
    if err:
        return {
            "account_type": f"부분실패({err})",
            "max_pvp_ticket": "",
            "create_account_date": "",
            "pvp_log_count": "",
        }
    if not entries:
        return {"account_type": "로그없음", "max_pvp_ticket": "", "create_account_date": "", "pvp_log_count": 0}

    # _create_account_date: 유저마다 동일 — 첫 로그에서 읽기
    create_date_str = ""
    for entry in entries:
        val = (entry.get("jsonPayload") or {}).get("_create_account_date", "")
        if val:
            create_date_str = val
            break

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

    max_ticket = None
    for entry in entries:
        val = (entry.get("jsonPayload") or {}).get("pvp_match_ticket_after_val")
        if val is not None:
            try:
                v = int(val)
                if max_ticket is None or v > max_ticket:
                    max_ticket = v
            except (TypeError, ValueError):
                pass

    return {
        "account_type": account_type,
        "max_pvp_ticket": "" if max_ticket is None else max_ticket,
        "create_account_date": create_date_str,
        "pvp_log_count": len(entries),
    }


def enrich_board_with_gcp(board_rows, logging_service, project, log_name, week_start_utc, now_utc):
    """board_rows 각 행에 GCP pvp_match 통계를 in-place로 추가."""
    if not logging_service or not project or not log_name:
        for row in board_rows:
            row.update({"account_type": "GCP미설정", "max_pvp_ticket": "", "create_account_date": "", "pvp_log_count": ""})
        return
    print(f"    [GCP] {len(board_rows)}명 pvp_match 로그 조회 중 "
          f"(기간: {week_start_utc.strftime('%m/%d %H:%MUTC')} ~ 현재)...")
    for row in board_rows:
        stats = query_pvp_stats(logging_service, project, log_name, row["uuid"], week_start_utc, now_utc)
        row.update(stats)


def _parse_price_to_int(text: str) -> int:
    """금액 셀 텍스트에서 숫자만 추출해 정수로. (예: '₩55,000' → 55000)"""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def summarize_recent_payments(page, uuid_value, start_url, project_name, timeout_error,
                              limit=RECENT_PAYMENT_LIMIT):
    """콘솔 영수증검증 메뉴 조회 → 최근 limit건 결제액 합계. 읽기 전용.

    영수증검증은 이미 100개씩 보기로 최신순 표시되므로 rows 앞에서 limit건을 합산한다.
    """
    result = run_receipt_verification(
        page, uuid_value, "", start_url, project_name, timeout_error
    )
    rows = result.get("rows") or []
    recent = rows[:limit]
    total = sum(_parse_price_to_int(r.get("금액", "")) for r in recent)
    return {
        "recent_payment_count": len(recent),
        "recent_payment_sum": total,
        "receipt_total_amount": result.get("total_amount", ""),
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
    for uuid_value in new_uuids:
        try:
            summary = summarize_recent_payments(page, uuid_value, start_url, project_name, timeout_error)
            print(f"    [{uuid_value}] {summary['recent_payment_count']}건 합계 {summary['recent_payment_sum']:,}")
        except Exception as exc:  # noqa: BLE001 — 한 명 실패가 전체를 막지 않게
            print(f"    [{uuid_value}] 영수증검증 실패: {exc}")
            summary = {"recent_payment_count": "", "recent_payment_sum": "", "receipt_total_amount": ""}
        summary_by_uuid[uuid_value] = summary

    for r in all_rows:
        if r["uuid"] in summary_by_uuid:
            r.update(summary_by_uuid[r["uuid"]])


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


def _format_board_row(rank, uuid_value, nickname, account_type, max_ticket, log_count) -> str:
    return "  ".join(
        [
            pad_display(rank, RANK_COL_WIDTH, align="right"),
            pad_display(uuid_value, UUID_COL_WIDTH),
            pad_display(nickname, NICKNAME_COL_WIDTH),
            pad_display(account_type, ACCOUNT_TYPE_COL_WIDTH),
            pad_display(max_ticket, MAX_TICKET_COL_WIDTH, align="right"),
            pad_display(log_count, LOG_COUNT_COL_WIDTH, align="right"),
        ]
    )


def run(
    page,
    explicit_project_base,
    start_url,
    project_name,
    logging_service=None,
    gcp_project="",
    gcp_log="",
    timeout_error=Exception,
) -> list:
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )

    now_utc = datetime.datetime.utcnow()
    week_start_utc = get_week_start_utc()

    open_leaderboard_list_and_search(page)
    board_names = collect_visible_board_names(page)
    if not board_names:
        raise RuntimeError("PvPRank_* 리더보드를 찾지 못했습니다. 검색 결과를 확인해 주세요.")

    all_rows = []
    for index, board_name in enumerate(board_names):
        if index > 0:
            print("[7-retry] 다음 리더보드를 위해 목록 화면을 다시 엽니다.")
            open_leaderboard_page(page, nav_context="return")
            search_pvp_rank(page)
            print(f"[7] 목록을 {LIST_ROWS_PER_PAGE}개씩 보기로 맞춥니다.")
            list_footer = page.locator(".MuiDataGrid-footerContainer").first
            set_rows_per_page(page, LIST_ROWS_PER_PAGE, "리더보드 목록 표시 개수", verify_prefix="list_rows", container=list_footer)

        enter_leaderboard_detail(page, board_name)
        set_rows_per_page(page, DETAIL_ROWS_PER_PAGE, "리더보드 상세 표시 개수", verify_prefix=f"detail_rows_{board_name}")
        board_rows = extract_top_ranks(page, board_name)

        # 각 보드 추출 직후 GCP pvp_match 로그로 계정 상태 보강
        enrich_board_with_gcp(board_rows, logging_service, gcp_project, gcp_log, week_start_utc, now_utc)

        # 보드별 통합 출력
        print(f"\n  == {board_name} ==")
        print(
            "  "
            + _format_board_row(
                "순위",
                "UUID",
                "닉네임",
                "계정상태",
                "최고티켓",
                "로그수",
            )
        )
        print(
            "  "
            + "  ".join(
                [
                    "-" * RANK_COL_WIDTH,
                    "-" * UUID_COL_WIDTH,
                    "-" * NICKNAME_COL_WIDTH,
                    "-" * ACCOUNT_TYPE_COL_WIDTH,
                    "-" * MAX_TICKET_COL_WIDTH,
                    "-" * LOG_COUNT_COL_WIDTH,
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
                    str(r.get("max_pvp_ticket", "")),
                    str(r.get("pvp_log_count", "")),
                )
            )
        all_rows.extend(board_rows)

    step_and_verify_ui(page, "leaderboard_complete")

    # 신규 유저(계정 240시간 이내)만 영수증검증으로 최근 결제액 합계 점검
    enrich_new_users_with_payments(page, all_rows, start_url, project_name, timeout_error)

    return all_rows


def save_csv(rows: list, out_dir: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"leaderboard_pvprank_{ts}.csv"
    base_fields = ["leaderboard", "rank", "uuid", "nickname"]
    gcp_fields = ["account_type", "max_pvp_ticket", "create_account_date", "pvp_log_count"]
    payment_fields = ["recent_payment_count", "recent_payment_sum", "receipt_total_amount"]
    has_gcp = any(row.get("account_type") for row in rows)
    has_payment = any("recent_payment_sum" in row for row in rows)
    fieldnames = base_fields + (gcp_fields if has_gcp else []) + (payment_fields if has_payment else [])
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 저장: {csv_path.name} ({len(rows)}행, GCP컬럼={'포함' if has_gcp else '없음'})")
    return csv_path


def save_artifacts(page, out_dir: Path, succeeded: bool, rows: list, error_message: str):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_leaderboard_{ts}"
    png_path = stem.with_suffix(".png")
    html_path = stem.with_suffix(".html")
    txt_path = stem.with_suffix(".txt")

    page.screenshot(path=str(png_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")

    unique_boards = sorted({row["leaderboard"] for row in rows})
    lines = [
        f"succeeded={succeeded}",
        f"url={page.url}",
        f"row_count={len(rows)}",
        f"board_count={len(unique_boards)}",
        f"boards={', '.join(unique_boards[:20])}",
    ]
    if error_message:
        lines.append(f"error={error_message}")

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n아티팩트 저장 완료: {png_path} / {html_path} / {txt_path}")


def main():
    configure_console_output()
    args = parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
        include_key_file=True,
        include_gcp=True,
    )
    sync_playwright, _timeout_error = load_playwright()
    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    LEADERBOARD_OUT_DIR.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    if args.title:
        prefix = args.title.upper()
        missing = [
            f"{prefix}_{k}" for k, v in [
                ("KEY_FILE", args.key),
                ("GCP_PROJECT", args.gcp_project),
                ("LOGNAME", args.gcp_log),
                ("PROJECT_NAME", args.project_name),
            ] if not v
        ]
        if missing:
            raise SystemExit(f"[오류] --title {args.title}: 다음 env가 비어 있습니다 — {', '.join(missing)}")

    logging_service = None
    if args.key:
        logging_service = build_logging_service(args.key)
        if logging_service is None:
            print("[경고] GCP 서비스계정 로드 실패. pvp_match 로그 조회 없이 진행합니다.")

    print("=" * 55)
    print(" Leaderboard PvPRank extractor")
    print("=" * 55)
    print(f"프로필   : {profile_dir.name}")
    print(f"출력     : {out_dir.name}")
    print(f"CSV 저장 : {LEADERBOARD_OUT_DIR}")
    print(f"덤프     : {out_dir} (30일 초과 자동 삭제)")
    gcp_ready = logging_service and args.gcp_project and args.gcp_log
    print(f"GCP 조회 : {'활성 (' + args.gcp_project + ' / ' + args.gcp_log + ')' if gcp_ready else '비활성 (--key / --gcp-project / --gcp-log 미지정)'}")

    succeeded = False
    all_rows = []
    error_message = ""
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
            all_rows = run(
                page=page,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
                logging_service=logging_service,
                gcp_project=args.gcp_project,
                gcp_log=args.gcp_log,
                timeout_error=_timeout_error,
            )
            save_csv(all_rows, LEADERBOARD_OUT_DIR)

            board_count = len(set(row["leaderboard"] for row in all_rows))
            print(f"\n=== 완료: {board_count}개 리더보드, 총 {len(all_rows)}행 ===")
            succeeded = True

            if args.hold_seconds > 0:
                print(f"{args.hold_seconds}초 대기 후 종료합니다.")
                page.wait_for_timeout(args.hold_seconds * 1_000)
        except Exception as exc:
            error_message = str(exc)
            print(f"\n[오류] {exc}")
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
                    )
            finally:
                context.close()

    if not succeeded:
        sys.exit(1)


if __name__ == "__main__":
    main()
