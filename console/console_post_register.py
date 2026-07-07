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
- Select item by ShopTable_ID (looked up from web_docs/ CSV)

Final registration (receiver input, submit) requires human approval.
"""

import argparse
import sys
import time
from pathlib import Path

from console_step_verify import (
    configure_console_output,
    init_dump_dir,
    record_step_dump,
    save_page_artifacts,
    step_and_verify_ui,
    wait_until,
)
from console_user_search import (
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
from console_chart_lookup import PAYMENT_DOCS_DIR, _read_csv_and_lookup
from test_config import TEST_CHART_NAME, TEST_PURCHASE_CODE, TEST_UUID, apply_title_profile

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
    parser.add_argument(
        "--title",
        default="",
        metavar="NAME",
        help="Title env profile to apply (example: gametitle)",
    )
    parser.add_argument("--gametitle", action="store_true", help="Shortcut for --title gametitle")
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
    record_step_dump(page, "post_nav_pre")
    post_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("table tbody tr").first.wait_for(state="visible", timeout=15_000)


def open_post_register_popup(page):
    print("[5] '우편 등록' 버튼을 클릭합니다.")
    button = page.locator("button.ui.button").filter(has_text="우편 등록").first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    record_step_dump(page, "post_popup_pre")
    button.click()

    dialog = get_post_register_dialog(page)
    dialog.wait_for(state="visible", timeout=15_000)


def select_expiry_7days(page):
    print("[6] 만료기간 '7일'을 선택합니다.")
    dialog = get_post_register_dialog(page)
    field = dialog.locator(".field").filter(has_text="만료기간").first
    radio_box = field.locator(".ui.radio.checkbox").filter(has_text="7일").first
    hidden_radio = field.locator("input[name='expirationType'][value='7']").first

    radio_box.wait_for(state="visible", timeout=10_000)
    radio_box.scroll_into_view_if_needed()
    record_step_dump(page, "expiry_pre")
    radio_box.click()

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
    record_step_dump(page, "title_pre_fill")
    title_input.fill(title)

    print(f"[8] 내용 입력: '{content}'")
    content_area.wait_for(state="visible", timeout=10_000)
    content_area.scroll_into_view_if_needed()
    record_step_dump(page, "content_pre_fill")
    content_area.fill(content)


def open_item_add_popup(page):
    print("[9] '아이템 등록' 버튼을 클릭합니다.")
    button = page.locator("button.ui.basic.button").filter(has_text="아이템 등록").first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    record_step_dump(page, "item_popup_pre")
    button.click()

    dialog = get_item_add_dialog(page)
    dialog.wait_for(state="visible", timeout=15_000)


def load_shop_table_id(chart_name: str) -> str:
    """web_docs/ 에 저장된 CSV에서 TEST_PURCHASE_CODE → ShopTable_ID 반환."""
    return load_shop_table_id_for_code(chart_name, TEST_PURCHASE_CODE)


def load_shop_table_id_for_code(chart_name: str, purchase_code: str) -> str:
    """web_docs/ 에 저장된 CSV에서 임의의 purchase_code → ShopTable_ID 반환(재지급용).

    load_shop_table_id()는 TEST_PURCHASE_CODE 고정값만 조회하지만, 재지급은 판정
    결과(description 또는 GCP log shop_click_id)로 확정된 임의의 purchase_code를
    받아야 하므로 별도로 분리한다.
    """
    csvs = sorted(PAYMENT_DOCS_DIR.glob(f"chart_{chart_name}_*.csv"))
    if not csvs:
        raise RuntimeError(
            f"web_docs/ 에 '{chart_name}' CSV 없음 — console_chart_lookup.py 먼저 실행 필요"
        )
    csv_path = csvs[-1]
    print(f"[CSV] {csv_path.name} 에서 ShopTable_ID 조회 중...")
    result = _read_csv_and_lookup(csv_path, purchase_code)
    shop_table_id = result.get("shop_table_id", "")
    if not shop_table_id:
        raise RuntimeError(
            f"CSV에서 purchase_code='{purchase_code}'에 대응하는 ShopTable_ID를 찾지 못했습니다."
        )
    print(f"    ShopTable_ID: {shop_table_id}")
    return shop_table_id


def select_chart_in_item_popup(page, chart_name):
    print(f"[10] 아이템 추가 팝업 차트 드롭다운에서 '{chart_name}'을 선택합니다.")
    dialog = get_item_add_dialog(page)
    dropdown = dialog.locator("[role='listbox']").first
    dropdown.wait_for(state="visible", timeout=10_000)
    dropdown.scroll_into_view_if_needed()
    record_step_dump(page, "chart_dd_pre")
    dropdown.click()

    option = find_exact_text_match(dialog.locator("[role='option']"), chart_name)
    if option is None:
        raise RuntimeError(f"차트 드롭다운에서 정확히 '{chart_name}'와 일치하는 항목을 찾지 못했습니다.")
    option.wait_for(state="visible", timeout=5_000)
    option.scroll_into_view_if_needed()
    record_step_dump(page, "chart_option_pre")
    option.click()

    selected_text = dropdown.locator(".text, .divider.text").first.inner_text().strip()
    print(f"    선택 결과: '{selected_text}'")
    if selected_text != chart_name:
        raise RuntimeError(
            f"차트 드롭다운 선택 결과가 기대값과 다릅니다: expected='{chart_name}', actual='{selected_text}'"
        )
    return selected_text


def ensure_receiver_list_rows_per_page(page, rows_per_page: int = 100):
    dialog = get_post_register_dialog(page)
    target_text = f"{rows_per_page}개씩 보기"
    limit_dd = dialog.locator("[role='listbox']").filter(has_text="개씩").first
    limit_dd.wait_for(state="visible", timeout=10_000)
    current_text = limit_dd.locator(".text, .divider.text").first.inner_text().strip()
    if current_text == target_text:
        return

    print(f"[15-page] 수신자 목록을 {target_text} 보기로 전환합니다.")
    limit_dd.scroll_into_view_if_needed()
    record_step_dump(page, "rcvr_rows_dd_pre")
    limit_dd.click()

    option = find_exact_text_match(dialog.locator("[role='option']"), target_text)
    if option is None:
        raise RuntimeError(f"수신자 목록 개수 옵션에서 정확히 '{target_text}'와 일치하는 항목을 찾지 못했습니다.")
    option.wait_for(state="visible", timeout=10_000)
    option.scroll_into_view_if_needed()
    record_step_dump(page, "rcvr_rows_option_pre")
    option.click()

    def _rows_per_page_applied():
        selected_text = limit_dd.locator(".text, .divider.text").first.inner_text().strip()
        if selected_text == target_text:
            return True
        return None

    if wait_until(page, _rows_per_page_applied, timeout_ms=10_000, wait_ms=1_000):
        return

    selected_text = limit_dd.locator(".text, .divider.text").first.inner_text().strip()
    if selected_text != target_text:
        raise RuntimeError(
            f"수신자 목록 개수 전환 결과가 기대와 다릅니다: expected='{target_text}', actual='{selected_text}'"
        )


def select_item_in_popup(page, shop_table_id: str):
    print(f"[11] 아이템 드롭다운에서 ShopTable_ID='{shop_table_id}' 선택합니다.")
    dialog = get_item_add_dialog(page)
    item_dropdown = dialog.locator("[name='item'][role='listbox']").first
    item_dropdown.wait_for(state="visible", timeout=10_000)
    item_dropdown.scroll_into_view_if_needed()
    record_step_dump(page, "item_dd_pre")
    item_dropdown.click()

    target_substr = f'"ShopTable_ID":"{shop_table_id}"'
    option = dialog.locator("[role='option']").filter(has_text=target_substr).first
    option.wait_for(state="visible", timeout=10_000)
    option.scroll_into_view_if_needed()
    record_step_dump(page, "item_option_pre")
    option.click()

    selected_text = item_dropdown.locator(".text, .divider.text").first.inner_text().strip()
    print(f"    선택 결과: {selected_text[:80]}...")
    if target_substr not in selected_text:
        raise RuntimeError(
            f"아이템 선택 결과에 ShopTable_ID='{shop_table_id}'가 없습니다. actual='{selected_text[:120]}'"
        )
    return selected_text


def fill_item_count(page, count: int = 1):
    print(f"[12] 수량 '{count}' 입력합니다.")
    dialog = get_item_add_dialog(page)
    count_input = dialog.locator("input[name='itemCount']").first
    count_input.wait_for(state="visible", timeout=10_000)
    count_input.scroll_into_view_if_needed()
    record_step_dump(page, "count_pre_fill")
    count_input.fill(str(count))


def confirm_item_add_popup(page):
    print("[13] 아이템 추가 팝업 '확인' 버튼을 클릭합니다.")
    dialog = get_item_add_dialog(page)
    confirm_btn = dialog.locator("button.ui.medium.positive.button").first
    confirm_btn.wait_for(state="visible", timeout=10_000)
    confirm_btn.scroll_into_view_if_needed()
    record_step_dump(page, "item_confirm_pre")
    confirm_btn.click()
    dialog.wait_for(state="hidden", timeout=10_000)
    # 팝업 닫힘 대기


def fill_receiver_uuid(page, uuid: str):
    print(f"[14] 유저 번호/닉네임 란에 UUID 입력합니다: {uuid}")
    dialog = get_post_register_dialog(page)
    gamer_input = dialog.locator("input[name='gamer']").first
    gamer_input.wait_for(state="visible", timeout=10_000)
    gamer_input.scroll_into_view_if_needed()
    record_step_dump(page, "receiver_pre_fill")
    gamer_input.fill(uuid)


def click_receiver_register(page):
    print("[15] 수신자 '등록' 버튼을 클릭합니다.")
    dialog = get_post_register_dialog(page)
    register_btn = dialog.locator("button.ui.primary.button").filter(has_text="등록").first
    register_btn.wait_for(state="visible", timeout=10_000)
    register_btn.scroll_into_view_if_needed()
    record_step_dump(page, "receiver_register_pre")
    register_btn.click()


def wait_for_receiver_registered(page, uuid: str, timeout_ms: int = 15_000):
    dialog = get_post_register_dialog(page)
    gamer_input = dialog.locator("input[name='gamer']").first
    gamer_input.wait_for(state="visible", timeout=10_000)

    input_cleared = False

    def _receiver_registered():
        nonlocal input_cleared
        current_value = gamer_input.input_value().strip()
        if not current_value:
            input_cleared = True

        try:
            dialog_text = dialog.inner_text()
        except Exception:
            dialog_text = ""

        if uuid in dialog_text:
            return dialog_text
        return None

    if wait_until(page, _receiver_registered, timeout_ms=timeout_ms, wait_ms=300):
        print(f"    수신자 반영 확인: {uuid}")
        return

    if not input_cleared:
        raise RuntimeError(f"수신자 UUID 입력란이 비워지지 않았습니다: {uuid}")
    raise RuntimeError(f"수신자 UUID가 목록에 반영되지 않았습니다: {uuid}")


def register_receiver_uuid_and_wait(page, uuid: str, timeout_ms: int = 15_000):
    fill_receiver_uuid(page, uuid)
    click_receiver_register(page)
    wait_for_receiver_registered(page, uuid, timeout_ms=timeout_ms)


# console_post_bulk.py와 재지급(run_post_register_for_recipient) 공용.
POST_SEND_WAIT_MS = 5_000


def confirm_post_send(page):
    """N초 대기 후 우편 등록 다이얼로그의 '확인' 버튼을 클릭해 발송(비가역)."""
    print(f"[16] {POST_SEND_WAIT_MS // 1000}초 대기 후 우편 등록 '확인'을 클릭합니다.")
    page.wait_for_timeout(POST_SEND_WAIT_MS)

    dialog = get_post_register_dialog(page)
    confirm_btn = dialog.locator("button.ui.medium.positive.button").first
    confirm_btn.wait_for(state="visible", timeout=10_000)
    confirm_btn.scroll_into_view_if_needed()
    record_step_dump(page, "post_send_confirm_pre")
    confirm_btn.click()

    dialog.wait_for(state="hidden", timeout=15_000)


class PostSendUncertainError(RuntimeError):
    """confirm_post_send()(비가역 발송 확인 클릭) 이후 발생한 예외를 감싼다.

    이 시점 이후 예외는 실제로 발송이 됐는지 안 됐는지 화면 기준으로 확정할 수
    없으므로, 호출부가 "실패"(발송 전 중단, 재시도 가능)와 구분해 "불확실"(재시도
    금지, 사람 확인 필요)로 처리하게 한다(AGENTS.md 원칙 11).
    """


def run_post_register_for_recipient(
    page,
    uuid_value: str,
    product_code: str,
    title: str,
    content: str,
    chart_name: str = "",
    explicit_project_base: str = "",
    start_url: str = DEFAULT_START_URL,
    project_name: str = DEFAULT_PROJECT_NAME,
):
    """확정된 product_code(Inapp_PurchaseCode) 1건을 UUID 1명에게 우편으로 재지급.

    호출부가 이미 사람 승인(터미널 '재지급' 입력)을 받은 뒤에만 불러야 한다 — 이
    함수는 최종 발송 확인(confirm_post_send)까지 수행하는 비가역 동작이다.
    실패 지점이 confirm_post_send() 이전이면 일반 예외(아무것도 발송되지 않음),
    이후면 PostSendUncertainError로 감싸 반환한다.

    반환: {"shop_table_id", "chart_name"}
    """
    chart_name = chart_name or TEST_CHART_NAME
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    shop_table_id = load_shop_table_id_for_code(chart_name, product_code)

    open_post_page(page)
    open_post_register_popup(page)
    select_expiry_7days(page)
    fill_title_and_content(page, title, content)
    open_item_add_popup(page)
    select_chart_in_item_popup(page, chart_name)
    select_item_in_popup(page, shop_table_id)
    fill_item_count(page, count=1)
    confirm_item_add_popup(page)
    register_receiver_uuid_and_wait(page, uuid_value)

    try:
        confirm_post_send(page)
    except Exception as exc:  # noqa: BLE001
        raise PostSendUncertainError(str(exc)) from exc

    step_and_verify_ui(page, "post_regrant_sent")
    return {"shop_table_id": shop_table_id, "chart_name": chart_name}


def run_post_register(page, chart_name, explicit_project_base, start_url, project_name):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )

    open_post_page(page)

    open_post_register_popup(page)

    select_expiry_7days(page)

    fill_title_and_content(page, POST_TITLE, POST_CONTENT)

    open_item_add_popup(page)

    selected = select_chart_in_item_popup(page, chart_name)

    shop_table_id = load_shop_table_id(chart_name)
    select_item_in_popup(page, shop_table_id)

    fill_item_count(page, count=1)

    confirm_item_add_popup(page)

    fill_receiver_uuid(page, TEST_UUID)

    click_receiver_register(page)
    wait_for_receiver_registered(page, TEST_UUID)
    step_and_verify_ui(page, "post_receiver_registered")

    return {"chart_selected": selected, "shop_table_id": shop_table_id}


def save_artifacts(page, out_dir, succeeded, result_summary=None, error_message=""):
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

    save_page_artifacts(page, out_dir, "console_post_register", lines)


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

            print("\n=== 완료 (아이템 선택까지) ===")
            for key, value in result_summary.items():
                print(f"  {key}: {value}")

            if args.hold_seconds > 0:
                print(f"[16] {args.hold_seconds}초 대기 후 종료합니다.")
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

