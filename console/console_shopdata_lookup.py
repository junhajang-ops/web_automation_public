# -*- coding: utf-8 -*-
"""
Console console ShopData lookup smoke test.

Scope:
- Open the Console console
- Select the target project
- Open Game Info > Data
- Select the ShopData table
- Search by user UUID
- Verify that at least one result row contains the UUID
- Open the detail popup and inspect PurchaseCode / Count
- Enter edit mode and change the matched Count line to 0 without saving
"""

import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path

from console_user_search_test import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    MIN_STEP_WAIT_MS,
    click_login_if_needed,
    ensure_uuid_dropdown,
    load_playwright,
    prepare_console_project,
    safe_wait_for_load,
    select_target_page,
    snap_and_check_ui,
    step_pause,
    wait_for_visible,
)
from test_config import TEST_PURCHASE_CODE, TEST_TABLE_NAME, TEST_UUID

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = "dumps_console_shopdata_lookup"
DEFAULT_UUID = TEST_UUID
DEFAULT_TABLE_NAME = TEST_TABLE_NAME
DEFAULT_PURCHASE_CODE = TEST_PURCHASE_CODE
POLL_WAIT_MS = 1_000
HIGHLIGHT_WAIT_MS = 3_000


def parse_args():
    parser = argparse.ArgumentParser(
        description="Console console ShopData UUID lookup smoke test"
    )
    parser.add_argument(
        "--uuid",
        default=DEFAULT_UUID,
        help=f"Target user UUID (default: {DEFAULT_UUID})",
    )
    parser.add_argument(
        "--table-name",
        default=DEFAULT_TABLE_NAME,
        help=f"Game info table name (default: {DEFAULT_TABLE_NAME})",
    )
    parser.add_argument(
        "--purchase-code",
        default=DEFAULT_PURCHASE_CODE,
        help=f"Exact purchase code to inspect (default: {DEFAULT_PURCHASE_CODE})",
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
        help="Optional full project base URL, e.g. https://console.example.io/ko/project/...",
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


def open_game_info_menu(page):
    print("[4] 사이드 메뉴에서 '게임 정보'로 이동합니다.")
    game_info_link = page.locator("a#baseGameInfo, a[href*='/baseGameInfo']").first
    game_info_link.wait_for(state="visible", timeout=15_000)
    game_info_link.scroll_into_view_if_needed()
    game_info_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    game_info_link.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def open_game_info_data_tab(page):
    print("[5] '게임 정보' 화면에서 '데이터' 탭으로 이동합니다.")
    data_tab = page.locator(
        ".ui.pointing.secondary.menu a.item",
        has_text="데이터",
    ).first
    data_tab.wait_for(state="visible", timeout=15_000)
    data_tab.scroll_into_view_if_needed()
    data_tab.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("form").first.wait_for(state="visible", timeout=15_000)
    step_pause(page)


def get_table_dropdown(page):
    table_field = page.locator("form .inline.field").filter(
        has_text="테이블 선택"
    ).first
    table_field.wait_for(state="visible", timeout=15_000)
    return table_field.locator("[role='listbox']").first


def open_table_dropdown(page):
    table_field = page.locator("form .inline.field").filter(
        has_text="테이블 선택"
    ).first
    table_field.wait_for(state="visible", timeout=15_000)
    dropdown = table_field.locator("[role='listbox']").first

    for _ in range(3):
        expanded = (dropdown.get_attribute("aria-expanded") or "").lower() == "true"
        if not expanded:
            dropdown.scroll_into_view_if_needed()
            dropdown.click()
            step_pause(page)

        search_input = table_field.locator(
            "[role='listbox'][aria-expanded='true'] .menu input[type='text']"
        ).first
        if wait_for_visible(search_input, 3_000):
            search_input.scroll_into_view_if_needed()
            search_input.click()
            step_pause(page)
            return dropdown, search_input
    else:
        raise RuntimeError("테이블 선택 드롭다운을 열지 못했습니다.")


def ensure_detail_search_open(page):
    if wait_for_visible(page.locator("input[name='defaultSearchValue']").first, 1_000):
        return

    title = page.locator(".accordion .title", has_text="상세 검색").first
    title.wait_for(state="visible", timeout=15_000)
    title.click()
    step_pause(page)
    page.locator("input[name='defaultSearchValue']").first.wait_for(
        state="visible",
        timeout=15_000,
    )


def ensure_table_selected(page, table_name):
    print(f"[8] 테이블 드롭다운을 열고 '{table_name}'를 입력합니다.")
    dropdown = get_table_dropdown(page)
    current_text = dropdown.locator(".divider.text, .divider.default.text").first
    if table_name in current_text.inner_text().strip():
        return

    dropdown, search_input = open_table_dropdown(page)
    search_input.fill("")
    search_input.fill(table_name)
    step_pause(page)

    option = dropdown.locator("[role='option']").filter(has_text=table_name).first
    if wait_for_visible(option, 5_000):
        option.scroll_into_view_if_needed()
        option.click()
    else:
        search_input.press("Enter")
    step_pause(page)

    deadline = time.time() + 10
    while time.time() < deadline:
        if table_name in current_text.inner_text().strip():
            return
        page.wait_for_timeout(POLL_WAIT_MS)

    raise RuntimeError(f"테이블 선택이 완료되지 않았습니다: {table_name}")


def fill_shopdata_uuid_filter(page, uuid_value):
    print("[6] 상세 검색을 열고 검색 대상을 '유저 UUID'로 맞춥니다.")
    ensure_detail_search_open(page)
    ensure_uuid_dropdown(page)

    print(f"[7] UUID 입력: {uuid_value}")
    search_input = page.locator("input[name='defaultSearchValue']").first
    search_input.wait_for(state="visible", timeout=15_000)
    search_input.fill("")
    search_input.fill(uuid_value)
    step_pause(page)


def click_shopdata_search_button(page):
    print("[9] 검색 버튼을 클릭합니다.")
    page.locator("form button[type='submit']").first.click()
    safe_wait_for_load(page, "networkidle", 5_000)
    step_pause(page)


def find_shopdata_result_row(page, uuid_value, wait_timeout_ms):
    deadline = time.time() + (wait_timeout_ms / 1000)
    while time.time() < deadline:
        cell = page.locator("td#gamer_id p", has_text=uuid_value).first
        if wait_for_visible(cell, 700):
            row = cell.locator("xpath=ancestor::tr[1]").first
            found_text = cell.inner_text().strip()
            if found_text != uuid_value:
                raise RuntimeError(f"Unexpected ShopData gamer_id text: {found_text}")
            return row
        page.wait_for_timeout(POLL_WAIT_MS)
    return None


def wait_for_shopdata_result_row(page, uuid_value, timeout_error):
    row = find_shopdata_result_row(page, uuid_value, 15_000)
    if row is None:
        raise timeout_error(f"ShopData result row not found for UUID: {uuid_value}")
    return row


def get_row_cell_text(row, cell_id):
    cell = row.locator(f"td#{cell_id} p").first
    if not wait_for_visible(cell, 1_000):
        return ""
    return cell.inner_text().strip()


def open_top_shopdata_detail(page, result_row, uuid_value, timeout_error):
    print("[10] 조회된 결과의 최상단 UUID 링크를 클릭해 상세 팝업을 엽니다.")
    uuid_link = result_row.locator("td#gamer_id p._link_16cvg_110, td#gamer_id p").first
    uuid_link.wait_for(state="visible", timeout=15_000)
    uuid_link.scroll_into_view_if_needed()
    uuid_link.click()
    step_pause(page)

    dialog = page.locator("[role='dialog']").first
    dialog.wait_for(state="visible", timeout=15_000)
    step_pause(page)

    header = dialog.locator(".header").first
    if not wait_for_visible(header, 3_000):
        raise timeout_error(f"ShopData detail popup did not open for UUID: {uuid_value}")

    header_text = header.inner_text().strip()
    if uuid_value not in header_text:
        raise RuntimeError(f"Unexpected ShopData detail header: {header_text}")

    confirm_button = dialog.locator("button", has_text="확인").first
    if not wait_for_visible(confirm_button, 5_000):
        raise timeout_error(f"ShopData detail popup confirm button not visible for UUID: {uuid_value}")

    return dialog


def click_detail_edit_button(page, dialog):
    print("[12] 상세 팝업 상단의 '수정' 버튼을 눌러 편집 모드로 전환합니다.")
    edit_button = dialog.locator("button", has_text="수정").first
    edit_button.wait_for(state="visible", timeout=15_000)
    edit_button.scroll_into_view_if_needed()
    edit_button.click()
    step_pause(page)
    dialog.locator("input[name$='.columnName']").first.wait_for(
        state="visible",
        timeout=15_000,
    )
    step_pause(page)


def refresh_detail_popup_with_mouse_scroll(page, dialog):
    print("[11] 상세 팝업 안에서 아래로 내렸다가 다시 올려 로딩을 깨웁니다.")
    content = dialog.locator(".content").first
    content.wait_for(state="visible", timeout=15_000)
    step_pause(page)

    box = content.bounding_box()
    if not box:
        raise RuntimeError("상세 팝업 스크롤 영역 위치를 확인하지 못했습니다.")

    mouse_x = box["x"] + min(120, box["width"] / 2)
    mouse_y = box["y"] + min(160, box["height"] / 2)
    page.mouse.move(mouse_x, mouse_y)
    step_pause(page)

    for delta in (1_200, 1_200, 1_200, -1_200, -1_200, -1_200):
        page.mouse.wheel(0, delta)
        step_pause(page)


def get_detail_column_block(dialog, column_name):
    blocks = dialog.locator("form > div")
    count = blocks.count()
    for index in range(count):
        block = blocks.nth(index)
        name_locator = block.locator("div.inline.field").nth(0).locator("p").first
        if not wait_for_visible(name_locator, 500):
            continue
        if name_locator.inner_text().strip() == column_name:
            return block
    raise RuntimeError(f"상세 팝업에서 '{column_name}' 컬럼을 찾지 못했습니다.")


def ensure_detail_json_editor_loaded(page, dialog, column_name):
    block = get_detail_column_block(dialog, column_name)

    for _ in range(4):
        block.scroll_into_view_if_needed()
        step_pause(page)

        editor = block.locator(".ace_editor").first
        if wait_for_visible(editor, 2_000):
            return editor

        value_area = block.locator("div.inline.field").nth(2)
        value_box = value_area.bounding_box()
        if value_box:
            page.mouse.move(
                value_box["x"] + min(120, value_box["width"] / 2),
                value_box["y"] + min(120, value_box["height"] / 2),
            )
            step_pause(page)
            page.mouse.wheel(0, 800)
            step_pause(page)
            page.mouse.wheel(0, -800)
            step_pause(page)

    raise RuntimeError(f"상세 팝업에서 '{column_name}' 편집기 로딩을 확인하지 못했습니다.")


def get_edit_mode_column_block(dialog, column_name):
    column_inputs = dialog.locator("input[name$='.columnName']")
    count = column_inputs.count()
    for index in range(count):
        column_input = column_inputs.nth(index)
        if not wait_for_visible(column_input, 500):
            continue

        current_value = (column_input.input_value() or "").strip()
        if current_value != column_name:
            continue

        input_name = column_input.get_attribute("name") or ""
        match = re.search(r"dataSet\.(\d+)\.columnName$", input_name)
        if not match:
            continue

        dataset_index = match.group(1)
        block = column_input.locator(
            "xpath=ancestor::div[contains(@class, '_columnBorder-edit-modal')][1]"
        ).first
        if block.count() == 0:
            continue
        return dataset_index, block

    raise RuntimeError(f"Edit mode column not found: {column_name}")


def ensure_edit_mode_json_editor_loaded(page, dialog, column_name):
    dataset_index, block = get_edit_mode_column_block(dialog, column_name)
    editor = block.locator(f"[id='dataSet.{dataset_index}.dataValue']").first

    for _ in range(5):
        block.scroll_into_view_if_needed()
        step_pause(page)

        if wait_for_visible(editor, 2_000):
            return editor

        value_area = block.locator("div.inline.field").nth(2)
        value_box = value_area.bounding_box()
        if value_box:
            page.mouse.move(
                value_box["x"] + min(120, value_box["width"] / 2),
                value_box["y"] + min(120, value_box["height"] / 2),
            )
            step_pause(page)
            page.mouse.wheel(0, 800)
            step_pause(page)
            page.mouse.wheel(0, -800)
            step_pause(page)

    raise RuntimeError(f"Edit mode editor not loaded for column: {column_name}")


def read_ace_editor_value(editor):
    raw_value = editor.evaluate(
        """
        (element) => {
          const fromEnv = element?.env?.editor?.getValue?.();
          if (typeof fromEnv === "string" && fromEnv.trim()) {
            return fromEnv;
          }

          const aceGlobal = window.ace;
          if (aceGlobal?.edit) {
            try {
              const editorInstance = aceGlobal.edit(element);
              const fromAce = editorInstance?.getValue?.();
              if (typeof fromAce === "string" && fromAce.trim()) {
                return fromAce;
              }
            } catch (error) {
            }
          }

          return Array.from(element.querySelectorAll(".ace_line"))
            .map((line) => line.innerText || "")
            .join("\\n");
        }
        """
    )
    return (raw_value or "").strip()


def split_editor_lines(raw_value, column_name):
    if not raw_value:
        raise RuntimeError(f"'{column_name}' 값이 비어 있습니다.")
    return raw_value.splitlines()


def find_exact_match_line_number(lines, purchase_code):
    exact_targets = {
        f"{json.dumps(purchase_code, ensure_ascii=False)},",
        json.dumps(purchase_code, ensure_ascii=False),
    }
    for index, line in enumerate(lines, start=1):
        if line.strip() in exact_targets:
            return index
    raise RuntimeError(
        f"PurchaseCode 에디터에서 정확히 일치하는 코드가 없습니다: {purchase_code}"
    )


def parse_editor_line_value(lines, line_number, column_name):
    if line_number < 1 or line_number > len(lines):
        raise RuntimeError(
            f"'{column_name}' 줄번호가 범위를 벗어났습니다: line={line_number}, total={len(lines)}"
        )

    raw_line = lines[line_number - 1].strip()
    if not raw_line or raw_line in {"[", "]"}:
        raise RuntimeError(
            f"'{column_name}' {line_number}번째 줄이 실제 데이터 값이 아닙니다: {raw_line!r}"
        )

    normalized = raw_line[:-1] if raw_line.endswith(",") else raw_line
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"'{column_name}' {line_number}번째 줄 파싱에 실패했습니다: {raw_line}"
        ) from exc


