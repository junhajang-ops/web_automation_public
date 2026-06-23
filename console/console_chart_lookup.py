# -*- coding: utf-8 -*-
"""
Console console chart lookup smoke test.

Scope:
- Open the Console console
- Select the target project
- Open the chart page from the side menu
- Change rows per page from 10 to 100
- Search the exact chart link across paged list results
- Click the chart link and verify that the chart detail page opens

This script is intentionally read-only. It does not click any mutation action.
"""

import argparse
import datetime
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
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
    snap_and_check_ui,
    step_pause,
    wait_for_visible,
)
from test_config import TEST_CHART_NAME

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


BASE_DIR = Path(__file__).resolve().parent
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
        "--hold-seconds",
        type=int,
        default=DEFAULT_HOLD_SECONDS,
        help=(
            "Seconds to keep the browser open after success "
            f"(default: {DEFAULT_HOLD_SECONDS})"
        ),
    )
    return parser.parse_args()


def open_chart_page(page):
    print("[4] 사이드 메뉴에서 '차트' 페이지로 이동합니다.")
    chart_link = page.locator("a#baseChart, a[href*='/baseChart']").first
    chart_link.wait_for(state="visible", timeout=15_000)
    chart_link.scroll_into_view_if_needed()
    step_pause(page)
    chart_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("table tbody tr").first.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def get_chart_rows_per_page_dropdown(page):
    dropdown = page.locator("tfoot [role='listbox']").first
    dropdown.wait_for(state="visible", timeout=15_000)
    return dropdown


def set_chart_rows_per_page(page, rows_per_page):
    print(f"[5] 우측 하단 표시 개수를 '{rows_per_page}개씩 보기'로 변경합니다.")
    target_text = f"{rows_per_page}개씩 보기"
    dropdown = get_chart_rows_per_page_dropdown(page)
    current_text = dropdown.locator(".divider.text").first

    if current_text.inner_text().strip() == target_text:
        step_pause(page)
        return

    dropdown.scroll_into_view_if_needed()
    step_pause(page)
    dropdown.click()
    step_pause(page)

    option = page.locator("[role='option'] .text").filter(has_text=target_text).first
    option.wait_for(state="visible", timeout=15_000)
    option.click()
    step_pause(page)

    deadline = time.time() + 10
    while time.time() < deadline:
        if current_text.inner_text().strip() == target_text:
            step_pause(page)
            return
        page.wait_for_timeout(POLL_WAIT_MS)

    raise RuntimeError(f"Rows-per-page dropdown did not change to: {target_text}")


def build_chart_link_locator(page, chart_name):
    exact_name = re.compile(rf"^{re.escape(chart_name)}$")
    return page.locator("table tbody tr td a").filter(has_text=exact_name).first


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
    step_pause(page)
    print(
        f"[7] 현재 페이지에 없어서 차트 목록 {current_page_number + 1}페이지로 이동합니다."
    )
    next_button.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("table tbody tr").first.wait_for(state="visible", timeout=15_000)
    step_pause(page)


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
        step_pause(page)
        return chart_link

    previous_scroll_y = get_window_scroll_y(page)
    for _ in range(MAX_SCROLL_ATTEMPTS):
        try:
            page.locator("table").first.hover()
        except Exception:
            pass
        page.mouse.wheel(0, SCROLL_STEP_PX)
        step_pause(page)
        if wait_for_visible(chart_link, 1_000):
            chart_link.scroll_into_view_if_needed()
            step_pause(page)
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
    chart_link.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.get_by_role("button", name="차트 파일 업로드").wait_for(
        state="visible",
        timeout=15_000,
    )
    page.get_by_text("현재 적용 차트 파일").wait_for(state="visible", timeout=15_000)
    step_pause(page)

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


def click_applied_chart_file_row(page):
    """파일 이력 목록에서 현재 적용 중인 행(checkmark)을 클릭해 데이터 뷰를 엽니다."""
    print("[10] 현재 적용 중인 차트 파일 행을 클릭합니다.")
    row = page.locator("table tbody tr").filter(
        has=page.locator("i.checkmark, i.check.circle, i.green.check")
    ).first
    if wait_for_visible(row, 3_000):
        row.click()
    else:
        print("    (checkmark 행 미발견 — 첫 번째 행으로 fallback)")
        page.locator("table tbody tr").first.click()
    safe_wait_for_load(page, "networkidle", 5_000)
    step_pause(page)


def set_chart_data_rows_per_page(page, count: int = 100):
    """차트 데이터 테이블(파일 이력 아래)의 페이지당 행 수를 변경합니다."""
    print(f"[11] 차트 데이터 {count}개씩 보기로 변경합니다.")
    target_text = f"{count}개씩 보기"
    # 파일 이력 테이블과 데이터 테이블 각각 tfoot listbox를 가짐 → 마지막(데이터) 사용
    dropdowns = page.locator("tfoot [role='listbox']")
    dropdown = dropdowns.last if dropdowns.count() >= 1 else None
    if dropdown is None or not wait_for_visible(dropdown, 5_000):
        print("    (행 수 드롭다운 없음 — 건너뜁니다.)")
        return
    current = dropdown.locator(".divider.text").first
    if current.inner_text().strip() == target_text:
        return
    dropdown.scroll_into_view_if_needed()
    step_pause(page)
    dropdown.click()
    step_pause(page)
    option = page.locator("[role='option'] .text").filter(has_text=target_text).first
    option.wait_for(state="visible", timeout=10_000)
    option.click()
    step_pause(page)
    safe_wait_for_load(page, "networkidle", 5_000)


def navigate_all_chart_data_pages(page) -> dict:
    """차트 데이터 전 페이지를 순서대로 탐색합니다. 페이지 간 1초 대기 없음."""
    print("[12] 차트 데이터 전 페이지 탐색을 시작합니다.")
    start_ts = time.time()
    page_num = 1

    while True:
        # 데이터 테이블 next 버튼 — 파일 이력과 데이터 테이블이 각각 페이지네이션을 가지므로 last 사용
        next_btn = page.locator(
            "[aria-label='Pagination Navigation'] a[type='nextItem']"
        ).last
        if not wait_for_visible(next_btn, 2_000):
            break
        aria_disabled = (next_btn.get_attribute("aria-disabled") or "").lower()
        cls = (next_btn.get_attribute("class") or "").lower()
        if aria_disabled == "true" or "disabled" in cls:
            break
        next_btn.scroll_into_view_if_needed()
        next_btn.click()
        # step_pause 없음 — 네트워크 완료 + 첫 행 노출만 대기
        safe_wait_for_load(page, "networkidle", 10_000)
        page.locator("table").last.locator("tbody tr").first.wait_for(
            state="visible", timeout=10_000
        )
        page_num += 1

    elapsed = round(time.time() - start_ts, 1)
    print(f"    완료: {page_num}페이지, 소요시간 {elapsed}초")
    return {
        "data_page_count": page_num,
        "data_elapsed_seconds": elapsed,
    }


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
    snap_and_check_ui(page, "chart_list")
    set_chart_rows_per_page(page, ROWS_PER_PAGE)
    snap_and_check_ui(page, "chart_list_100")
    summary = open_chart_detail(page, chart_name)
    if summary["lookup_status"] == "found":
        snap_and_check_ui(page, "chart_detail")
        click_applied_chart_file_row(page)
        set_chart_data_rows_per_page(page, 100)
        nav = navigate_all_chart_data_pages(page)
        summary.update(nav)
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
            "data_page_count",
            "data_elapsed_seconds",
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
    sync_playwright, _timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

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
