# -*- coding: utf-8 -*-
"""
Console console chart CSV refresh utility.

console_payment_error.py / console_post_register.py가 판정·재지급에 쓰는
web_docs/chart_{chart_name}_*.csv 캐시를 최신화하는 도구입니다.

Scope:
- Open the Console console
- Select the target project
- Open the chart page from the side menu
- Change chart-list rows per page from 10 to 100
- Search the exact chart link across paged list results
- Open the chart detail page
- Click the currently applied chart file row
- Download the CSV for the currently applied chart file

This script is intentionally read-only. It does not click any mutation action.
"""

import argparse
import csv as csv_mod
import sys
from pathlib import Path

from console_step_verify import (
    POLL_INTERVAL_MS,
    configure_console_output,
    get_retry_max_retries,
    init_dump_dir,
    record_step_dump,
    retry_with_recovery,
    save_page_artifacts,
    step_and_verify_ui,
    wait_for_loading_settled,
    wait_until,
)
from console_user_search import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    click_login_if_needed,
    find_exact_text_match,
    hold_open_loop,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
    wait_for_visible,
)
from test_config import TEST_CHART_NAME, apply_title_profile

BASE_DIR = Path(__file__).resolve().parent
PAYMENT_DOCS_DIR = BASE_DIR.parent / "web_docs"
DEFAULT_OUTPUT = "dumps_console_chart_lookup"
DEFAULT_CHART_NAME = TEST_CHART_NAME
# 공용 폴링/정착 대기 = console_step_verify.POLL_INTERVAL_MS(500). (2026-07-23 공용화)
POLL_WAIT_MS = POLL_INTERVAL_MS
SCROLL_STEP_PX = 1_200
MAX_SCROLL_ATTEMPTS = 20
ROWS_PER_PAGE = 100
RETRY_MAX_RETRIES = get_retry_max_retries()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console chart CSV refresh utility"
    )
    parser.add_argument(
        "--chart-name",
        default=DEFAULT_CHART_NAME,
        help=f"Exact chart name to open (default: {DEFAULT_CHART_NAME})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Persistent Playwright profile directory (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help=f"Artifact output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--project-base",
        default="",
        help="Optional full project base URL. Ignored in favor of menu navigation.",
    )
    parser.add_argument(
        "--start-url",
        default=DEFAULT_START_URL,
        help=f"Initial console URL (default: {DEFAULT_START_URL})",
    )
    parser.add_argument(
        "--project-name",
        default=DEFAULT_PROJECT_NAME,
        help=f"Project name hint to select (default: {DEFAULT_PROJECT_NAME})",
    )
    parser.add_argument(
        "--title",
        default="",
        metavar="NAME",
        help="Title env profile to apply (example: gametitle)",
    )
    parser.add_argument("--gametitle", action="store_true", help="Shortcut for --title gametitle")
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=DEFAULT_HOLD_SECONDS,
        help=(
            "Seconds to keep the browser open after success "
            f"(default: {DEFAULT_HOLD_SECONDS})"
        ),
    )
    return parser.parse_args()


def set_dropdown_value(dropdown, target_text, label, verify_prefix=""):
    current_text_locator = dropdown.locator(".divider.text, .divider.default.text").first
    current_text = current_text_locator.inner_text().strip()
    print(f"    현재값: '{current_text}'")
    if current_text == target_text:
        print("    (이미 설정되어 있어 건너뜁니다.)")
        return

    dropdown.scroll_into_view_if_needed()

    opened = False
    for _ in range(3):
        record_step_dump(
            dropdown.page,
            f"{verify_prefix}_dd_pre" if verify_prefix else "dd_pre",
        )
        dropdown.click()
        expanded = (dropdown.get_attribute("aria-expanded") or "").lower()
        if expanded == "true":
            opened = True
            break

    if not opened:
        raise RuntimeError(f"{label} 드롭다운을 열지 못했습니다.")

    option = find_exact_text_match(
        dropdown.locator(".menu [role='option']"),
        target_text,
    )
    if option is None:
        raise RuntimeError(f"{label} 옵션에서 정확히 '{target_text}'와 일치하는 항목을 찾지 못했습니다.")
    option.wait_for(state="visible", timeout=10_000)
    option.scroll_into_view_if_needed()

    record_step_dump(
        dropdown.page,
        f"{verify_prefix}_option_pre" if verify_prefix else "option_pre",
    )
    option.click()
    safe_wait_for_load(dropdown.page, "networkidle", 5_000)

    last_seen = current_text

    def _target_applied():
        nonlocal last_seen
        last_seen = current_text_locator.inner_text().strip()
        if last_seen == target_text:
            return last_seen
        return None

    applied_text = wait_until(
        dropdown.page,
        _target_applied,
        timeout_ms=10_000,
        wait_ms=POLL_WAIT_MS,
    )
    if applied_text is not None:
        print(f"    전환 완료: '{applied_text}'")
        return

    raise RuntimeError(
        f"{label} '{target_text}'로 전환하지 못했습니다(현재값 '{last_seen}'). "
        "드롭다운/옵션 조작 방식 재확인 필요 — 실패를 무시하고 진행하지 않습니다."
    )


