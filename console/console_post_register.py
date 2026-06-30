# -*- coding: utf-8 -*-
"""
Console console post-register helper.

Scope:
- Open the post page from the side menu
- Open the post-register popup
- Select expiration period "7??
- Fill title and content
- Open the item-add popup
- Select TEST_CHART_NAME in the chart dropdown
- Select item by ShopTable_ID (looked up from payment_docs/ CSV)

Final registration (receiver input, submit) requires human approval.
"""

import argparse
import datetime
import sys
import time
from pathlib import Path

from console_step_verify import init_dump_dir, record_step_dump, step_and_verify_ui, wait_until
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
    wait_for_visible,
)
from console_chart_lookup import PAYMENT_DOCS_DIR, _read_csv_and_lookup
from test_config import TEST_CHART_NAME, TEST_PURCHASE_CODE, TEST_UUID, apply_title_profile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = "dumps_console_post_register"
POST_TITLE = "寃곗젣?곹뭹吏湲?"
POST_CONTENT = "寃곗젣?곹뭹吏湲?"


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
    return page.locator("[role='dialog']").filter(has_text="?고렪 ?깅줉").first


def get_item_add_dialog(page):
    return page.locator("[role='dialog']").filter(has_text="?꾩씠??異붽?").last


def open_post_page(page):
    print("[4] ?ъ씠??硫붾돱?먯꽌 '?고렪' ?섏씠吏濡??대룞?⑸땲??")
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
    print("[5] '?고렪 ?깅줉' 踰꾪듉???대┃?⑸땲??")
    button = page.locator("button.ui.button").filter(has_text="?고렪 ?깅줉").first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    record_step_dump(page, "post_popup_pre")
    button.click()

    dialog = get_post_register_dialog(page)
    dialog.wait_for(state="visible", timeout=15_000)


def select_expiry_7days(page):
    print("[6] 留뚮즺湲곌컙 '7?????좏깮?⑸땲??")
    dialog = get_post_register_dialog(page)
    field = dialog.locator(".field").filter(has_text="留뚮즺湲곌컙").first
    radio_box = field.locator(".ui.radio.checkbox").filter(has_text="7??").first
    hidden_radio = field.locator("input[name='expirationType'][value='7']").first

    radio_box.wait_for(state="visible", timeout=10_000)
    radio_box.scroll_into_view_if_needed()
    record_step_dump(page, "expiry_pre")
    radio_box.click()

    is_checked = (hidden_radio.get_attribute("checked") is not None) or (
        "checked" in ((radio_box.get_attribute("class") or "").lower())
    )
    if not is_checked:
        raise RuntimeError("留뚮즺湲곌컙 '7?? ?좏깮???곹깭??諛섏쁺?섏? ?딆븯?듬땲??")


def fill_title_and_content(page, title, content):
    dialog = get_post_register_dialog(page)
    title_input = dialog.locator("input[name='country.0.title']").first
    content_area = dialog.locator("textarea[name='country.0.content']").first

    print(f"[7] ?쒕ぉ ?낅젰: '{title}'")
    title_input.wait_for(state="visible", timeout=10_000)
    title_input.scroll_into_view_if_needed()
    record_step_dump(page, "title_pre_fill")
    title_input.fill(title)

    print(f"[8] ?댁슜 ?낅젰: '{content}'")
    content_area.wait_for(state="visible", timeout=10_000)
    content_area.scroll_into_view_if_needed()
    record_step_dump(page, "content_pre_fill")
    content_area.fill(content)


def open_item_add_popup(page):
    print("[9] '?꾩씠???깅줉' 踰꾪듉???대┃?⑸땲??")
    button = page.locator("button.ui.basic.button").filter(has_text="?꾩씠???깅줉").first
    button.wait_for(state="visible", timeout=15_000)
    button.scroll_into_view_if_needed()
    record_step_dump(page, "item_popup_pre")
    button.click()

    dialog = get_item_add_dialog(page)
    dialog.wait_for(state="visible", timeout=15_000)


