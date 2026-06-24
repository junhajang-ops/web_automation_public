# -*- coding: utf-8 -*-
"""
Console console post-register helper.

Scope:
- Open the post page from the side menu
- Open the post-register popup
- Select expiration period "7일"
- Fill title and content
- Open the item-add popup
- Select TEST_CHART_NAME in the chart dropdown

This script stays read-only up to chart selection.
It does not complete final registration.
"""

import argparse
import datetime
import re
import sys
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
DEFAULT_OUTPUT = "dumps_console_post_register"
POST_TITLE = "결제상품지급"
POST_CONTENT = "결제상품지급"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console post-register helper"
    )
    parser.add_argument(
        "--chart-name",
        default=TEST_CHART_NAME,
        help=f"Chart name to select in the item popup (default: {TEST_CHART_NAME})",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--project-base", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    return parser.parse_args()


def get_post_register_dialog(page):
    return page.locator("[role='dialog']").filter(has_text="우편 등록").first


def get_item_add_dialog(page):
    return page.locator("[role='dialog']").filter(has_text="아이템 추가").last


def open_post_page(page):
    print("[4] 사이드 메뉴에서 '우편' 페이지로 이동합니다.")
    post_link = page.locator("a#basePost, a[href*='/basePost']").first
    post_link.wait_for(state="visible", timeout=15_000)
    post_link.scroll_into_view_if_needed()
    step_pause(page)
    post_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("table tbody tr").first.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def open_post_register_popup(page):
    print("[5] '우편 등록' 버튼을 클릭합니다.")
    button = page.locator("button.ui.button").filter(has_text="우편 등록").first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    step_pause(page)
    button.click()

    dialog = get_post_register_dialog(page)
    dialog.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def select_expiry_7days(page):
    print("[6] 만료기간 '7일'을 선택합니다.")
    dialog = get_post_register_dialog(page)
    field = dialog.locator(".field").filter(has_text="만료기간").first
    radio_box = field.locator(".ui.radio.checkbox").filter(has_text="7일").first
    hidden_radio = field.locator("input[name='expirationType'][value='7']").first

    radio_box.wait_for(state="visible", timeout=10_000)
    radio_box.scroll_into_view_if_needed()
    step_pause(page)
    radio_box.click()
    step_pause(page)

    is_checked = (hidden_radio.get_attribute("checked") is not None) or (
        "checked" in ((radio_box.get_attribute("class") or "").lower())
    )
    if not is_checked:
        raise RuntimeError("만료기간 '7일' 선택이 상태에 반영되지 않았습니다.")


def fill_title_and_content(page, title, content):
    dialog = get_post_register_dialog(page)
    title_input = dialog.locator("input[name='country.0.title']").first
    content_area = dialog.locator("textarea[name='country.0.content']").first

    print(f"[7] 제목 입력: '{title}'")
    title_input.wait_for(state="visible", timeout=10_000)
    title_input.scroll_into_view_if_needed()
    step_pause(page)
    title_input.fill(title)
    step_pause(page)

    print(f"[8] 내용 입력: '{content}'")
    content_area.wait_for(state="visible", timeout=10_000)
    content_area.scroll_into_view_if_needed()
    step_pause(page)
    content_area.fill(content)
    step_pause(page)


def open_item_add_popup(page):
    print("[9] '아이템 등록' 버튼을 클릭합니다.")
    button = page.locator("button.ui.basic.button").filter(has_text="아이템 등록").first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    step_pause(page)
    button.click()

    dialog = get_item_add_dialog(page)
    dialog.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def select_chart_in_item_popup(page, chart_name):
    print(f"[10] 아이템 추가 팝업 차트 드롭다운에서 '{chart_name}'을 선택합니다.")
    dialog = get_item_add_dialog(page)
    dropdown = dialog.locator("[role='listbox']").first
    dropdown.wait_for(state="visible", timeout=10_000)
    dropdown.scroll_into_view_if_needed()
    step_pause(page)
    dropdown.click()
    step_pause(page)

    option = dialog.locator("[role='option']").filter(
        has_text=re.compile(rf"^{re.escape(chart_name)}$")
    ).first
    option.wait_for(state="visible", timeout=5_000)
    option.scroll_into_view_if_needed()
    step_pause(page)
    option.click()
    step_pause(page)

    selected_text = dropdown.locator(".text, .divider.text").first.inner_text().strip()
    print(f"    선택 결과: '{selected_text}'")
    if selected_text != chart_name:
        raise RuntimeError(
            f"차트 드롭다운 선택 결과가 기대값과 다릅니다: expected='{chart_name}', actual='{selected_text}'"
        )
    return selected_text


def run_post_register(page, chart_name, explicit_project_base, start_url, project_name):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )

    open_post_page(page)
    snap_and_check_ui(page, "post_list")

    open_post_register_popup(page)
    snap_and_check_ui(page, "post_register_popup")

    select_expiry_7days(page)
    snap_and_check_ui(page, "post_expiry_7days")

    fill_title_and_content(page, POST_TITLE, POST_CONTENT)
    snap_and_check_ui(page, "post_title_content")

    open_item_add_popup(page)
    snap_and_check_ui(page, "post_item_add_popup")

    selected = select_chart_in_item_popup(page, chart_name)
    snap_and_check_ui(page, "post_chart_selected")
    return {"chart_selected": selected}


def save_artifacts(page, out_dir, succeeded, result_summary=None, error_message=""):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_post_register_{ts}"

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as exc:
        print(f"  (스크린샷 저장 실패: {exc})")

    try:
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML 저장 실패: {exc})")

    lines = [
        f"success={succeeded}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if result_summary:
        for key, value in result_summary.items():
            lines.append(f"{key}={value}")
    if error_message:
        lines.append(f"error={error_message}")

    try:
        Path(f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        print(f"  (요약 저장 실패: {exc})")

    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")


def main():
    args = parse_args()
    sync_playwright, _timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" Console console post-register helper")
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
            result_summary = run_post_register(
                page=page,
                chart_name=args.chart_name,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
            )
            succeeded = True

            print("\n=== 완료 (차트 선택까지) ===")
            for key, value in result_summary.items():
                print(f"  {key}: {value}")

            if args.hold_seconds > 0:
                print(f"[11] {args.hold_seconds}초 대기 후 종료합니다.")
                page.wait_for_timeout(args.hold_seconds * 1_000)

        except Exception as exc:
            error_message = str(exc)
            print(f"\n[오류] {error_message}")
        finally:
            try:
                page = select_target_page(context, page)
                save_artifacts(
                    page=page,
                    out_dir=out_dir,
                    succeeded=succeeded,
                    result_summary=result_summary,
                    error_message=error_message,
                )
            except Exception as exc:
                print(f"  (아티팩트 저장 중 오류: {exc})")
            context.close()


if __name__ == "__main__":
    main()