def open_chart_page(page):
    print("[4] 사이드 메뉴에서 '차트' 페이지로 이동합니다.")
    chart_link = page.locator("a#baseChart, a[href*='/baseChart']").first
    chart_link.wait_for(state="visible", timeout=15_000)
    chart_link.scroll_into_view_if_needed()
    record_step_dump(page, "chart_nav_pre")
    chart_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("table tbody tr").first.wait_for(state="visible", timeout=15_000)


def get_chart_list_rows_per_page_dropdown(page):
    dropdown = page.locator("tfoot [role='listbox']").first
    dropdown.wait_for(state="visible", timeout=15_000)
    return dropdown


def set_chart_list_rows_per_page(page, rows_per_page):
    print(f"[5] 우측 하단 {rows_per_page}개씩 보기로 변경합니다.")
    set_dropdown_value(
        get_chart_list_rows_per_page_dropdown(page),
        f"{rows_per_page}개씩 보기",
        "차트 목록 표시 개수",
        verify_prefix="chart_list_rows",
    )


def build_chart_link_locator(page, chart_name):
    return find_exact_text_match(page.locator("table tbody tr td a"), chart_name)


def count_chart_rows(page):
    return page.locator("table tbody tr").count()


def get_chart_next_page_button(page):
    return page.locator(
        "[aria-label='Pagination Navigation'] a[type='nextItem']"
    ).first


def is_chart_next_page_available(page):
    next_button = get_chart_next_page_button(page)
    if not wait_for_visible(next_button, 2_000):
        return False

    aria_disabled = (next_button.get_attribute("aria-disabled") or "").lower()
    class_name = (next_button.get_attribute("class") or "").lower()
    return aria_disabled != "true" and "disabled" not in class_name


def go_to_next_chart_page(page, current_page_number):
    next_button = get_chart_next_page_button(page)
    next_button.wait_for(state="visible", timeout=15_000)
    next_button.scroll_into_view_if_needed()
    record_step_dump(page, "chart_next_pre")
    print(f"[7] 현재 페이지에 없어서 차트 목록 {current_page_number + 1}페이지로 이동합니다.")
    next_button.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("table tbody tr").first.wait_for(state="visible", timeout=15_000)


def get_window_scroll_y(page):
    try:
        return int(page.evaluate("() => Math.round(window.scrollY)"))
    except Exception:
        return -1


def scroll_until_chart_visible(page, chart_name, page_number):
    print(f"[6] 차트 목록 {page_number}페이지에서 '{chart_name}' 링크를 찾습니다.")
    chart_link = build_chart_link_locator(page, chart_name)
    if wait_for_visible(chart_link, 1_000):
        chart_link.scroll_into_view_if_needed()
        return chart_link

    previous_scroll_y = get_window_scroll_y(page)
    for _ in range(MAX_SCROLL_ATTEMPTS):
        try:
            page.locator("table").first.hover()
        except Exception:
            pass
        record_step_dump(page, "chart_scroll_pre")
        page.mouse.wheel(0, SCROLL_STEP_PX)
        if wait_for_visible(chart_link, 1_000):
            chart_link.scroll_into_view_if_needed()
            return chart_link

        current_scroll_y = get_window_scroll_y(page)
        if current_scroll_y == previous_scroll_y:
            break
        previous_scroll_y = current_scroll_y

    return None


def find_chart_across_pages(page, chart_name):
    page_number = 1

    while True:
        row_count = count_chart_rows(page)
        chart_link = scroll_until_chart_visible(page, chart_name, page_number)
        if chart_link is not None:
            return {
                "lookup_status": "found",
                "page_number": page_number,
                "row_count": row_count,
                "chart_link": chart_link,
            }

        if row_count < ROWS_PER_PAGE:
            print(
                f"[7] 차트 목록 {page_number}페이지 행 수가 {row_count}개라 마지막 페이지로 판단했습니다. "
                f"'{chart_name}' 차트가 없습니다."
            )
            return {
                "lookup_status": "not_found",
                "page_number": page_number,
                "row_count": row_count,
                "chart_link": None,
            }

        if not is_chart_next_page_available(page):
            print(f"[7] 다음 페이지가 없어 '{chart_name}' 차트가 없습니다.")
            return {
                "lookup_status": "not_found",
                "page_number": page_number,
                "row_count": row_count,
                "chart_link": None,
            }

        go_to_next_chart_page(page, page_number)
        page_number += 1


