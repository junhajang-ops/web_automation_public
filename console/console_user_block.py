# -*- coding: utf-8 -*-
"""
Console console user block helper.

Warning:
- Running this script performs an actual mutation in the Console console.
- It opens the user-access page, registers a block, and confirms the result.
- 차단 대상 UUID 목록은 기본적으로 web_docs의 CSV 파일(--uuid-csv)에서 읽어온다.
  CSV의 "uuid" 컬럼에 한 줄씩 대상을 적어두면 된다(예시: web_docs/user_block/user_block_targets.csv).
- --uuid를 명시하면 CSV 대신 그 값(콤마로 여러 개 가능)을 임시로 사용한다(임시 점검용).
- Each UUID is processed independently with its own retry budget (--retries);
  a UUID that exhausts its retries is skipped and the batch continues with the next UUID.
"""

import argparse
import csv
import datetime
import sys
from pathlib import Path

from console_step_verify import (
    configure_console_output,
    get_retry_max_retries,
    init_dump_dir,
    record_step_dump,
    retry_with_recovery,
    save_page_artifacts,
    step_and_verify_ui,
    wait_until,
)
from console_user_search import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_START_URL,
    click_login_if_needed,
    ensure_sidebar_link_expanded,
    find_exact_text_match,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
)
from test_config import apply_title_profile

BASE_DIR = Path(__file__).resolve().parent
WEB_DOCS_DIR = BASE_DIR.parent / "web_docs"
DEFAULT_UUID_CSV = WEB_DOCS_DIR / "user_block" / "user_block_targets.csv"
DEFAULT_OUTPUT = "dumps_console_user_block"
DEFAULT_PROJECT_NAME = "\ud5cc\ud130 \ud0a4\uc6b0\uae30"
DEFAULT_BLOCK_REASON = "UserBlock/Permanent_DataHack_Desc"
DEFAULT_BLOCK_PERIOD_DAYS = 9999
DEFAULT_DEVICE_BAN_COUNT = 3
# console_leaderboard.py의 진입 재시도(RETRY_MAX_RETRIES)와 동일한 env로 공용 관리.
DEFAULT_RETRIES = get_retry_max_retries()

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
# 2026-07-03: \ub514\ubc14\uc774\uc2a4 \ucc28\ub2e8 \ub4f1\ub85d \uc2dc \uc11c\ubc84\uac00 "404 NotFoundException \uc624\ub958\uac00 \ubc1c\uc0dd\ud588\uc2b5\ub2c8\ub2e4."
# \uac19\uc740 \ubcc4\ub3c4 \uc81c\ubaa9\uc758 \uc624\ub958 \ub2e4\uc774\uc5bc\ub85c\uadf8\ub97c \ub744\uc6b0\ub294 \uc0ac\ub840 \ud655\uc778(\uc608: \ubaa9\ub85d\uc5d0\ub294 \uc788\uc73c\ub098 \uc11c\ubc84\uc5d0\uc11c
# \uc774\ubbf8 \uc9c0\uc6cc\uc9c4 deviceInfo). \uc774 \ub2e4\uc774\uc5bc\ub85c\uadf8\ub294 \uc81c\ubaa9\uc774 "\uc548\ub0b4"\uac00 \uc544\ub2c8\ub77c\uc11c \uae30\uc874
# already_blocked \ud310\uc815\uc5d0 \uac78\ub9ac\uc9c0 \uc54a\uace0, \ub4f1\ub85d \ub2e4\uc774\uc5bc\ub85c\uadf8\ub3c4 \uacc4\uc18d \uc5f4\ub824 \uc788\ub294 \uc0c1\ud0dc\ub77c
# confirm_device_result_popup\uc774 15\ucd08\uac04 \uc544\ubb34 \uac83\ub3c4 \ud655\uc815\ud558\uc9c0 \ubabb\ud55c \ucc44 \ud0c0\uc784\uc544\uc6c3
# \uc608\uc678\ub97c \ub358\uc84c\ub2e4 \u2014 \uc774 \uacbd\uc6b0 save_device_ban_history \ud638\ucd9c \uc804\uc5d0 \uc608\uc678\uac00 \ub098\uc11c
# CSV\uc5d0 \uc2e4\ud328 \uae30\ub85d\uc870\ucc28 \ub0a8\uc9c0 \uc54a\ub294 \ubb38\uc81c\uac00 \uc788\uc5c8\ub2e4. \uc81c\ubaa9 \ubb38\uad6c\ub85c \uc774 \uc720\ud615\uc758 \uc624\ub958\ub97c
# \uc2dd\ubcc4\ud574 "error" \uc0c1\ud0dc\ub85c \uba85\ud655\ud788 \ubd84\ub958\ud558\uace0 CSV\uc5d0 \uae30\ub85d\ud55c\ub2e4.
TEXT_ERROR_HEADING_MARKER = "\uc624\ub958\uac00 \ubc1c\uc0dd"