def build_replacement_line(lines, line_number, new_value):
    raw_line = lines[line_number - 1]
    indent_width = len(raw_line) - len(raw_line.lstrip())
    indent = raw_line[:indent_width]
    suffix = "," if raw_line.rstrip().endswith(",") else ""
    serialized = json.dumps(new_value, ensure_ascii=False)
    return f"{indent}{serialized}{suffix}"


def highlight_ace_editor_line(editor, line_number):
    highlighted = editor.evaluate(
        """
        (element, targetLine) => {
          const aceGlobal = window.ace;
          let editorInstance = element?.env?.editor;

          if (!editorInstance && aceGlobal?.edit) {
            try {
              editorInstance = aceGlobal.edit(element);
            } catch (error) {
            }
          }

          if (!editorInstance) {
            return false;
          }

          editorInstance.focus();
          editorInstance.gotoLine(targetLine, 0, true);
          editorInstance.scrollToLine(targetLine - 1, true, true, () => {});

          if (editorInstance.selection?.selectLine) {
            editorInstance.selection.selectLine();
          }

          editorInstance.renderer?.scrollCursorIntoView?.();
          return true;
        }
        """,
        line_number,
    )
    if not highlighted:
        raise RuntimeError(f"Ace editor {line_number}번째 줄 하이라이트에 실패했습니다.")