def find_column_index_by_header(table, header_text: str) -> int:
    """table의 thead에서 header_text와 정확히 일치하는 컬럼 인덱스를 반환(없으면 -1).

    콘솔(Semantic UI) 표는 앞뒤에 텍스트가 빈 collapsing th(체크박스/액션 열)를
    두므로, 보이는 헤더 순서만으로는 td 인덱스와 어긋난다. thead의 모든 th(빈 것
    포함)를 세면 같은 행의 td.nth()와 인덱스가 일치한다. 컬럼 순서가 바뀌어도
    헤더명 기준으로 안전하게 찾기 위한 헬퍼(고정 nth 상수 대체).
    """
    headers = table.locator("thead th")
    count = headers.count()
    for i in range(count):
        try:
            if headers.nth(i).inner_text().strip() == header_text:
                return i
        except Exception:
            continue
    return -1


def open_chart_detail(page, chart_name):
    locate_result = find_chart_across_pages(page, chart_name)
    if locate_result["lookup_status"] != "found":
        return {
            "chart_name": chart_name,
            "lookup_status": "not_found",
            "page_number": locate_result["page_number"],
            "row_count": locate_result["row_count"],
            "chart_number": "",
            "applied_chart": "",
            "current_file": "",
        }

    chart_link = locate_result["chart_link"]
    result_row = chart_link.locator("xpath=ancestor::tr[1]").first
    result_table = result_row.locator("xpath=ancestor::table[1]")
    row_cells = result_row.locator("td")
    number_idx = find_column_index_by_header(result_table, "번호")
    applied_idx = find_column_index_by_header(result_table, "적용된 차트")
    chart_number = row_cells.nth(number_idx).inner_text().strip() if number_idx >= 0 else ""
    applied_chart = row_cells.nth(applied_idx).inner_text().strip() if applied_idx >= 0 else ""

    print(f"[8] '{chart_name}' 차트 링크를 클릭합니다.")
    record_step_dump(page, "chart_detail_link_pre")
    chart_link.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.get_by_role("button", name="차트 파일 업로드").wait_for(
        state="visible",
        timeout=15_000,
    )
    page.get_by_text("현재 적용 차트 파일").wait_for(state="visible", timeout=15_000)

    current_file = ""
    current_file_locator = page.locator("text=/IdleGametitle_.*\\.xlsx/").first
    if wait_for_visible(current_file_locator, 2_000):
        current_file = current_file_locator.inner_text().strip()

    print(
        "[9] 차트 상세 페이지 진입을 확인했습니다: "
        f"chart_number={chart_number}, applied_chart={applied_chart}"
    )
    return {
        "chart_name": chart_name,
        "lookup_status": "found",
        "page_number": locate_result["page_number"],
        "row_count": locate_result["row_count"],
        "chart_number": chart_number,
        "applied_chart": applied_chart,
        "current_file": current_file,
    }


def find_applied_chart_file_row(page, timeout_ms=10_000):
    """체크 표시(현재 적용) 행이 정확히 1개 나타날 때까지 폴링한다.

    파일 이력 테이블은 상세 페이지의 다른 요소(업로드 버튼 등)보다 늦게
    하이드레이션될 수 있어, 단발성 스냅샷 검사는 로딩 중인 화면을 "행 없음"으로
    오판할 수 있다. 최종적으로도 0개/2개 이상이면 실제로 모호한 상태이므로 그대로
    실패 처리한다(첫 행을 임의로 쓰지 않는다).
    """
    wait_for_loading_settled(page, timeout_ms=timeout_ms)
    table_rows = page.locator("table tbody tr")
    if not wait_for_visible(table_rows.first, timeout_ms):
        raise RuntimeError("차트 파일 이력 테이블에 행이 없습니다.")

    checkmark = page.locator("i.checkmark, i.check.circle, i.green.check, i.check")

    def _single_checked_row():
        preferred = table_rows.filter(has=checkmark)
        if preferred.count() == 1 and wait_for_visible(preferred.first, 500):
            return preferred.first
        return None

    row = wait_until(page, _single_checked_row, timeout_ms=timeout_ms, wait_ms=POLL_WAIT_MS)
    if row is not None:
        return row

    final_count = table_rows.filter(has=checkmark).count()
    if final_count == 0:
        raise RuntimeError(
            "현재 적용 중인 차트 파일 행을 찾지 못했습니다. 체크 표시 행이 없으므로 첫 행을 임의로 사용하지 않습니다."
        )
    raise RuntimeError(
        f"현재 적용 중인 차트 파일 행이 {final_count}개로 모호합니다. CSV 다운로드를 중단합니다."
    )