def load_shop_table_id(chart_name: str) -> str:
    """payment_docs/ ????λ맂 CSV?먯꽌 TEST_PURCHASE_CODE ??ShopTable_ID 諛섑솚."""
    csvs = sorted(PAYMENT_DOCS_DIR.glob(f"chart_{chart_name}_*.csv"))
    if not csvs:
        raise RuntimeError(
            f"payment_docs/ ??'{chart_name}' CSV ?놁쓬 ??console_chart_lookup.py 癒쇱? ?ㅽ뻾 ?꾩슂"
        )
    csv_path = csvs[-1]
    print(f"[CSV] {csv_path.name} ?먯꽌 ShopTable_ID 議고쉶 以?..")
    result = _read_csv_and_lookup(csv_path, TEST_PURCHASE_CODE)
    shop_table_id = result.get("shop_table_id", "")
    if not shop_table_id:
        raise RuntimeError(
            f"CSV?먯꽌 purchase_code='{TEST_PURCHASE_CODE}'????묓븯??ShopTable_ID瑜?李얠? 紐삵뻽?듬땲??"
        )
    print(f"    ShopTable_ID: {shop_table_id}")
    return shop_table_id


def select_chart_in_item_popup(page, chart_name):
    print(f"[10] ?꾩씠??異붽? ?앹뾽 李⑦듃 ?쒕∼?ㅼ슫?먯꽌 '{chart_name}'???좏깮?⑸땲??")
    dialog = get_item_add_dialog(page)
    dropdown = dialog.locator("[role='listbox']").first
    dropdown.wait_for(state="visible", timeout=10_000)
    dropdown.scroll_into_view_if_needed()
    record_step_dump(page, "chart_dd_pre")
    dropdown.click()

    option = find_exact_text_match(dialog.locator("[role='option']"), chart_name)
    if option is None:
        raise RuntimeError(f"李⑦듃 ?쒕∼?ㅼ슫?먯꽌 ?뺥솗??'{chart_name}'? ?쇱튂?섎뒗 ??ぉ??李얠? 紐삵뻽?듬땲??")
    option.wait_for(state="visible", timeout=5_000)
    option.scroll_into_view_if_needed()
    record_step_dump(page, "chart_option_pre")
    option.click()

    selected_text = dropdown.locator(".text, .divider.text").first.inner_text().strip()
    print(f"    ?좏깮 寃곌낵: '{selected_text}'")
    if selected_text != chart_name:
        raise RuntimeError(
            f"李⑦듃 ?쒕∼?ㅼ슫 ?좏깮 寃곌낵媛 湲곕?媛믨낵 ?ㅻ쫭?덈떎: expected='{chart_name}', actual='{selected_text}'"
        )
    return selected_text


def find_exact_text_match(items, target_text):
    count = items.count()
    for idx in range(count):
        item = items.nth(idx)
        try:
            if item.inner_text().strip() == target_text:
                return item
        except Exception:
            continue
    return None


def ensure_receiver_list_rows_per_page(page, rows_per_page: int = 100):
    dialog = get_post_register_dialog(page)
    target_text = f"{rows_per_page}媛쒖뵫 蹂닿린"
    limit_dd = dialog.locator("[role='listbox']").filter(has_text="媛쒖뵫").first
    limit_dd.wait_for(state="visible", timeout=10_000)
    current_text = limit_dd.locator(".text, .divider.text").first.inner_text().strip()
    if current_text == target_text:
        return

    print(f"[15-page] ?섏떊??紐⑸줉??{target_text} 蹂닿린濡??꾪솚?⑸땲??")
    limit_dd.scroll_into_view_if_needed()
    record_step_dump(page, "rcvr_rows_dd_pre")
    limit_dd.click()

    option = find_exact_text_match(dialog.locator("[role='option']"), target_text)
    if option is None:
        raise RuntimeError(f"?섏떊??紐⑸줉 媛쒖닔 ?듭뀡?먯꽌 ?뺥솗??'{target_text}'? ?쇱튂?섎뒗 ??ぉ??李얠? 紐삵뻽?듬땲??")
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
            f"?섏떊??紐⑸줉 媛쒖닔 ?꾪솚 寃곌낵媛 湲곕?? ?ㅻ쫭?덈떎: expected='{target_text}', actual='{selected_text}'"
        )


def select_item_in_popup(page, shop_table_id: str):
    print(f"[11] ?꾩씠???쒕∼?ㅼ슫?먯꽌 ShopTable_ID='{shop_table_id}' ?좏깮?⑸땲??")
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
    print(f"    ?좏깮 寃곌낵: {selected_text[:80]}...")
    if target_substr not in selected_text:
        raise RuntimeError(
            f"?꾩씠???좏깮 寃곌낵??ShopTable_ID='{shop_table_id}'媛 ?놁뒿?덈떎. actual='{selected_text[:120]}'"
        )
    return selected_text