def replace_ace_editor_line(editor, line_number, replacement_line):
    result = editor.evaluate(
        """
        (element, payload) => {
          const aceGlobal = window.ace;
          let editorInstance = element?.env?.editor;

          if (!editorInstance && aceGlobal?.edit) {
            try {
              editorInstance = aceGlobal.edit(element);
            } catch (error) {
            }
          }

          if (!editorInstance) {
            return { ok: false, reason: "editor_instance_missing" };
          }

          const row = payload.targetLine - 1;
          const session = editorInstance.session;
          if (!session || row < 0 || row >= session.getLength()) {
            return {
              ok: false,
              reason: "line_out_of_range",
              lineCount: session?.getLength?.() ?? null,
            };
          }

          const currentLine = session.getLine(row);
          const range_ctor = aceGlobal?.require?.("ace/range")?.Range;

          if (range_ctor) {
            session.replace(
              new range_ctor(row, 0, row, currentLine.length),
              payload.replacementLine,
            );
          } else {
            const document = session.getDocument?.();
            if (!document?.removeFullLines || !document?.insertFullLines) {
              return { ok: false, reason: "range_and_document_missing" };
            }
            document.removeFullLines(row, row);
            document.insertFullLines(row, [payload.replacementLine]);
          }

          editorInstance.focus();
          editorInstance.gotoLine(payload.targetLine, 0, true);
          editorInstance.scrollToLine(payload.targetLine - 1, true, true, () => {});
          if (editorInstance.selection?.selectLine) {
            editorInstance.selection.selectLine();
          }
          editorInstance.renderer?.scrollCursorIntoView?.();

          return {
            ok: true,
            before: currentLine,
            after: session.getLine(row),
          };
        }
        """,
        {
            "targetLine": line_number,
            "replacementLine": replacement_line,
        },
    )
    if not result.get("ok"):
        raise RuntimeError(
            "Ace editor 줄 수정에 실패했습니다: "
            f"{result.get('reason', 'unknown_error')}"
        )
    return result


