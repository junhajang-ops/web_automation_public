# -*- coding: utf-8 -*-
"""
Console console user block helper.

Warning:
- Running this script performs an actual mutation in the Console console.
- It opens the user-access page, registers a block, and confirms the result.
"""

import argparse
import csv
import datetime
import sys
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
    DEFAULT_START_URL,
    click_login_if_needed,
    find_exact_text_match,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
)
from test_config import TEST_UUID, apply_title_profile

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = "dumps_console_user_block"
DEFAULT_PROJECT_NAME = "\ud5cc\ud130 \ud0a4\uc6b0\uae30"
DEFAULT_BLOCK_REASON = "UserBlock/Permanent_DataHack_Desc"
DEFAULT_BLOCK_PERIOD_DAYS = 9999
DEFAULT_DEVICE_BAN_COUNT = 3

TEXT_USER_ACCESS = "\uc720\uc800 \uc811\uadfc"
TEXT_DENY_TAB = "\uc811\uadfc \ucc28\ub2e8"
TEXT_OPEN_BLOCK_DIALOG = "\uc811\uadfc \ucc28\ub2e8 \ub4f1\ub85d"
TEXT_TARGET_USER = "\uc720\uc800"
TEXT_INPUT_MODE_TEXT = "\uc9c1\uc811 \uc785\ub825"
TEXT_UUID_PLACEHOLDER = "UUID\ub97c \uc785\ub825\ud558\uc138\uc694."
TEXT_RANK_DELETE = "\ucc28\ub2e8 \uc2dc \ub9ac\ub354\ubcf4\ub4dc \uc21c\uc704 \uc0ad\uc81c\ud558\uae30"
TEXT_REASON_PLACEHOLDER = "\uc0ac\uc720\ub97c \uc785\ub825\ud558\uc138\uc694."
TEXT_SUBMIT_BLOCK = "\ucc28\ub2e8 \ub4f1\ub85d\ud558\uae30"
TEXT_RESULT_DIALOG = "\uc811\uadfc \ucc28\ub2e8 \ub4f1\ub85d \ucc98\ub9ac \uacb0\uacfc"
TEXT_RESULT_SUCCESS = "\uc811\uadfc \ucc28\ub2e8 \ub4f1\ub85d\uc774 \uc131\uacf5\uc801\uc73c\ub85c \uc644\ub8cc\ub418\uc5c8\uc2b5\ub2c8\ub2e4."
TEXT_ALREADY_BLOCKED_DIALOG = "\uc548\ub0b4"
TEXT_ALREADY_BLOCKED_MSG = "\uc774\ubbf8 \ub4f1\ub85d\ub41c \uc720\uc800\uc785\ub2c8\ub2e4."
TEXT_CONFIRM = "\ud655\uc778"
TEXT_CANCEL = "\ucde8\uc18c"

# \ub514\ubc14\uc774\uc2a4 \ucc28\ub2e8(\uc720\uc800 \uc811\uadfc > \uc811\uadfc \ucc28\ub2e8 \ud0ed > \uc811\uadfc \ucc28\ub2e8 \ub4f1\ub85d, \ub300\uc0c1=\ub514\ubc14\uc774\uc2a4) - 2026-07-01 \ub77c\uc774\ube0c \ub364\ud504\ub85c \ud655\uc778\ub41c \uc2e4\uc81c DOM \uad6c\uc870
TEXT_TARGET_DEVICE = "\ub514\ubc14\uc774\uc2a4"
TEXT_DEVICE_SEARCH_INPUT_PLACEHOLDER = "UUID \ub610\ub294 \ub2c9\ub124\uc784\uc744 \uc785\ub825\ud558\uc138\uc694"
TEXT_DEVICE_SEARCH_BUTTON = "\uac80\uc0c9"
TEXT_ALREADY_BLOCKED_DEVICE_MSG = "\uc774\ubbf8 \ub4f1\ub85d\ub41c \ub514\ubc14\uc774\uc2a4\uc785\ub2c8\ub2e4."

