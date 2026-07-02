# -*- coding: utf-8 -*-
"""
Console console webshop history UUID lookup.

Scope:
- Select the target project
- Open '지급 내역' from the side menu
- Search by payer UUID (`searchType=buyer`)
- Collect webshop history rows from the on-screen grid
- Summarize exact `payitem_숫자` items while excluding `mission` variants
- Save debug artifacts when run standalone
"""

import argparse
import re
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
from test_config import TEST_UUID, apply_title_profile

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = "dumps_console_webshop_history"
DEFAULT_UUID = TEST_UUID
POLL_WAIT_MS = 1_000
GRID_SCROLL_STEP_PX = 900
GRID_SCROLL_IDLE_LIMIT = 3
PAYITEM_ITEM_RE = re.compile(r"(?:^|_)payitem_(\d+)$", re.I)
RETRY_MAX_RETRIES = get_retry_max_retries()
# UUID별 지급 내역 조회 결과 유무(0건/N건)에 따라 그리드의 gridcell/rowgroup role 존재 여부가
# 바뀐다. 여러 UUID를 순회하며 같은 스텝 이름으로 지문을 비교하므로, 이 role 유무 차이는
# 실제 UI 구조 변경이 아니라 검색 결과 데이터 차이일 뿐이다.
WEBSHOP_HISTORY_GRID_IGNORE_PATTERNS = [
    r"role: gridcell$",
    r"role: rowgroup$",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Console console webshop history UUID lookup")
    parser.add_argument("--uuid", default=DEFAULT_UUID, help=f"Target user UUID (default: {DEFAULT_UUID})")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--project-base", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--title", default="", metavar="NAME", help="Title env profile to apply (example: gametitle)")
    parser.add_argument("--gametitle", action="store_true", help="Shortcut for --title gametitle")
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    return parser.parse_args()


def open_webshop_history_menu(page):
    print("[4] 사이드 메뉴에서 '지급 내역'으로 이동합니다.")
    history_link = page.locator("a#webshopHistory, a[href*='/webshopHistory']").first
    history_link.wait_for(state="visible", timeout=15_000)
    history_link.scroll_into_view_if_needed()
    record_step_dump(page, "webshop_history_nav_pre")
    history_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("input#searchValue").first.wait_for(state="visible", timeout=15_000)
    wait_for_webshop_history_page_render_stable(page)


def _read_visible_role_signature(page):
    return page.evaluate(
        """() => [...new Set(
            [...document.querySelectorAll('[role]')]
              .map(el => el.getAttribute('role'))
              .filter(Boolean)
        )].sort().join('|')"""
    )


def wait_for_webshop_history_page_render_stable(page, timeout_ms: int = 6_000, stable_rounds: int = 2):
    print("[4-1] 지급 내역 페이지 렌더가 안정될 때까지 기다립니다.")
    page.locator("input#searchValue").first.wait_for(state="visible", timeout=15_000)

    previous_signature = ""
    stable_count = 0

    def _render_stable():
        nonlocal previous_signature, stable_count
        current_signature = _read_visible_role_signature(page)
        if current_signature and current_signature == previous_signature:
            stable_count += 1
        else:
            previous_signature = current_signature
            stable_count = 1 if current_signature else 0
        if stable_count >= stable_rounds:
            return True
        return None

    if wait_until(page, _render_stable, timeout_ms=timeout_ms, wait_ms=POLL_WAIT_MS):
        return

    print("    (지급 내역 렌더 구성이 완전히 고정되진 않았지만 최신 상태로 진행합니다.)")


def _wait_webshop_grid_not_loading(page, timeout_ms: int = 10_000):
    progressbar = page.locator("[role='progressbar']").first

    def _not_loading():
        try:
            return not progressbar.is_visible()
        except Exception:
            return True

    wait_until(page, _not_loading, timeout_ms=timeout_ms, wait_ms=POLL_WAIT_MS)


