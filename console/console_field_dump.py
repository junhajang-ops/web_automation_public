# -*- coding: utf-8 -*-
"""
콘솔(Console) 콘솔 화면 구조 dump 스크립트
=========================================

목적
----
콘솔 콘솔의 조회 화면과 조작 UI를 "읽기 전용"으로 떠서 저장합니다.
필드, 표, 입력값, 버튼/액션 요소를 함께 기록해 이후 화면 매핑에 사용합니다.

안전 설계
--------
- 이 스크립트는 비밀번호를 묻거나 저장하지 않습니다.
- 브라우저 창에서 사람이 직접 로그인하고 원하는 화면을 엽니다.
- 자동 클릭, 폼 제출, 회수/지급/제재 같은 조작은 하지 않습니다.

실행 예시
---------
  python console_field_dump.py
  python console_field_dump.py --profile pw_profile_console_A --out dumps_console_A
  python console_field_dump.py --url https://api.example.io/
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOGIN_SKIP = ("/login", "/signin", "/oauth", "/logout", "about:blank")

EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

  const definitionLists = [];
  document.querySelectorAll("dl").forEach(dl => {
    const pairs = [];
    const dts = dl.querySelectorAll("dt");
    dts.forEach(dt => {
      let dd = dt.nextElementSibling;
      while (dd && dd.tagName !== "DD") dd = dd.nextElementSibling;
      pairs.push({ label: clean(dt.innerText), value: clean(dd ? dd.innerText : "") });
    });
    if (pairs.length) definitionLists.push(pairs);
  });

  const tables = [];
  document.querySelectorAll("table").forEach(t => {
    const rows = [];
    t.querySelectorAll("tr").forEach(tr => {
      const cells = [];
      tr.querySelectorAll("th,td").forEach(c => cells.push(clean(c.innerText)));
      if (cells.some(x => x)) rows.push(cells);
    });
    if (rows.length) tables.push(rows);
  });

  const labelFor = {};
  document.querySelectorAll("label").forEach(l => {
    const f = l.getAttribute("for");
    if (f) labelFor[f] = clean(l.innerText);
  });

  const formFields = [];
  document.querySelectorAll("input, select, textarea").forEach(el => {
    const type = (el.getAttribute("type") || el.tagName).toLowerCase();
    if (["hidden", "submit", "button"].includes(type)) return;

    let label = labelFor[el.id] || "";
    if (!label) {
      const parentLabel = el.closest("label");
      if (parentLabel) label = clean(parentLabel.innerText);
    }
    if (!label && el.previousElementSibling) {
      label = clean(el.previousElementSibling.innerText);
    }

    let value = "";
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      value = opt ? clean(opt.text) : "";
    } else {
      value = clean(el.value);
    }

    formFields.push({
      label: label,
      tag: el.tagName.toLowerCase(),
      type: type,
      name: el.getAttribute("name") || "",
      id: el.id || "",
      placeholder: el.getAttribute("placeholder") || "",
      value: value,
      disabled: !!el.disabled,
      readonly: !!el.readOnly
    });
  });

  const actionElements = [];
  document.querySelectorAll("button, a[role=button], a.btn, .btn, [onclick]").forEach(el => {
    const text = clean(el.innerText) || clean(el.getAttribute("value")) || clean(el.getAttribute("aria-label"));
    const href = el.getAttribute("href") || "";
    const onclick = el.getAttribute("onclick") || "";
    const id = el.id || "";
    const className = clean(el.className || "");
    const name = el.getAttribute("name") || "";
    if (!(text || href || onclick || id || className || name)) return;
    actionElements.push({
      tag: el.tagName.toLowerCase(),
      text: text,
      id: id,
      className: className,
      name: name,
      href: href,
      onclick: onclick,
      disabled: !!el.disabled
    });
  });

  const bodyText = document.body ? document.body.innerText : "";
  const labeledLines = [];
  bodyText.split("\n").forEach(line => {
    const m = line.match(/^\s*([^\n:：]{1,40})\s*[:：]\s*(.+?)\s*$/);
    if (m) labeledLines.push({ label: clean(m[1]), value: clean(m[2]) });
  });

  return {
    url: location.href,
    title: document.title,
    definitionLists,
    tables,
    formFields,
    actionElements,
    labeledLines,
    bodyText
  };
}
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="콘솔 콘솔 화면 구조 dump 도구 (읽기 전용)"
    )
    parser.add_argument(
        "--profile",
        default="pw_profile_console",
        help="로그인 세션을 저장할 프로필 폴더명 (기본: pw_profile_console)",
    )
    parser.add_argument(
        "--url",
        default="about:blank",
        help="처음 열 주소 (기본: about:blank)",
    )
    parser.add_argument(
        "--out",
        default="dumps_console",
        help="dump 결과를 저장할 폴더명 (기본: dumps_console)",
    )
    return parser.parse_args()


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n[안내] playwright 가 설치되어 있지 않습니다. 아래 두 줄을 먼저 실행하세요:")
        print("    pip install playwright")
        print("    python -m playwright install chromium\n")
        sys.exit(1)
    return sync_playwright


def _select_target_page(context, fallback):
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


def dump_page(page, dump_dir: Path):
    retries = 3
    retry_wait = 1.5
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = dump_dir / f"console_{ts}"

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    html_saved = False
    try:
        with open(f"{stem}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        html_saved = True
    except Exception as e:
        print(f"  (HTML 저장 실패: {e})")

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as e:
        print(f"  (캡처 실패: {e})")

    data = None
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            data = page.evaluate(EXTRACT_JS)
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"  [재시도 {attempt}/{retries}] 잠시 대기 후 재시도합니다 ({retry_wait}초)...")
                time.sleep(retry_wait)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass

    if data is None:
        saved_hint = "\n  HTML 파일은 저장됐으니 분석에 활용할 수 있습니다." if html_saved else ""
        raise RuntimeError(
            f"구조화 추출 실패 (원인: {last_err}){saved_hint}\n"
            "  화면이 멈춘 뒤 다시 Enter 를 눌러 재시도해 주세요."
        )

    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    lines = []
    lines.append(f"URL   : {data['url']}")
    lines.append(f"TITLE : {data['title']}")
    lines.append("")
    lines.append("===== 라벨:값 (텍스트 줄에서 추출) =====")
    for kv in data["labeledLines"]:
        lines.append(f"  {kv['label']} : {kv['value']}")
    lines.append("")
    lines.append("===== 정의목록(dl) =====")
    for i, dl in enumerate(data["definitionLists"], 1):
        lines.append(f"  [목록 {i}]")
        for kv in dl:
            lines.append(f"    {kv['label']} : {kv['value']}")
    lines.append("")
    lines.append("===== 표(table) =====")
    for i, table in enumerate(data["tables"], 1):
        lines.append(f"  [표 {i}]")
        for row in table:
            lines.append("    | " + " | ".join(row))
    lines.append("")
    lines.append("===== 입력필드(input/select/textarea) =====")
    for field in data["formFields"]:
        lines.append(
            f"    [{field['tag']}/{field['type']}] 라벨='{field['label']}' "
            f"name='{field['name']}' id='{field['id']}' "
            f"placeholder='{field['placeholder']}' 값='{field['value']}' "
            f"disabled={field['disabled']} readonly={field['readonly']}"
        )
    lines.append("")
    lines.append("===== 버튼/액션 요소 =====")
    for action in data["actionElements"]:
        lines.append(
            f"    [{action['tag']}] text='{action['text']}' id='{action['id']}' "
            f"class='{action['className']}' name='{action['name']}' "
            f"href='{action['href']}' disabled={action['disabled']}"
        )
        if action["onclick"]:
            lines.append(f"      onclick='{action['onclick']}'")
    lines.append("")
    lines.append("===== 화면 전체 텍스트 =====")
    lines.append(data["bodyText"])

    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n저장 완료 → {stem}.json / .txt / .html / .png")
    print(
        f"  - 라벨:값 줄 {len(data['labeledLines'])}개, "
        f"정의목록 {len(data['definitionLists'])}개, "
        f"표 {len(data['tables'])}개, "
        f"입력필드 {len(data['formFields'])}개, "
        f"액션요소 {len(data['actionElements'])}개 추출됨"
    )


def main():
    args = parse_args()
    sync_playwright = _load_playwright()
    profile_dir = BASE_DIR / args.profile
    dump_dir = BASE_DIR / args.out
    dump_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" 콘솔 콘솔 화면 구조 dump 도구")
    print("=" * 60)
    print(" [주의] 이 도구는 읽기 전용입니다. 자동 클릭이나 저장 동작은 하지 않습니다.")
    print(f" 프로필 폴더 : {profile_dir.name}")
    print(f" 저장 폴더   : {dump_dir.name}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        if args.url and args.url != "about:blank":
            page.goto(args.url)

        print("\n[1] 뜬 크롬 창에서 콘솔 콘솔에 직접 로그인하세요. (처음 한 번만)")
        print("[2] 분석할 조회/상세/영수증/우편함 화면을 여세요.")
        print("[3] 준비되면 여기로 돌아와 Enter 를 누르세요.")
        print("    - 다른 화면을 또 뜨려면: 브라우저에서 화면을 바꾼 뒤 다시 Enter")
        print("    - 종료: q 입력 후 Enter\n")

        while True:
            cmd = input(">> 떠올릴 준비가 되면 Enter (종료 q): ").strip().lower()
            if cmd == "q":
                break
            target = _select_target_page(context, page)
            try:
                dump_page(target, dump_dir)
            except Exception as e:
                print(f"  [오류] 추출 실패: {e}")

        context.close()
        print(f"\n종료했습니다. 결과는 {dump_dir.name} 폴더에 저장되었습니다.")


if __name__ == "__main__":
    main()