def fill_item_count(page, count: int = 1):
    print(f"[12] ?섎웾 '{count}' ?낅젰?⑸땲??")
    dialog = get_item_add_dialog(page)
    count_input = dialog.locator("input[name='itemCount']").first
    count_input.wait_for(state="visible", timeout=10_000)
    count_input.scroll_into_view_if_needed()
    record_step_dump(page, "count_pre_fill")
    count_input.fill(str(count))


def confirm_item_add_popup(page):
    print("[13] ?꾩씠??異붽? ?앹뾽 '?뺤씤' 踰꾪듉???대┃?⑸땲??")
    dialog = get_item_add_dialog(page)
    confirm_btn = dialog.locator("button.ui.medium.positive.button").first
    confirm_btn.wait_for(state="visible", timeout=10_000)
    confirm_btn.scroll_into_view_if_needed()
    record_step_dump(page, "item_confirm_pre")
    confirm_btn.click()
    dialog.wait_for(state="hidden", timeout=10_000)
    # ?앹뾽 ?ロ옒 ?湲?    dialog.wait_for(state="hidden", timeout=10_000)


def fill_receiver_uuid(page, uuid: str):
    print(f"[14] ?좎? 踰덊샇/?됰꽕?????UUID ?낅젰?⑸땲?? {uuid}")
    dialog = get_post_register_dialog(page)
    gamer_input = dialog.locator("input[name='gamer']").first
    gamer_input.wait_for(state="visible", timeout=10_000)
    gamer_input.scroll_into_view_if_needed()
    record_step_dump(page, "receiver_pre_fill")
    gamer_input.fill(uuid)


def click_receiver_register(page):
    print("[15] ?섏떊??'?깅줉' 踰꾪듉???대┃?⑸땲??")
    dialog = get_post_register_dialog(page)
    register_btn = dialog.locator("button.ui.primary.button").filter(has_text="?깅줉").first
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
        print(f"    ?섏떊??諛섏쁺 ?뺤씤: {uuid}")
        return

    if not input_cleared:
        raise RuntimeError(f"?섏떊??UUID ?낅젰???鍮꾩썙吏吏 ?딆븯?듬땲?? {uuid}")
    raise RuntimeError(f"?섏떊??UUID媛 紐⑸줉??諛섏쁺?섏? ?딆븯?듬땲?? {uuid}")


def register_receiver_uuid_and_wait(page, uuid: str, timeout_ms: int = 15_000):
    fill_receiver_uuid(page, uuid)
    click_receiver_register(page)
    wait_for_receiver_registered(page, uuid, timeout_ms=timeout_ms)


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
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"console_post_register_{ts}"

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as exc:
        print(f"  (?ㅽ겕由곗꺑 ????ㅽ뙣: {exc})")

    try:
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML ????ㅽ뙣: {exc})")

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
        print(f"  (?붿빟 ????ㅽ뙣: {exc})")

    print(f"\n?꾪떚?⑺듃 ????꾨즺: {stem}.png / .html / .txt")


def main():
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
    print(f"?꾨줈???대뜑: {profile_dir.name}")
    print(f"異쒕젰 ?대뜑  : {out_dir.name}")
    print(f"???李⑦듃  : {args.chart_name}")
    print(f"?쒖옉 URL   : {args.start_url}")
    print(f"?꾨줈?앺듃紐?: {args.project_name}")

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

            print("\n=== ?꾨즺 (?꾩씠???좏깮源뚯?) ===")
            for key, value in result_summary.items():
                print(f"  {key}: {value}")

            if args.hold_seconds > 0:
                print(f"[16] {args.hold_seconds}珥??湲???醫낅즺?⑸땲??")
                page.wait_for_timeout(args.hold_seconds * 1_000)

        except Exception as exc:
            error_message = str(exc)
            print(f"\n[?ㅻ쪟] {error_message}")
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
                print(f"  (?꾪떚?⑺듃 ???以??ㅻ쪟: {exc})")
            context.close()


if __name__ == "__main__":
    main()

