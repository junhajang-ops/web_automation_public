# -*- coding: utf-8 -*-
"""
console_post_bulk.py — 우편 일괄 발송 스크립트

payment_docs/post_bulk*.csv 의 각 행마다 우편 등록 팝업을 통해
제목·내용·아이템·수신자를 입력하고 발송합니다.

CSV 필수 열: posttitle, postbody, chart category, item, uuid

실행:
  python console_post_bulk.py
"""

import csv
import datetime
import sys
from pathlib import Path

from console_user_search_test import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PROFILE,
    DEFAULT_PROJECT_NAME,
    DEFAULT_START_URL,
    load_playwright,
    prepare_console_project,
    select_target_page,
    step_pause,
)
from console_post_register import (
    click_receiver_register,
    confirm_item_add_popup,
    fill_item_count,
    fill_receiver_uuid,
    fill_title_and_content,
    get_item_add_dialog,
    get_post_register_dialog,
    open_item_add_popup,
    open_post_page,
    open_post_register_popup,
    select_chart_in_item_popup,
    select_expiry_7days,
)
from console_chart_lookup import PAYMENT_DOCS_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = "dumps_console_post_bulk"
POST_SEND_WAIT_MS = 5_000


# ── CSV 로드 ──────────────────────────────────────────────────────────────────

def load_bulk_csv() -> list:
    """payment_docs/post_bulk*.csv 중 가장 최신 파일을 읽어 행 목록 반환."""
    csvs = sorted(PAYMENT_DOCS_DIR.glob("post_bulk*.csv"))
    if not csvs:
        raise RuntimeError(
            "payment_docs/ 에 'post_bulk*.csv' 없음 — 파일을 먼저 준비해 주세요."
        )
    csv_path = csvs[-1]
    print(f"[CSV] {csv_path.name} 로드 중...")
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k.strip(): v.strip() for k, v in row.items()})
    required = {"posttitle", "postbody", "chart category", "item", "value", "uuid"}
    missing = required - set(rows[0].keys()) if rows else set()
    if missing:
        raise RuntimeError(f"CSV 열 누락: {missing}")
    print(f"    총 {len(rows)}행 로드됨.")
    return rows, csv_path


# ── 아이템 선택 (bulk 전용: chart category + item 값으로 매칭) ─────────────────

def select_bulk_item_in_popup(page, chart_category: str, item_value: str):
    """아이템 추가 팝업에서 chart_category/item_value 로 아이템 선택.

    드롭다운 옵션 텍스트는 JSON 형식이므로 item_value 를 포함하는
    여러 후보 substring 을 순서대로 시도한다.
    예) chart_category='Coin', item_value='2000'
        → '":\"2000\"' 또는 '":2000,' 등
    """
    print(f"[11] 아이템 드롭다운에서 chart='{chart_category}', item='{item_value}' 선택합니다.")
    dialog = get_item_add_dialog(page)
    item_dropdown = dialog.locator("[name='item'][role='listbox']").first
    item_dropdown.wait_for(state="visible", timeout=10_000)
    item_dropdown.scroll_into_view_if_needed()
    step_pause(page)
    item_dropdown.click()
    step_pause(page)

    # JSON 값 형식이 문자열("2000") 또는 숫자(2000) 모두 대응
    substrs = [
        f'":"{item_value}"',   # "CoinTable_Id":"2000"
        f'":{item_value},',    # "CoinTable_ID":2000,
        f'":{item_value}}}',   # "CoinTable_ID":2000} (JSON 마지막 키)
    ]
    option = None
    for substr in substrs:
        candidates = dialog.locator("[role='option']").filter(has_text=substr)
        if candidates.count() > 0:
            option = candidates.first
            break

    if option is None:
        raise RuntimeError(
            f"아이템 드롭다운에서 chart='{chart_category}', item='{item_value}' 옵션을 찾지 못했습니다."
        )

    option.wait_for(state="visible", timeout=10_000)
    option.scroll_into_view_if_needed()
    step_pause(page)
    option.click()
    step_pause(page)

    selected_text = item_dropdown.locator(".text, .divider.text").first.inner_text().strip()
    print(f"    선택 결과: {selected_text[:100]}...")
    return selected_text


# ── 우편 등록 최종 확인 (5초 대기 후 발송) ────────────────────────────────────

