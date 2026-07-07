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
import os
import re
import sys
import time
from pathlib import Path

# Windows PowerShell 한국어 깨짐 방지
from console_step_verify import (
    SIDEBAR_BASE_MENU_IGNORE_PATTERNS,
    configure_console_output,
    get_retry_max_retries,
    init_dump_dir,
    record_step_dump,
    retry_with_recovery,
    save_page_artifacts,
    set_project_label_provider,
    step_and_verify_ui,
    step_pause,
    wait_for_loading_settled,
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


# project_menu_open_pre / project_select_pre 전용: 사이드바 카테고리(아코디언) 펼침
# 상태는 세션에 걸쳐 남아있다(ensure_sidebar_link_expanded 참고). 자동화가 여러 화면을
# 거치며 카테고리를 펼칠수록, 그 뒤에 실행되는 프로젝트 진입 스텝에서 보이는 사이드바
# 링크·nav 텍스트 목록이 이전 baseline보다 누적되어 늘어난다 — 실제 화면 구조 변경이
# 아니라 펼침 상태 누적에 따른 정상적 차이이므로 이 두 스텝에서만 항목 추가(+)를
# 무시한다. 제거(-)는 실제 문제(메뉴 소실 등)일 수 있어 그대로 감지 대상으로 둔다.
PROJECT_ENTRY_IGNORE_PATTERNS = [
    r"^\s*\[\+\]\s+sidebar a#id:",
    r"^\s*\[\+\]\s+structural_text: nav:",
] + SIDEBAR_BASE_MENU_IGNORE_PATTERNS


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


def project_fingerprint_label(page) -> str:
    """현재 화면에 표시된 프로젝트명을 fingerprint 스텝 이름에 쓸 수 있게 정규화한다.

    사이드바 카테고리 열림/닫힘 상태와 활성 메뉴는 프로젝트마다 실제로 다르다(다른
    화면이므로 다른 지문이 정상). 스텝 이름이 프로젝트와 무관하게 고정돼 있으면 서로
    다른 프로젝트를 번갈아 실행할 때마다 baseline이 덮어써져 "바뀌었다"는 오탐이 반복된다.
    아래 set_project_label_provider 등록을 통해 모든 record_step_dump/step_and_verify_ui
    호출에 자동으로 반영되므로, 개별 스텝 이름에 직접 엮을 필요는 없다.
    """
    try:
        text = find_project_selector_button(page).inner_text(timeout=2_000).strip()
    except Exception:
        return "unknown"
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", text).strip("_")
    return slug or "unknown"


set_project_label_provider(project_fingerprint_label)


def ensure_project_menu_open(page):
    if wait_for_visible(page.locator("[role='menuitem']").first, 1_000):
        return

    selector_button = find_project_selector_button(page)
    selector_button.wait_for(state="visible", timeout=15_000)
    record_step_dump(page, "project_menu_open_pre", ignore_patterns=PROJECT_ENTRY_IGNORE_PATTERNS)
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
    record_step_dump(page, "project_select_pre", ignore_patterns=PROJECT_ENTRY_IGNORE_PATTERNS)
    selected_item.click()
    safe_wait_for_load(page, "domcontentloaded", 15_000)
    safe_wait_for_load(page, "networkidle", 5_000)
    page.locator("a#baseGamer").first.wait_for(state="visible", timeout=15_000)
    return selected_name


def ensure_search_column(page, target_text, step_prefix):
    """'상세 검색' 검색 기준 드롭다운(`div[name='defaultSearchColumn']`)을 target_text로 맞춘다.

    '유저 UUID'/'닉네임' 등 여러 검색 기준에서 공용으로 쓴다. step_prefix로 record_step_dump
    이름을 기준별로 분리해, 서로 다른 검색 기준 전환이 같은 fingerprint baseline을
    공유해 오탐을 내지 않게 한다.
    """
    dropdown = page.locator("div[name='defaultSearchColumn']").first
    dropdown.wait_for(state="visible", timeout=15_000)

    current_text = dropdown.locator(".text").first.inner_text().strip()
    if current_text == target_text:
        return

    dropdown.scroll_into_view_if_needed()
    record_step_dump(page, f"{step_prefix}_dropdown_pre")
    dropdown.click()

    option = find_exact_text_match(dropdown.locator(".menu .item"), target_text)
    if option is None:
        raise RuntimeError(f"Could not find exact search-column option: {target_text}")
    option.scroll_into_view_if_needed()
    record_step_dump(page, f"{step_prefix}_option_pre")
    option.click()

    selected_text = dropdown.locator(".text").first.inner_text().strip()
    if selected_text != target_text:
        raise RuntimeError(
            f"Search-column selection did not apply: expected='{target_text}', actual='{selected_text}'"
        )


def ensure_uuid_dropdown(page):
    ensure_search_column(page, "유저 UUID", "user_uuid")


def ensure_nickname_search_column(page):
    ensure_search_column(page, "닉네임", "user_nickname")


# 유저 탭 MuiDataGrid의 결과 없음 안내 문구(2026-06-23 dump 실측: dumps_console_search/
# console_user_search_20260623_154529.html, invalid UUID 케이스). 영수증 검증의
# "검색 결과가 없습니다."와는 다른 문구다.
USER_SEARCH_EMPTY_TEXT = "생성된 유저가 없습니다."
USER_SEARCH_POLL_MS = 500
# 같은 페이지(Playwright context)를 여러 티켓/검색에 걸쳐 재사용하는 호출부(예:
# cs_copilot.ConsoleJudgeWorker)에서는, 새 검색 결과가 실제로 그려지기 직전
# 순간에 "직전 검색의 결과없음 안내 문구"가 DOM에 잠깐 남아있는 게 그대로 잡히는
# 레이스가 있다(2026-07-09 사용자 제보: 닉네임 재검색이 실제로는 결과가 있었는데
# "결과 없음"으로 오판됨). '결과 있음'은 한 번만 보여도 즉시 확정하되, '결과 없음'은
# 연속 USER_SEARCH_EMPTY_STABLE_ROUNDS회(폴링 간격 USER_SEARCH_POLL_MS 기준 약 2초)
# 확인돼야 최종 확정한다(행이 뒤늦게 나타나면 스트릭이 끊기고 즉시 "rows"로 전환됨).
# "무효 판정이 느리다"던 이전 문제(2026-07-08)는 불필요한 15~30초 풀타임아웃이
# 원인이었고, 이 debounce는 최대 2초 추가일 뿐이라 그 문제를 되살리지 않는다 —
# 오탐(진짜 결과를 놓치는 것)이 지연보다 훨씬 더 큰 대가이므로 여유 있게 잡는다.
USER_SEARCH_EMPTY_STABLE_ROUNDS = 4


def find_user_result_row(page, uuid_value, wait_timeout_ms):
    """검색 결과 그리드를 폴링해 UUID 행을 찾는다.

    기존에는 새 MuiDataGrid 행을 최대 wait_timeout_ms(기본 15초)까지 기다린 뒤
    실패하면 레거시 테이블 행을 다시 최대 wait_timeout_ms까지 기다려, 존재하지
    않는 UUID 판정에 최악 2×wait_timeout_ms(30초)가 걸렸다(2026-07-08 사용자 제보 —
    무효 UUID 판정이 너무 느림). 결과 없음 안내 문구(USER_SEARCH_EMPTY_TEXT)까지
    같은 폴링 루프에서 함께 확인해, 그리드가 실제로 빈 상태로 안정되는 즉시 짧게
    확정한다. 유효한 UUID가 렌더링에 시간이 걸리는 경우를 위한 대기 상한
    (wait_timeout_ms)은 그대로 유지된다. '결과 없음'은 연속 확인(안정화) 후에만
    확정한다 — 이유는 USER_SEARCH_EMPTY_STABLE_ROUNDS 주석 참고.
    """
    wait_for_loading_settled(page)
    grid_row = page.locator(f"div.MuiDataGrid-row[data-id='{uuid_value}']").first
    legacy_result = page.locator("td#gamer_id p", has_text=uuid_value).first
    empty_notice = page.get_by_text(USER_SEARCH_EMPTY_TEXT, exact=False).first
    empty_streak = [0]

    def _outcome():
        try:
            if grid_row.is_visible():
                return "grid"
        except Exception:
            pass
        try:
            if legacy_result.is_visible():
                return "legacy"
        except Exception:
            pass
        try:
            if empty_notice.is_visible():
                empty_streak[0] += 1
                return "empty" if empty_streak[0] >= USER_SEARCH_EMPTY_STABLE_ROUNDS else None
        except Exception:
            pass
        empty_streak[0] = 0
        return None

    outcome = wait_until(page, _outcome, timeout_ms=wait_timeout_ms, wait_ms=USER_SEARCH_POLL_MS)

    if outcome == "grid":
        cell_text = grid_row.locator("div[data-field='uuid']").first.inner_text().strip()
        if uuid_value not in cell_text:
            raise RuntimeError(f"Unexpected search result text: {cell_text}")
        return grid_row

    if outcome == "legacy":
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


class InvalidUuidError(RuntimeError):
    """'유저' 탭 조회 결과 존재하지 않는(등록되지 않은) UUID로 확인됐을 때."""


def _levenshtein_distance(a: str, b: str) -> int:
    """편집거리(삽입/삭제/치환 각 비용 1). 외부 의존성 없이 순수 파이썬 DP로 계산."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current_row = [i]
        for j, char_b in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (0 if char_a == char_b else 1)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]


# 닉네임 대조로 "동일인의 오탈자"를 자동 판정할 편집거리 상한(2026-07-08 사용자 지시:
# "2개 이하로 차이나면 동일인의 오탈자"). 운영 중 조정 가능하도록 env로 노출.
USER_UUID_NICKNAME_MAX_DISTANCE = max(
    0, int(os.environ.get("USER_UUID_NICKNAME_MAX_DISTANCE", "2"))
)


def collect_user_search_uuid_candidates(page):
    """검색 결과 그리드의 모든 행에서 UUID 값을 수집한다(닉네임은 유일하지 않을 수 있음)."""
    candidates = []
    rows = page.locator("div.MuiDataGrid-row")
    for index in range(rows.count()):
        try:
            cell = rows.nth(index).locator("[data-field='uuid']").first
            value = (cell.get_attribute("title") or cell.inner_text() or "").strip()
        except Exception:
            value = ""
        if value:
            candidates.append(value)

    if candidates:
        return candidates

    legacy_cells = page.locator("td#gamer_id p")
    for index in range(legacy_cells.count()):
        try:
            value = legacy_cells.nth(index).inner_text().strip()
        except Exception:
            value = ""
        if value:
            candidates.append(value)
    return candidates


def _wait_for_user_search_grid_outcome(page, timeout_ms=15_000, wait_ms=USER_SEARCH_POLL_MS):
    """검색 결과가 '행 있음'/'결과 없음' 중 어느 쪽으로 안정됐는지 폴링으로 확정한다.

    '결과 없음'은 연속 USER_SEARCH_EMPTY_STABLE_ROUNDS회 확인돼야 최종 확정한다 —
    같은 페이지를 재사용하는 호출부(닉네임 재검색 등)에서 새 결과가 그려지기 직전
    순간에 직전 검색의 "결과없음" 문구가 DOM에 잠깐 남아있는 레이스를 피하기 위함
    (2026-07-09 사용자 제보: 실제로는 결과가 있었는데 결과없음으로 오판됨). '결과
    있음'은 한 번만 보여도 즉시 확정한다.
    """
    wait_for_loading_settled(page)
    any_row = page.locator("div.MuiDataGrid-row, td#gamer_id p").first
    empty_notice = page.get_by_text(USER_SEARCH_EMPTY_TEXT, exact=False).first
    empty_streak = [0]

    def _outcome():
        try:
            if any_row.is_visible():
                return "rows"
        except Exception:
            pass
        try:
            if empty_notice.is_visible():
                empty_streak[0] += 1
                return "empty" if empty_streak[0] >= USER_SEARCH_EMPTY_STABLE_ROUNDS else None
        except Exception:
            pass
        empty_streak[0] = 0
        return None

    return wait_until(page, _outcome, timeout_ms=timeout_ms, wait_ms=wait_ms)


def find_uuid_candidates_by_nickname(page, nickname, timeout_error):
    """'유저' 탭 검색 기준을 '닉네임'으로 바꿔 조회하고, 결과 그리드의 UUID를 모두 모은다.

    닉네임은 유일하지 않을 수 있어 0/1/N건 어느 쪽도 나올 수 있다 — 후보를 좁히는 건
    호출부(resolve_uuid_via_nickname)의 몫이다. 읽기 전용.
    """
    print(f"[유저 탭] 검색 기준을 '닉네임'으로 바꿔 '{nickname}'을(를) 조회합니다.")
    ensure_nickname_search_column(page)

    search_input = page.locator("input[name='defaultSearchValue']").first
    search_input.wait_for(state="visible", timeout=15_000)
    search_input.scroll_into_view_if_needed()
    record_step_dump(page, "user_nickname_input_pre")
    search_input.fill("")
    search_input.fill(nickname)
    actual_value = search_input.input_value().strip()
    if actual_value != nickname.strip():
        raise RuntimeError(
            f"Nickname input mismatch: expected='{nickname}', actual='{actual_value}'"
        )

    record_step_dump(page, "user_nickname_search_submit_pre")
    page.locator("button[type='submit']").first.click()
    safe_wait_for_load(page, "networkidle", 5_000)

    outcome = _wait_for_user_search_grid_outcome(page)
    step_and_verify_ui(page, "user_nickname_search_results")
    if outcome != "rows":
        return []
    return collect_user_search_uuid_candidates(page)


def resolve_uuid_via_nickname(page, submitted_uuid, nickname, timeout_error,
                               max_distance=USER_UUID_NICKNAME_MAX_DISTANCE):
    """닉네임으로 조회한 UUID 후보들과 제출된 UUID를 대조해 오탈자 여부를 판정한다.

    편집거리(Levenshtein) <= max_distance인 후보가 정확히 1개면 동일인의 오탈자로
    판정한다(2026-07-08 사용자 지시). 후보가 0개거나 2개 이상(모호)이면 잘못된 계정으로
    확정하는 위험을 피하기 위해 자동 판정하지 않는다.
    반환: {"resolved_uuid": str|None, "candidates": list[str], "close_matches": list[(uuid, distance)]}
    """
    candidates = find_uuid_candidates_by_nickname(page, nickname, timeout_error)
    normalized_submitted = (submitted_uuid or "").strip().lower()

    distances = {
        candidate: _levenshtein_distance(normalized_submitted, candidate.strip().lower())
        for candidate in candidates
    }
    close_matches = sorted(
        ((uuid, dist) for uuid, dist in distances.items() if dist <= max_distance),
        key=lambda pair: pair[1],
    )

    resolved_uuid = close_matches[0][0] if len(close_matches) == 1 else None

    return {
        "resolved_uuid": resolved_uuid,
        "candidates": candidates,
        "close_matches": close_matches,
    }


def ensure_uuid_registered(page, uuid_value, timeout_error, nickname=None, nickname_source=None):
    """'유저' 사이드 탭에서 UUID 존재 여부를 먼저 확인한다(상세 팝업은 열지 않음, 읽기 전용).

    영수증 검증 등 다른 화면에 UUID를 입력하기 전에 호출해, 오탈자·존재하지 않는 UUID를
    "기록 없음"(정상 판정 대상)과 구분해 조기에 걸러낸다. open_user_page/submit_uuid_search/
    classify_uuid_search_result를 그대로 재사용한다.

    존재하지 않으면(오탈자 등) nickname이 주어졌을 때 '닉네임'으로 재검색해 후보 UUID들과
    편집거리를 대조한다(2026-07-08 사용자 지시). 정확히 1개의 후보만 편집거리
    USER_UUID_NICKNAME_MAX_DISTANCE(기본 2) 이내면 동일인의 오탈자로 판정하고, 그 콘솔
    UUID로 유저 존재를 확정한다. 후보가 없거나 모호하면(0개/2개 이상) InvalidUuidError.

    nickname_source(참고용 출처 표기 — "custom_field" | "sender_display_name")는 판정
    로직에 영향을 주지 않는다. 사용자가 cs '보낸 사람' 표시명을 이메일 앞에 실제
    닉네임이 오도록 설정해뒀다고 확인(2026-07-08)했으므로, 두 출처 모두 동일하게
    신뢰한다.

    반환: 이후 절차에 쓸 확정 UUID(원래 제출값 그대로이거나, 닉네임 대조로 보정된 값).
    """
    print("[유저 탭] UUID 유효성(존재 여부)을 먼저 확인합니다.")
    open_user_page(page)
    submit_uuid_search(page, uuid_value)
    lookup_status, _ = classify_uuid_search_result(page, uuid_value)
    # 검색 실행 결과가 "있음/없음"에 따라 결과 그리드(ag-Grid)가 렌더/미렌더되며
    # gridcell·rowgroup·컬럼 헤더 menu role이 나타났다 사라진다. 이는 조회 데이터에
    # 따라 갈리는 반복적 차이일 뿐 화면 구조(셀렉터) 변경이 아니므로, 이 단계에 한해
    # 세 role diff만 화이트리스트한다(2026-06-29 fingerprint whitelist note 원칙:
    # 공용 헬퍼 범위를 약화시키지 않고 해당 단계에서만 알려진 diff를 무시).
    step_and_verify_ui(
        page,
        "user_uuid_validity_check",
        ignore_patterns=[r"role: (gridcell|menu|rowgroup)$"] + SIDEBAR_BASE_MENU_IGNORE_PATTERNS,
    )
    if lookup_status == "valid":
        print(f"[유저 탭] UUID 확인됨(존재함): {uuid_value}")
        return uuid_value

    if not nickname:
        raise InvalidUuidError(
            f"'유저' 탭에서 존재하지 않는 UUID로 확인됨(닉네임 정보 없어 대조 불가): {uuid_value}"
        )

    print(f"[유저 탭] UUID 미확인 — 닉네임 '{nickname}'으로 재검색해 오탈자 여부를 대조합니다.")
    match = resolve_uuid_via_nickname(page, uuid_value, nickname, timeout_error)
    if match["resolved_uuid"]:
        distance = dict(match["close_matches"])[match["resolved_uuid"]]
        print(
            f"[유저 탭] 닉네임 '{nickname}' 조회로 콘솔 UUID {match['resolved_uuid']} 확인"
            f"(제출값과 편집거리 {distance}) → 동일인의 오탈자로 판정, 이 UUID로 진행합니다."
        )
        return match["resolved_uuid"]

    if not match["candidates"]:
        raise InvalidUuidError(
            f"'유저' 탭에서 존재하지 않는 UUID로 확인됨, 닉네임 '{nickname}'으로도 결과 없음: {uuid_value}"
        )

    raise InvalidUuidError(
        f"'유저' 탭에서 존재하지 않는 UUID로 확인됨, 닉네임 '{nickname}' 조회 결과 "
        f"{len(match['candidates'])}건 중 제출 UUID와 편집거리 {USER_UUID_NICKNAME_MAX_DISTANCE} 이내인 "
        f"후보가 {len(match['close_matches'])}건(0건 또는 모호)이라 자동 판정 보류: {uuid_value}"
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
    record_step_dump(page, "user_nav_pre", ignore_patterns=SIDEBAR_BASE_MENU_IGNORE_PATTERNS)
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
    record_step_dump(page, "user_uuid_input_pre", ignore_patterns=SIDEBAR_BASE_MENU_IGNORE_PATTERNS)
    search_input.fill("")
    search_input.fill(uuid_value)

    print("[7] 검색 버튼을 클릭합니다.")
    record_step_dump(page, "user_search_submit_pre", ignore_patterns=SIDEBAR_BASE_MENU_IGNORE_PATTERNS)
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