BAN_HISTORY_DIR = Path(__file__).resolve().parent.parent / "web_docs" / "ban_history"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console user block helper"
    )
    parser.add_argument(
        "--uuid",
        default=TEST_UUID,
        help=f"Target user UUID (default: {TEST_UUID})",
    )
    parser.add_argument(
        "--reason",
        default=DEFAULT_BLOCK_REASON,
        help=f"Block reason text (default: {DEFAULT_BLOCK_REASON})",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=DEFAULT_BLOCK_PERIOD_DAYS,
        help=f"Block period days (default: {DEFAULT_BLOCK_PERIOD_DAYS})",
    )
    parser.add_argument(
        "--skip-rank-delete",
        action="store_true",
        help="Do not check the leaderboard-rank removal checkbox.",
    )
    parser.add_argument(
        "--skip-device-block",
        action="store_true",
        help="유저 차단 후 디바이스 차단 절차를 건너뜁니다.",
    )
    parser.add_argument(
        "--device-ban-count",
        type=int,
        default=DEFAULT_DEVICE_BAN_COUNT,
        help=f"디바이스 목록 중 최하단 몇 개를 차단할지 (default: {DEFAULT_DEVICE_BAN_COUNT})",
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


def get_visible_dialog_by_title(page, title_text):
    # 반환값은 항상 필터 기반의 동적 locator여야 한다. dialogs.nth(index)를 그대로
    # 반환하면, 이후 다른 다이얼로그가 닫혀 [role='dialog'] 개수/순서가 바뀔 때
    # 그 인덱스가 다른 요소를 가리키게 되어 클릭이 타임아웃된다(실제 발생 사례:
    # '이미 등록됨' 안내 팝업이 뜨며 뒤의 등록 폼 다이얼로그가 닫혀 인덱스가 밀림).
    dialogs = page.locator("[role='dialog']")
    count = dialogs.count()
    for index in range(count):
        dialog = dialogs.nth(index)
        try:
            if not dialog.is_visible():
                continue
            heading = dialog.locator("h2, [role='heading']").first
            if heading.inner_text().strip() == title_text:
                return page.locator("[role='dialog']").filter(has_text=title_text).first
        except Exception:
            continue
    return page.locator("[role='dialog']").filter(has_text=title_text).first


def open_user_access_page(page):
    print(f"[4] 사이드 메뉴에서 '{TEXT_USER_ACCESS}' 페이지로 이동합니다.")
    menu_link = page.locator("a#baseGamerAccess, a[href*='/baseGamerAccess']").first
    menu_link.wait_for(state="visible", timeout=15_000)
    menu_link.scroll_into_view_if_needed()
    record_step_dump(page, "user_access_nav_pre")
    menu_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator(".ui.pointing.secondary.menu a.item, .ui.secondary.menu a.item").first.wait_for(
        state="visible",
        timeout=15_000,
    )


def open_block_tab(page):
    print(f"[5] '{TEXT_DENY_TAB}' 탭으로 전환합니다.")
    deny_tab = find_exact_text_match(
        page.locator(".ui.pointing.secondary.menu a.item, .ui.secondary.menu a.item"),
        TEXT_DENY_TAB,
    )
    if deny_tab is None:
        raise RuntimeError(f"'{TEXT_DENY_TAB}' 탭을 찾지 못했습니다.")
    deny_tab.scroll_into_view_if_needed()
    record_step_dump(page, "user_access_deny_tab_pre")
    deny_tab.click()

    def _deny_tab_ready():
        current = page.locator(
            ".ui.pointing.secondary.menu a.item.active, .ui.secondary.menu a.item.active"
        ).first
        try:
            if current.inner_text().strip() == TEXT_DENY_TAB:
                return True
        except Exception:
            pass
        if "/deny" in page.url:
            return True
        return None

    if not wait_until(page, _deny_tab_ready, timeout_ms=15_000, wait_ms=1_000):
        raise RuntimeError(f"'{TEXT_DENY_TAB}' 탭 전환을 확인하지 못했습니다.")


def open_block_register_dialog(page):
    print(f"[6] '{TEXT_OPEN_BLOCK_DIALOG}' 버튼을 클릭합니다.")
    button = page.locator("button.ui.button").filter(has_text=TEXT_OPEN_BLOCK_DIALOG).first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_open_dialog_pre")
    button.click()

    dialog = get_visible_dialog_by_title(page, TEXT_OPEN_BLOCK_DIALOG)
    dialog.wait_for(state="visible", timeout=15_000)
    return dialog


def ensure_radio_checked(radio_input, value_name):
    if not radio_input.is_checked():
        raise RuntimeError(f"'{value_name}' 라디오 선택이 반영되지 않았습니다.")


def select_target_type_user(page, dialog):
    print(f"[7] 차단 대상을 '{TEXT_TARGET_USER}'로 맞춥니다.")
    radio_label = dialog.locator("label").filter(has_text=TEXT_TARGET_USER).first
    radio_input = radio_label.locator("input[type='radio'][value='user']").first
    radio_label.wait_for(state="visible", timeout=10_000)
    if not radio_input.is_checked():
        radio_label.scroll_into_view_if_needed()
        record_step_dump(page, "user_block_target_user_pre")
        radio_label.click()
    ensure_radio_checked(radio_input, TEXT_TARGET_USER)


def select_target_type_device(page, dialog):
    print(f"[D1] 차단 대상을 '{TEXT_TARGET_DEVICE}'로 맞춥니다.")
    radio_label = dialog.locator("label").filter(has_text=TEXT_TARGET_DEVICE).first
    radio_input = radio_label.locator("input[type='radio'][value='device']").first
    radio_label.wait_for(state="visible", timeout=10_000)
    if not radio_input.is_checked():
        radio_label.scroll_into_view_if_needed()
        record_step_dump(page, "device_block_target_device_pre")
        radio_label.click()
    ensure_radio_checked(radio_input, TEXT_TARGET_DEVICE)


def fill_device_search_uuid(page, dialog, uuid_value):
    print(f"[D2] 디바이스 조회용 UUID를 입력합니다: {uuid_value}")
    uuid_input = dialog.locator(f"input[placeholder='{TEXT_DEVICE_SEARCH_INPUT_PLACEHOLDER}']").first
    uuid_input.wait_for(state="visible", timeout=10_000)
    uuid_input.scroll_into_view_if_needed()
    record_step_dump(page, "device_block_uuid_fill_pre")
    uuid_input.fill("")
    uuid_input.fill(uuid_value)


def click_device_search_button(page, dialog):
    print(f"[D3] '{TEXT_DEVICE_SEARCH_BUTTON}' 버튼을 클릭합니다.")
    search_button = dialog.get_by_role("button", name=TEXT_DEVICE_SEARCH_BUTTON, exact=True).first

    def _search_ready():
        try:
            if search_button.is_enabled():
                return True
        except Exception:
            return None
        return None

    if not wait_until(page, _search_ready, timeout_ms=10_000, wait_ms=1_000):
        raise RuntimeError(f"'{TEXT_DEVICE_SEARCH_BUTTON}' 버튼이 활성화되지 않았습니다.")

    search_button.scroll_into_view_if_needed()
    record_step_dump(page, "device_block_search_pre")
    search_button.click()


def open_device_dropdown(page, dialog):
    print("[D4] 디바이스 목록 드롭다운을 엽니다.")
    device_combobox = dialog.get_by_role("combobox").first
    device_combobox.wait_for(state="visible", timeout=15_000)

    def _combobox_ready():
        try:
            if device_combobox.get_attribute("aria-disabled") != "true":
                return True
        except Exception:
            return None
        return None

    if not wait_until(page, _combobox_ready, timeout_ms=15_000, wait_ms=1_000):
        raise RuntimeError("디바이스 목록 조회 결과를 받지 못했습니다(드롭다운이 계속 비활성 상태).")

    device_combobox.scroll_into_view_if_needed()
    record_step_dump(page, "device_dropdown_open_pre")
    device_combobox.click()

    options = page.locator("li[role='option'][data-value]")
    options.first.wait_for(state="visible", timeout=10_000)
    return options


def list_device_ids(options_locator) -> list[str]:
    ids = []
    for index in range(options_locator.count()):
        value = options_locator.nth(index).get_attribute("data-value")
        if value:
            ids.append(value)
    return ids


def select_bottom_device_ids(device_ids: list[str], max_count: int = DEFAULT_DEVICE_BAN_COUNT) -> list[str]:
    if len(device_ids) <= max_count:
        return list(device_ids)
    return device_ids[-max_count:]


def select_device_option(page, dialog, device_id: str):
    print(f"[D5] 디바이스 '{device_id}'를 선택합니다.")
    option = page.locator(f"li[role='option'][data-value='{device_id}']").first
    option.wait_for(state="visible", timeout=10_000)
    option.scroll_into_view_if_needed()
    record_step_dump(page, "device_option_select_pre")
    option.click()

    device_combobox = dialog.get_by_role("combobox").first
    selected_text = device_combobox.inner_text().strip()
    if selected_text != device_id:
        raise RuntimeError(f"디바이스 선택이 '{device_id}'로 반영되지 않았습니다: {selected_text}")


def select_input_mode_text(page, dialog):
    print(f"[8] 입력 방식을 '{TEXT_INPUT_MODE_TEXT}'으로 맞춥니다.")
    radio_label = dialog.locator("label").filter(has_text=TEXT_INPUT_MODE_TEXT).first
    radio_input = radio_label.locator("input[type='radio'][value='text']").first
    radio_label.wait_for(state="visible", timeout=10_000)
    if not radio_input.is_checked():
        radio_label.scroll_into_view_if_needed()
        record_step_dump(page, "user_block_input_mode_pre")
        radio_label.click()
    ensure_radio_checked(radio_input, TEXT_INPUT_MODE_TEXT)


def ensure_uuid_key_selected(page, dialog):
    print("[9] 대상 키를 'UUID'로 확인합니다.")
    combo = dialog.locator("[role='combobox']").first
    combo.wait_for(state="visible", timeout=10_000)
    selected_text = combo.inner_text().strip()
    if selected_text == "UUID":
        return

    combo.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_uuid_key_pre")
    combo.click()

    option = dialog.get_by_role("option", name="UUID", exact=True).first
    option.wait_for(state="visible", timeout=10_000)
    option.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_uuid_key_option_pre")
    option.click()

    selected_text = combo.inner_text().strip()
    if selected_text != "UUID":
        raise RuntimeError(f"대상 키 선택이 UUID로 반영되지 않았습니다: {selected_text}")


def fill_block_uuid(page, dialog, uuid_value):
    print(f"[10] UUID를 입력합니다: {uuid_value}")
    uuid_input = dialog.locator(f"input[placeholder='{TEXT_UUID_PLACEHOLDER}']").first
    uuid_input.wait_for(state="visible", timeout=10_000)
    uuid_input.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_uuid_fill_pre")
    uuid_input.fill("")
    uuid_input.fill(uuid_value)


def fill_block_period(page, dialog, period_days: int):
    print(f"[11] 기간에 '{period_days}'를 입력합니다.")
    period_input = dialog.locator("input[type='number']").first
    period_input.wait_for(state="visible", timeout=10_000)
    period_input.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_period_fill_pre")
    period_input.fill("")
    period_input.fill(str(period_days))


def set_rank_delete_checkbox(page, dialog, enabled: bool):
    state_text = "활성화" if enabled else "해제"
    print(f"[12] 리더보드 순위 삭제 체크를 {state_text}합니다.")
    checkbox_label = dialog.locator("label").filter(has_text=TEXT_RANK_DELETE).first
    checkbox_input = checkbox_label.locator("input[type='checkbox']").first
    checkbox_label.wait_for(state="visible", timeout=10_000)

    is_checked = checkbox_input.is_checked()
    if is_checked == enabled:
        return

    checkbox_label.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_rank_delete_pre")
    checkbox_label.click()

    if checkbox_input.is_checked() != enabled:
        raise RuntimeError("리더보드 순위 삭제 체크 상태가 반영되지 않았습니다.")


def fill_block_reason(page, dialog, reason_text, step_name="user_block_reason_fill_pre"):
    print(f"[13] 사유를 입력합니다: {reason_text}")
    reason_input = dialog.locator(f"input[placeholder='{TEXT_REASON_PLACEHOLDER}']").first
    reason_input.wait_for(state="visible", timeout=10_000)
    reason_input.scroll_into_view_if_needed()
    record_step_dump(page, step_name)
    reason_input.fill("")
    reason_input.fill(reason_text)


def submit_block_registration(page, dialog, step_name="user_block_submit_pre"):
    print(f"[14] '{TEXT_SUBMIT_BLOCK}' 버튼을 클릭합니다.")
    submit_button = dialog.get_by_role("button", name=TEXT_SUBMIT_BLOCK, exact=True).first
    submit_button.wait_for(state="visible", timeout=10_000)

    def _submit_ready():
        try:
            if submit_button.is_enabled():
                return True
        except Exception:
            return None
        return None

    if not wait_until(page, _submit_ready, timeout_ms=10_000, wait_ms=1_000):
        raise RuntimeError(f"'{TEXT_SUBMIT_BLOCK}' 버튼이 활성화되지 않았습니다.")

    submit_button.scroll_into_view_if_needed()
    record_step_dump(page, step_name)
    submit_button.click()


def confirm_result_popup(page) -> str:
    """결과 팝업을 처리하고 상태 문자열을 반환한다.

    Returns:
        "success"         — 차단 등록 성공
        "already_blocked" — 이미 등록된 유저
    """
    def _any_result_dialog():
        for title, status in [
            (TEXT_RESULT_DIALOG, "success"),
            (TEXT_ALREADY_BLOCKED_DIALOG, "already_blocked"),
        ]:
            dlg = get_visible_dialog_by_title(page, title)
            try:
                if dlg.is_visible():
                    return (status, dlg)
            except Exception:
                pass
        return None

    result = wait_until(page, _any_result_dialog, timeout_ms=15_000, wait_ms=1_000)
    if result is None:
        raise RuntimeError("결과 팝업(성공/이미등록)이 나타나지 않았습니다.")

    status, dialog = result

    if status == "success":
        print(f"[15] 차단 등록 성공. '{TEXT_CONFIRM}'을 누릅니다.")
        dialog.locator(f"text={TEXT_RESULT_SUCCESS}").first.wait_for(state="visible", timeout=5_000)
    else:
        print(f"[15] 이미 차단된 유저입니다. '{TEXT_CONFIRM}'을 누릅니다.")

    confirm_button = dialog.get_by_role("button", name=TEXT_CONFIRM, exact=True).first
    confirm_button.wait_for(state="visible", timeout=10_000)
    confirm_button.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_result_confirm_pre")
    confirm_button.click()

    def _result_closed():
        try:
            if not dialog.is_visible():
                return True
        except Exception:
            return True
        return None

    if not wait_until(page, _result_closed, timeout_ms=15_000, wait_ms=1_000):
        raise RuntimeError("결과 팝업이 닫히는 것을 확인하지 못했습니다.")

    return status


def confirm_device_result_popup(page, dialog) -> str:
    """디바이스 차단 결과를 판정하고 처리한다.

    실측 결과(run_device_block 참고): 성공 시에는 별도 "결과" 팝업이 뜨지 않고
    '접근 차단 등록' 다이얼로그 자체가 자동으로 닫힌다. '이미 등록된 디바이스입니다'
    안내만 별도의 '안내' 팝업으로 뜨며, 이 팝업을 확인해도 등록 다이얼로그는 열린 채
    유지된다. 유저 차단 흐름(confirm_result_popup)과 달리 "성공" 전용 결과 다이얼로그는
    존재하지 않으므로 기다리지 않는다.

    Returns:
        "success"         — 차단 등록 성공(등록 다이얼로그가 자동으로 닫힘)
        "already_blocked" — 이미 등록된 디바이스(안내 팝업 확인)
    """
    def _outcome():
        already_dialog = get_visible_dialog_by_title(page, TEXT_ALREADY_BLOCKED_DIALOG)
        try:
            if already_dialog.is_visible():
                return ("already_blocked", already_dialog)
        except Exception:
            pass
        try:
            if not dialog.is_visible():
                return ("success", None)
        except Exception:
            return ("success", None)
        return None

    result = wait_until(page, _outcome, timeout_ms=15_000, wait_ms=1_000)
    if result is None:
        raise RuntimeError("디바이스 차단 결과(성공/이미등록)를 확인하지 못했습니다.")

    status, popup = result

    if status == "success":
        print("[D6] 디바이스 차단 등록 성공(등록 다이얼로그가 자동으로 닫힘).")
        return status

    print(f"[D6] 이미 차단된 디바이스입니다. '{TEXT_CONFIRM}'을 누릅니다.")
    popup.locator(f"text={TEXT_ALREADY_BLOCKED_DEVICE_MSG}").first.wait_for(state="visible", timeout=5_000)

    confirm_button = popup.get_by_role("button", name=TEXT_CONFIRM, exact=True).first
    confirm_button.wait_for(state="visible", timeout=10_000)
    confirm_button.scroll_into_view_if_needed()
    record_step_dump(page, "device_block_result_confirm_pre")
    confirm_button.click()

    def _result_closed():
        try:
            if not popup.is_visible():
                return True
        except Exception:
            return True
        return None

    if not wait_until(page, _result_closed, timeout_ms=15_000, wait_ms=1_000):
        raise RuntimeError("이미등록 안내 팝업이 닫히는 것을 확인하지 못했습니다.")

    return status


def save_device_ban_history(uuid_value, device_id, reason, status, project_key=""):
    BAN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{project_key}" if project_key else ""
    csv_path = BAN_HISTORY_DIR / f"device_ban_history{suffix}.csv"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = ["timestamp", "uuid", "device_id", "reason", "status"]
    row = [ts, uuid_value, device_id, reason, status]
    write_header = not csv_path.exists()
    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        writer.writerow(row)
    print(f"  디바이스 차단 기록 저장: {csv_path} (status={status})")


def is_block_dialog_open(page) -> bool:
    dialog = page.locator("[role='dialog']").filter(has_text=TEXT_OPEN_BLOCK_DIALOG).first
    try:
        return dialog.is_visible()
    except Exception:
        return False


def run_device_block(
    page,
    uuid_value: str,
    reason_text: str,
    project_key: str = "",
    max_device_count: int = DEFAULT_DEVICE_BAN_COUNT,
):
    """유저 차단에 이어 해당 유저의 디바이스 중 최하단 N개를 차단한다.

    디바이스는 한 번에 하나만 등록 가능하다. 실측 결과:
    - 차단 등록 성공 시 '접근 차단 등록' 다이얼로그가 자동으로 닫힌다.
    - '이미 등록된 디바이스입니다' 안내를 확인해도 다이얼로그는 열린 채 유지되며,
      드롭다운에서 다음 디바이스를 다시 골라 이어서 등록할 수 있다.
    매 반복마다 다이얼로그가 실제로 열려 있는지 직접 확인해 분기한다(추측 금지).
    """
    print("\n[D0] 디바이스 차단 절차를 시작합니다.")
    dialog = open_block_register_dialog(page)
    select_target_type_device(page, dialog)
    fill_device_search_uuid(page, dialog, uuid_value)
    click_device_search_button(page, dialog)
    options = open_device_dropdown(page, dialog)
    device_ids = list_device_ids(options)

    if not device_ids:
        print("  등록된 디바이스가 없어 디바이스 차단을 건너뜁니다.")
        record_step_dump(page, "device_block_cancel_pre")
        dialog.get_by_role("button", name=TEXT_CANCEL, exact=True).first.click()
        step_and_verify_ui(page, "device_block_completed")
        return []

    target_ids = select_bottom_device_ids(device_ids, max_device_count)
    print(
        f"  전체 디바이스 {len(device_ids)}개 중 최하단 {len(target_ids)}개를 "
        f"차단 대상으로 선택합니다: {target_ids}"
    )

    results = []
    dropdown_open = True  # 방금 open_device_dropdown()으로 목록을 열어 둔 상태

    # 최하단(가장 마지막) 디바이스부터 위로 하나씩 처리한다.
    for device_id in reversed(target_ids):
        if not is_block_dialog_open(page):
            dialog = open_block_register_dialog(page)
            select_target_type_device(page, dialog)
            fill_device_search_uuid(page, dialog, uuid_value)
            click_device_search_button(page, dialog)
            open_device_dropdown(page, dialog)
            dropdown_open = True
        elif not dropdown_open:
            open_device_dropdown(page, dialog)
            dropdown_open = True

        select_device_option(page, dialog, device_id)
        dropdown_open = False  # 옵션 선택 시 드롭다운은 자동으로 닫힌다.
        fill_block_reason(page, dialog, reason_text, step_name="device_block_reason_fill_pre")
        submit_block_registration(page, dialog, step_name="device_block_submit_pre")
        status = confirm_device_result_popup(page, dialog)
        save_device_ban_history(uuid_value, device_id, reason_text, status, project_key)
        results.append({"uuid": uuid_value, "device_id": device_id, "status": status})

    if is_block_dialog_open(page):
        print("  마지막 대상까지 처리했지만 다이얼로그가 남아 있어 '취소'로 닫습니다.")
        cancel_button = dialog.get_by_role("button", name=TEXT_CANCEL, exact=True).first
        cancel_button.wait_for(state="visible", timeout=10_000)
        cancel_button.scroll_into_view_if_needed()
        record_step_dump(page, "device_block_cancel_pre")
        cancel_button.click()

    step_and_verify_ui(page, "device_block_completed")
    return results


def save_ban_history(uuid_value, period_days, reason, rank_delete, status, project_key=""):
    BAN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{project_key}" if project_key else ""
    csv_path = BAN_HISTORY_DIR / f"ban_history{suffix}.csv"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = ["timestamp", "uuid", "period_days", "reason", "rank_delete", "status"]
    row = [ts, uuid_value, str(period_days), reason, str(rank_delete), status]
    write_header = not csv_path.exists()
    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        writer.writerow(row)
    print(f"  차단 기록 저장: {csv_path} (status={status})")


def run_user_block(
    page,
    uuid_value: str,
    reason_text: str,
    period_days: int,
    remove_rank: bool,
    explicit_project_base: str,
    start_url: str,
    project_name: str,
    project_key: str = "",
    skip_device_block: bool = False,
    device_ban_count: int = DEFAULT_DEVICE_BAN_COUNT,
):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )

    open_user_access_page(page)
    open_block_tab(page)
    dialog = open_block_register_dialog(page)
    select_target_type_user(page, dialog)
    select_input_mode_text(page, dialog)
    ensure_uuid_key_selected(page, dialog)
    fill_block_uuid(page, dialog, uuid_value)
    fill_block_period(page, dialog, period_days)
    set_rank_delete_checkbox(page, dialog, remove_rank)
    fill_block_reason(page, dialog, reason_text)
    submit_block_registration(page, dialog)
    block_status = confirm_result_popup(page)
    save_ban_history(uuid_value, period_days, reason_text, remove_rank, block_status, project_key)

    device_results = []
    if skip_device_block:
        step_and_verify_ui(page, "user_block_completed")
    else:
        device_results = run_device_block(
            page,
            uuid_value=uuid_value,
            reason_text=reason_text,
            project_key=project_key,
            max_device_count=device_ban_count,
        )

    return {
        "uuid": uuid_value,
        "period_days": period_days,
        "reason": reason_text,
        "rank_delete": remove_rank,
        "status": block_status,
        "device_block_count": len(device_results),
        "device_block_results": device_results,
    }


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

    save_page_artifacts(page, out_dir, "console_user_block", lines)


def main():
    configure_console_output()
    args = parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
        default_block_reason=DEFAULT_BLOCK_REASON,
        include_block_reason=True,
    )
    import re as _re
    project_key = args.title.strip() if args.title.strip() else _re.sub(r"[^\w가-힣]", "_", args.project_name).strip("_")
    sync_playwright, _timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    print("=" * 60)
    print(" Console console user block helper")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"대상 UUID  : {args.uuid}")
    print(f"차단 기간  : {args.period_days}")
    print(f"차단 사유  : {args.reason}")
    print(f"리더보드 삭제: {not args.skip_rank_delete}")
    print(f"디바이스 차단: {not args.skip_device_block} (최하단 {args.device_ban_count}개)")
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
            result_summary = run_user_block(
                page=page,
                uuid_value=args.uuid,
                reason_text=args.reason,
                period_days=args.period_days,
                remove_rank=not args.skip_rank_delete,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
                project_key=project_key,
                skip_device_block=args.skip_device_block,
                device_ban_count=args.device_ban_count,
            )
            succeeded = True

            print("\n=== 완료 ===")
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

    if not succeeded:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n사용자 요청으로 종료했습니다.")
        sys.exit(130)