def resolve_purchase_line_and_count(page, dialog, purchase_code):
    print(f"[12] 상세 컬럼에서 '{purchase_code}'의 정확 일치 위치를 확인합니다.")
    purchase_editor = ensure_detail_json_editor_loaded(page, dialog, "PurchaseCode")
    purchase_lines = split_editor_lines(
        read_ace_editor_value(purchase_editor),
        "PurchaseCode",
    )
    purchase_line_number = find_exact_match_line_number(purchase_lines, purchase_code)
    highlight_ace_editor_line(purchase_editor, purchase_line_number)
    page.wait_for_timeout(HIGHLIGHT_WAIT_MS)

    count_editor = ensure_detail_json_editor_loaded(page, dialog, "Count")
    count_lines = split_editor_lines(
        read_ace_editor_value(count_editor),
        "Count",
    )
    highlight_ace_editor_line(count_editor, purchase_line_number)
    page.wait_for_timeout(HIGHLIGHT_WAIT_MS)
    purchase_count = parse_editor_line_value(
        count_lines,
        purchase_line_number,
        "Count",
    )

    return purchase_line_number, purchase_count


def edit_count_line_to_zero(page, dialog, purchase_line_number):
    print(
        f"[14] 편집 모드에서 Count {purchase_line_number}번 줄을 0으로 변경하고 저장 없이 유지합니다."
    )
    count_lines = split_editor_lines(
        read_ace_editor_value(count_editor),
        "Count",
    )
    replacement_line = build_replacement_line(count_lines, purchase_line_number, 0)
    replace_result = replace_ace_editor_line(
        count_editor,
        purchase_line_number,
        replacement_line,
    )
    step_pause(page)
    highlight_ace_editor_line(count_editor, purchase_line_number)
    page.wait_for_timeout(HIGHLIGHT_WAIT_MS)

    updated_lines = split_editor_lines(
        read_ace_editor_value(count_editor),
        "Count",
    )
    updated_value = parse_editor_line_value(
        updated_lines,
        purchase_line_number,
        "Count",
    )
    if updated_value != 0:
        raise RuntimeError(
            f"Count {purchase_line_number}번 줄 수정 결과가 0이 아닙니다: {updated_value}"
        )

    return {
        "count_line_before_edit": replace_result.get("before", "").strip(),
        "count_line_after_edit": replace_result.get("after", "").strip(),
        "edited_count_value": updated_value,
    }