def ensure_webshop_buyer_search_type(page):
    search_type = page.locator("input[name='searchType']").first
    search_type.wait_for(state="attached", timeout=10_000)
    current_value = (search_type.input_value() or "").strip().lower()
    if current_value == "buyer":
        return
    raise RuntimeError(f"지급 내역 검색 기준이 buyer(결제자 UUID)가 아닙니다: current='{current_value}'")


def fill_webshop_uuid_search(page, uuid_value):
    print(f"[5] 지급 내역 검색 UUID 입력: {uuid_value}")
    ensure_webshop_buyer_search_type(page)
    search_input = page.locator("input#searchValue").first
    search_input.wait_for(state="visible", timeout=15_000)
    search_input.scroll_into_view_if_needed()
    _wait_webshop_grid_not_loading(page)
    record_step_dump(page, "webshop_history_uuid_input_pre", ignore_patterns=WEBSHOP_HISTORY_GRID_IGNORE_PATTERNS)
    search_input.fill("")
    search_input.fill(uuid_value)


def click_webshop_search_button(page):
    print("[6] 지급 내역 검색 버튼을 클릭합니다.")
    search_button = page.get_by_role("button", name="검색", exact=True).first
    search_button.wait_for(state="visible", timeout=15_000)
    search_button.scroll_into_view_if_needed()
    _wait_webshop_grid_not_loading(page)
    record_step_dump(page, "webshop_history_search_submit_pre", ignore_patterns=WEBSHOP_HISTORY_GRID_IGNORE_PATTERNS)
    search_button.click()
    safe_wait_for_load(page, "networkidle", 5_000)
    _wait_webshop_grid_not_loading(page)


def set_rows_per_page(page, count: int = 100):
    print(f"[6-1] 페이지당 행 수를 {count}개로 변경합니다.")
    target_text = f"{count}개씩 보기"
    trigger = page.locator(".MuiTablePagination-select[role='combobox']").first
    if not wait_for_visible(trigger, 5_000):
        print("    (페이지 크기 드롭다운 없음 — 건너뜁니다.)")
        return
    current_text = trigger.inner_text().strip()
    if current_text == target_text:
        print("    (already selected)")
        return
    trigger.scroll_into_view_if_needed()
    record_step_dump(page, "webshop_history_rows_dd_pre")
    trigger.click()

    listbox = page.locator("ul[role='listbox']").first
    listbox.wait_for(state="visible", timeout=10_000)
    option = find_exact_text_match(listbox.locator("li[role='option']"), target_text)
    if option is None:
        raise RuntimeError(f"지급 내역 표시 개수에서 '{target_text}' 옵션을 찾지 못했습니다.")
    option.wait_for(state="visible", timeout=5_000)
    record_step_dump(page, "webshop_history_rows_option_pre")
    option.click()
    safe_wait_for_load(page, "networkidle", 5_000)

    def _rows_per_page_applied():
        if trigger.inner_text().strip() == target_text:
            return True
        return None

    if wait_until(page, _rows_per_page_applied, timeout_ms=10_000, wait_ms=POLL_WAIT_MS):
        return

    raise RuntimeError(
        f"지급 내역 표시 개수 전환 결과가 기대와 다릅니다: expected='{target_text}', actual='{trigger.inner_text().strip()}'"
    )


def _read_grid_field(row, field_name: str) -> str:
    try:
        title_node = row.locator(f"[data-field='{field_name}'] [title]").first
        title_value = title_node.get_attribute("title")
        if title_value is not None:
            return title_value.strip()
    except Exception:
        pass

    try:
        return row.locator(f"[data-field='{field_name}']").first.inner_text().strip()
    except Exception:
        return ""


