# -*- coding: utf-8 -*-
"""
cs_copilot.py — cs 실시간 보조 co-pilot (읽기 전용 MVP)
================================================================

상담원이 cs 티켓 상세 페이지를 열면 자동으로:
  필드 추출 → UUID·주문번호 정규화 → Play API orders.get 조회
  → 채널·환불상태 verdict를 PowerShell 패널에 출력.

실행:
  .venv\\Scripts\\python.exe cs_copilot.py --key <JSON키파일>
  또는 start_copilot.ps1 (원클릭)

옵션:
  --key  <경로>   Google 서비스 계정 JSON 키 파일 (필수)
  --test          build_verdict 단위 테스트 후 종료
"""

import argparse
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[설치 필요] playwright가 없습니다:")
    print("    .venv\\Scripts\\python.exe -m pip install playwright")
    print("    .venv\\Scripts\\python.exe -m playwright install chromium")
    sys.exit(1)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[설치 필요] google-api-python-client google-auth가 없습니다:")
    print("    .venv\\Scripts\\python.exe -m pip install google-api-python-client google-auth")
    sys.exit(1)

from cs_parse import parse_ticket  # import-time 부작용 없음, 직접 import

# ── EXTRACT_JS ────────────────────────────────────────────────────────────────
# cs_field_dump.py 동일 상수. import 시 DUMP_DIR.mkdir() 부작용이 있어 복사 사용.
from cs_parse import (
    list_known_packages,
    resolve_brand_console_project,
    resolve_brand_package,
    resolve_brand_regrant_mail,
)
from cs_gcp_logging import build_logging_service

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
      rows.get(rowKey).push({ x: rect.left, text });
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
      label: label, tag: el.tagName.toLowerCase(), type: type,
      name: el.getAttribute("name") || "", id: el.id || "",
      placeholder: el.getAttribute("placeholder") || "", value: value
    });
  });

  // 현재 티켓의 우측 메타 영역(생성/업데이트 시각). 라벨 문자열은 파서에서 쓰지 않고
  // 이 구조 안의 KST 시각 값만 사용한다.
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

  return { url: location.href, title: document.title,
           definitionLists, tables, formFields, labeledLines, bodyText,
           ticketMetaTimes,
           customFields: customFields.pairs, customFieldMap: customFields.map };
}
"""

# ── 상수 ──────────────────────────────────────────────────────────────────────

# 패키지 목록은 cs_parse.py의 패키지-브랜드 규칙에서 불러옵니다.
_ALL_PACKAGES = list_known_packages()

SCOPES = [
    "https://www.googleapis.com/auth/androidpublisher",
    "https://www.googleapis.com/auth/logging.read",
]
TICKET_URL_RE = re.compile(r"/tickets/[^/]+/(\d+)")
_DISPLAY_TICKET_RE = re.compile(r"#(\d{3,8})\b")  # #NNNNN 형식
_SIDEBAR_TICKET_RE = re.compile(r"^\s*(\d{3,8})\s*\|", re.MULTILINE)  # NNNNN | label 형식 (사이드바)
BASE_DIR = Path(__file__).resolve().parent
CONSOLE_DIR = BASE_DIR.parent / "console"
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

PROFILE_DIR = BASE_DIR / "pw_profile"
REFUNDED_STATES = {"REFUNDED", "PARTIALLY_REFUNDED", "PENDING_REFUND", "CANCELED"}

# 브라우저 창 위치/크기 기억(2026-07-09 사용자 요청). start_copilot.ps1이 띄우는
# 브라우저 2개(cs 티켓 창="cs_browser" 키, 콘솔 판정용 창="console_browser"
# 키) 모두 적용한다. 콘솔 판정용 창은 console_leaderboard.py 등이 쓰는
# pw_profile_console와는 별도 프로필(pw_profile_console_copilot)을 써서, 창 위치가
# 프로필에 새겨져 console_leaderboard.py로 새어가는 것을 막는다(2026-07-10).
# PowerShell 콘솔 창 쪽 기억 기능은 ConPTY 호스팅 환경에서 동작하지 않아 원복됨
# (`.claude/CHANGELOG.md` 2026-07-09 참고). 로컬 화면 배치일 뿐이라 .gitignore에
# 등록(커밋 금지).
WINDOW_STATE_PATH = BASE_DIR / "window_state.json"


def _load_window_state() -> dict:
    try:
        return json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _save_window_bounds(key: str, bounds: dict) -> None:
    """window_state.json에서 key 항목만 갱신한다(다른 키는 그대로 보존)."""
    state = _load_window_state()
    state[key] = bounds
    try:
        WINDOW_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[안내] 창 위치/크기 저장 실패({key}): {exc}")


def _window_launch_args(key: str) -> list[str]:
    """저장된 위치/크기가 있으면 그 값으로, 없거나(최초 실행) 최대화 상태로 저장돼
    있었으면 --start-maximized(기존 기본 동작)로 launch_persistent_context의 args를 만든다.
    """
    bounds = _load_window_state().get(key) or {}
    if bounds.get("maximized"):
        return ["--start-maximized"]
    try:
        left, top = int(bounds["left"]), int(bounds["top"])
        width, height = int(bounds["width"]), int(bounds["height"])
    except (KeyError, TypeError, ValueError):
        return ["--start-maximized"]
    if width <= 0 or height <= 0:
        return ["--start-maximized"]
    return [f"--window-position={left},{top}", f"--window-size={width},{height}"]


def _capture_window_bounds(context, key: str) -> None:
    """CDP(Browser.getWindowBounds)로 현재 브라우저 창 위치/크기를 읽어 저장한다.

    실패해도(창이 이미 닫혔거나 CDP 응답이 예상과 다르거나 등) 호출부 절차를 막으면
    안 되는 부가 기능이라 조용히 건너뛴다.

    ★ 반드시 이 Playwright context를 만든 바로 그 스레드에서만 호출한다.
    Playwright 동기 API는 스레드 전용(그린렛 기반 동기↔비동기 브리지가 생성
    스레드에 묶여 있음) — 2026-07-09 라이브 재현 테스트로 두 가지를 직접 확인했다:
    (1) Ctrl+C(KeyboardInterrupt)가 `page.wait_for_timeout()` 같은 Playwright
    동기 호출 도중 발생하면, 그 직후 같은 스레드에서 부르는 모든 Playwright 호출
    (`new_cdp_session()`, `context.close()` 포함)이 영원히 멈출 수 있다. (2) 이
    함수를 다른 스레드(예: 타임아웃을 걸려고 만든 데몬 스레드)에서 부르면 멈추진
    않지만 `greenlet.error: cannot switch to a different thread`로 항상 실패한다
    — 즉 "다른 스레드 + 타임아웃"으로는 이 문제를 못 피한다. 실제 해법은 이 함수를
    (1) 인터럽트가 없었던 게 확실한 정상 루프 중에 주기적으로 호출하고, (2)
    KeyboardInterrupt를 잡은 직후(종료 처리 중)에는 아예 호출하지 않는 것이다
    (호출부의 "종료 시 캡처 생략" 주석 참고 — `with sync_playwright()` 자체의
    종료 처리가 브라우저 프로세스 정리를 대신 맡는다).
    """
    try:
        pages = context.pages
        if not pages:
            return
        cdp = context.new_cdp_session(pages[0])
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        bounds = cdp.send("Browser.getWindowBounds", {"windowId": window_id})["bounds"]
        if bounds.get("windowState") == "maximized":
            # 최대화 상태 그대로면 다음 실행 때 --window-position/size가 무의미해지므로
            # 위치/크기 대신 "최대화였다"는 사실만 남긴다.
            _save_window_bounds(key, {"maximized": True})
            return
        _save_window_bounds(
            key,
            {
                "left": bounds.get("left", 0),
                "top": bounds.get("top", 0),
                "width": bounds.get("width", 0),
                "height": bounds.get("height", 0),
            },
        )
    except Exception:
        pass


# 결제정보 가격 패턴: 통화기호+숫자 또는 숫자+통화기호 (라벨 무관, 값 셀 직접 감지)
_PRICE_VAL_RE = re.compile(
    r"[A-Z]{0,3}[$€£¥₩R]\s*[\d,\.]+|[$€£¥₩]\s*[\d,\.]+|[\d,\.]+\s*[$€£¥₩]",
    re.IGNORECASE,
)
_SEP = "═" * 44

_STATE_LABEL = {
    "PROCESSED":          "정상 결제 완료",
    "PENDING":            "결제 대기/미확정",
    "CANCELED":           "취소됨",
    "PENDING_REFUND":     "환불 진행 중",
    "PARTIALLY_REFUNDED": "부분 환불됨",
    "REFUNDED":           "전액 환불됨",
}

# UTC ISO8601('...Z') → KST 표시 변환용 (2026-07-08 요청: 결제일을 KST로 보이게)
_UTC_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z$")

ANSI_GREEN = "\033[92m"
ANSI_RESET = "\033[0m"


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _fmt_money(m):
    if not m:
        return "(금액정보 없음)"
    units = int(m.get("units", 0))
    nanos = int(m.get("nanos", 0))
    return f"{units + nanos / 1e9:,.2f} {m.get('currencyCode', '')}".strip()


def _to_kst_display(iso_utc: str) -> str:
    """Google Play API의 UTC ISO8601('...Z') 문자열을 KST(UTC+9) 표시용으로 변환.

    형식이 안 맞으면 원문 그대로 반환한다(임의 보정 금지).
    """
    m = _UTC_ISO_RE.match(iso_utc or "")
    if not m:
        return iso_utc
    base, frac = m.groups()
    dt_kst = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=9)
    return dt_kst.strftime("%Y-%m-%dT%H:%M:%S") + (frac or "") + "KST"


def _supports_color() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _green(text: str) -> str:
    if not _supports_color():
        return text
    return f"{ANSI_GREEN}{text}{ANSI_RESET}"


def _enable_windows_ansi() -> None:
    """conhost 등 구형 콘솔에서도 ANSI 색상 코드가 실제 색으로 렌더링되도록
    가상 터미널 처리를 켠다(console_step_verify.configure_console_output과 동일 패턴).
    """
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


# ── 핵심 verdict 함수 — 배치 파이프라인 단계2·3 재사용 대상 ────────────────────

def build_verdict(parsed: dict, order_result: dict | None, http_status: int | None) -> dict:
    """검증 verdict 생성. 읽기 전용 — 외부 API 쓰기 없음.

    반환 키:
      channel       : "google" | "unknown"
      state         : Play API state 원문 (str | None)
      verdict_label : 한국어 판정 문구 (str)
      detail_lines  : ["금액     : …", "결제일   : …", …] (list[str])
      is_refunded   : bool
    """
    if order_result is not None:
        state = order_result.get("state", "STATE_UNSPECIFIED")
        is_refunded = state in REFUNDED_STATES

        if state in REFUNDED_STATES:
            verdict_label = "환불·취소됨 — 재처리 금지 / 이중수혜 주의"
        elif state == "PROCESSED":
            verdict_label = "정상 결제 (환불 아님)"
        elif state == "PENDING":
            verdict_label = "결제 대기 / 미확정"
        else:
            verdict_label = "상태 불명 — 사람 확인"

        detail_lines = []
        total = order_result.get("total")
        if total:
            detail_lines.append(f"금액     : {_fmt_money(total)}")
        if order_result.get("createTime"):
            detail_lines.append(f"결제일   : {_to_kst_display(order_result['createTime'])}")

        hist = order_result.get("orderHistory") or {}
        ref_ev = hist.get("refundEvent")
        if ref_ev:
            detail_lines.append(f"환불일   : {ref_ev.get('eventTime', '-')}")
        for j, pr in enumerate(hist.get("partialRefundEvents") or [], 1):
            detail_lines.append(
                f"부분환불#{j}: {pr.get('processTime', '-')} ({pr.get('state', '-')})"
            )

        for li in order_result.get("lineItems", []):
            detail_lines.append(f"Google 상품 코드 : {li.get('productId', '?')}")

        return {
            "channel": "google",
            "state": state,
            "verdict_label": verdict_label,
            "detail_lines": detail_lines,
            "is_refunded": is_refunded,
        }

    elif http_status == 404:
        return {
            "channel": "unknown",
            "state": None,
            "verdict_label": "구글 미조회 — 애플/식별실패 가능, 사람 확인",
            "detail_lines": [],
            "is_refunded": False,
        }

    else:
        err = f"HTTP {http_status}" if http_status else "연결 오류"
        return {
            "channel": "unknown",
            "state": None,
            "verdict_label": f"API 오류 — 사람 확인 ({err})",
            "detail_lines": [],
            "is_refunded": False,
        }


# ── build_verdict 단위 테스트 ─────────────────────────────────────────────────

def _run_verdict_tests():
    """합성 응답으로 build_verdict 케이스별 검증. 실 PII 하드코딩 없음."""
    passed = failed = 0

    def check(name, cond, msg=""):
        nonlocal passed, failed
        if cond:
            print(f"  [OK] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name}" + (f" — {msg}" if msg else ""))
            failed += 1

    _dummy = {"order_id": "GPA.0000-0000-0000-00000", "uuid": None, "brand": None, "flags": []}
    _empty_order = {"lineItems": [], "orderHistory": {}}

    # REFUNDED
    v = build_verdict(_dummy, {**_empty_order, "state": "REFUNDED"}, 200)
    check("REFUNDED → 재처리 금지 문구", "재처리 금지" in v["verdict_label"])
    check("REFUNDED is_refunded=True", v["is_refunded"])
    check("REFUNDED channel=google", v["channel"] == "google")

    # PARTIALLY_REFUNDED
    v = build_verdict(_dummy, {**_empty_order, "state": "PARTIALLY_REFUNDED"}, 200)
    check("PARTIALLY_REFUNDED is_refunded=True", v["is_refunded"])
    check("PARTIALLY_REFUNDED → 재처리 금지 문구", "재처리 금지" in v["verdict_label"])

    # PENDING_REFUND
    v = build_verdict(_dummy, {**_empty_order, "state": "PENDING_REFUND"}, 200)
    check("PENDING_REFUND is_refunded=True", v["is_refunded"])

    # CANCELED
    v = build_verdict(_dummy, {**_empty_order, "state": "CANCELED"}, 200)
    check("CANCELED is_refunded=True", v["is_refunded"])

    # PROCESSED
    v = build_verdict(_dummy, {**_empty_order, "state": "PROCESSED"}, 200)
    check("PROCESSED → 정상 결제 문구", "정상 결제" in v["verdict_label"])
    check("PROCESSED is_refunded=False", not v["is_refunded"])

    # PENDING
    v = build_verdict(_dummy, {**_empty_order, "state": "PENDING"}, 200)
    check("PENDING → 결제 대기 문구", "결제 대기" in v["verdict_label"])

    # 404
    v = build_verdict(_dummy, None, 404)
    check("404 → 구글 미조회 문구", "구글 미조회" in v["verdict_label"])
    check("404 channel=unknown", v["channel"] == "unknown")
    check("404 is_refunded=False", not v["is_refunded"])

    # API 오류 (500)
    v = build_verdict(_dummy, None, 500)
    check("500 → API 오류 문구", "API 오류" in v["verdict_label"])
    check("500 channel=unknown", v["channel"] == "unknown")

    # detail_lines: 금액·결제일·상품명 포함 확인
    _rich_order = {
        "state": "PROCESSED",
        "total": {"units": "55000", "nanos": 0, "currencyCode": "KRW"},
        "createTime": "2026-06-12T12:56:00Z",
        "lineItems": [{"productId": "p001", "productTitle": "아이템팩"}],
        "orderHistory": {
            "refundEvent": {"eventTime": "2026-06-14T11:36:00Z"}
        },
    }
    v = build_verdict(_dummy, _rich_order, 200)
    dl = "\n".join(v["detail_lines"])
    check("detail_lines 금액 포함", "55,000.00 KRW" in dl)
    check("detail_lines 결제일 포함", "2026-06-12" in dl)
    check("detail_lines 환불일 포함", "2026-06-14" in dl)
    check("detail_lines 상품 코드 포함", "p001" in dl)

    print(f"\n결과: 통과 {passed}건 / 실패 {failed}건")
    if failed:
        sys.exit(1)


# ── Playwright 헬퍼 ────────────────────────────────────────────────────────────

def _extract_from_page(page):
    """EXTRACT_JS로 페이지 필드 추출. 최대 3회 재시도."""
    # cs는 SPA — URL 변경 후 DOM이 AJAX로 늦게 채워지므로 networkidle 대기
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    for attempt in range(1, 4):
        try:
            return page.evaluate(EXTRACT_JS)
        except Exception as e:
            if attempt < 3:
                time.sleep(1.5)
            else:
                print(f"  [추출 실패] {e}")
                return None


def _fetch_order(service, packages, order_id):
    """패키지 목록을 순서대로 시도해 orders.get 결과 반환.

    반환: (order_result | None, http_status | None, package_used | None)
    """
    for pkg in packages:
        try:
            result = service.orders().get(packageName=pkg, orderId=order_id).execute()
            return result, 200, pkg
        except HttpError as e:
            status = int(getattr(getattr(e, "resp", None), "status", 0))
            if status == 404:
                continue
            return None, status, None
        except Exception:
            return None, None, None
    return None, 404, None  # 전체 패키지 404


def _find_display_ticket_num(data: dict) -> str | None:
    """cs UI 표시용 티켓 번호 추출.

    탐색 순서:
      1) title — #NNNNN 패턴
      2) labeledLines — 티켓/번호 라벨 → #NNNNN
      3) bodyText 상단 100줄 — 'NNNNN |' 사이드바 형식 우선, 없으면 #NNNNN
    찾으면 '#NNNNN' 문자열 반환, 없으면 None.
    """
    # 1) 페이지 제목
    m = _DISPLAY_TICKET_RE.search(data.get("title", ""))
    if m:
        return f"#{m.group(1)}"

    # 2) labeledLines — 티켓·번호 관련 라벨
    for ll in data.get("labeledLines", []):
        label = ll.get("label", "").lower()
        if any(k in label for k in ("티켓", "ticket", "번호", "no.")):
            m = _DISPLAY_TICKET_RE.search(ll.get("value", ""))
            if m:
                return f"#{m.group(1)}"

    # 3) bodyText 상단 100줄
    body_head = "\n".join(data.get("bodyText", "").splitlines()[:100])

    # 3a) 사이드바 형식: " 24671 | 재지급 " — cs 티켓 목록에서 표시되는 형식
    m = _SIDEBAR_TICKET_RE.search(body_head)
    if m:
        return f"#{m.group(1)}"

    # 3b) #NNNNN 형식 (미래 대비)
    m = _DISPLAY_TICKET_RE.search(body_head)
    if m:
        return f"#{m.group(1)}"

    return None


def _print_verdict(ticket_id, parsed, verdict, warnings=None, custom_fields=None):
    """PowerShell 패널에 verdict 출력."""
    brand = parsed.get("brand") or "(브랜드 없음)"
    uuid_val = parsed.get("uuid") or "(없음)"
    order_id = parsed.get("order_id") or "(없음)"

    channel_disp = (
        "Google (orders.get 확인)" if verdict["channel"] == "google" else "미확인"
    )
    state = verdict.get("state")
    state_disp = f"{state} → {_STATE_LABEL.get(state, state)}" if state else None

    print()
    print(_SEP)
    print(f" 티켓 {ticket_id}  |  브랜드: {brand}")
    print(_SEP)
    if warnings:
        for w in warnings:
            print(f" [경고] {w}")
    print(f" * UUID     : {uuid_val}")
    print(f" * 주문번호 : {order_id}")
    if custom_fields:
        for k, v in custom_fields.items():
            print(f" * {k} : {v}")
    print(f" 채널     : {channel_disp}")
    is_processed = state == "PROCESSED"
    if state_disp:
        line = f" 상태     : {state_disp}"
        print(_green(line) if is_processed else line)
    for detail in verdict["detail_lines"]:
        line = f" {detail}"
        if is_processed and detail.startswith("결제일"):
            line = _green(line)
        print(line)
    verdict_line = f" 판정     : {verdict['verdict_label']}"
    print(_green(verdict_line) if is_processed else verdict_line)
    print(_SEP)


def _format_config_error(error_code: str):
    if error_code.startswith("env_missing:"):
        return f"환경변수 '{error_code.split(':', 1)[1]}'가 없거나 비어 있습니다."
    if error_code.startswith("brand_not_configured:"):
        return f"브랜드 '{error_code.split(':', 1)[1]}'가 CS_PACKAGE_BRAND_RULES에 없습니다."
    if error_code.startswith("field_not_configured:"):
        return f"브랜드 규칙에 '{error_code.split(':', 1)[1]}' 항목이 없습니다."
    return error_code


class ConsoleJudgeWorker:
    """콘솔 지급 상태 판정을 별도 worker thread에서 처리한다."""

    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._tasks: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._worker_main,
            name="console-judge-worker",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._tasks.put(None)
        self._thread.join(timeout=10)

    def submit(self, ticket_id: str, parsed: dict, order_result: dict | None = None) -> None:
        order_result = order_result or {}
        line_items = order_result.get("lineItems") or []
        product_id = line_items[0].get("productId") if line_items else None
        self._tasks.put(
            {
                "task_type": "judge",
                "ticket_id": ticket_id,
                "uuid": parsed.get("uuid"),
                "nickname": parsed.get("nickname"),
                "nickname_source": parsed.get("nickname_source"),
                "brand": parsed.get("brand"),
                "order_id": parsed.get("order_id"),
                "product_id": product_id,
                "order_create_time": order_result.get("createTime"),
                "inquiry_time": parsed.get("inquiry_time"),
            }
        )

    def submit_regrant(self, ticket_id: str, uuid_value: str, product_code: str, brand: str | None = None) -> None:
        """사람이 터미널에 '재지급'을 입력해 승인한 뒤에만 호출한다(비가역 우편 발송)."""
        self._tasks.put(
            {
                "task_type": "regrant",
                "ticket_id": ticket_id,
                "uuid": uuid_value,
                "product_code": product_code,
                "brand": brand,
            }
        )

    def drain_results(self) -> list[dict]:
        items = []
        while True:
            try:
                items.append(self._results.get_nowait())
            except queue.Empty:
                break
        return items

    def _worker_main(self) -> None:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright as console_sync_playwright

            from console_payment_error import judge_nonpayment
            from console_post_register import (
                PostSendUncertainError,
                run_post_register_for_recipient,
            )
            from console_step_verify import (
                get_retry_max_retries,
                init_dump_dir,
                retry_with_recovery,
            )
            from console_user_search import (
                DEFAULT_START_URL as CONSOLE_START_URL,
                prepare_console_project,
                select_target_page as select_console_target_page,
            )
        except Exception as exc:  # noqa: BLE001
            self._results.put(
                {
                    "ticket_id": None,
                    "result": None,
                    "error": f"console worker 초기화 실패: {exc}",
                }
            )
            return

        # copilot 전용 대기값 분리(2026-07-11). 이 워커는 console_leaderboard.py 등
        # 사람이 직접 실행하며 화면을 지켜보는 콘솔 스크립트와 달리 무인 백그라운드
        # 스레드로 콘솔을 조작한다. get_step_wait_ms()(console_step_verify.py)가 읽는
        # CONSOLE_STEP_WAIT_MS/STEP_WAIT_MS를 그대로 쓰면 사람이 지켜보는 다른 콘솔
        # 스크립트의 대기값과 얽인다. 이 프로세스는 이 워커 스레드에서만
        # console_step_verify를 쓰므로, COPILOT_STEP_WAIT_MS가 설정돼 있으면 이 프로세스
        # 안에서만 CONSOLE_STEP_WAIT_MS를 덮어써 분리한다(공용 헬퍼 자체는 수정하지 않음).
        copilot_wait_ms = os.environ.get("COPILOT_STEP_WAIT_MS", "").strip()
        if copilot_wait_ms:
            os.environ["CONSOLE_STEP_WAIT_MS"] = copilot_wait_ms

        # 재베이스라인(UI_FP_REBASELINE)은 사람이 화면을 지켜보며 실행할 때만 안전하다
        # (켜진 채 일시적 이상 화면을 지나가면 fingerprint 기준이 오염됨). 이 워커는
        # 무인 백그라운드 스레드이므로, 외부에서 이 env가 켜져 있어도 이 프로세스
        # 안에서는 무조건 꺼서 새어들지 않게 한다(console_step_verify는 이 프로세스에서
        # 이 워커만 쓴다).
        os.environ.pop("UI_FP_REBASELINE", None)

        # console_leaderboard.py 등이 쓰는 pw_profile_console와는 별도 프로필을 쓴다
        # (2026-07-10 사용자 요청). 창 위치/크기 기억("console_browser" 키)이 프로필에
        # 새겨져 그 값을 console_leaderboard.py로도 새어가게 했던 문제를 프로필 분리로
        # 막는다. 대가: 이 프로필은 처음엔 로그인 세션이 없어 콘솔 로그인을 다시
        # 거쳐야 한다(기존 pw_profile_console의 로그인 세션을 재사용하지 않음).
        console_profile_dir = BASE_DIR / "pw_profile_console_copilot"
        logging_service = build_logging_service(self.key_path) if self.key_path else None
        # record_step_dump/step_and_verify_ui(console_*.py 전반에서 호출)는 dump 디렉터리가
        # 설정돼야 HTML/스크린샷을 남긴다 — 이전엔 미설정이라 이 worker의 실패를 사후에
        # 재현·확인할 증거가 전혀 안 남았다(2026-07-09, 닉네임 재검색 오탐 진단 중 확인).
        init_dump_dir(CONSOLE_DIR / "dumps_console_copilot_worker")

        with console_sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(console_profile_dir),
                    headless=False,
                    no_viewport=True,
                    args=_window_launch_args("console_browser"),
                )
            except Exception as exc:  # noqa: BLE001
                # 여기서 잡지 않으면 예외가 스레드 밖으로 나가 Python 기본 핸들러가
                # 전체 스택 트레이스를 PowerShell에 그대로 찍는다(데몬 스레드라
                # main 스레드에 전파되지도 않고 그냥 콘솔만 어지럽힌다). worker
                # 초기화 실패와 동일하게 결과 큐로만 조용히 알린다.
                #
                # str(exc) 자체가 Playwright의 "Browser logs:" 전문(launch 커맨드라인
                # 전체 + call log)을 포함하는 경우가 있어(TargetClosedError 등),
                # 그대로 쓰면 예외를 잡아도 화면엔 여전히 긴 로그가 그대로 남는다.
                # 사람이 볼 판정 실패 메시지는 첫 줄만 남기고, 나머지는 버린다.
                short_msg = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                self._results.put(
                    {
                        "ticket_id": None,
                        "result": None,
                        "error": f"console 브라우저 실행 실패: {short_msg}",
                    }
                )
                return
            try:
                while not self._stop_event.is_set():
                    task = self._tasks.get()
                    if task is None:
                        break

                    ticket_id = task["ticket_id"]
                    task_type = task.get("task_type", "judge")

                    if task_type == "regrant":
                        console_project_name = resolve_brand_console_project(task.get("brand"))
                        if not console_project_name:
                            # 아직 아무것도 발송하지 않은 시점 — 모호한 기본값으로 진행하지
                            # 않고 여기서 바로 중단한다(대상 선택 모호성 금지 원칙).
                            self._results.put({
                                "task_type": "regrant",
                                "ticket_id": ticket_id,
                                "result": {
                                    "status": "failed",
                                    "uuid": task["uuid"],
                                    "product_code": task["product_code"],
                                },
                                "error": f"브랜드 '{task.get('brand')}'의 console_project_name 설정 없음(CS_PACKAGE_BRAND_RULES 확인)",
                            })
                            continue
                        mail_title, mail_content = resolve_brand_regrant_mail(task.get("brand"))
                        if not mail_title or not mail_content:
                            self._results.put({
                                "task_type": "regrant",
                                "ticket_id": ticket_id,
                                "result": {
                                    "status": "failed",
                                    "uuid": task["uuid"],
                                    "product_code": task["product_code"],
                                },
                                "error": (
                                    f"브랜드 '{task.get('brand')}'의 재지급 우편 제목/내용 설정 없음"
                                    "(CS_PACKAGE_BRAND_RULES regrant_mail_title/regrant_mail_content 확인)"
                                ),
                            })
                            continue
                        # 발송 전 단계 실패만 재시도한다(AGENTS.md 원칙 4·10·11).
                        #  - action: 페이지 확보 + 발송 전체 절차(prepare→우편 화면→아이템→
                        #    수신자 등록→발송 확인)까지 포함(원칙 7). 발송 전에서 죽으면
                        #    아무것도 발송되지 않았으므로 멱등하게 다시 시작해도 안전하다.
                        #  - recovery: 다음 시도 전 초기 상태 복구 — 콘솔 홈으로 이동하며,
                        #    로그인 만료 시 prepare_console_project 내부 click_login_if_needed가
                        #    재로그인한다(원칙 8, 목표 화면으로 직접 URL 이동 금지).
                        #  - no_retry_exceptions: PostSendUncertainError(발송 확인 클릭 이후
                        #    불확실)는 재시도하면 중복 지급 위험 → 복구·재시도 없이 즉시 전파해
                        #    아래 except에서 "불확실"로 멈춘다(원칙 11).
                        def _do_regrant():
                            page = context.pages[0] if context.pages else context.new_page()
                            page = select_console_target_page(context, page)
                            return run_post_register_for_recipient(
                                page,
                                task["uuid"],
                                task["product_code"],
                                title=mail_title,
                                content=mail_content,
                                start_url=CONSOLE_START_URL,
                                project_name=console_project_name,
                            )

                        def _recover_regrant():
                            page = context.pages[0] if context.pages else context.new_page()
                            page = select_console_target_page(context, page)
                            prepare_console_project(page, "", CONSOLE_START_URL, console_project_name)

                        try:
                            summary = retry_with_recovery(
                                _do_regrant,
                                _recover_regrant,
                                label="재지급",
                                recovery_desc="콘솔 홈부터 재시작(필요 시 재로그인)",
                                max_retries=get_retry_max_retries(),
                                no_retry_exceptions=(PostSendUncertainError,),
                            )
                            self._results.put({
                                "task_type": "regrant",
                                "ticket_id": ticket_id,
                                "result": {
                                    "status": "sent",
                                    "uuid": task["uuid"],
                                    "product_code": task["product_code"],
                                    **summary,
                                },
                                "error": None,
                            })
                        except PostSendUncertainError as exc:
                            # 발송 확인 클릭 이후 예외 — 실제 발송 여부 불명. 재시도 절대 금지
                            # (AGENTS.md 원칙 11), 사람이 콘솔 우편 목록에서 직접 확인해야 한다.
                            self._results.put({
                                "task_type": "regrant",
                                "ticket_id": ticket_id,
                                "result": {
                                    "status": "uncertain",
                                    "uuid": task["uuid"],
                                    "product_code": task["product_code"],
                                },
                                "error": str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__,
                            })
                        except Exception as exc:  # noqa: BLE001
                            # 발송 확인 클릭 이전 실패 — 아무것도 발송되지 않았다.
                            short_msg = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                            self._results.put({
                                "task_type": "regrant",
                                "ticket_id": ticket_id,
                                "result": {
                                    "status": "failed",
                                    "uuid": task["uuid"],
                                    "product_code": task["product_code"],
                                },
                                "error": short_msg,
                            })
                        _capture_window_bounds(context, "console_browser")
                        continue

                    console_project_name = resolve_brand_console_project(task.get("brand"))
                    if not console_project_name:
                        # 모호한 기본값으로 진행하지 않고 이 티켓만 건너뛴다(대상 선택
                        # 모호성 금지 원칙 — 2026-07-07). 다른 티켓 처리는 막지 않는다.
                        self._results.put(
                            {
                                "task_type": "judge",
                                "ticket_id": ticket_id,
                                "result": None,
                                "error": f"브랜드 '{task.get('brand')}'의 console_project_name 설정 없음(CS_PACKAGE_BRAND_RULES 확인)",
                            }
                        )
                        continue

                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page = select_console_target_page(context, page)
                        result = judge_nonpayment(
                            page,
                            task["uuid"],
                            task["brand"],
                            order_id=task["order_id"] or None,
                            product_id=task.get("product_id") or None,
                            order_create_time=task.get("order_create_time") or None,
                            inquiry_time=task.get("inquiry_time") or None,
                            nickname=task.get("nickname") or None,
                            nickname_source=task.get("nickname_source"),
                            logging_service=logging_service,
                            start_url=CONSOLE_START_URL,
                            project_name=console_project_name,
                            timeout_error=PlaywrightTimeoutError,
                        )
                        self._results.put(
                            {
                                "task_type": "judge",
                                "ticket_id": ticket_id,
                                "brand": task.get("brand"),
                                "result": result,
                                "error": None,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Playwright 예외(TargetClosedError 등)는 str(exc)에 launch
                        # 커맨드라인·call log 전문이 붙어 나올 수 있어 첫 줄만 남긴다
                        # (위 launch_persistent_context 예외 처리와 동일한 이유).
                        short_msg = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                        self._results.put(
                            {
                                "task_type": "judge",
                                "ticket_id": ticket_id,
                                "result": None,
                                "error": short_msg,
                            }
                        )
                    # 판정 1건 끝날 때마다 창 위치를 갱신 저장한다 — 이 worker는 다음
                    # 티켓이 올 때까지 오래(수십 분~) 대기할 수 있어, 활동이 있을 때마다
                    # 갱신해두면 종료 시점 위치와 크게 어긋나지 않는다. 이 스레드는 파이썬
                    # SIGINT(Ctrl+C)를 절대 받지 않으므로(신호는 항상 메인 스레드로만
                    # 전달됨) Playwright 동기 호출이 인터럽트로 깨질 위험이 없어 그대로
                    # 직접 호출한다(_capture_window_bounds 자체가 실패는 조용히 삼킴).
                    _capture_window_bounds(context, "console_browser")
            finally:
                # 위와 같은 이유로 이 스레드는 인터럽트에 의한 Playwright 상태 손상
                # 위험이 없어(_capture_window_bounds 상단 주석 참고) 그대로 직접 호출한다.
                _capture_window_bounds(context, "console_browser")
                context.close()


# ── 재지급(우편 발송) ────────────────────────────────────────────────────────
# 사람이 이미 확정한 미지급 판정에 한해, 터미널에 '재지급'(단일 확정 상품) 또는
# '재지급 N'(GCP 후보 여러 건 중 N번)을 입력하면 우편으로 재지급한다.
# 대상 verdict는 두 그룹으로 나뉜다:
#  - SINGLE: product_code가 이미 하나로 확정됨 → '재지급'만으로 충분.
#  - CANDIDATE: GCP 로그 후보로 상품을 특정하는 분기 → 후보가 정확히
#    1건이면 SINGLE과 동일하게 product_code가 이미 확정돼 있고, 2건 이상이면
#    번호 지정이 필요하다.
REGRANT_SINGLE_VERDICTS = {
    "pattern1_regrant_shopdata_missing",  # ShopData에 상품 기록 없음 → 재지급
    "pattern1_regrant_no_followup_order",  # Count 미달 + 문의 이후 재결제 없음 → 재지급
    "pattern1_regrant_no_receipt_code",  # 같은 상품 정상 영수증 기록 없음 → 재지급
    "pattern1_regrant_unlimited",  # 무제한 구매 예외 상품 → 재지급
    "pattern1_regrant_limit_not_reached",  # ShopData Count가 구매 제한보다 작음 → 재지급
    "pattern1_regrant_no_period_receipt_code",  # 주기 초기화 이후 같은 상품 기록 없음 → 재지급
    "pattern1_regrant_period_limit_not_reached",  # 주기형 상품 Count가 제한보다 작음 → 재지급
    "pattern3_count_confirmed_missing",  # ShopData Count 기준 미지급 확정 (None/Onetime 한정)
    "pattern3_code_not_found",  # ShopData PurchaseCode 자체가 없음 → 구매 시도 기록조차 없어 미지급 확정
    "pattern2_regrant_unlimited",  # PurchaseCodeNull + 무제한 예외 → 재지급
    "pattern2_regrant_shopdata_missing",  # PurchaseCodeNull + ShopData 상품 기록 없음 → 재지급
    "pattern2_regrant_no_followup_order",  # PurchaseCodeNull + 문의 이후 재결제 없음 → 재지급
}
REGRANT_CANDIDATE_VERDICTS = {
    "pattern2_purchase_code_null",  # description=PurchaseCodeNull/빈값 → GCP 로그 후보로 상품 특정
}
REGRANT_COMMAND_RE = re.compile(r"^재지급(?:\s+(\d+))?$")
def _resolve_regrant_context(ticket_id, result):
    """판정 결과에서 재지급 가능 여부/대상을 뽑는다. 대상 아니면 None.

    반환: {"ticket_id", "uuid", "product_code"(확정 시) | None,
           "candidates"(여러 건일 때만) | None}
    """
    if not result:
        return None
    verdict = result.get("verdict")
    uuid_value = result.get("resolved_uuid") or result.get("submitted_uuid")
    if not uuid_value:
        return None
    if result.get("recommended_action") and result.get("recommended_action") != "regrant":
        return None

    if verdict in REGRANT_SINGLE_VERDICTS:
        product_code = result.get("product_code")
        if not product_code:
            return None
        return {"ticket_id": ticket_id, "uuid": uuid_value, "product_code": product_code, "candidates": None}

    if verdict in REGRANT_CANDIDATE_VERDICTS:
        product_code = result.get("product_code")
        candidates = result.get("product_candidates") or []
        if product_code and len(candidates) == 1:
            return {"ticket_id": ticket_id, "uuid": uuid_value, "product_code": product_code, "candidates": None}
        if len(candidates) > 1:
            return {"ticket_id": ticket_id, "uuid": uuid_value, "product_code": None, "candidates": candidates}
        return None

    return None


def _payment_error_sources_label(result):
    """이번 판정에서 실제로 조회한 데이터 소스를 판정 결과에서 도출해 헤더에 쓴다.

    분기마다 조회 소스가 다르다. 주문번호 미기록/PurchaseCodeNull은 상품 특정 후
    Onetime/None일 때 ShopData Count까지 조회할 수 있고, description 정상 분기도 ShopData를
    조회한다. verdict와 실제 결과값을 기준으로 조회 소스를 표기한다.

    "영수증검증 O/X"의 O/X는 영수증검증에서 매칭 행을 확인했는지(matched_row 존재
    여부, 2026-07-10 사용자 요청) 여부다 — pattern1(주문번호 미기록)은 매칭 행이
    없어 X, pattern2/3(매칭 행은 있으나 상품코드/Count로 갈림)은 O.
    """
    verdict = (result or {}).get("verdict") or ""
    if verdict == "invalid_uuid":
        return "유저 탭 UUID 존재 확인"
    matched_mark = "O" if (result or {}).get("matched_row") is not None else "X"
    receipt_label = f"영수증검증 {matched_mark}"
    if verdict.startswith("pattern3"):
        return f"{receipt_label} + ShopData"
    if verdict and verdict.startswith("pattern1_"):
        return f"{receipt_label} + 상품분기"
    if verdict.startswith("pattern2"):
        if (result or {}).get("shopdata") is not None:
            return f"{receipt_label} + 상품분기 + ShopData"
        return f"{receipt_label} + 상품분기"
    return receipt_label


# 판정 결과 note 중 최종 확정 결론(초록색 강조 대상)을 가려내는 표식.
# 재지급/환불처럼 "실행 후보"가 아니라, ShopData 대조로 확정된 지급 여부 결론만 강조한다.
_FINAL_NOTE_MARKERS = ("미지급 확정", "이미 지급됨")


def _is_final_verdict_note(note: str) -> bool:
    return any(marker in note for marker in _FINAL_NOTE_MARKERS)


def _short_date(date_str):
    """GCP 로그 후보 발생 시각에서 연도 접두사만 뺀다(2026-07-09T.. → 07-09T..).

    같은 티켓 안에서는 연도가 항상 현재 연도라 화면 표시엔 불필요하다는
    사용자 요청(2026-07-10). 형식이 다르면(연도 4자리+'-'가 아니면) 원본 그대로 둔다.
    """
    if isinstance(date_str, str) and len(date_str) > 5 and date_str[4] == "-" and date_str[:4].isdigit():
        return date_str[5:]
    return date_str


def _print_payment_error(ticket_id, result, error):
    from console_payment_error import describe_decision, describe_verdict, load_purchase_limit_info_map
    print(_SEP)
    if error or not result:
        # 판정을 못 끝냈으면(예외/결과 없음) 조회 소스를 특정할 수 없으므로 단정하지 않는다.
        print(f" [지급 상태 판정] 티켓 {ticket_id}")
    else:
        print(f" [지급 상태 판정] 티켓 {ticket_id} — {_payment_error_sources_label(result)}")
    if error:
        print(f"   판정 실패: {error}")
        print(_SEP)
        return None
    if not result:
        print("   판정 결과 없음")
        print(_SEP)
        return None
    print(f"   판정       : {describe_verdict(result.get('verdict'))}")
    decision_text = describe_decision(result)
    if decision_text:
        print(f"   처리분기   : {decision_text}")
    submitted_uuid = result.get("submitted_uuid")
    resolved_uuid = result.get("resolved_uuid")
    if submitted_uuid and resolved_uuid and submitted_uuid != resolved_uuid:
        print(f"   UUID       : 제출값={submitted_uuid} → 닉네임 대조로 확정={resolved_uuid}")
    regrant_ctx = _resolve_regrant_context(ticket_id, result)
    is_actionable = bool(regrant_ctx)

    # 상품코드는 아래 Inapp/GCP 후보 조회로 확정되는 결과이므로, 후보 나열보다 먼저
    # 보여주면 "이미 정해진 값"처럼 보여 인과관계가 뒤바뀐다(2026-07-10 사용자 지적) —
    # 후보 블록을 먼저 찍고 상품코드는 그 결과로서 마지막에 찍는다.
    candidates = result.get("product_candidates")
    if candidates:
        # 구매제한(유형/횟수)은 후보 상품마다 다르므로 후보 하나당 한 번씩 붙여 표시한다
        # (2026-07-10 사용자 요청 — 후보가 여럿이면 상품별로 값이 다를 수 있어 공용 한 줄로는 모호함).
        try:
            limit_map = load_purchase_limit_info_map()
        except Exception:  # noqa: BLE001 — 표시용 부가정보라 실패해도 판정은 계속한다
            limit_map = {}

        def _candidate_limit_str(code):
            info = limit_map.get(code) or {}
            limit_type = info.get("type") or "(미확인)"
            limit_count = info.get("count")
            count_disp = limit_count if limit_count is not None else "(미확인)"
            return f"{limit_type}, {count_disp}"

        if len(candidates) == 1:
            c = candidates[0]
            code = c.get("shop_click_id", "?")
            cand_line = (
                f"   GCP 로그 구매상품 1개: {code} @ {_short_date(c.get('update_date', '?'))} "
                f"({_candidate_limit_str(code)})"
            )
            print(_green(cand_line) if is_actionable else cand_line)
        else:
            cand_header = f"   GCP 로그 구매상품 {len(candidates)}개:"
            print(_green(cand_header) if is_actionable else cand_header)
            for i, c in enumerate(candidates, 1):
                code = c.get("shop_click_id", "?")
                print(f"     {i}) {code} @ {_short_date(c.get('update_date', '?'))} "
                      f"({_candidate_limit_str(code)})")
    product_line = f"   상품코드   : {result.get('product_code') or '(미특정)'}"
    print(_green(product_line) if is_actionable else product_line)
    sd = result.get("shopdata")
    if sd:
        print(f"   ShopData   : line={sd.get('purchase_line_number')} "
              f"count={sd.get('purchase_count')} judgment={sd.get('count_judgment')}")
    for note in result.get("notes", []) or []:
        line = f"   - {note}"
        # 최종 확정 결론(미지급 확정 / 이미 지급됨)은 초록색으로 강조(2026-07-08 사용자 요청).
        print(_green(line) if _is_final_verdict_note(note) else line)

    if regrant_ctx:
        if regrant_ctx["candidates"]:
            print(f"   [재지급 가능] 후보 중 번호를 골라 '재지급 N' 입력(예: 재지급 1)")
        else:
            print(f"   [재지급 가능] 상품코드={regrant_ctx['product_code']} — 터미널에 '재지급' 입력 시 우편 발송")
    elif result.get("recommended_action") == "refund":
        print(f"   [환불 후보] {describe_decision(result) or describe_verdict(result.get('verdict'))}")
    elif result.get("recommended_action") == "review":
        print(f"   [미결정] 사람 확인 필요")
    print(_SEP)
    return regrant_ctx


def _print_regrant_result(ticket_id, result, error):
    result = result or {}
    status = result.get("status")
    print(_SEP)
    print(f" [재지급] 티켓 {ticket_id}")
    print(f"   UUID       : {result.get('uuid')}")
    print(f"   상품코드   : {result.get('product_code')}")
    if status == "sent":
        print(f"   상태       : 발송 완료 (ShopTable_ID={result.get('shop_table_id')}, chart={result.get('chart_name')})")
    elif status == "uncertain":
        print("   상태       : 불확실 — 발송 확인 클릭 이후 오류 발생, 실제 발송 여부 화면 확인 필요")
        print("   조치       : 콘솔 우편 목록에서 직접 확인하세요. 이 건은 자동 재시도하지 않습니다.")
        print(f"   오류       : {error}")
    else:
        print("   상태       : 실패 (발송 전 단계에서 중단 — 우편이 발송되지 않았습니다)")
        print(f"   오류       : {error}")
    print(_SEP)


class TerminalCommandReader:
    """터미널 입력을 별도 daemon thread에서 non-blocking하게 읽어 큐에 쌓는다.

    메인 루프는 Playwright 이벤트 펌핑(wait_for_timeout)으로 바쁘므로, 여기서
    input()으로 블로킹 대기하는 스레드를 따로 두고 메인 루프는 매 tick마다
    drain()으로 쌓인 명령만 non-blocking하게 가져간다(ConsoleJudgeWorker의
    큐 패턴과 동일).
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="terminal-command-reader", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                line = input()
            except (EOFError, RuntimeError):
                break
            line = line.strip()
            if line:
                self._queue.put(line)

    def drain(self) -> list[str]:
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items