def edit_count_line_to_zero_in_edit_mode(page, dialog, purchase_line_number):
    print(
        f"[14] 편집 모드에서 Count {purchase_line_number}번 줄을 0으로 변경하고 저장 없이 유지합니다."
    )
    count_editor = ensure_edit_mode_json_editor_loaded(page, dialog, "Count")
    count_lines = split_editor_lines(
        read_ace_editor_value(count_editor),
        "Count",
    )
    replacement_line = build_replacement_line(count_lines, purchase_line_number, 0)
    replace_result = replace_ace_editor_line(
        count_editor,
        purchase_line_number,
        replacement_line,
    )
    step_pause(page)
    highlight_ace_editor_line(count_editor, purchase_line_number)
    page.wait_for_timeout(HIGHLIGHT_WAIT_MS)

    updated_lines = split_editor_lines(
        read_ace_editor_value(count_editor),
        "Count",
    )
    updated_value = parse_editor_line_value(
        updated_lines,
        purchase_line_number,
        "Count",
    )
    if updated_value != 0:
        raise RuntimeError(
            f"Count {purchase_line_number}번 줄 수정 결과가 0이 아닙니다: {updated_value}"
        )

    return {
        "count_line_before_edit": replace_result.get("before", "").strip(),
        "count_line_after_edit": replace_result.get("after", "").strip(),
        "edited_count_value": updated_value,
    }