def confirm_post_send(page):
    """5초 대기 후 우편 등록 다이얼로그의 '확인' 버튼을 클릭해 발송."""
    print(f"[16] {POST_SEND_WAIT_MS // 1000}초 대기 후 우편 등록 '확인'을 클릭합니다.")
    page.wait_for_timeout(POST_SEND_WAIT_MS)

    dialog = get_post_register_dialog(page)
    confirm_btn = dialog.locator("button.ui.medium.positive.button").first
    confirm_btn.wait_for(state="visible", timeout=10_000)
    confirm_btn.scroll_into_view_if_needed()
    step_pause(page)
    confirm_btn.click()

    dialog.wait_for(state="hidden", timeout=15_000)
    step_pause(page)


# ── 행 단위 우편 발송 ─────────────────────────────────────────────────────────

def send_one_row(page, row_num: int, total: int, row: dict):
    """CSV 한 행에 대해 우편 등록 전 과정을 수행."""
    posttitle = row["posttitle"]
    postbody = row["postbody"]
    chart_category = row["chart category"]
    item_value = row["item"]
    item_count = int(row["value"])
    uuid = row["uuid"]

    print(f"\n{'─' * 55}")
    print(f" 행 {row_num}/{total}  |  {uuid[:8]}...  |  {chart_category}/{item_value} x{item_count}")
    print(f"{'─' * 55}")

    open_post_register_popup(page)
    select_expiry_7days(page)
    fill_title_and_content(page, posttitle, postbody)
    open_item_add_popup(page)
    select_chart_in_item_popup(page, chart_category)
    select_bulk_item_in_popup(page, chart_category, item_value)
    fill_item_count(page, count=item_count)
    confirm_item_add_popup(page)
    fill_receiver_uuid(page, uuid)
    click_receiver_register(page)
    confirm_post_send(page)

    print(f" ✓ 행 {row_num} 완료")


# ── 전체 루프 ─────────────────────────────────────────────────────────────────

def run_post_bulk(page, rows, explicit_project_base, start_url, project_name):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    open_post_page(page)

    ok_count = 0
    fail_rows = []

    for i, row in enumerate(rows, 1):
        try:
            send_one_row(page, i, len(rows), row)
            ok_count += 1
        except Exception as exc:
            print(f"\n[오류] 행 {i} 실패: {exc}")
            fail_rows.append({"row": i, "uuid": row.get("uuid", ""), "error": str(exc)})
            # 팝업이 열려있으면 ESC로 닫기 시도
            try:
                page.keyboard.press("Escape")
                step_pause(page)
                page.keyboard.press("Escape")
                step_pause(page)
            except Exception:
                pass

    return {"ok": ok_count, "fail": len(fail_rows), "fail_rows": fail_rows}


# ── 아티팩트 저장 ──────────────────────────────────────────────────────────────

def save_artifacts(page, out_dir, result):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_post_bulk_{ts}"

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as exc:
        print(f"  (스크린샷 저장 실패: {exc})")

    try:
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML 저장 실패: {exc})")

    lines = [
        f"ok={result['ok']}",
        f"fail={result['fail']}",
    ]
    for fr in result.get("fail_rows", []):
        lines.append(f"fail_row={fr['row']}  uuid={fr['uuid']}  error={fr['error']}")

    try:
        Path(f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        print(f"  (요약 저장 실패: {exc})")

    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="우편 일괄 발송")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--project-base", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    args = parser.parse_args()

    rows, csv_path = load_bulk_csv()

    sync_playwright, _timeout_error = load_playwright()
    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 55)
    print(" Console console post bulk sender")
    print("=" * 55)
    print(f"CSV     : {csv_path.name}  ({len(rows)}행)")
    print(f"프로필  : {profile_dir.name}")
    print(f"출력    : {out_dir.name}")

    result = {"ok": 0, "fail": 0, "fail_rows": []}

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
            result = run_post_bulk(
                page=page,
                rows=rows,
                explicit_project_base=args.project_base,
                start_url=args.start_url,
                project_name=args.project_name,
            )

            print(f"\n=== 완료: 성공 {result['ok']}건 / 실패 {result['fail']}건 ===")
            for fr in result["fail_rows"]:
                print(f"  실패 행 {fr['row']}: {fr['error']}")

            if args.hold_seconds > 0:
                print(f"{args.hold_seconds}초 대기 후 종료합니다.")
                page.wait_for_timeout(args.hold_seconds * 1_000)

        except Exception as exc:
            print(f"\n[오류] {exc}")
        finally:
            try:
                page = select_target_page(context, page)
                save_artifacts(page, out_dir, result)
            except Exception as exc:
                print(f"  (아티팩트 저장 중 오류: {exc})")
            context.close()


if __name__ == "__main__":
    main()