def get_applied_file_id(page) -> str:
    """파일 이력 테이블에서 현재 적용 중인 행의 파일 ID를 반환합니다(헤더 '파일 ID' 기준)."""
    row = find_applied_chart_file_row(page)
    table = row.locator("xpath=ancestor::table[1]")
    idx = find_column_index_by_header(table, "파일 ID")
    if idx < 0:
        raise RuntimeError("차트 파일 이력 테이블에서 '파일 ID' 컬럼을 찾지 못했습니다.")
    try:
        return row.locator("td").nth(idx).inner_text().strip()
    except Exception as exc:
        raise RuntimeError("현재 적용 중인 차트 파일 행에서 파일 ID를 읽지 못했습니다.") from exc


def click_applied_chart_file_row(page):
    """체크박스 셀(td.nth(0)) 클릭으로 행 선택 → CSV 다운로드 버튼 활성화."""
    print("[10] 현재 적용 중인 차트 파일 행을 선택합니다.")

    csv_btn = page.locator("button.ui").filter(has_text="CSV 다운로드").first
    try:
        cls = csv_btn.get_attribute("class") or ""
        if "disabled" not in cls:
            print("    (이미 선택됨 — 행 클릭 생략)")
            return
    except Exception:
        pass

    row = find_applied_chart_file_row(page)
    checkbox_cell = row.locator("td").nth(0)
    if wait_for_visible(checkbox_cell, 2_000):
        record_step_dump(page, "chart_file_select_pre")
        checkbox_cell.click()
    else:
        record_step_dump(page, "chart_file_select_pre")
        row.click()

    # CSV 버튼 활성화 대기 (최대 5초)
    def _csv_enabled():
        try:
            cls = csv_btn.get_attribute("class") or ""
            if "disabled" not in cls:
                return True
        except Exception:
            pass
        return None

    wait_until(page, _csv_enabled, timeout_ms=5_000, wait_ms=300)


def _read_csv_and_lookup(csv_path: Path, purchase_code: str) -> dict:
    """CSV 한 번 순회: 행·열 수 집계 + purchase_code → ShopTable_ID 탐색."""
    row_count = col_count = 0
    shop_table_id = ""
    for enc in ("utf-8-sig", "utf-8", "euc-kr"):
        try:
            with open(csv_path, encoding=enc, newline="") as f:
                reader = csv_mod.DictReader(f)
                pc_cols = None
                for i, row in enumerate(reader):
                    if i == 0:
                        col_count = len(row)
                        pc_cols = [c for c in row.keys() if "PurchaseCode" in c]
                    row_count = i + 1
                    if purchase_code and not shop_table_id:
                        for col in (pc_cols or []):
                            if row.get(col, "").strip() == purchase_code.strip():
                                shop_table_id = row.get("ShopTable_ID", "")
                                print(f"    {col}='{purchase_code}' → ShopTable_ID={shop_table_id}")
                                break
            break
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            print(f"    (CSV 읽기 오류: {exc})")
            break
    if purchase_code and not shop_table_id:
        print(f"    (purchase_code '{purchase_code}' 미발견)")
    return {"csv_row_count": row_count, "csv_col_count": col_count, "shop_table_id": shop_table_id}


def _accept_dialog(dialog):
    dialog.accept()


def _do_download_csv(page, csv_path: Path):
    """CSV 다운로드 버튼 클릭 → 확인 모달 처리 → 파일 저장."""
    csv_btn = page.locator("button.ui").filter(has_text="CSV 다운로드").first
    csv_btn.scroll_into_view_if_needed()
    record_step_dump(page, "csv_download_pre")
    page.on("dialog", _accept_dialog)
    try:
        with page.expect_download(timeout=60_000) as dl_info:
            csv_btn.click()
            confirm_btn = page.locator("[role='dialog'] button").filter(
                has_text="확인"
            ).first
            if wait_for_visible(confirm_btn, 5_000):
                print("    (확인 모달 감지 — 확인 버튼 클릭)")
                record_step_dump(page, "csv_confirm_pre")
                confirm_btn.click()
    finally:
        page.remove_listener("dialog", _accept_dialog)
    dl_info.value.save_as(str(csv_path))
    print(f"    저장: {csv_path.name}")


