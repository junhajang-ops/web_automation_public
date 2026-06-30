# -*- coding: utf-8 -*-
"""
console_post_bulk.py — 우편 일괄 발송 스크립트

payment_docs/post_bulk*.csv 의 행을 (posttitle, postbody, chart category, item, value)
기준으로 묶어, 동일 조합이면 수신자만 반복 등록 후 한 번에 발송합니다.

CSV 필수 열: posttitle, postbody, chart category, item, value, uuid

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
)
from console_step_verify import (
    configure_console_output,
    init_dump_dir,
    record_step_dump,
    step_and_verify_ui,
)
from console_post_register import (
    confirm_item_add_popup,
    ensure_receiver_list_rows_per_page,
    fill_item_count,
    fill_title_and_content,
    get_item_add_dialog,
    get_post_register_dialog,
    open_item_add_popup,
    open_post_page,
    open_post_register_popup,
    register_receiver_uuid_and_wait,
    select_chart_in_item_popup,
    select_expiry_7days,
)
from console_chart_lookup import PAYMENT_DOCS_DIR
from test_config import apply_title_profile

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


def group_rows(rows: list) -> list:
    """동일한 (posttitle, postbody, chart category, item, value) 조합을 하나의 발송 건으로 묶는다.

    Returns: [{"posttitle":..., "postbody":..., "chart category":...,
               "item":..., "value":..., "uuids": [uuid, ...]}, ...]
    원본 CSV 순서를 유지하며, 같은 조합이 흩어져 있어도 첫 등장 위치 기준으로 모은다.
    """
    order = []   # 조합 첫 등장 순서
    groups = {}  # key → group dict
    for row in rows:
        key = (
            row["posttitle"],
            row["postbody"],
            row["chart category"],
            row["item"],
            row["value"],
        )
        if key not in groups:
            order.append(key)
            groups[key] = {
                "posttitle": row["posttitle"],
                "postbody": row["postbody"],
                "chart category": row["chart category"],
                "item": row["item"],
                "value": row["value"],
                "uuids": [],
            }
        groups[key]["uuids"].append(row["uuid"])
    result = [groups[k] for k in order]
    merged = sum(len(g["uuids"]) for g in result)
    print(f"    {len(rows)}행 → {len(result)}건으로 묶음 (수신자 합계 {merged}명).")
    return result


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
    record_step_dump(page, "bulk_item_dd_pre")
    item_dropdown.click()

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
    record_step_dump(page, "bulk_item_option_pre")
    option.click()

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
    record_step_dump(page, "bulk_send_confirm_pre")
    confirm_btn.click()

    dialog.wait_for(state="hidden", timeout=15_000)


# ── 그룹 단위 우편 발송 ───────────────────────────────────────────────────────

def send_one_group(page, group_num: int, total: int, group: dict) -> list:
    """동일 아이템·수량 그룹에 대해 팝업·아이템은 1회 등록, 수신자만 반복 추가 후 발송.

    Returns: 등록 실패한 uuid 목록 (성공 uuid가 1명이라도 있으면 발송까지 진행).
    """
    posttitle = group["posttitle"]
    postbody = group["postbody"]
    chart_category = group["chart category"]
    item_value = group["item"]
    item_count = int(group["value"])
    uuids = group["uuids"]

    print(f"\n{'─' * 55}")
    print(f" 건 {group_num}/{total}  |  {chart_category}/{item_value} x{item_count}  |  수신자 {len(uuids)}명")
    print(f"{'─' * 55}")

    open_post_register_popup(page)
    select_expiry_7days(page)
    fill_title_and_content(page, posttitle, postbody)
    open_item_add_popup(page)
    select_chart_in_item_popup(page, chart_category)
    select_bulk_item_in_popup(page, chart_category, item_value)
    fill_item_count(page, count=item_count)
    confirm_item_add_popup(page)

    for index, uuid in enumerate(uuids):
        if index == 10 and len(uuids) > 10:
            ensure_receiver_list_rows_per_page(page, rows_per_page=100)

        register_receiver_uuid_and_wait(page, uuid)

    # 발송 전 전원 등록 확인 — 다이얼로그 텍스트에 각 UUID가 모두 있는지 검사
    print("[15-check] 수신자 등록 최종 확인 중...")
    dialog = get_post_register_dialog(page)
    dialog_text = dialog.inner_text()
    missing = [uuid for uuid in uuids if uuid not in dialog_text]
    if missing:
        raise RuntimeError(
            f"수신자 등록 불일치 — 발송 중단. 미등록 UUID({len(missing)}명):\n"
            + "\n".join(f"  {u}" for u in missing)
        )

    print(f"    전원 확인 완료 ({len(uuids)}명)")
    confirm_post_send(page)
    print(f" ✓ 건 {group_num} 완료  ({len(uuids)}명)")
    return []


# ── 전체 루프 ─────────────────────────────────────────────────────────────────

def run_post_bulk(page, rows, explicit_project_base, start_url, project_name):
    prepare_console_project(
        page=page,
        explicit_project_base=explicit_project_base,
        start_url=start_url,
        project_name=project_name,
    )
    open_post_page(page)

    groups = group_rows(rows)
    ok_count = 0
    fail_groups = []

    for i, group in enumerate(groups, 1):
        try:
            failed_uuids = send_one_group(page, i, len(groups), group)
            ok_count += 1
            if failed_uuids:
                fail_groups.append({
                    "group": i,
                    "error": "수신자 일부 실패",
                    "failed_uuids": failed_uuids,
                })
        except Exception as exc:
            print(f"\n[오류] 건 {i} 실패: {exc}")
            fail_groups.append({
                "group": i,
                "error": str(exc),
                "failed_uuids": group.get("uuids", []),
            })
            raise

    step_and_verify_ui(page, "post_bulk_complete")
    return {"ok": ok_count, "fail": len(fail_groups), "fail_groups": fail_groups}


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
    for fg in result.get("fail_groups", []):
        uuids_str = ",".join(fg.get("failed_uuids", []))
        lines.append(f"fail_group={fg['group']}  error={fg['error']}  uuids={uuids_str}")

    try:
        Path(f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        print(f"  (요약 저장 실패: {exc})")

    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    configure_console_output()
    import argparse
    parser = argparse.ArgumentParser(description="우편 일괄 발송")
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
    args = parser.parse_args()
    apply_title_profile(
        args,
        default_project_name=DEFAULT_PROJECT_NAME,
        require_project_name=True,
    )

    rows, csv_path = load_bulk_csv()

    sync_playwright, _timeout_error = load_playwright()
    profile_dir = BASE_DIR / args.profile
    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    init_dump_dir(out_dir)

    print("=" * 55)
    print(" Console console post bulk sender")
    print("=" * 55)
    print(f"CSV     : {csv_path.name}  ({len(rows)}행)")
    print(f"프로필  : {profile_dir.name}")
    print(f"출력    : {out_dir.name}")

    result = {"ok": 0, "fail": 0, "fail_rows": []}

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
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
        for fg in result.get("fail_groups", []):
            print(f"  실패 건 {fg['group']}: {fg['error']}  uuids={fg.get('failed_uuids', [])}")

        if args.hold_seconds > 0:
            print(f"{args.hold_seconds}초 대기 후 종료합니다.")
            page.wait_for_timeout(args.hold_seconds * 1_000)

        try:
            save_artifacts(page, out_dir, result)
        except Exception as exc:
            print(f"  (아티팩트 저장 중 오류: {exc})")
        context.close()
        pw.stop()

    except Exception as exc:
        print(f"\n[오류] {exc}")
        print("브라우저를 열어둡니다. 확인 후 Enter 를 눌러 아티팩트 저장 후 종료합니다.")
        input()
        try:
            save_artifacts(page, out_dir, result)
        except Exception as save_exc:
            print(f"  (아티팩트 저장 중 오류: {save_exc})")


if __name__ == "__main__":
    main()