def _handle_command(command_text, regrant_state, console_worker):
    """'재지급'/'재지급 N' 명령 처리. 그 외 명령은 안내만 하고 무시한다."""
    m = REGRANT_COMMAND_RE.match(command_text.strip())
    if not m:
        print(f"[안내] 알 수 없는 명령: '{command_text}' (지원: 재지급, 재지급 N)")
        return

    ctx = regrant_state.get("last_actionable")
    if not ctx:
        print("[재지급] 재지급 가능한 판정 결과가 없습니다.")
        return

    index_str = m.group(1)
    index = int(index_str) if index_str else None

    if ctx["candidates"]:
        if index is None:
            print(f"[재지급] 후보가 {len(ctx['candidates'])}건입니다 — '재지급 N' 형식으로 번호를 지정하세요(예: 재지급 1).")
            return
        if not (1 <= index <= len(ctx["candidates"])):
            print(f"[재지급] 잘못된 번호입니다: {index} (1~{len(ctx['candidates'])})")
            return
        product_code = ctx["candidates"][index - 1].get("shop_click_id")
        if not product_code:
            print("[재지급] 선택한 후보에 상품코드(shop_click_id)가 없습니다.")
            return
    else:
        product_code = ctx["product_code"]

    # 1회성 소비 — 같은 결과에 '재지급'을 두 번 입력해도 중복 발송되지 않는다.
    regrant_state["last_actionable"] = None
    print(f"[재지급] 티켓 {ctx['ticket_id']} — UUID={ctx['uuid']}, 상품코드={product_code} 발송을 등록합니다.")
    console_worker.submit_regrant(ctx["ticket_id"], ctx["uuid"], product_code, ctx.get("brand"))


