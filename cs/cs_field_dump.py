# -*- coding: utf-8 -*-
"""
cs 문의 상세 화면 '필드 구조' 추출 스크립트
================================================

목적
----
cs 문의 상세 페이지를 열어, 화면에 있는 항목(라벨)·값·입력필드·표·본문을
한꺼번에 떠서(dump) 파일로 저장한다. 이 결과를 보면 어떤 커스텀 필드
(주문번호, UUID 등)가 어떤 라벨/형식으로 들어오는지 매핑할 수 있다.

안전 설계
--------
- 이 스크립트는 비밀번호를 묻거나 저장하지 않는다.
- 브라우저 창을 띄워두면, 사람이 직접 로그인하고 문의 화면을 연다.
- 로그인 세션은 옆에 만들어지는 'pw_profile' 폴더에 보관 → 다음 실행부터 자동 로그인.

준비물 (한 번만)
----------------
1) 파이썬 설치 (3.9 이상)        : https://www.python.org/downloads/
2) 터미널(명령 프롬프트)에서:
       pip install playwright
       python -m playwright install chromium

실행 방법
--------
   python cs_field_dump.py

   → 크롬 창이 하나 뜬다.
   → (처음이면) cs에 로그인한다.
   → 분석하고 싶은 '문의 상세' 화면을 그 창에서 연다.
   → 터미널로 돌아와 Enter 를 누른다.
   → 현재 보고 있는 화면을 떠서 'dumps' 폴더에 저장한다.
   → 다른 유형의 문의도 보고 싶으면, 창에서 그 문의를 열고 다시 Enter.
   → 끝내려면 터미널에서 q 입력 후 Enter (또는 그냥 창을 닫는다).

저장 결과 (dumps 폴더)
---------------------
   - cs_<시간>.json   : 라벨·값, 입력필드, 표, "키:값" 줄, 본문 텍스트(구조화)
   - cs_<시간>.txt    : 화면 전체 텍스트(사람이 읽기 좋음)
   - cs_<시간>.html   : 페이지 원본 HTML(정밀 분석용)
   - cs_<시간>.png    : 전체 화면 캡처

이 4개 파일을 Claude에게 주면(특히 .json 과 .txt) 필드 매핑을 정리해 준다.
"""

import os
import sys
import json
import time
import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("\n[안내] playwright 가 설치되어 있지 않습니다. 아래 두 줄을 먼저 실행하세요:")
    print("    pip install playwright")
    print("    python -m playwright install chromium\n")
    sys.exit(1)

# 이 스크립트가 있는 폴더 기준으로 경로 설정
BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "pw_profile"   # 로그인 세션 저장 (자동 생성)
DUMP_DIR = BASE_DIR / "dumps"           # 추출 결과 저장 (자동 생성)
DUMP_DIR.mkdir(exist_ok=True)

# 처음 띄울 주소 (원하면 cs 주소로 바꿔도 됨. 비워두면 빈 페이지)
START_URL = "about:blank"

# 탭 선택 시 제외할 URL 패턴 (로그인·리다이렉트 페이지)
_LOGIN_SKIP = ("/login", "/signin", "/oauth", "/logout", "about:blank")

