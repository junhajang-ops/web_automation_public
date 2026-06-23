# -*- coding: utf-8 -*-
"""
Console console 영수증 검증 UUID search smoke test.

Scope:
- Select the target project (reuses prepare_console_project)
- Open '영수증 검증' menu from the sidebar
- Enter UUID into the search field (name='searchValue') and click '검색' button
- Collect result: row count, total amount, per-row summary
- Dump artifacts: screenshot, HTML, txt (read-only)

DOM mapping basis: console_20260623_121712~121753.txt (field_dump 2026-06-23)
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
from test_config import TEST_UUID

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = "dumps_console_receipt"
DEFAULT_UUID = TEST_UUID
POLL_WAIT_MS = 1_000

RESULT_COLUMNS = [
    "거래일시", "닉네임", "유저 UUID", "주문 ID",
    "제품 ID", "Description", "구분", "스토어", "TBC", "금액", "상태",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console 영수증 검증 UUID search smoke test"
    )
    parser.add_argument(
        "--uuid",
        default=DEFAULT_UUID,
        help=f"Target user UUID (default: {DEFAULT_UUID})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Playwright profile directory (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help=f"Artifact output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--project-base",
        default="",
        help="Optional full project base URL",
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
        help=f"Seconds to keep the browser open after success (default: {DEFAULT_HOLD_SECONDS})",
    )
    return parser.parse_args()


def open_receipt_verification_menu(page):
    print("[4] 사이드 메뉴에서 '영수증 검증'으로 이동합니다.")
    receipt_link = page.locator("a", has_text="영수증 검증").first
    receipt_link.wait_for(state="visible", timeout=15_000)
    receipt_link.scroll_into_view_if_needed()
    receipt_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    receipt_link.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def fill_uuid_search(page, uuid_value):
    print(f"[5] UUID 입력창(name='searchValue')에 값을 입력합니다: {uuid_value}")
    uuid_input = page.locator("input#searchValue").first
    uuid_input.wait_for(state="visible", timeout=15_000)
    uuid_input.scroll_into_view_if_needed()
    uuid_input.fill("")
    uuid_input.fill(uuid_value)
    step_pause(page)


def click_search_button(page):
    print("[6] '검색' 버튼을 클릭합니다.")
    search_button = page.locator("button", has_text="검색").first
    search_button.wait_for(state="visible", timeout=15_000)
    search_button.scroll_into_view_if_needed()
    search_button.click()
    safe_wait_for_load(page, "networkidle", 5_000)
    step_pause(page)


def read_total_amount(page):
    total_match = re.search(
        r"검색 결과 총액:\s*(.+)",
        page.locator("body").inner_text(),
    )
    return total_match.group(1).strip() if total_match else ""


def collect_result(page, uuid_value, timeout_error):
    print("[7] 영수증 검증 결과를 수집합니다.")

    no_result_locator = page.locator("text=검색 결과가 없습니다.").first
    if wait_for_visible(no_result_locator, 5_000):
        total_amount = read_total_amount(page)
        print(f"    결과 없음. 총액: {total_amount}")
        return {
            "has_results": False,
            "row_count": 0,
            "total_amount": total_amount,
            "rows": [],
        }

    row_locator = page.locator("div.MuiDataGrid-row")
    if not wait_for_visible(row_locator.first, 10_000):
        raise timeout_error(f"영수증 검증 결과 행이 나타나지 않았습니다: {uuid_value}")

    row_count = row_locator.count()
    total_amount = read_total_amount(page)
    print(f"    결과 {row_count}건, 총액: {total_amount}")

    rows = []
    for i in range(row_count):
        row = row_locator.nth(i)
        cells = row.locator("div[role='cell']")
        cell_texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
        rows.append(cell_texts)
        print(f"    [{i + 1}] {' | '.join(cell_texts)}")

    return {
        "has_results": True,
        "row_count": row_count,
        "total_amount": total_amount,
        "rows": rows,
    }


def save_artifacts(page, out_dir, uuid_value, succeeded, result_summary=None, error_message=""):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_receipt_{ts}"

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as exc:
        print(f"  (스크린샷 저장 실패: {exc})")

    try:
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML 저장 실패: {exc})")

    summary_lines = [
        f"success={succeeded}",
        f"uuid={uuid_value}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if result_summary:
        summary_lines.append(f"has_results={result_summary.get('has_results', '')}")
        summary_lines.append(f"row_count={result_summary.get('row_count', '')}")
        summary_lines.append(f"total_amount={result_summary.get('total_amount', '')}")
        for i, row in enumerate(result_summary.get("rows", []), start=1):
            summary_lines.append(f"row_{i}={' | '.join(str(c) for c in row)}")
    if error_message:
        summary_lines.append(f"error={error_message}")

    try:
        Path(f"{stem}.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    except Exception as exc:
        print(f"  (요약 저장 실패: {exc})")

    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return
    print(f"[8] 화면을 {hold_seconds}초 동안 유지합니다.")
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        page.wait_for_timeout(POLL_WAIT_MS)


def run_receipt_verification(
    page,
    uuid_value,
    explicit_project_base,
    start_url,
    project_name,
    timeout_error,
):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    open_receipt_verification_menu(page)
    snap_and_check_ui(page, "receipt_verification")
    fill_uuid_search(page, uuid_value)
    click_search_button(page)
    snap_and_check_ui(page, "receipt_results")
    return collect_result(page, uuid_value, timeout_error)


def main():
    args = parse_args()
    sync_playwright, timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" Console 영수증 검증 UUID search smoke test")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"프로젝트명 : {args.project_name}")
    print(f"대상 UUID  : {args.uuid}")
    print(f"시작 URL   : {args.start_url}")

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
            result_summary = run_receipt_verification(
                page=page,
                uuid_value=args.uuid,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
                timeout_error=timeout_error,
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
                    uuid_value=args.uuid,
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