def _handle_ticket(page, ticket_id, service, console_worker=None, console_jobs=None):
    """티켓 상세 처리: 추출 → 파싱 → API 조회 → verdict 출력."""
    print("\n[티켓 감지] 분석 중...")

    data = _extract_from_page(page)
    if data is None:
        print(f"[티켓 #{ticket_id}] 추출 실패. 페이지가 로딩된 후 다시 열어보세요.")
        return

    parsed = parse_ticket(data)
    flags = parsed["flags"]
    config_errors = parsed.get("config_errors") or []
    warnings = []

    if "brand_missing" in flags:
        warnings.append("브랜드 없음 — 알려진 모든 패키지로 조회 시도")
    if "uuid_missing" in flags:
        warnings.append("UUID 없음")

    # 주문번호 없거나 형식 오류 → API 조회 생략
    if config_errors:
        print()
        print(_SEP)
        display_num = _find_display_ticket_num(data) or ticket_id
        print(f" ?곗폆 {display_num}  |  釉뚮옖?? {parsed.get('brand') or '(釉뚮옖???놁쓬)'}")
        print(_SEP)
        for item in config_errors:
            print(f" [오류] {_format_config_error(item)}")
        print(" ?먯젙     : 환경변수 규칙 오류")
        print(_SEP)
        return

    if "order_missing" in flags or "order_invalid" in flags:
        reason = (
            "주문번호 없음" if "order_missing" in flags
            else f"주문번호 형식 오류 ({parsed.get('order_norm_status')})"
        )
        print()
        print(_SEP)
        display_num = _find_display_ticket_num(data) or ticket_id
        print(f" 티켓 {display_num}  |  브랜드: {parsed.get('brand') or '(브랜드 없음)'}")
        print(_SEP)
        for w in warnings:
            print(f" [경고] {w}")
        print(f" * UUID     : {parsed.get('uuid') or '(없음)'}")
        print(f" * 주문번호 : (없음)")
        print(f" 판정     : 정보부족 — {reason}")
        print(_SEP)
        return

    # 패키지 결정
    brand = parsed.get("brand", "")
    package_name = parsed.get("app_package") or resolve_brand_package(brand)
    if package_name:
        packages = [package_name]
    elif brand:
        warnings.append(f"브랜드 '{brand}' 패키지 규칙 없음 — 전체 패키지 시도")
        packages = _ALL_PACKAGES
    else:
        packages = _ALL_PACKAGES

    # 주문번호 커스텀 필드는 원문 라벨(예: "GPA.로 시작하는 주문번호 (카드사, 페이,
    # 카카오톡 영수증 X)")이 그대로 찍혀 바로 아래 "* 주문번호 : ..." 줄과 중복
    # 표시된다. 파싱(selected_custom_fields 자체)은 그대로 두고 터미널 출력에서만
    # 값이 order_id_raw와 같은 항목을 제외한다(2026-07-08 요청, 표시 전용 간소화).
    custom_fields = parsed.get("selected_custom_fields") or {}
    order_id_raw = parsed.get("order_id_raw")
    if order_id_raw:
        custom_fields = {k: v for k, v in custom_fields.items() if v != order_id_raw}
    display_num = _find_display_ticket_num(data) or ticket_id  # #NNNNN 또는 URL ID
    order_result, http_status, _ = _fetch_order(service, packages, parsed["order_id"])
    verdict = build_verdict(parsed, order_result, http_status)
    _print_verdict(display_num, parsed, verdict, warnings=warnings or None,
                   custom_fields=custom_fields or None)

    # 영수증(주문) 조회됨 → 콘솔 지급 상태 판정 작업을 console worker에 비동기 등록
    if verdict.get("channel") == "google":
        if not parsed.get("uuid"):
            print(" [콘솔 지급 상태 판정] UUID 없음 — 생략")
        elif verdict.get("is_refunded"):
            print(" [콘솔 지급 상태 판정] 환불/취소 상태 — 생략")
        elif verdict.get("state") != "PROCESSED":
            print(f" [콘솔 지급 상태 판정] Google 주문 상태가 PROCESSED가 아님({verdict.get('state')}) — 생략")
        elif console_worker is None or console_jobs is None:
            print(" [콘솔 지급 상태 판정] worker 없음 — 생략")
        elif ticket_id in console_jobs:
            print(" [콘솔 지급 상태 판정] 이미 진행 중 — 중복 등록 생략")
        else:
            console_jobs[ticket_id] = {
                "uuid": parsed.get("uuid"),
                "brand": parsed.get("brand"),
                "order_id": parsed.get("order_id"),
            }
            console_worker.submit(ticket_id, parsed, order_result=order_result)
            print(" [콘솔 지급 상태 판정] 콘솔 worker에 등록했습니다. 결과는 준비되면 이어서 출력합니다.")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    _enable_windows_ansi()

    parser = argparse.ArgumentParser(description="cs 실시간 보조 co-pilot (읽기 전용)")
    parser.add_argument("--key", help="Google 서비스 계정 JSON 키 파일 경로")
    parser.add_argument("--test", action="store_true", help="build_verdict 단위 테스트 후 종료")
    args = parser.parse_args()

    if args.test:
        print("[build_verdict 단위 테스트]")
        _run_verdict_tests()
        return

    if not args.key:
        parser.error("--key 인자가 필요합니다.")

    key_path = Path(args.key)
    if not key_path.exists():
        print(f"[오류] 키 파일을 찾을 수 없습니다: {key_path}")
        sys.exit(1)

    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=SCOPES
        )
        service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
        print(f"[인증] OK — {getattr(creds, 'service_account_email', '(확인불가)')}")
    except Exception as e:
        print(f"[오류] Google API 인증 실패: {e}")
        sys.exit(1)

    # 페이지 등록 상태 (GIL로 dict 단순 읽기/쓰기 보호)
    pending = {}     # page_id -> ticket_id (Playwright 스레드에서 기록)
    page_map = {}    # page_id -> page object
    last_ticket = {} # page_id -> 마지막 처리 ticket_id
    console_jobs = {}  # ticket_id -> 간단 메타
    console_worker = ConsoleJudgeWorker(key_path)
    console_worker.start()
    command_reader = TerminalCommandReader()
    command_reader.start()
    regrant_state = {"last_actionable": None}  # 재지급 대상 판정 1건(1회성 소비)

    def _register_page(page):
        pid = id(page)
        page_map[pid] = page

        def _on_nav(frame):
            if frame.parent_frame is not None:  # 메인 프레임만
                return
            url = frame.url
            if not url:
                return
            m = TICKET_URL_RE.search(url)
            if not m:
                return
            tid = m.group(1)
            if last_ticket.get(pid) == tid:
                return  # 직전과 동일 ticket_id → 스킵
            pending[pid] = tid

        page.on("framenavigated", _on_nav)

        # 새 탭이 이미 티켓 URL에 있을 경우 즉시 감지 (레이스 컨디션 방지)
        try:
            m = TICKET_URL_RE.search(page.url)
            if m:
                tid = m.group(1)
                if last_ticket.get(pid) != tid:
                    pending[pid] = tid
        except Exception:
            pass

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
            args=_window_launch_args("cs_browser"),
        )

        start_page = context.pages[0] if context.pages else context.new_page()
        for page in context.pages:
            _register_page(page)

        context.on("page", _register_page)

        try:
            start_page.goto("https://companyname.cs.example.com/", timeout=15_000)
        except Exception:
            pass  # 이미 로그인된 세션이면 리다이렉트 등으로 타임아웃 나도 무방

        try:
            from cs_login import CsLoginError, ensure_logged_in

            ensure_logged_in(start_page, totp_secret=os.environ.get("CS_TOTP_SECRET"))
        except CsLoginError as e:
            print(f"[로그인 실패] {e}")
            print("[안내] 브라우저 창에서 직접 로그인을 확인한 뒤 다시 실행하세요.")
        except Exception as e:  # noqa: BLE001
            print(f"[로그인 확인 중 오류] {e}")

        print()
        print("=" * 44)
        print(" cs co-pilot 실행 중 (읽기 전용)")
        print("=" * 44)
        print()

        # 창 위치 주기 저장(2026-07-09 추가) — Ctrl+C 시점(종료 처리 중)의 저장은
        # 아예 시도하지 않는다(_capture_window_bounds 상단 주석 참고: 인터럽트 직후
        # Playwright 호출이 영원히 멈출 수 있고, 다른 스레드로 옮겨도 대신 즉시
        # 에러가 나 해결되지 않음). 대신 정상 동작 중 10초마다 저장해두면 대부분의
        # 경우 마지막 위치가 크게 뒤처지지 않는다.
        last_window_capture = time.monotonic()
        WINDOW_CAPTURE_INTERVAL_SECONDS = 10

        try:
            while True:
                # ── Playwright 이벤트 펌핑 ────────────────────────────────
                # time.sleep()은 Playwright 내부 메시지 루프를 돌리지 않아
                # framenavigated 콜백과 page.url 갱신이 모두 차단된다.
                # 살아있는 page로 wait_for_timeout을 호출해야 이벤트가 처리된다.
                alive_page = None
                for pid in list(page_map.keys()):
                    pg = page_map[pid]
                    try:
                        pg.url  # 살아있으면 OK, 닫혔으면 예외
                        alive_page = pg
                        break
                    except Exception:
                        page_map.pop(pid, None)
                        last_ticket.pop(pid, None)
                        pending.pop(pid, None)

                if alive_page:
                    try:
                        alive_page.wait_for_timeout(500)
                    except Exception:
                        page_map.pop(id(alive_page), None)
                        last_ticket.pop(id(alive_page), None)
                        pending.pop(id(alive_page), None)
                else:
                    time.sleep(0.5)  # 열린 탭이 없을 때만 fallback

                if time.monotonic() - last_window_capture >= WINDOW_CAPTURE_INTERVAL_SECONDS:
                    last_window_capture = time.monotonic()
                    # 방금 위 wait_for_timeout()이 인터럽트 없이 정상 반환한 직후라
                    # Playwright 상태가 멀쩡함이 보장됨 — 직접 호출한다
                    # (_capture_window_bounds 상단 주석의 스레드/인터럽트 제약 참고).
                    _capture_window_bounds(context, "cs_browser")

                for console_result in console_worker.drain_results():
                    result_ticket_id = console_result.get("ticket_id")
                    if console_result.get("task_type") == "regrant":
                        _print_regrant_result(
                            result_ticket_id or "(unknown)",
                            console_result.get("result"),
                            console_result.get("error"),
                        )
                        continue
                    regrant_ctx = _print_payment_error(
                        result_ticket_id or "(unknown)",
                        console_result.get("result"),
                        console_result.get("error"),
                    )
                    if regrant_ctx:
                        regrant_ctx["brand"] = console_result.get("brand")
                        regrant_state["last_actionable"] = regrant_ctx
                    if result_ticket_id in console_jobs:
                        console_jobs.pop(result_ticket_id, None)

                for command_text in command_reader.drain():
                    _handle_command(command_text, regrant_state, console_worker)

                # ── 1) 이벤트 기반 감지 (framenavigated) ─────────────────
                to_handle = {}
                for pid in list(pending.keys()):
                    tid = pending.pop(pid, None)
                    if tid is not None:
                        to_handle[pid] = tid

                # ── 2) URL 폴링 폴백 — 이벤트 누락 대비 ──────────────────
                for pid, page in list(page_map.items()):
                    if pid in to_handle:
                        continue
                    try:
                        m = TICKET_URL_RE.search(page.url)
                    except Exception:
                        page_map.pop(pid, None)
                        last_ticket.pop(pid, None)
                        pending.pop(pid, None)
                        continue
                    if not m:
                        continue
                    tid = m.group(1)
                    if last_ticket.get(pid) == tid:
                        continue
                    to_handle[pid] = tid

                # ── 3) 처리 ───────────────────────────────────────────────
                for pid, tid in to_handle.items():
                    page = page_map.get(pid)
                    if page is None:
                        continue
                    last_ticket[pid] = tid
                    try:
                        _handle_ticket(
                            page,
                            tid,
                            service,
                            console_worker=console_worker,
                            console_jobs=console_jobs,
                        )
                    except Exception as e:
                        print(f"[오류] 티켓 #{tid} 처리 실패: {e}")
        except KeyboardInterrupt:
            print("\n[종료] co-pilot을 종료합니다.")
        finally:
            console_worker.stop()
            # 2026-07-09 실측(라이브 재현 테스트): Ctrl+C가 위 루프의
            # alive_page.wait_for_timeout() 도중 발생하면, 그 직후 이 스레드에서 부르는
            # 모든 Playwright 호출(창 위치 캡처든 context.close()든)이 영원히 멈출 수
            # 있다는 것을 확인했다 — 다른 스레드로 옮겨도 해결되지 않는다(그린렛이 생성
            # 스레드 전용이라 스레드를 옮기면 대신 즉시 에러가 난다, _capture_window_bounds
            # 상단 주석 참고). 그래서 여기서는 아예 Playwright를 호출하지 않는다 — 위치는
            # 이미 10초마다 저장해뒀고(WINDOW_CAPTURE_INTERVAL_SECONDS), 브라우저 프로세스
            # 정리는 이 함수를 감싸는 `with sync_playwright() as p:` 블록 자신의 종료
            # 처리가 맡는다(재현 테스트로 이쪽은 항상 즉시 끝나는 것까지 확인함).


if __name__ == "__main__":
    main()
