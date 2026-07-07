# -*- coding: utf-8 -*-
"""
Console console 영수증 검증 UUID search smoke test.

Scope:
- Select the target project (reuses prepare_console_project)
- Before entering the UUID here, confirm it exists via the '유저' side tab
  (console_user_search.ensure_uuid_registered) — raises InvalidUuidError if not found,
  so a typo'd/invalid UUID is never mistaken for a valid "no receipt records" result.
- Open '영수증 검증' menu from the sidebar
- Enter UUID into the search field (name='searchValue') and click '검색' button
- Collect result: row count, total amount, per-row summary
- Dump artifacts: screenshot, HTML, txt (read-only)

DOM mapping basis: console_20260623_121712~121753.txt (field_dump 2026-06-23)
"""

import argparse
import re
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
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    click_login_if_needed,
    ensure_uuid_registered,
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
DEFAULT_OUTPUT = "dumps_console_receipt"
DEFAULT_UUID = TEST_UUID
POLL_WAIT_MS = 1_000
GRID_SCROLL_STEP_PX = 1_200
GRID_SCROLL_IDLE_LIMIT = 3
RECEIPT_IGNORE_PATTERNS = [
    r"button: badge\|type=button$",
]
# 'receipt_uuid_input_pre'/'receipt_search_submit_pre' 전용: 이 두 스텝은 검색을
# 실행하기 '전'에 찍는다. 그런데 영수증 검증 화면은 세션에 직전 조회 결과 그리드를
# 남겨두므로, 이 시점의 gridcell/rowgroup 유무는 이번 실행이 아니라 과거(다른 실행)
# 마지막 조회에 결과가 있었는지에 좌우되는 잔존 데이터일 뿐 구조 변경이 아니다
# (2026-07-06 실측: 같은 스텝이 회차마다 +/-로 뒤집힘).
RECEIPT_PRE_SEARCH_IGNORE_PATTERNS = RECEIPT_IGNORE_PATTERNS + [
    r"role: gridcell$",
    r"role: rowgroup$",
]
# 'receipt_results' 전용: 검색 직후 DataGrid 로딩 스피너(progressbar) 유무는
# 이전 실행 baseline과의 타이밍 차이일 뿐 실제 화면 구조 변경이 아니다(오탐).
# gridcell/rowgroup도 2026-07-07부터 여기 포함(사용자 라이브 실행 제보): 서로 다른
# UUID로 실행할 때마다 "결과 있음"↔"결과 없음"이 자연스럽게 뒤바뀌는데(특히
# console_payment_error.py의 패턴1 = 영수증 기록 없음이 정상 케이스), 그때마다 이전
# baseline과 gridcell/rowgroup 유무가 갈려 매번 [UI change]가 떴다. 이 결과 유무 자체는
# collect_result()가 has_results/row_count로 이미 명시적으로 반환·검증하므로(원칙:
# fingerprint는 구조 감시용이며 명시적 값 검증을 대체하지 않는다) fingerprint가 같은
# 신호를 중복 경보할 필요가 없다 — "행 유무는 의미 있는 신호라 화이트리스트를 공유하지
# 않는다"던 기존 판단(2026-07-06)을 이 실측으로 뒤집는다.
RECEIPT_RESULTS_IGNORE_PATTERNS = RECEIPT_IGNORE_PATTERNS + [
    r"role: progressbar$",
    r"role: gridcell$",
    r"role: rowgroup$",
]
RETRY_MAX_RETRIES = get_retry_max_retries()

# (data-field, 화면 컬럼명) — dump console_20260623_121712.html 확인
RECEIPT_FIELDS = [
    ("index",              "번호"),
    ("purchaseTimeMillis", "거래일시"),
    ("nickname",           "닉네임"),
    ("gamerId",            "유저 UUID"),
    ("orderId",            "주문 ID"),
    ("productId",          "제품 ID"),
    ("description",        "Description"),
    ("type",               "구분"),
    ("platform",           "스토어"),
    ("tbc",                "TBC"),
    ("price",              "금액"),
    ("status",             "상태"),
]
RECEIPT_GAMER_ID_LABEL = next(
    (label for field, label in RECEIPT_FIELDS if field == "gamerId"),
    "gamerId",
)


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
        help=f"Seconds to keep the browser open after success (default: {DEFAULT_HOLD_SECONDS})",
    )
    return parser.parse_args()