BAN_HISTORY_DIR = WEB_DOCS_DIR / "ban_history"

# '유저 접근' 메뉴 이동 직전 / '접근 차단 등록' 다이얼로그 열기 직전 전용.
# 접근 차단 목록의 페이지네이션(role=navigation)은 목록 로딩 스피너
# (role=progressbar, MuiCircularProgress)가 사라진 직후에만 렌더된다. 공용
# wait_for_loading_settled()가 스피너 소멸을 기다리긴 하지만, 라이브 서버 응답이
# 느린 순간에는 그 대기 안에 로딩이 안 끝나 "로딩 중" 스냅샷이 baseline으로 저장될
# 때가 있다(2026-07-01/07-03/07-06 반복 실측: 같은 스텝이 회차마다 navigation<->
# progressbar로 뒤집힘). 이 두 스텝에 한해 두 role의 등장/소멸을 구조 변경으로 보지
# 않는다 — 다른 스텝/화면의 navigation·progressbar 변화까지 숨기지 않도록 이 두
# 호출부에만 로컬로 적용한다.
LOADING_TRANSITION_IGNORE_PATTERNS = [
    r"role: navigation$",
    r"role: progressbar$",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console user block helper"
    )
    parser.add_argument(
        "--uuid",
        default="",
        help=(
            "Target user UUID(s)를 콤마로 직접 지정(예: uuid1,uuid2) — "
            "지정 시 --uuid-csv 대신 이 값을 임시로 사용합니다(1회성 점검용). "
            "각 UUID는 독립적으로 처리되며 한 UUID가 재시도를 모두 소진해도 "
            "다음 UUID로 계속 진행합니다 (default: 비어있음 → --uuid-csv 사용)"
        ),
    )
    parser.add_argument(
        "--uuid-csv",
        default=str(DEFAULT_UUID_CSV),
        help=(
            "차단 대상 UUID 목록 CSV 경로. 'uuid' 컬럼에 한 줄씩 UUID를 적어둔다. "
            f"--uuid를 지정하지 않으면 이 CSV를 읽어 배치 처리한다 (default: {DEFAULT_UUID_CSV})"
        ),
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
    parser.add_argument("--dc", action="store_true", help="Shortcut for --title dc (게임B)")
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=(
            "페이지 로딩 실패 등 개별 오류 발생 시, 유저/디바이스 차단 절차 전체를 "
            f"처음부터 다시 시도할 최대 횟수 (default: {DEFAULT_RETRIES}, env RETRY_MAX_RETRIES로 재정의 가능)"
        ),
    )
    return parser.parse_args()