# 화면에서 정보를 긁어내는 JavaScript.
# - 정의목록(dl/dt/dd), 표(table), 입력필드(input/select/textarea), 라벨,
#   그리고 "라벨 : 값" 형태의 텍스트 줄을 모은다.
EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return (
      style &&
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      rect.width > 0 &&
      rect.height > 0
    );
  };

  const findTicketInfoPanel = () => {
    const headings = Array.from(document.querySelectorAll("body *")).filter(
      el => clean(el.innerText) === "티켓 정보"
    );
    for (const heading of headings) {
      let node = heading;
      for (let depth = 0; depth < 6 && node; depth += 1) {
        const text = clean(node.innerText);
        if (text.includes("티켓 정보") && text.includes("추가 정보")) {
          return node;
        }
        node = node.parentElement;
      }
    }
    return null;
  };

  const collectCustomFields = () => {
    const panel = findTicketInfoPanel();
    if (!panel) {
      return { pairs: [], map: {} };
    }

    const additionalTab = Array.from(panel.querySelectorAll("*")).find(
      el => clean(el.innerText) === "추가 정보"
    );
    const panelRect = panel.getBoundingClientRect();
    const minTop = additionalTab
      ? additionalTab.getBoundingClientRect().bottom + 8
      : panelRect.top;

    const rows = new Map();
    Array.from(panel.querySelectorAll("*")).forEach(el => {
      if (!isVisible(el)) return;
      const text = clean(el.innerText);
      if (!text) return;

      const rect = el.getBoundingClientRect();
      if (rect.top < minTop || rect.left < panelRect.left || rect.right > panelRect.right + 1) {
        return;
      }

      const childTextElements = Array.from(el.children).filter(
        child => isVisible(child) && clean(child.innerText)
      );
      if (childTextElements.length) return;

      const rowKey = String(Math.round(rect.top / 4) * 4);
      if (!rows.has(rowKey)) rows.set(rowKey, []);
      rows.get(rowKey).push({
        x: rect.left,
        text,
      });
    });

    const pairs = [];
    Array.from(rows.keys())
      .sort((a, b) => Number(a) - Number(b))
      .forEach(rowKey => {
        const items = rows.get(rowKey)
          .sort((a, b) => a.x - b.x)
          .filter((item, index, arr) => index === 0 || item.text !== arr[index - 1].text);
        if (items.length < 2) return;

        const label = clean(items[0].text);
        const value = clean(items.slice(1).map(item => item.text).join(" "));
        if (!label || !value) return;
        pairs.push({ label, value });
      });

    const map = {};
    pairs.forEach(pair => {
      map[pair.label] = pair.value;
    });
    return { pairs, map };
  };

  // 1) 정의목록 dl > dt/dd  (cs 같은 폼이 자주 쓰는 구조)
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

  // 2) 표 table > tr > th/td
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

  // 3) 입력 필드: 라벨 텍스트와 값/속성 함께
  const labelFor = {};
  document.querySelectorAll("label").forEach(l => {
    const f = l.getAttribute("for");
    if (f) labelFor[f] = clean(l.innerText);
  });
  const formFields = [];
  document.querySelectorAll("input, select, textarea").forEach(el => {
    const type = (el.getAttribute("type") || el.tagName).toLowerCase();
    if (["hidden", "submit", "button"].includes(type)) return;
    // 가까운 라벨 추정: for 매칭 → 부모 label → 앞 형제 텍스트
    let label = labelFor[el.id] || "";
    if (!label) {
      const pl = el.closest("label");
      if (pl) label = clean(pl.innerText);
    }
    if (!label && el.previousElementSibling) label = clean(el.previousElementSibling.innerText);
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
      value: value
    });
  });

  // 4) "라벨 : 값" 형태의 텍스트 줄 (한글/영문 콜론 모두)
  const ticketMetaTimes = Array.from(
    document.querySelectorAll(".box-aside-body-top-upper p.text-xs.text-dark-grey span")
  ).map(el => clean(el.innerText)).filter(Boolean);

  const bodyText = document.body ? document.body.innerText : "";
  const labeledLines = [];
  bodyText.split("\n").forEach(line => {
    const m = line.match(/^\s*([^\n:：]{1,30})\s*[:：]\s*(.+?)\s*$/);
    if (m) labeledLines.push({ label: clean(m[1]), value: clean(m[2]) });
  });

  const customFields = collectCustomFields();

  return {
    url: location.href,
    title: document.title,
    definitionLists,
    tables,
    formFields,
    labeledLines,
    bodyText,
    ticketMetaTimes,
    customFields: customFields.pairs,
    customFieldMap: customFields.map
  };
}
"""


def _select_target_page(context, fallback):
    """활성 탭 선택: 로그인·about:blank 제외 → 마지막(가장 최근) 탭.
    bring_to_front()로 해당 탭을 전면으로 올린다.
    """
    candidates = [
        pg for pg in context.pages
        if not any(pat in pg.url for pat in _LOGIN_SKIP)
    ]
    if not candidates:
        return fallback
    target = candidates[-1]
    try:
        target.bring_to_front()
    except Exception:
        pass
    return target


def dump_page(page):
    """현재 페이지를 떠서 4개 파일로 저장.

    순서: ① 안정화 대기 → ② HTML·PNG 먼저 저장(best-effort) →
          ③ 구조화 추출(evaluate, 최대 3회 재시도) → ④ JSON·TXT 저장.
    evaluate가 실패해도 HTML·PNG는 이미 저장된다.

    TODO(iframe): cs 상세 본문이 iframe 안에 렌더되는지 미확정.
    HTML 저장 후 <iframe> 유무를 확인해 확정되면 evaluate 대상을
    해당 프레임으로 교체 필요. (OPEN_ISSUES.md #1 연계)
    """
    _RETRIES = 3
    _RETRY_WAIT = 1.5

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = DUMP_DIR / f"cs_{ts}"

    # ── 1) 페이지 안정화 대기 ──────────────────────────────────────────
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass  # SPA가 networkidle 미도달해도 계속 진행

    # ── 2) HTML · PNG 먼저 저장 (best-effort, 구조화 실패와 무관) ──────
    html_saved = False
    for _html_attempt in range(3):
        try:
            html_content = page.content()
            with open(f"{stem}.html", "w", encoding="utf-8") as f:
                f.write(html_content)
            html_saved = True
            break
        except Exception as e:
            if _html_attempt < 2:
                time.sleep(1.0)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
            else:
                print(f"  (HTML 저장 실패: {e})")

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as e:
        print(f"  (캡처 실패: {e})")

    # ── 3) 구조화 추출 (최대 3회 재시도) ──────────────────────────────
    data = None
    last_err = None
    for attempt in range(1, _RETRIES + 1):
        try:
            data = page.evaluate(EXTRACT_JS)
            break
        except Exception as e:
            last_err = e
            if attempt < _RETRIES:
                print(f"  [재시도 {attempt}/{_RETRIES}] 잠시 대기 후 재시도합니다 ({_RETRY_WAIT}초)...")
                time.sleep(_RETRY_WAIT)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass

    if data is None:
        saved_hint = "\n  HTML 파일은 저장됐으니 분석에 활용할 수 있습니다." if html_saved else ""
        raise RuntimeError(
            f"구조화 추출 실패 (원인: {last_err}){saved_hint}\n"
            "  화면이 완전히 멈춘 상태에서 다시 Enter 를 눌러보세요."
        )

    # ── 4) JSON 저장 ──────────────────────────────────────────────────
    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 5) TXT 저장 ───────────────────────────────────────────────────
    lines = []
    lines.append(f"URL   : {data['url']}")
    lines.append(f"TITLE : {data['title']}")
    lines.append("")
    lines.append("===== 라벨:값 (텍스트 줄에서 추출) =====")
    for kv in data["labeledLines"]:
        lines.append(f"  {kv['label']} : {kv['value']}")
    lines.append("")
    lines.append("===== 우측 추가 정보(custom fields) =====")
    for pair in data.get("customFields", []):
        lines.append(f"  {pair['label']} : {pair['value']}")
    lines.append("")
    lines.append("===== 정의목록(dl) =====")
    for i, dl in enumerate(data["definitionLists"], 1):
        lines.append(f"  [목록 {i}]")
        for kv in dl:
            lines.append(f"    {kv['label']} : {kv['value']}")
    lines.append("")
    lines.append("===== 표(table) =====")
    for i, t in enumerate(data["tables"], 1):
        lines.append(f"  [표 {i}]")
        for row in t:
            lines.append("    | " + " | ".join(row))
    lines.append("")
    lines.append("===== 입력필드(input/select/textarea) =====")
    for fld in data["formFields"]:
        lines.append(f"    [{fld['tag']}/{fld['type']}] 라벨='{fld['label']}' "
                     f"name='{fld['name']}' id='{fld['id']}' "
                     f"placeholder='{fld['placeholder']}' 값='{fld['value']}'")
    lines.append("")
    lines.append("===== 화면 전체 텍스트 =====")
    lines.append(data["bodyText"])
    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n저장 완료 → {stem}.json / .txt / .html / .png")
    print(f"  - 라벨:값 줄 {len(data['labeledLines'])}개, "
          f"정의목록 {len(data['definitionLists'])}개, "
          f"표 {len(data['tables'])}개, "
          f"입력필드 {len(data['formFields'])}개 추출됨")


def main():
    print("=" * 60)
    print(" cs 문의 화면 필드 추출기")
    print("=" * 60)
    with sync_playwright() as p:
        # 사람이 직접 로그인할 수 있도록 '보이는' 브라우저로 실행 (headless=False)
        # 로그인 세션은 PROFILE_DIR 에 저장되어 다음 실행 때 유지됨
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport=None,  # 실제 창 크기 사용
        )
        page = context.pages[0] if context.pages else context.new_page()
        if START_URL and START_URL != "about:blank":
            page.goto(START_URL)

        print("\n[1] 뜬 크롬 창에서 cs에 로그인하세요. (처음 한 번만)")
        print("[2] 분석할 '문의 상세' 화면을 그 창에서 여세요.")
        print("[3] 준비되면 여기로 돌아와 Enter 를 누르세요.")
        print("    - 다른 문의를 또 뜨려면: 창에서 그 문의를 열고 다시 Enter")
        print("    - 종료: q 입력 후 Enter\n")

        while True:
            cmd = input(">> 떠올릴 준비가 되면 Enter (종료 q): ").strip().lower()
            if cmd == "q":
                break
            # 활성 탭 선택 (로그인·about:blank 제외, bring_to_front 적용)
            target = _select_target_page(context, page)
            try:
                dump_page(target)
            except Exception as e:
                print(f"  [오류] 추출 실패: {e}")

        context.close()
        print("\n종료했습니다. dumps 폴더의 파일을 Claude에게 전달하세요.")


if __name__ == "__main__":
    main()