def open_receipt_verification_menu(page):
    print("[4] 사이드 메뉴에서 '영수증 검증'으로 이동합니다.")
    receipt_link = page.locator("a", has_text="영수증 검증").first
    receipt_link.wait_for(state="visible", timeout=15_000)
    receipt_link.scroll_into_view_if_needed()
    record_step_dump(page, "receipt_nav_pre", ignore_patterns=RECEIPT_IGNORE_PATTERNS)
    receipt_link.click()
    click_login_if_needed(page)
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    receipt_link.wait_for(state="visible", timeout=15_000)


def _read_visible_role_signature(page):
    return page.evaluate(
        """() => [...new Set(
            [...document.querySelectorAll('[role]')]
              .map(el => el.getAttribute('role'))
              .filter(Boolean)
        )].sort().join('|')"""
    )


def wait_for_receipt_page_render_stable(page, timeout_ms: int = 6_000, stable_rounds: int = 2):
    print("[4-1] 영수증 검증 페이지 렌더가 안정될 때까지 기다립니다.")
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

    print("    (렌더 역할 구성이 완전히 고정되기 전 타임아웃되어 최신 상태로 진행합니다.)")


def _wait_grid_not_loading(page, timeout_ms: int = 10_000):
    """DataGrid progressbar가 사라질 때까지 폴링 대기."""
    progressbar = page.locator("[role='progressbar']").first

    def _not_loading():
        try:
            return not progressbar.is_visible()
        except Exception:
            return True

    wait_until(page, _not_loading, timeout_ms=timeout_ms, wait_ms=POLL_WAIT_MS)


def _normalize_uuid(value: str) -> str:
    return (value or "").strip().lower()


def _verify_uuid_input_value(uuid_input, expected_uuid: str) -> None:
    actual_value = uuid_input.input_value().strip()
    if _normalize_uuid(actual_value) == _normalize_uuid(expected_uuid):
        return
    raise RuntimeError(
        f"UUID input mismatch: expected='{expected_uuid}', actual='{actual_value}'"
    )


def fill_uuid_search(page, uuid_value):
    print(f"[5] UUID 입력창(name='searchValue')에 값을 입력합니다: {uuid_value}")
    uuid_input = page.locator("input#searchValue").first
    uuid_input.wait_for(state="visible", timeout=15_000)
    uuid_input.scroll_into_view_if_needed()
    _wait_grid_not_loading(page)
    record_step_dump(page, "receipt_uuid_input_pre", ignore_patterns=RECEIPT_PRE_SEARCH_IGNORE_PATTERNS)
    uuid_input.fill("")
    uuid_input.fill(uuid_value)
    _verify_uuid_input_value(uuid_input, uuid_value)


def click_search_button(page):
    print("[6] '검색' 버튼을 클릭합니다.")
    search_button = page.locator("button", has_text="검색").first
    search_button.wait_for(state="visible", timeout=15_000)
    search_button.scroll_into_view_if_needed()
    record_step_dump(page, "receipt_search_submit_pre", ignore_patterns=RECEIPT_PRE_SEARCH_IGNORE_PATTERNS)
    search_button.click()
    safe_wait_for_load(page, "networkidle", 5_000)
    _wait_grid_not_loading(page)