def collect_shopdata_result_summary(page, uuid_value):
    rows = page.locator("tbody tr")
    row_count = rows.count()
    matching_count = page.locator("td#gamer_id p", has_text=uuid_value).count()
    return {
        "row_count": row_count,
        "matching_count": matching_count,
    }


def run_shopdata_lookup(
    page,
    uuid_value,
    table_name,
    purchase_code,
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
    open_game_info_menu(page)
    open_game_info_data_tab(page)
    snap_and_check_ui(page, "gameinfo_data")
    fill_shopdata_uuid_filter(page, uuid_value)
    ensure_table_selected(page, table_name)
    click_shopdata_search_button(page)

    result_row = wait_for_shopdata_result_row(page, uuid_value, timeout_error)
    step_pause(page)
    snap_and_check_ui(page, "shopdata_results")
    summary = collect_shopdata_result_summary(page, uuid_value)
    summary["first_inDate"] = get_row_cell_text(result_row, "inDate")
    summary["first_updatedAt"] = get_row_cell_text(result_row, "updatedAt")
    dialog = open_top_shopdata_detail(page, result_row, uuid_value, timeout_error)
    refresh_detail_popup_with_mouse_scroll(page, dialog)
    snap_and_check_ui(page, "shopdata_detail_popup")
    purchase_line_number, purchase_count = resolve_purchase_line_and_count(
        page,
        dialog,
        purchase_code,
    )
    click_detail_edit_button(page, dialog)
    snap_and_check_ui(page, "shopdata_detail_edit")
    edit_summary = edit_count_line_to_zero_in_edit_mode(
        page,
        dialog,
        purchase_line_number,
    )
    summary["purchase_code"] = purchase_code
    summary["purchase_line_number"] = purchase_line_number
    summary["purchase_count"] = purchase_count
    summary.update(edit_summary)
    print(
        "[13] ShopData 조회 성공: "
        f"rows={summary['row_count']}, matches={summary['matching_count']}, "
        f"first_inDate={summary['first_inDate']}, "
        f"purchase_line_number={summary['purchase_line_number']}, "
        f"purchase_count={summary['purchase_count']}, "
        f"edited_count_value={summary['edited_count_value']}"
    )
    return summary


def save_artifacts(
    page,
    out_dir,
    uuid_value,
    table_name,
    succeeded,
    result_summary=None,
    error_message="",
):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_shopdata_lookup_{ts}"
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
        f"uuid={uuid_value}",
        f"table={table_name}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if result_summary:
        for key in [
            "row_count",
            "matching_count",
            "first_inDate",
            "first_updatedAt",
            "purchase_code",
            "purchase_line_number",
            "purchase_count",
            "count_line_before_edit",
            "count_line_after_edit",
            "edited_count_value",
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

    print(f"[10] 조회 화면을 {hold_seconds}초 동안 유지합니다.")
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        page.wait_for_timeout(POLL_WAIT_MS)


def main():
    args = parse_args()
    sync_playwright, timeout_error = load_playwright()

    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" Console ShopData lookup smoke test")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"프로젝트명 : {args.project_name}")
    print(f"테이블명   : {args.table_name}")
    print(f"상품 코드  : {args.purchase_code}")
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
            result_summary = run_shopdata_lookup(
                page=page,
                uuid_value=args.uuid,
                table_name=args.table_name,
                purchase_code=args.purchase_code,
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
                    table_name=args.table_name,
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
