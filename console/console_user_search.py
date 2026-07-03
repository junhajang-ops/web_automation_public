# -*- coding: utf-8 -*-
"""
Console console user search smoke test.

Scope:
- Open the Console console user page
- Put a UUID into the user search field
- Click search
- Verify that at least one result row contains the UUID

This script is intentionally read-only. It does not click any mutation action.
"""

import argparse
import re
import sys
import time
from pathlib import Path

# Windows PowerShell 한국어 깨짐 방지
from console_step_verify import (
    configure_console_output,
    get_retry_max_retries,
    init_dump_dir,
    record_step_dump,
    retry_with_recovery,
    save_page_artifacts,
    step_and_verify_ui,
    step_pause,
    wait_until,
)
from test_config import TEST_UUID, apply_title_profile

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = "pw_profile_console"
DEFAULT_OUTPUT = "dumps_console_search"
DEFAULT_UUID = TEST_UUID
DEFAULT_INVALID_UUID = "11111111-1111-1111-1111-111111111111"  # placeholder — 존재하지 않는 UUID(결과 없음 경로 테스트용)
DEFAULT_SDK_VERSION = "5.11.0"
DEFAULT_START_URL = "https://console.example.io/ko"
DEFAULT_HOLD_SECONDS = 15
DEFAULT_PROJECT_NAME = "게임타이틀"
LOGIN_SKIP = ("/login", "/signin", "/oauth", "/logout", "about:blank")
RETRY_MAX_RETRIES = get_retry_max_retries()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console user search smoke test"
    )
    parser.add_argument(
        "--uuid",
        default=DEFAULT_UUID,
        help=f"Target user UUID (default: {DEFAULT_UUID})",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Persistent Playwright profile directory (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--invalid-uuid",
        default=DEFAULT_INVALID_UUID,
        help=(
            "Deprecated compatibility option. No longer used for UUID validity "
            f"judgment (default: {DEFAULT_INVALID_UUID})"
        ),
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help=f"Artifact output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--project-base",
        default="",
        help="Optional full project base URL, e.g. https://console.example.io/ko/project/...",
    )
    parser.add_argument(
        "--sdk-version",
        default=DEFAULT_SDK_VERSION,
        help=f"Console user page SDK version segment (default: {DEFAULT_SDK_VERSION})",
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
    parser.add_argument(
        "--skip-invalid-check",
        dest="skip_invalid_check",
        action="store_true",
        help="Deprecated compatibility option. Ignored.",
    )
    parser.add_argument(
        "--with-invalid-check",
        dest="skip_invalid_check",
        action="store_false",
        help="Deprecated compatibility option. Ignored.",
    )
    parser.set_defaults(skip_invalid_check=True)
    return parser.parse_args()


def load_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n[안내] playwright가 설치되어 있지 않습니다.")
        print("  pip install playwright")
        print("  python -m playwright install chromium\n")
        sys.exit(1)
    return sync_playwright, PlaywrightTimeoutError


def select_target_page(context, fallback):
    candidates = [
        page for page in context.pages
        if not any(pattern in page.url for pattern in LOGIN_SKIP)
    ]
    if not candidates:
        return fallback

    target = candidates[-1]
    try:
        target.bring_to_front()
    except Exception:
        pass
    return target


def wait_for_visible(locator, timeout_ms):
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def safe_wait_for_load(page, state="networkidle", timeout_ms=15_000):
    try:
        page.wait_for_load_state(state, timeout=timeout_ms)
        return True
    except Exception:
        return False


def ensure_sidebar_link_expanded(page, link, step_name):
    """콘솔 사이드바는 카테고리별 아코디언이며, 프로젝트/세션마다 열림·닫힘 상태가
    남아있는 채로 유지된다(즐겨찾기 등록 여부와 무관). 목표 링크가 DOM에는 있지만
    상위 카테고리가 접혀 있어 안 보이면, 그 카테고리 헤더를 직접 클릭해 펼친다.
    """
    link.wait_for(state="attached", timeout=15_000)
    if wait_for_visible(link, 500):
        return
    header = link.locator(
        "xpath=ancestor::div[contains(@class,'MuiCollapse-root')][1]/preceding-sibling::div[1]"
    ).first
    header.wait_for(state="visible", timeout=15_000)
    header.scroll_into_view_if_needed()
    record_step_dump(page, step_name)
    header.click()
    link.wait_for(state="visible", timeout=15_000)


def is_login_page(page):
    return (
        wait_for_visible(page.locator("input[name='username']"), 1_500)
        and wait_for_visible(page.locator("input[name='password']"), 1_500)
    )


def click_login_if_needed(page):
    if not is_login_page(page):
        return

    print("\n[2] 로그인 화면을 감지했습니다. 저장된 계정 정보로 로그인 시도합니다.")
    login_button = page.locator("button[type='submit']").first
    login_button.wait_for(state="visible", timeout=15_000)
    record_step_dump(page, "login_submit_pre")
    login_button.click()

    def _login_completed():
        if not is_login_page(page) and "/login" not in page.url:
            return True
        return None

    if wait_until(page, _login_completed, timeout_ms=20_000, wait_ms=500):
        return

    raise RuntimeError("로그인 버튼 클릭 후에도 로그인 화면에서 벗어나지 못했습니다.")


def normalize_project_name(value):
    compact = re.sub(r"[\s\-_*\[\]\(\)]+", "", value or "")
    return compact.lower()


def score_project_name(name, project_name):
    name_norm = normalize_project_name(name)
    target_norm = normalize_project_name(project_name)
    if target_norm not in name_norm:
        return -1

    score = 100
    if "라이브" in name:
        score += 50
    if "live" in name.lower():
        score += 40
    if "엔터프라이즈" in name or "enterprise" in name.lower():
        score += 5

    penalty_words = ["ios", "dev", "test", "테스트", "qa", "stage", "azur"]
    for word in penalty_words:
        if word in name.lower():
            score -= 25
    return score


def find_project_selector_button(page):
    return page.locator("button:has(p):has(svg[name='chevron-down'])").first


def ensure_project_menu_open(page):
    if wait_for_visible(page.locator("[role='menuitem']").first, 1_000):
        return

    selector_button = find_project_selector_button(page)
    selector_button.wait_for(state="visible", timeout=15_000)
    record_step_dump(page, "project_menu_open_pre")
    selector_button.click()
    page.locator("[role='menuitem']").first.wait_for(state="visible", timeout=15_000)


def find_exact_text_match(items, target_text):
    count = items.count()
    for index in range(count):
        item = items.nth(index)
        try:
            if item.inner_text().strip() == target_text:
                return item
        except Exception:
            continue
    return None


def select_project_by_name(page, project_name):
    ensure_project_menu_open(page)
    menu_items = page.locator("[role='menuitem']")
    count = menu_items.count()

    candidates = []
    for index in range(count):
        item = menu_items.nth(index)
        name = item.locator("p").first.inner_text().strip()
        score = score_project_name(name, project_name)
        if score >= 0:
            candidates.append((score, name, index))

    if not candidates:
        raise RuntimeError(f"프로젝트 목록에서 '{project_name}' 후보를 찾지 못했습니다.")

    candidates.sort(key=lambda row: row[0], reverse=True)
    _, selected_name, selected_index = candidates[0]
    selected_item = menu_items.nth(selected_index)

    print(f"[3] 프로젝트 선택: {selected_name}")
    selected_item.scroll_into_view_if_needed()
    record_step_dump(page, "project_select_pre")
    selected_item.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("a#baseGamer").first.wait_for(state="visible", timeout=15_000)
    return selected_name


def ensure_uuid_dropdown(page):
    target_text = "유저 UUID"
    dropdown = page.locator("div[name='defaultSearchColumn']").first
    dropdown.wait_for(state="visible", timeout=15_000)

    current_text = dropdown.locator(".text").first.inner_text().strip()
    if current_text == target_text:
        return

    dropdown.scroll_into_view_if_needed()
    record_step_dump(page, "user_uuid_dropdown_pre")
    dropdown.click()

    option = find_exact_text_match(dropdown.locator(".menu .item"), target_text)
    if option is None:
        raise RuntimeError(f"Could not find exact search-column option: {target_text}")
    option.scroll_into_view_if_needed()
    record_step_dump(page, "user_uuid_option_pre")
    option.click()

    selected_text = dropdown.locator(".text").first.inner_text().strip()
    if selected_text != target_text:
        raise RuntimeError(
            f"Search-column selection did not apply: expected='{target_text}', actual='{selected_text}'"
        )


def find_user_result_row(page, uuid_value, wait_timeout_ms):
    grid_row = page.locator(
        f"div.MuiDataGrid-row[data-id='{uuid_value}']"
    ).first
    if wait_for_visible(grid_row, wait_timeout_ms):
        uuid_cell = grid_row.locator("div[data-field='uuid']").first
        cell_text = uuid_cell.inner_text().strip()
        if uuid_value not in cell_text:
            raise RuntimeError(f"Unexpected search result text: {cell_text}")
        return grid_row

    legacy_result = page.locator("td#gamer_id p", has_text=uuid_value).first
    if wait_for_visible(legacy_result, wait_timeout_ms):
        found_text = legacy_result.inner_text().strip()
        if found_text != uuid_value:
            raise RuntimeError(f"Unexpected search result text: {found_text}")
        return legacy_result.locator("xpath=ancestor::tr[1]").first

    return None


def wait_for_user_result_row(page, uuid_value, timeout_error):
    result_row = find_user_result_row(page, uuid_value, 15_000)
    if result_row is None:
        raise timeout_error(f"Search result row not found for UUID: {uuid_value}")
    return result_row


def classify_uuid_search_result(page, uuid_value):
    result_row = find_user_result_row(page, uuid_value, 15_000)
    if result_row is not None:
        return "valid", result_row

    if wait_for_visible(page.locator("[role='dialog']").first, 1_000):
        raise RuntimeError(
            f"Detail dialog opened unexpectedly without a matching result row: {uuid_value}"
        )

    if page.locator("div.MuiDataGrid-row").count() == 0:
        return "invalid", None

    if page.locator("tbody tr").count() == 0:
        return "invalid", None

    raise RuntimeError(
        f"Search settled, but could not classify UUID as valid or invalid: {uuid_value}"
    )


def open_user_detail(page, result_row, uuid_value, timeout_error):
    print("[9] 검색 결과에서 조회 필드를 클릭합니다.")

    before_url = page.url
    uuid_cell = result_row.locator("div[data-field='uuid'] > div[title]").first

    if wait_for_visible(uuid_cell, 5_000):
        uuid_cell.scroll_into_view_if_needed()
        record_step_dump(page, "user_detail_open_pre")
        uuid_cell.click()
    else:
        legacy_uuid_cell = result_row.locator("td#gamer_id p").first
        if wait_for_visible(legacy_uuid_cell, 5_000):
            legacy_uuid_cell.scroll_into_view_if_needed()
            record_step_dump(page, "user_detail_open_pre")
            legacy_uuid_cell.click()
        else:
            action_button = result_row.locator("div[data-field='action'] button").first
            if wait_for_visible(action_button, 5_000):
                action_button.scroll_into_view_if_needed()
                record_step_dump(page, "user_detail_open_pre")
                action_button.click()
            else:
                legacy_button = result_row.locator("td#hasButton button").first
                legacy_button.wait_for(state="visible", timeout=10_000)
                legacy_button.scroll_into_view_if_needed()
                record_step_dump(page, "user_detail_open_pre")
                legacy_button.click()

    dialog_locator = page.locator("[role='dialog']").first
    if wait_for_visible(dialog_locator, 10_000):
        print("[10] 상세조회 팝업이 열렸습니다.")
        return

    safe_wait_for_load(page, "domcontentloaded", 10_000)
    if page.url != before_url:
        print("[10] 상세조회 페이지로 이동했습니다.")
        return

    detail_edit_button = page.locator("button", has_text="수정").first
    if wait_for_visible(detail_edit_button, 5_000):
        print("[10] 상세조회 화면이 로드되었습니다.")
        return

    raise timeout_error(
        f"User detail did not open after clicking detail action for {uuid_value}"
    )


def wait_for_uuid_in_scope(scope, uuid_value, wait_timeout_ms):
    candidates = [
        scope.get_by_text(uuid_value, exact=True).first,
        scope.locator(f"[title='{uuid_value}']").first,
        scope.locator(f"input[value='{uuid_value}']").first,
        scope.locator("textarea").filter(has_text=uuid_value).first,
    ]
    for candidate in candidates:
        if wait_for_visible(candidate, wait_timeout_ms):
            return True
    return False


def verify_user_detail_uuid(page, uuid_value, timeout_error):
    dialog_locator = page.locator("[role='dialog']").first
    if wait_for_visible(dialog_locator, 1_000):
        if wait_for_uuid_in_scope(dialog_locator, uuid_value, 10_000):
            print("[10] 상세조회 내부에서 UUID를 확인했습니다.")
            return
        raise timeout_error(
            f"User detail dialog opened, but UUID was not visible: {uuid_value}"
        )

    if wait_for_uuid_in_scope(page, uuid_value, 10_000):
        print("[10] 상세조회 화면에서 UUID를 확인했습니다.")
        return

    raise timeout_error(
        f"User detail opened, but UUID was not visible in the detail view: {uuid_value}"
    )


def open_user_page(page):
    print("[4] 사이드 메뉴에서 '유저' 페이지로 이동합니다.")
    user_link = page.locator("a#baseGamer, a[href*='/baseGamer/']").first
    user_link.wait_for(state="visible", timeout=15_000)
    user_link.scroll_into_view_if_needed()
    record_step_dump(page, "user_nav_pre")
    user_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    user_link.wait_for(state="visible", timeout=15_000)


def submit_uuid_search(page, uuid_value):
    print("[5] 검색 기준을 '유저 UUID'로 맞춥니다.")
    ensure_uuid_dropdown(page)

    print(f"[6] UUID 입력: {uuid_value}")
    search_input = page.locator("input[name='defaultSearchValue']").first
    search_input.wait_for(state="visible", timeout=15_000)
    record_step_dump(page, "user_uuid_input_pre")
    search_input.fill("")
    search_input.fill(uuid_value)

    print("[7] 검색 버튼을 클릭합니다.")
    record_step_dump(page, "user_search_submit_pre")
    page.locator("button[type='submit']").first.click()
    safe_wait_for_load(page, "networkidle", 5_000)


def save_artifacts(page, out_dir, uuid_value, succeeded, lookup_status="", error_message=""):
    summary_lines = [
        f"success={succeeded}",
        f"lookup_status={lookup_status}",
        f"uuid={uuid_value}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if error_message:
        summary_lines.append(f"error={error_message}")
    save_page_artifacts(page, out_dir, "console_user_search", summary_lines)


def hold_open_loop(page, hold_seconds, poll_ms=500):
    """hold_seconds 동안 브라우저 화면을 열어둔다(안내 문구는 호출부가 출력).

    poll_ms는 종료 시각을 재확인하는 keep-alive 폴링 주기일 뿐이며,
    조작 전 사람 확인 대기(step_pause/STEP_WAIT_MS)와는 무관하다.
    """
    if hold_seconds <= 0:
        return
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        page.wait_for_timeout(poll_ms)


def hold_browser_open(page, hold_seconds, lookup_status=""):
    if hold_seconds <= 0:
        return

    if lookup_status == "valid":
        print(f"[11] 상세 화면을 {hold_seconds}초 동안 유지합니다.")
    elif lookup_status == "invalid":
        print(f"[9] 무효 UUID 판정 화면을 {hold_seconds}초 동안 유지합니다.")
    else:
        print(f"[11] 현재 화면을 {hold_seconds}초 동안 유지합니다.")
    hold_open_loop(page, hold_seconds)


def prepare_console_project(
    page,
    explicit_project_base,
    start_url,
    project_name,
):
    print(f"\n[1] 콘솔 콘솔 첫 페이지로 이동: {start_url}")
    page.goto(start_url)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    step_pause(page)

    click_login_if_needed(page)
    step_pause(page)

    if explicit_project_base:
        print("[3] project-base 인자는 현재 사용하지 않습니다. 프로젝트 선택은 메뉴 기준으로 진행합니다.")

    select_project_by_name(page, project_name)


def run_user_lookup(page, uuid_value, timeout_error):
    open_user_page(page)
    submit_uuid_search(page, uuid_value)

    lookup_status, result_row = classify_uuid_search_result(page, uuid_value)

    if lookup_status == "invalid":
        print("[8] 검색 결과 행이 없고 상세조회도 열리지 않아 무효 UUID로 판단했습니다.")
        step_and_verify_ui(page, "user_results_invalid")
        return lookup_status

    print("[8] 검색 결과에서 UUID를 확인했습니다.")
    open_user_detail(page, result_row, uuid_value, timeout_error)
    verify_user_detail_uuid(page, uuid_value, timeout_error)
    step_and_verify_ui(page, "user_detail_popup")
    return lookup_status


def run_search(
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

    return run_user_lookup(
        page=page,
        uuid_value=uuid_value,
        timeout_error=timeout_error,
    )


def main():
    configure_console_output()
    args = parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
    )
    sync_playwright, timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    print("=" * 60)
    print(" Console user search smoke test")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"대상 UUID  : {args.uuid}")
    print(f"시작 URL   : {args.start_url}")
    print(f"프로젝트명 : {args.project_name}")

    succeeded = False
    lookup_status = ""
    error_message = ""

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
            lookup_status = retry_with_recovery(
                action=lambda: run_search(
                    page=page,
                    uuid_value=args.uuid,
                    explicit_project_base=args.project_base,
                    start_url=args.start_url,
                    project_name=args.project_name,
                    timeout_error=timeout_error,
                ),
                recovery=lambda: prepare_console_project(
                    page=page,
                    explicit_project_base=args.project_base,
                    start_url=args.start_url,
                    project_name=args.project_name,
                ),
                label=f"UUID {args.uuid} 조회 재시도",
                recovery_desc=f"콘솔 초기화면({args.start_url})/프로젝트 선택부터 다시 준비합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            succeeded = True
            hold_browser_open(page, args.hold_seconds, lookup_status)
        except Exception as exc:
            error_message = str(exc)
            print(f"\n[오류] {error_message}")
        finally:
            try:
                page = select_target_page(context, page)
                save_artifacts(
                    page,
                    out_dir,
                    args.uuid,
                    succeeded,
                    lookup_status,
                    error_message,
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