def download_chart_csv(page, chart_name: str, applied_file_id: str) -> dict:
    """파일 ID 기반 캐시 확인 → 필요 시 다운로드 → 행/열 수 집계."""
    print("[11] CSV 파일을 확인합니다.")
    PAYMENT_DOCS_DIR.mkdir(parents=True, exist_ok=True)

    csv_filename = f"chart_{chart_name}_{applied_file_id}.csv"
    csv_path = PAYMENT_DOCS_DIR / csv_filename

    if csv_path.exists():
        print(f"    (파일 ID {applied_file_id} 기저장 — 다운로드 스킵)")
        print(f"    기존 파일 사용: {csv_filename}")
    else:
        # 같은 차트의 이전 버전 삭제
        for old in PAYMENT_DOCS_DIR.glob(f"chart_{chart_name}_*.csv"):
            old.unlink()
            print(f"    (이전 버전 삭제: {old.name})")
        # 행 선택 후 다운로드
        click_applied_chart_file_row(page)
        _do_download_csv(page, csv_path)

    stats = _read_csv_and_lookup(csv_path, "")
    print(f"    행 수: {stats['csv_row_count']}, 열 수: {stats['csv_col_count']}")
    return {**stats, "csv_file": csv_filename, "applied_file_id": applied_file_id}


def run_chart_lookup(
    page,
    chart_name,
    explicit_project_base,
    start_url,
    project_name,
):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    open_chart_page(page)
    set_chart_list_rows_per_page(page, ROWS_PER_PAGE)

    summary = open_chart_detail(page, chart_name)
    if summary["lookup_status"] == "found":
        applied_file_id = get_applied_file_id(page)
        print(f"[10-pre] 현재 적용 파일 ID: {applied_file_id}")
        csv_summary = download_chart_csv(page, chart_name, applied_file_id)
        summary.update(csv_summary)
        step_and_verify_ui(page, "chart_lookup_complete")
    else:
        step_and_verify_ui(page, "chart_lookup_not_found")

    return summary


def save_artifacts(
    page,
    out_dir,
    chart_name,
    succeeded,
    result_summary=None,
    error_message="",
):
    summary_lines = [
        f"success={succeeded}",
        f"chart_name={chart_name}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if result_summary:
        for key in [
            "lookup_status",
            "page_number",
            "row_count",
            "chart_number",
            "applied_chart",
            "current_file",
            "applied_file_id",
            "csv_file",
            "csv_row_count",
            "csv_col_count",
        ]:
            summary_lines.append(f"{key}={result_summary.get(key, '')}")
    if error_message:
        summary_lines.append(f"error={error_message}")

    save_page_artifacts(page, out_dir, "console_chart_lookup", summary_lines)


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return

    print(f"[13] 현재 화면을 {hold_seconds}초 동안 유지합니다.")
    hold_open_loop(page, hold_seconds, POLL_WAIT_MS)


def main():
    configure_console_output()
    args = parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
    )
    sync_playwright, _timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    print("=" * 60)
    print(" Console 차트 CSV 갱신")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"대상 차트  : {args.chart_name}")
    print(f"시작 URL   : {args.start_url}")
    print(f"프로젝트명 : {args.project_name}")

    succeeded = False
    error_message = ""
    result_summary = None

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
            result_summary = retry_with_recovery(
                action=lambda: run_chart_lookup(
                    page=page,
                    chart_name=args.chart_name,
                    explicit_project_base=args.project_base,
                    start_url=args.start_url,
                    project_name=args.project_name,
                ),
                recovery=lambda: prepare_console_project(
                    page=page,
                    explicit_project_base=args.project_base,
                    start_url=args.start_url,
                    project_name=args.project_name,
                ),
                label=f"차트 {args.chart_name} 조회 재시도",
                recovery_desc=f"콘솔 초기화면({args.start_url})/프로젝트 선택부터 다시 준비합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            succeeded = True
            hold_browser_open(page, args.hold_seconds)
        except Exception as exc:
            error_message = str(exc)
            print(f"\n[오류] {error_message}")
        finally:
            try:
                page = select_target_page(context, page)
                save_artifacts(
                    page=page,
                    out_dir=out_dir,
                    chart_name=args.chart_name,
                    succeeded=succeeded,
                    result_summary=result_summary,
                    error_message=error_message,
                )
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