def _collect_current_webshop_page_rows(page) -> list:
    collected = {}
    idle_rounds = 0

    while idle_rounds < GRID_SCROLL_IDLE_LIMIT:
        rows = page.locator("div.MuiDataGrid-row")
        visible_count = rows.count()
        added_this_round = 0

        for index in range(visible_count):
            row = rows.nth(index)
            row_data = {
                "key": _read_grid_field(row, "key"),
                "webshop": _read_grid_field(row, "webshop"),
                "orderId": _read_grid_field(row, "orderId"),
                "itemId": _read_grid_field(row, "itemId"),
                "quantity": _read_grid_field(row, "quantity"),
                "price": _read_grid_field(row, "price"),
                "buyer": _read_grid_field(row, "buyer"),
                "receiver": _read_grid_field(row, "receiver"),
                "senderNickname": _read_grid_field(row, "senderNickname"),
                "receiverNickname": _read_grid_field(row, "receiverNickname"),
                "sentAt": _read_grid_field(row, "sentAt"),
                "receivedAt": _read_grid_field(row, "receivedAt"),
            }
            row_key = "||".join(
                [
                    row_data.get("orderId", ""),
                    row_data.get("itemId", ""),
                    row_data.get("buyer", ""),
                    row_data.get("sentAt", ""),
                ]
            )
            if row_key in collected:
                continue
            collected[row_key] = row_data
            added_this_round += 1

        before_state = page.evaluate(
            """() => {
              const el = document.querySelector('.MuiDataGrid-virtualScroller');
              if (!el) return null;
              return {
                scrollTop: el.scrollTop,
                clientHeight: el.clientHeight,
                scrollHeight: el.scrollHeight
              };
            }"""
        )
        if before_state is None:
            break

        idle_rounds = idle_rounds + 1 if added_this_round == 0 else 0
        reached_bottom = (
            before_state["scrollTop"] + before_state["clientHeight"]
            >= before_state["scrollHeight"] - 4
        )
        if reached_bottom and idle_rounds > 0:
            break

        scroller = page.locator(".MuiDataGrid-virtualScroller").first
        if not wait_for_visible(scroller, 5_000):
            break
        box = scroller.bounding_box()
        if not box:
            break

        page.mouse.move(box["x"] + (box["width"] / 2), box["y"] + (box["height"] / 2))
        record_step_dump(page, "webshop_history_scroll_pre")
        page.mouse.wheel(0, GRID_SCROLL_STEP_PX)
        page.wait_for_timeout(POLL_WAIT_MS)

        after_state = page.evaluate(
            """() => {
              const el = document.querySelector('.MuiDataGrid-virtualScroller');
              if (!el) return null;
              return {
                scrollTop: el.scrollTop,
                clientHeight: el.clientHeight,
                scrollHeight: el.scrollHeight
              };
            }"""
        )
        if after_state is not None and before_state["scrollTop"] == after_state["scrollTop"]:
            idle_rounds += 1

    return list(collected.values())


def collect_all_webshop_rows(page) -> list:
    no_result_locator = page.get_by_text("검색 결과가 없습니다.", exact=False).first
    row_locator = page.locator("div.MuiDataGrid-row")
    _wait_webshop_grid_not_loading(page)
    if wait_for_visible(no_result_locator, 3_000):
        return []
    if not wait_for_visible(row_locator.first, 8_000):
        return []

    # 영수증 검증과 동일하게, 기본 페이지 크기를 넘는 행이 누락되지 않도록 먼저 100개씩 보기로 전환한다.
    set_rows_per_page(page, 100)
    page.wait_for_timeout(POLL_WAIT_MS)

    return _collect_current_webshop_page_rows(page)


def prepare_webshop_history_session(page, explicit_project_base, start_url, project_name):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    open_webshop_history_menu(page)
    return {
        "initialized": True,
    }


def _parse_payitem_value(item_id: str) -> int | None:
    item_text = (item_id or "").strip()
    if not item_text:
        return None
    if "mission" in item_text.lower():
        return None
    match = PAYITEM_ITEM_RE.search(item_text)
    if match is None:
        return None
    return int(match.group(1))