def set_rows_per_page(page, count: int = 100):
    print(f"[7-1] 페이지당 행 수를 {count}개로 변경합니다.")
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
    record_step_dump(page, "receipt_rows_dd_pre", ignore_patterns=RECEIPT_IGNORE_PATTERNS)
    trigger.click()

    listbox = page.locator("ul[role='listbox']").first
    listbox.wait_for(state="visible", timeout=10_000)
    option = find_exact_text_match(listbox.locator("li[role='option']"), target_text)
    if option is None:
        raise RuntimeError(f"Could not find exact rows-per-page option: {target_text}")
    option.wait_for(state="visible", timeout=5_000)
    record_step_dump(page, "receipt_rows_option_pre", ignore_patterns=RECEIPT_IGNORE_PATTERNS)
    option.click()
    safe_wait_for_load(page, "networkidle", 5_000)

    def _rows_per_page_applied():
        if trigger.inner_text().strip() == target_text:
            return True
        return None

    if wait_until(page, _rows_per_page_applied, timeout_ms=10_000, wait_ms=POLL_WAIT_MS):
        return

    raise RuntimeError(
        f"Rows-per-page selection did not apply: expected='{target_text}', actual='{trigger.inner_text().strip()}'"
    )


def _read_cell(row, field: str) -> str:
    try:
        inner = row.locator(f"[role='gridcell'][data-field='{field}'] div[title]").first
        val = inner.get_attribute("title")
        if val is not None:
            return val.strip()
    except Exception:
        pass
    try:
        return row.locator(f"[role='gridcell'][data-field='{field}']").first.inner_text().strip()
    except Exception:
        return ""


def _row_key(row_data: dict) -> str:
    key_fields = [
        row_data.get("주문 ID", ""),
        row_data.get("거래일시", ""),
        row_data.get("상품 ID", ""),
        row_data.get("번호", ""),
    ]
    return "||".join(key_fields)


def _read_visible_receipt_rows(page):
    rows = []
    row_locator = page.locator("div.MuiDataGrid-row")
    visible_count = row_locator.count()
    for i in range(visible_count):
        row = row_locator.nth(i)
        row_data = {label: _read_cell(row, field) for field, label in RECEIPT_FIELDS}
        rows.append(row_data)
    return rows


def _get_grid_scroll_state(page):
    return page.evaluate(
        """() => {
          const el = document.querySelector('.MuiDataGrid-virtualScroller');
          if (!el) return null;
          return {
            scrollTop: el.scrollTop,
            clientHeight: el.clientHeight,
            scrollHeight: el.scrollHeight,
          };
        }"""
    )


def _scroll_receipt_grid_once(page):
    scroller = page.locator(".MuiDataGrid-virtualScroller").first
    if not wait_for_visible(scroller, 5_000):
        return False

    box = scroller.bounding_box()
    if not box:
        return False

    page.mouse.move(box["x"] + (box["width"] / 2), box["y"] + (box["height"] / 2))
    page.mouse.wheel(0, GRID_SCROLL_STEP_PX)
    page.wait_for_timeout(POLL_WAIT_MS)
    return True


def collect_all_receipt_rows(page):
    collected = {}
    idle_rounds = 0

    while idle_rounds < GRID_SCROLL_IDLE_LIMIT:
        visible_rows = _read_visible_receipt_rows(page)
        added_this_round = 0

        for row_data in visible_rows:
            key = _row_key(row_data)
            if key in collected:
                continue
            collected[key] = row_data
            added_this_round += 1

        before_state = _get_grid_scroll_state(page)
        if before_state is None:
            break

        if added_this_round == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        reached_bottom = (
            before_state["scrollTop"] + before_state["clientHeight"]
            >= before_state["scrollHeight"] - 4
        )
        if reached_bottom and idle_rounds > 0:
            break

        if not _scroll_receipt_grid_once(page):
            break

        after_state = _get_grid_scroll_state(page)
        if (
            after_state is not None
            and before_state["scrollTop"] == after_state["scrollTop"]
        ):
            idle_rounds += 1

    return list(collected.values())


def _verify_receipt_rows_match_uuid(rows: list[dict], expected_uuid: str) -> None:
    expected_normalized = _normalize_uuid(expected_uuid)
    mismatches = []

    for row_index, row_data in enumerate(rows, start=1):
        actual_uuid = str(row_data.get(RECEIPT_GAMER_ID_LABEL, "")).strip()
        if _normalize_uuid(actual_uuid) == expected_normalized:
            continue
        mismatches.append(
            {
                "row_index": row_index,
                "actual_uuid": actual_uuid,
                "order_id": str(row_data.get("二쇰Ц ID", "")).strip(),
                "row_no": str(row_data.get("踰덊샇", "")).strip(),
            }
        )

    if not mismatches:
        return

    samples = ", ".join(
        (
            f"row {item['row_index']}[no={item['row_no']}, "
            f"orderId='{item['order_id']}', uuid='{item['actual_uuid']}']"
        )
        for item in mismatches[:3]
    )
    raise RuntimeError(
        f"Receipt result UUID mismatch for '{expected_uuid}': "
        f"{len(mismatches)} row(s) differ. {samples}"
    )