def load_uuid_list_from_csv(csv_path: Path) -> list:
    """CSV의 'uuid' 컬럼에서 차단 대상 UUID 목록을 읽어온다.

    중복은 첫 등장 순서를 유지한 채 제거하고, 빈 값·공백 행은 건너뛴다.
    """
    if not csv_path.exists():
        raise SystemExit(
            f"[오류] --uuid-csv 파일을 찾을 수 없습니다: {csv_path}\n"
            "web_docs/user_block/user_block_targets.csv 예시를 참고해 CSV를 준비하세요."
        )

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "uuid" not in reader.fieldnames:
            raise SystemExit(
                f"[오류] --uuid-csv에 'uuid' 컬럼이 없습니다: {csv_path} "
                f"(현재 컬럼: {reader.fieldnames})"
            )
        seen = set()
        uuid_list = []
        for row in reader:
            value = (row.get("uuid") or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            uuid_list.append(value)

    if not uuid_list:
        raise SystemExit(f"[오류] --uuid-csv에 유효한 UUID가 없습니다: {csv_path}")

    return uuid_list


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


def find_visible_error_dialog(page, exclude_title):
    """제목에 TEXT_ERROR_HEADING_MARKER("오류가 발생")가 포함된, exclude_title이
    아닌 별도의 오류 다이얼로그를 찾는다. 서버가 등록 다이얼로그를 닫지 않은 채
    "404 NotFoundException 오류가 발생했습니다." 같은 자체 제목의 오류 팝업만
    띄우는 사례를 already_blocked/success와 구분해 잡아내기 위함이다."""
    dialogs = page.locator("[role='dialog']")
    count = dialogs.count()
    for index in range(count):
        candidate = dialogs.nth(index)
        try:
            if not candidate.is_visible():
                continue
            heading = candidate.locator("h2, [role='heading']").first
            heading_text = heading.inner_text().strip()
        except Exception:
            continue
        if heading_text == exclude_title:
            continue
        if TEXT_ERROR_HEADING_MARKER in heading_text:
            return candidate, heading_text
    return None


def open_user_access_page(page):
    print(f"[4] 사이드 메뉴에서 '{TEXT_USER_ACCESS}' 페이지로 이동합니다.")
    menu_link = page.locator("a#baseGamerAccess, a[href*='/baseGamerAccess']").first
    ensure_sidebar_link_expanded(page, menu_link, "user_access_category_expand_pre")
    menu_link.scroll_into_view_if_needed()
    record_step_dump(page, "user_access_nav_pre", ignore_patterns=LOADING_TRANSITION_IGNORE_PATTERNS)
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


def open_block_register_dialog(page, step_name="user_block_open_dialog_pre"):
    """호출부마다 서로 다른 step_name을 넘겨야 한다. 유저 차단 흐름(탭 진입 직후)과
    디바이스 차단 흐름(직전 유저 차단 결과 팝업을 막 닫은 직후, 목록이 새로고침
    중이라 progressbar가 보임)은 이 버튼을 누르기 직전 화면 상태가 서로 다르므로,
    같은 이름을 공유하면 서로 다른 정상 상태를 비교해 매번 오탐([UI change])이
    난다(2026-07-03 실측: role navigation<->progressbar가 반복적으로 갈아치워짐).
    이 로딩 스피너 자체의 타이밍 흔들림은 record_step_dump가 공용으로 호출하는
    wait_for_loading_settled()가 스피너 소멸을 기다려 흡수하지만, 라이브 서버 응답이
    느릴 때는 그 대기 안에도 로딩이 안 끝날 수 있어(2026-07-06 재실측) 이 스텝은
    LOADING_TRANSITION_IGNORE_PATTERNS로 navigation/progressbar 자체를 로컬
    화이트리스트했다."""
    print(f"[6] '{TEXT_OPEN_BLOCK_DIALOG}' 버튼을 클릭합니다.")
    button = page.locator("button.ui.button").filter(has_text=TEXT_OPEN_BLOCK_DIALOG).first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    record_step_dump(page, step_name, ignore_patterns=LOADING_TRANSITION_IGNORE_PATTERNS)
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
    actual_value = uuid_input.input_value()
    if actual_value != uuid_value:
        raise RuntimeError(
            f"디바이스 조회 UUID 입력이 반영되지 않았습니다: expected='{uuid_value}', actual='{actual_value}'"
        )


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
    actual_value = uuid_input.input_value()
    if actual_value != uuid_value:
        raise RuntimeError(
            f"차단 대상 UUID 입력이 반영되지 않았습니다: expected='{uuid_value}', actual='{actual_value}'"
        )


def fill_block_period(page, dialog, period_days: int):
    print(f"[11] 기간에 '{period_days}'를 입력합니다.")
    period_input = dialog.locator("input[type='number']").first
    period_input.wait_for(state="visible", timeout=10_000)
    period_input.scroll_into_view_if_needed()
    record_step_dump(page, "user_block_period_fill_pre")
    period_input.fill("")
    period_input.fill(str(period_days))
    actual_value = period_input.input_value()
    if actual_value != str(period_days):
        raise RuntimeError(
            f"차단 기간 입력이 반영되지 않았습니다: expected='{period_days}', actual='{actual_value}'"
        )


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
    actual_value = reason_input.input_value()
    if actual_value != reason_text:
        raise RuntimeError(
            f"차단 사유 입력이 반영되지 않았습니다: expected='{reason_text}', actual='{actual_value}'"
        )


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
        # 다이얼로그 제목 '안내'는 다른 오류 팝업에도 쓰이는 범용 텍스트이므로,
        # 제목만으로 '이미 등록됨'으로 단정하지 않고 본문 문구까지 확인한다.
        # 본문이 다르면 여기서 타임아웃 예외가 발생해 오판을 막는다.
        dialog.locator(f"text={TEXT_ALREADY_BLOCKED_MSG}").first.wait_for(state="visible", timeout=5_000)
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


def confirm_device_result_popup(page, dialog) -> tuple:
    """디바이스 차단 결과를 판정하고 처리한다.

    실측 결과(run_device_block 참고): 성공 시에는 별도 "결과" 팝업이 뜨지 않고
    '접근 차단 등록' 다이얼로그 자체가 자동으로 닫힌다. '이미 등록된 디바이스입니다'
    안내만 별도의 '안내' 팝업으로 뜨며, 이 팝업을 확인해도 등록 다이얼로그는 열린 채
    유지된다. 유저 차단 흐름(confirm_result_popup)과 달리 "성공" 전용 결과 다이얼로그는
    존재하지 않으므로 기다리지 않는다.

    2026-07-03: 서버가 "404 NotFoundException 오류가 발생했습니다."처럼 "안내"가
    아닌 자체 제목의 오류 팝업을 띄우면서 등록 다이얼로그도 닫지 않는 사례가
    확인됐다. 이걸 already_blocked/success 어느 쪽으로도 못 잡으면 15초 뒤
    타임아웃 예외가 나 CSV 기록 전에 절차가 중단된다 — "error" 상태로 명시적으로
    구분해 호출부가 CSV에 실패로 남기고 다음 디바이스로 넘어갈 수 있게 한다.

    Returns:
        (status, message) 튜플. status는 다음 중 하나:
        "success"         — 차단 등록 성공(등록 다이얼로그가 자동으로 닫힘). message=""
        "already_blocked" — 이미 등록된 디바이스(안내 팝업 확인). message=""
        "error"           — 서버가 오류 팝업을 띄움(예: 404 NotFoundException).
                            message에 팝업 제목+본문을 담는다.
    """
    def _outcome():
        already_dialog = get_visible_dialog_by_title(page, TEXT_ALREADY_BLOCKED_DIALOG)
        try:
            if already_dialog.is_visible():
                return ("already_blocked", already_dialog)
        except Exception:
            pass

        error_found = find_visible_error_dialog(page, exclude_title=TEXT_OPEN_BLOCK_DIALOG)
        if error_found is not None:
            error_dialog, heading_text = error_found
            return ("error", (error_dialog, heading_text))

        try:
            if not dialog.is_visible():
                return ("success", None)
        except Exception:
            return ("success", None)
        return None

    result = wait_until(page, _outcome, timeout_ms=15_000, wait_ms=1_000)
    if result is None:
        raise RuntimeError("디바이스 차단 결과(성공/이미등록/오류)를 확인하지 못했습니다.")

    status, payload = result

    if status == "success":
        print("[D6] 디바이스 차단 등록 성공(등록 다이얼로그가 자동으로 닫힘).")
        return status, ""

    if status == "error":
        popup, heading_text = payload
        try:
            body_text = popup.locator(".MuiDialogContent-root").first.inner_text().strip()
        except Exception:
            body_text = ""
        message = f"{heading_text} {body_text}".strip()
        print(f"[D6] 디바이스 차단 등록 중 서버 오류가 발생했습니다: {message}")

        confirm_button = popup.get_by_role("button", name=TEXT_CONFIRM, exact=True).first
        try:
            confirm_button.wait_for(state="visible", timeout=5_000)
            confirm_button.scroll_into_view_if_needed()
            record_step_dump(page, "device_block_error_confirm_pre")
            confirm_button.click()
        except Exception:
            close_button = popup.locator("button:has(svg[name='close-modal'])").first
            close_button.click()

        def _error_closed():
            try:
                if not popup.is_visible():
                    return True
            except Exception:
                return True
            return None

        if not wait_until(page, _error_closed, timeout_ms=15_000, wait_ms=1_000):
            raise RuntimeError("오류 팝업이 닫히는 것을 확인하지 못했습니다.")

        return status, message

    popup = payload
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

    return status, ""


def save_device_ban_history(uuid_value, device_id, reason, status, project_key=""):
    """이 시점에 디바이스 차단(성공/이미등록) 자체는 이미 화면에서 확정된 상태다.
    CSV 기록은 그 결과를 남기는 부가 작업일 뿐이므로, 사람이 이 파일을 엑셀 등으로
    열어봐서 Windows가 잠가 쓰기가 실패해도(PermissionError) 이미 끝난 차단 절차를
    실패로 되돌리지 않는다 — 경고만 남기고 계속 진행한다."""
    BAN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{project_key}" if project_key else ""
    csv_path = BAN_HISTORY_DIR / f"device_ban_history{suffix}.csv"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = ["timestamp", "uuid", "device_id", "reason", "status"]
    row = [ts, uuid_value, device_id, reason, status]
    try:
        write_header = not csv_path.exists()
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(header)
            writer.writerow(row)
        print(f"  디바이스 차단 기록 저장: {csv_path} (status={status})")
    except OSError as exc:
        print(
            f"  [경고] 디바이스 차단 기록 CSV 저장 실패(파일이 다른 프로그램(엑셀 등)에서 "
            f"열려 있을 수 있음, 차단 자체는 이미 완료됨) — {csv_path}: {exc}"
        )


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
    dialog = open_block_register_dialog(page, step_name="device_block_open_dialog_pre")
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
            dialog = open_block_register_dialog(page, step_name="device_block_open_dialog_pre")
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
        # 2026-07-03: 등록 버튼 클릭(비가역 행동)은 이미 끝난 뒤이므로, 결과 확인이
        # 실패(타임아웃 등)해도 그 사실을 "기록 없이 사라짐"이 아니라 "uncertain"으로
        # CSV에 남긴다(원칙 11: 비가역 클릭 이후 확인 실패는 불확실로 멈추고 사람이
        # 확인할 수 있게 흔적을 남긴다). 이 uuid의 재시도 자체는 기존과 동일하게
        # retry_with_recovery로 계속 진행되므로 여기서는 기록만 남기고 그대로 재발생시킨다.
        try:
            status, error_message = confirm_device_result_popup(page, dialog)
        except Exception as exc:
            save_device_ban_history(
                uuid_value, device_id, f"{reason_text} | uncertain={exc}", "uncertain", project_key
            )
            print(
                f"  [불확실] device_id={device_id}: 등록 버튼 클릭 후 결과 확인에 실패했습니다"
                f"(실제 차단 여부 불명, uncertain으로 기록) — {exc}"
            )
            raise
        history_reason = f"{reason_text} | error={error_message}" if error_message else reason_text
        save_device_ban_history(uuid_value, device_id, history_reason, status, project_key)
        results.append(
            {"uuid": uuid_value, "device_id": device_id, "status": status, "error": error_message}
        )
        if status == "error":
            print(f"  [스킵] device_id={device_id}: 서버 오류로 이 디바이스는 건너뛰고 다음 디바이스로 진행합니다.")

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
    """이 시점에 유저 차단(성공/이미등록) 자체는 이미 화면에서 확정된 상태다.
    CSV 기록은 그 결과를 남기는 부가 작업일 뿐이므로, 사람이 이 파일을 엑셀 등으로
    열어봐서 Windows가 잠가 쓰기가 실패해도(PermissionError) 이미 끝난 차단 절차를
    실패로 되돌리지 않는다 — 경고만 남기고 계속 진행한다(디바이스 차단으로 이어짐)."""
    BAN_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{project_key}" if project_key else ""
    csv_path = BAN_HISTORY_DIR / f"ban_history{suffix}.csv"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = ["timestamp", "uuid", "period_days", "reason", "rank_delete", "status"]
    row = [ts, uuid_value, str(period_days), reason, str(rank_delete), status]
    try:
        write_header = not csv_path.exists()
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(header)
            writer.writerow(row)
        print(f"  차단 기록 저장: {csv_path} (status={status})")
    except OSError as exc:
        print(
            f"  [경고] 차단 기록 CSV 저장 실패(파일이 다른 프로그램(엑셀 등)에서 "
            f"열려 있을 수 있음, 차단 자체는 이미 완료됨) — {csv_path}: {exc}"
        )


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
    skip_navigation: bool = False,
):
    """skip_navigation=True면 직전 UUID 처리가 남겨둔 '접근 차단' 탭 화면을 그대로 재사용해
    초기화면 재진입(prepare_console_project)/유저 접근 이동/탭 전환을 건너뛰고 바로
    '접근 차단 등록' 버튼부터 클릭한다. 호출부(main)는 이 uuid의 첫 시도에서만, 그리고
    직전 uuid가 성공했을 때만 True를 넘긴다 — 실패해 재시도로 들어가면 항상 False로
    전체 절차(초기화면부터)를 다시 밟는다(2026-07-02 원칙 7: retry_with_recovery의
    action은 재시도 시 메뉴/탭/팝업 열기를 포함한 전체 절차여야 함)."""
    if skip_navigation:
        print("[1~5] 직전 UUID 처리 화면('접근 차단' 탭)을 재사용합니다(초기화면 재진입 생략).")
        dialog = open_block_register_dialog(page)
    else:
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
    # 2026-07-03: 디바이스 차단과 동일한 이유로, 등록 버튼 클릭 후 결과 확인이
    # 실패하면(타임아웃 등) 기록 없이 사라지지 않도록 "uncertain"으로 CSV에 남긴다
    # (원칙 11). 이 uuid의 재시도 자체는 기존과 동일하게 retry_with_recovery로
    # 계속 진행되므로 여기서는 기록만 남기고 그대로 재발생시킨다.
    try:
        block_status = confirm_result_popup(page)
    except Exception as exc:
        save_ban_history(
            uuid_value, period_days, f"{reason_text} | uncertain={exc}", remove_rank, "uncertain", project_key
        )
        print(
            f"  [불확실] uuid={uuid_value}: 유저 차단 등록 버튼 클릭 후 결과 확인에 실패했습니다"
            f"(실제 차단 여부 불명, uncertain으로 기록) — {exc}"
        )
        raise
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

    if args.uuid.strip():
        uuid_list = [u.strip() for u in args.uuid.split(",") if u.strip()]
        uuid_source = "--uuid 직접 지정"
    else:
        uuid_csv_path = Path(args.uuid_csv)
        uuid_list = load_uuid_list_from_csv(uuid_csv_path)
        uuid_source = f"CSV({uuid_csv_path})"
    if not uuid_list:
        raise SystemExit("[오류] 처리할 UUID가 없습니다.")

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    print("=" * 60)
    print(" Console console user block helper")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"UUID 출처  : {uuid_source}")
    print(f"대상 UUID  : {len(uuid_list)}건 — {', '.join(uuid_list)}")
    print(f"차단 기간  : {args.period_days}")
    print(f"차단 사유  : {args.reason}")
    print(f"리더보드 삭제: {not args.skip_rank_delete}")
    print(f"디바이스 차단: {not args.skip_device_block} (최하단 {args.device_ban_count}개)")
    print(f"시작 URL   : {args.start_url}")
    print(f"프로젝트명 : {args.project_name}")
    print(f"최대 재시도: {args.retries}회/UUID (개별 오류 시 해당 UUID 절차 전체를 처음부터 재시도, 소진 시 다음 UUID로 진행)")

    batch_results = []  # [{"uuid", "succeeded", "error", "result_summary"}]

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )

        page = context.pages[0] if context.pages else context.new_page()
        page = select_target_page(context, page)

        previous_uuid_ok = False

        try:
            for uuid_index, uuid_value in enumerate(uuid_list, start=1):
                print(f"\n{'=' * 60}")
                print(f" [{uuid_index}/{len(uuid_list)}] UUID={uuid_value} 처리 시작")
                print("=" * 60)

                uuid_succeeded = False
                uuid_error = ""
                uuid_result_summary = None

                # 유저/디바이스 차단은 '이미 등록됨' 판정이 실제 화면 문구까지 확인하므로
                # 멱등적이다: 이전 시도가 등록 이후 단계(예: 결과 팝업 대기)에서 실패해도
                # 재시도가 처음부터(prepare_console_project의 page.goto) 다시 진행하며
                # 이미 등록된 항목은 '이미 등록됨'으로 정확히 인식하고 넘어간다.
                # 따라서 페이지 로딩 지연·값 불일치 등 개별 오류는 이 UUID의 절차 전체
                # 재시도로 흡수하고, 재시도를 모두 소진해도 이 UUID만 건너뛰고
                # 다음 UUID로 계속 진행한다(하나의 실패가 전체 배치를 막지 않는다).
                attempt_counter = {"count": 0}

                def _run_uuid_block():
                    attempt_counter["count"] += 1
                    if attempt_counter["count"] > 1:
                        print(
                            f"\n[재시도 {attempt_counter['count']}/{max(1, args.retries)}] "
                            f"uuid={uuid_value} 절차를 처음부터 다시 시작합니다."
                        )
                    # 이 uuid의 첫 시도이고, 직전 uuid가 성공해 화면이 '접근 차단' 탭에
                    # 남아 있다고 신뢰할 수 있을 때만 초기화면 재진입을 건너뛴다.
                    # 재시도(첫 시도 실패 후)는 항상 전체 절차를 다시 밟는다.
                    skip_navigation = (
                        attempt_counter["count"] == 1
                        and uuid_index > 1
                        and previous_uuid_ok
                    )
                    return run_user_block(
                        page=page,
                        uuid_value=uuid_value,
                        reason_text=args.reason,
                        period_days=args.period_days,
                        remove_rank=not args.skip_rank_delete,
                        explicit_project_base=args.project_base,
                        start_url=args.start_url,
                        project_name=args.project_name,
                        project_key=project_key,
                        skip_device_block=args.skip_device_block,
                        device_ban_count=args.device_ban_count,
                        skip_navigation=skip_navigation,
                    )

                def _recover_uuid_block():
                    nonlocal page
                    page = select_target_page(context, page)
                    prepare_console_project(
                        page=page,
                        explicit_project_base=args.project_base,
                        start_url=args.start_url,
                        project_name=args.project_name,
                    )

                try:
                    uuid_result_summary = retry_with_recovery(
                        action=_run_uuid_block,
                        recovery=_recover_uuid_block,
                        label=f"uuid={uuid_value} 차단 절차 재시도",
                        recovery_desc=f"콘솔 초기화면({args.start_url})/프로젝트 선택부터 다시 준비합니다.",
                        max_retries=max(1, args.retries),
                    )
                    uuid_succeeded = True
                    uuid_error = ""
                    if attempt_counter["count"] > 1:
                        uuid_result_summary["attempts_used"] = attempt_counter["count"]
                except Exception as exc:
                    uuid_error = str(exc)
                    print(
                        f"[스킵] uuid={uuid_value}: 재시도를 모두 소진해 "
                        f"이 UUID는 건너뛰고 다음 UUID로 진행합니다. ({uuid_error})"
                    )

                if uuid_succeeded:
                    print(f"\n=== uuid={uuid_value} 완료 ===")
                    for key, value in uuid_result_summary.items():
                        print(f"  {key}: {value}")

                previous_uuid_ok = uuid_succeeded

                batch_results.append(
                    {
                        "uuid": uuid_value,
                        "succeeded": uuid_succeeded,
                        "error": uuid_error,
                        "result_summary": uuid_result_summary,
                    }
                )

                try:
                    page = select_target_page(context, page)
                    save_artifacts(
                        page=page,
                        out_dir=out_dir,
                        succeeded=uuid_succeeded,
                        result_summary=uuid_result_summary,
                        error_message=uuid_error,
                    )
                except Exception as exc:
                    print(f"  (uuid={uuid_value} 아티팩트 저장 중 오류: {exc})")

            success_count = sum(1 for r in batch_results if r["succeeded"])
            fail_count = len(batch_results) - success_count
            print(f"\n{'=' * 60}")
            print(f" 전체 배치 완료: 성공 {success_count}건 / 실패(스킵) {fail_count}건")
            print("=" * 60)
            for r in batch_results:
                status = "성공" if r["succeeded"] else "실패(스킵)"
                line = f"  [{status}] {r['uuid']}"
                if not r["succeeded"] and r["error"]:
                    line += f" — {r['error']}"
                print(line)

            if args.hold_seconds > 0:
                print(f"\n[16] {args.hold_seconds}초 대기 후 종료합니다.")
                page.wait_for_timeout(args.hold_seconds * 1_000)

        except Exception as exc:
            # 개별 UUID 처리 루프 바깥(예: 브라우저/컨텍스트 초기화)에서 발생한
            # 예기치 못한 예외 — 배치 전체를 계속할 수 없는 경우만 여기로 온다.
            print(f"\n[오류] 배치 처리 중 예기치 못한 오류로 중단되었습니다: {exc}")
            try:
                page = select_target_page(context, page)
                save_artifacts(
                    page=page,
                    out_dir=out_dir,
                    succeeded=False,
                    result_summary=None,
                    error_message=str(exc),
                )
            except Exception as save_exc:
                print(f"  (아티팩트 저장 중 오류: {save_exc})")
            context.close()
            sys.exit(1)

        context.close()

    if not batch_results or any(not r["succeeded"] for r in batch_results):
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n사용자 요청으로 종료했습니다.")
        sys.exit(130)