def summarize_payitem_history(
    page,
    uuid_value,
    *,
    explicit_project_base="",
    start_url=DEFAULT_START_URL,
    project_name=DEFAULT_PROJECT_NAME,
    session=None,
):
    if session is None:
        session = prepare_webshop_history_session(page, explicit_project_base, start_url, project_name)
    elif not session.get("initialized"):
        session.update(prepare_webshop_history_session(page, explicit_project_base, start_url, project_name))

    fill_webshop_uuid_search(page, uuid_value)
    click_webshop_search_button(page)
    step_and_verify_ui(page, "webshop_history_results", ignore_patterns=WEBSHOP_HISTORY_GRID_IGNORE_PATTERNS)
    rows = collect_all_webshop_rows(page)

    matched_rows = 0
    quantity_total = 0
    item_value_sum = 0

    for row in rows:
        item_value = _parse_payitem_value(row.get("itemId", ""))
        if item_value is None:
            continue
        quantity = int(re.sub(r"[^\d]", "", row.get("quantity", "")) or "1")
        matched_rows += 1
        quantity_total += quantity
        item_value_sum += item_value * quantity

    return {
        "rows": rows,
        "payitem_match_count": matched_rows,
        "payitem_quantity_total": quantity_total,
        "payitem_item_value_sum": item_value_sum,
    }


def save_artifacts(page, out_dir, uuid_value, succeeded, summary=None, error_message=""):
    lines = [
        f"success={succeeded}",
        f"uuid={uuid_value}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if summary:
        lines.extend(
            [
                f"payitem_match_count={summary.get('payitem_match_count', '')}",
                f"payitem_quantity_total={summary.get('payitem_quantity_total', '')}",
                f"payitem_item_value_sum={summary.get('payitem_item_value_sum', '')}",
                f"row_count={len(summary.get('rows', []))}",
            ]
        )
    if error_message:
        lines.append(f"error={error_message}")

    save_page_artifacts(page, out_dir, "console_webshop_history", lines)


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return
    print(f"[8] 화면을 {hold_seconds}초 동안 유지합니다.")
    hold_open_loop(page, hold_seconds, POLL_WAIT_MS)


def run_webshop_history_lookup(
    page,
    uuid_value,
    explicit_project_base,
    start_url,
    project_name,
):
    return summarize_payitem_history(
        page,
        uuid_value,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
        session=None,
    )


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
    print(" Console 지급 내역 UUID lookup")
    print("=" * 60)
    print(f"프로필 폴더: {profile_dir.name}")
    print(f"출력 폴더  : {out_dir.name}")
    print(f"프로젝트명 : {args.project_name}")
    print(f"대상 UUID  : {args.uuid}")
    print(f"시작 URL   : {args.start_url}")

    succeeded = False
    error_message = ""
    summary = None

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
            summary = retry_with_recovery(
                action=lambda: run_webshop_history_lookup(
                    page,
                    args.uuid,
                    args.project_base,
                    args.start_url,
                    args.project_name,
                ),
                recovery=lambda: prepare_console_project(
                    page=page,
                    explicit_project_base=args.project_base,
                    start_url=args.start_url,
                    project_name=args.project_name,
                ),
                label=f"지급내역 UUID {args.uuid} 조회 재시도",
                recovery_desc=f"콘솔 초기화면({args.start_url})/프로젝트 선택부터 다시 준비합니다.",
                max_retries=RETRY_MAX_RETRIES,
            )
            print(
                f"  [{args.uuid}] "
                f"match={summary['payitem_match_count']} "
                f"qty={summary['payitem_quantity_total']} "
                f"sum={summary['payitem_item_value_sum']:,}"
            )
            succeeded = True
            hold_browser_open(page, args.hold_seconds)
        except Exception as exc:  # noqa: BLE001
            error_message = str(exc)
            print(f"\n[오류] {error_message}")
            try:
                page = select_target_page(context, context.pages[0] if context.pages else None)
            except Exception:
                page = context.pages[0] if context.pages else None
            if page is not None:
                print("브라우저를 열어둡니다. 확인 후 Enter 를 눌러 아티팩트 저장 후 종료합니다.")
                try:
                    input()
                except EOFError:
                    pass
        finally:
            page = context.pages[0] if context.pages else None
            if page is not None:
                save_artifacts(page, out_dir, args.uuid, succeeded, summary, error_message)
            context.close()


if __name__ == "__main__":
    main()