def collect_result(page, uuid_value, timeout_error, ensure_rows_per_page=True):
    print("[7] 영수증 검증 결과를 수집합니다.")
    _wait_grid_not_loading(page)

    no_result_locator = page.locator("text=검색 결과가 없습니다.").first
    row_locator = page.locator("div.MuiDataGrid-row")

    def _no_result_or_rows():
        if no_result_locator.is_visible():
            return "no_result"
        if row_locator.first.is_visible():
            return "rows"
        return None

    outcome = wait_until(page, _no_result_or_rows, timeout_ms=10_000)
    if outcome == "no_result":
        print("    결과 없음.")
        return {
            "has_results": False,
            "row_count": 0,
            "rows": [],
        }
    if outcome != "rows":
        raise timeout_error(f"영수증 검증 결과 행이 나타나지 않았습니다: {uuid_value}")

    if ensure_rows_per_page:
        set_rows_per_page(page, 100)
        page.wait_for_timeout(POLL_WAIT_MS)

    rows = collect_all_receipt_rows(page)
    _verify_receipt_rows_match_uuid(rows, uuid_value)
    row_count = len(rows)
    print(f"    결과 {row_count}건 수집.")

    return {
        "has_results": True,
        "row_count": row_count,
        "rows": rows,
    }


def save_artifacts(page, out_dir, uuid_value, succeeded, result_summary=None, error_message=""):
    summary_lines = [
        f"success={succeeded}",
        f"uuid={uuid_value}",
        f"url={page.url}",
        f"title={page.title()}",
    ]
    if result_summary:
        summary_lines.append(f"has_results={result_summary.get('has_results', '')}")
        summary_lines.append(f"row_count={result_summary.get('row_count', '')}")
        for i, row in enumerate(result_summary.get("rows", []), start=1):
            if isinstance(row, dict):
                summary_lines.append(f"row_{i}={' | '.join(f'{k}={v}' for k, v in row.items())}")
            else:
                summary_lines.append(f"row_{i}={' | '.join(str(c) for c in row)}")
    if error_message:
        summary_lines.append(f"error={error_message}")

    save_page_artifacts(page, out_dir, "console_receipt", summary_lines)


def hold_browser_open(page, hold_seconds):
    if hold_seconds <= 0:
        return
    print(f"[8] 화면을 {hold_seconds}초 동안 유지합니다.")
    hold_open_loop(page, hold_seconds, POLL_WAIT_MS)


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
    # 영수증 검증 UUID 입력 전, '유저' 탭에서 존재하는 UUID인지 먼저 확인한다(오탈자/무효
    # UUID를 "기록 없음"으로 잘못 취급하지 않기 위함) — 존재하지 않으면 InvalidUuidError.
    ensure_uuid_registered(page, uuid_value, timeout_error)
    open_receipt_verification_menu(page)
    wait_for_receipt_page_render_stable(page)
    fill_uuid_search(page, uuid_value)
    click_search_button(page)
    step_and_verify_ui(page, "receipt_results", ignore_patterns=RECEIPT_RESULTS_IGNORE_PATTERNS)
    return collect_result(page, uuid_value, timeout_error, ensure_rows_per_page=True)


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
            result_summary = retry_with_recovery(
                action=lambda: run_receipt_verification(
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
                label=f"영수증 UUID {args.uuid} 조회 재시도",
                recovery_desc=f"콘솔 초기화면({args.start_url})/프로젝트 선택부터 다시 준비합니다.",
                max_retries=RETRY_MAX_RETRIES,
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
