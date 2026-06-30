# -*- coding: utf-8 -*-
"""
Console console chart lookup smoke test.

Scope:
- Open the Console console
- Select the target project
- Open the chart page from the side menu
- Change chart-list rows per page from 10 to 100
- Search the exact chart link across paged list results
- Open the chart detail page
- Click the currently applied chart file row
- Change chart-data rows per page from 10 to 100
- Traverse all chart-data pages

This script is intentionally read-only. It does not click any mutation action.
"""

import argparse
import csv as csv_mod
import datetime
import sys
import time
from pathlib import Path

from console_step_verify import init_dump_dir, record_step_dump, step_and_verify_ui
from console_user_search_test import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    click_login_if_needed,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
    wait_for_visible,
)
from test_config import TEST_CHART_NAME, TEST_PURCHASE_CODE, apply_title_profile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


BASE_DIR = Path(__file__).resolve().parent
PAYMENT_DOCS_DIR = BASE_DIR.parent / "payment_docs"
DEFAULT_OUTPUT = "dumps_console_chart_lookup"
DEFAULT_CHART_NAME = TEST_CHART_NAME
POLL_WAIT_MS = 1_000
SCROLL_STEP_PX = 1_200
MAX_SCROLL_ATTEMPTS = 20
ROWS_PER_PAGE = 100


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console chart lookup smoke test"
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

    deadline = time.time() + 10
    last_seen = current_text
    while time.time() < deadline:
        last_seen = current_text_locator.inner_text().strip()
        if last_seen == target_text:
            print(f"    전환 완료: '{last_seen}'")
            return
        dropdown.page.wait_for_timeout(POLL_WAIT_MS)

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


def find_exact_text_match(items, target_text):
    count = items.count()
    for idx in range(count):
        item = items.nth(idx)
        try:
            if item.inner_text().strip() == target_text:
                return item
        except Exception:
            continue
    return None


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
    row_cells = result_row.locator("td")
    chart_number = row_cells.nth(1).inner_text().strip()
    applied_chart = row_cells.nth(3).inner_text().strip()

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
    current_file_locator = page.locator("text=/Myapp_.*\\.xlsx/").first
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


def find_applied_chart_file_row(page):
    rows = page.locator("table tbody tr")
    preferred = rows.filter(
        has=page.locator("i.checkmark, i.check.circle, i.green.check, i.check")
    ).first
    if wait_for_visible(preferred, 2_000):
        return preferred
    return rows.first


def get_applied_file_id(page) -> str:
    """파일 이력 테이블에서 현재 적용 중인 행의 파일 ID(td.nth(3))를 반환합니다."""
    row = find_applied_chart_file_row(page)
    try:
        return row.locator("td").nth(3).inner_text().strip()
    except Exception:
        return ""


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
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            cls = csv_btn.get_attribute("class") or ""
            if "disabled" not in cls:
                break
        except Exception:
            pass
        page.wait_for_timeout(300)



def get_chart_data_rows_per_page_dropdown(page):
    dropdown = page.locator("[role='listbox']").last
    dropdown.wait_for(state="visible", timeout=15_000)
    return dropdown


def set_chart_data_rows_per_page(page, rows_per_page):
    print(f"[11] 차트 데이터 {rows_per_page}개씩 보기로 변경합니다.")
    set_dropdown_value(
        get_chart_data_rows_per_page_dropdown(page),
        f"{rows_per_page}개씩 보기",
        "차트 데이터 표시 개수",
        verify_prefix="chart_data_rows",
    )


def get_chart_data_next_page_button(page):
    return page.locator(
        "[aria-label='Pagination Navigation'] a[type='nextItem']"
    ).last


def navigate_all_chart_data_pages(page):
    print("[12] 차트 데이터 전체 페이지를 순회합니다.")
    start_ts = time.time()
    page_count = 1

    while True:
        next_button = get_chart_data_next_page_button(page)
        if not wait_for_visible(next_button, 2_000):
            break

        aria_disabled = (next_button.get_attribute("aria-disabled") or "").lower()
        class_name = (next_button.get_attribute("class") or "").lower()
        if aria_disabled == "true" or "disabled" in class_name:
            break

        next_button.scroll_into_view_if_needed()
        record_step_dump(page, "chart_data_next_pre")
        next_button.click()
        safe_wait_for_load(page, "networkidle", 10_000)
        page.locator("table").last.locator("tbody tr").first.wait_for(
            state="visible",
            timeout=10_000,
        )
        page_count += 1

    elapsed = round(time.time() - start_ts, 1)
    print(f"    완료: {page_count}페이지, 소요시간 {elapsed}초")
    return {
        "data_page_count": page_count,
        "data_elapsed_seconds": elapsed,
    }


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
    """파일 ID 기반 캐시 확인 → 필요 시 다운로드 → ShopTable_ID 탐색."""
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

    stats = _read_csv_and_lookup(csv_path, TEST_PURCHASE_CODE)
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
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_chart_lookup_{ts}"
    screenshot_path = f"{stem}.png"
    html_path = f"{stem}.html"
    txt_path = f"{stem}.txt"

    try:
        page.screenshot(path=screenshot_path, full_page=True)
    except Exception as exc:
        print(f"  (스크린샷 저장 실패: {exc})")

    try:
        Path(html_path).write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML 저장 실패: {exc})")

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
            "shop_table_id",
        ]:
            summary_lines.append(f"{key}={result_summary.get(key, '')}")
    if error_message:
        summary_lines.append(f"error={error_message}")

    try:
        Path(txt_path).write_text("\n".join(summary_lines), encoding="utf-8")
    except Exception as exc:
        print(f"  (요약 저장 실패: {exc})")

    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return

    print(f"[13] 현재 화면을 {hold_seconds}초 동안 유지합니다.")
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        page.wait_for_timeout(POLL_WAIT_MS)


def main():
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
    print(" Console chart lookup smoke test")
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
            result_summary = run_chart_lookup(
                page=page,
                chart_name=args.chart_name,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
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
