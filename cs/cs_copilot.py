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
import re
import sys
import time
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
from cs_parse import list_known_packages, resolve_brand_package

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

  const bodyText = document.body ? document.body.innerText : "";
  const labeledLines = [];
  bodyText.split("\n").forEach(line => {
    const m = line.match(/^\s*([^\n:：]{1,30})\s*[:：]\s*(.+?)\s*$/);
    if (m) labeledLines.push({ label: clean(m[1]), value: clean(m[2]) });
  });

  const customFields = collectCustomFields();

  return { url: location.href, title: document.title,
           definitionLists, tables, formFields, labeledLines, bodyText,
           customFields: customFields.pairs, customFieldMap: customFields.map };
}
"""

# ── 상수 ──────────────────────────────────────────────────────────────────────

# 패키지 목록은 cs_parse.py의 패키지-브랜드 규칙에서 불러옵니다.
_ALL_PACKAGES = list_known_packages()

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
TICKET_URL_RE = re.compile(r"/tickets/[^/]+/(\d+)")
_DISPLAY_TICKET_RE = re.compile(r"#(\d{3,8})\b")  # #NNNNN 형식
_SIDEBAR_TICKET_RE = re.compile(r"^\s*(\d{3,8})\s*\|", re.MULTILINE)  # NNNNN | label 형식 (사이드바)
BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "pw_profile"
REFUNDED_STATES = {"REFUNDED", "PARTIALLY_REFUNDED", "PENDING_REFUND", "CANCELED"}

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


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _fmt_money(m):
    if not m:
        return "(금액정보 없음)"
    units = int(m.get("units", 0))
    nanos = int(m.get("nanos", 0))
    return f"{units + nanos / 1e9:,.2f} {m.get('currencyCode', '')}".strip()


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
            detail_lines.append(f"결제일   : {order_result['createTime']}")

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
            print(f" {k} : {v}")
    print(f" 채널     : {channel_disp}")
    if state_disp:
        print(f" 상태     : {state_disp}")
    for line in verdict["detail_lines"]:
        print(f" {line}")
    print(f" 판정     : {verdict['verdict_label']}")
    print(_SEP)


def _format_config_error(error_code: str):
    if error_code.startswith("env_missing:"):
        return f"환경변수 '{error_code.split(':', 1)[1]}'가 없거나 비어 있습니다."
    if error_code.startswith("brand_not_configured:"):
        return f"브랜드 '{error_code.split(':', 1)[1]}'가 CS_PACKAGE_BRAND_RULES에 없습니다."
    if error_code.startswith("field_not_configured:"):
        return f"브랜드 규칙에 '{error_code.split(':', 1)[1]}' 항목이 없습니다."
    return error_code


def _handle_ticket(page, ticket_id, service):
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

    custom_fields = parsed.get("selected_custom_fields") or None
    display_num = _find_display_ticket_num(data) or ticket_id  # #NNNNN 또는 URL ID
    order_result, http_status, _ = _fetch_order(service, packages, parsed["order_id"])
    verdict = build_verdict(parsed, order_result, http_status)
    _print_verdict(display_num, parsed, verdict, warnings=warnings or None,
                   custom_fields=custom_fields or None)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
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
            args=["--start-maximized"],
        )

        start_page = context.pages[0] if context.pages else context.new_page()
        for page in context.pages:
            _register_page(page)

        context.on("page", _register_page)

        try:
            start_page.goto("https://companyname.cs.example.com/", timeout=15_000)
        except Exception:
            pass  # 이미 로그인된 세션이면 리다이렉트 등으로 타임아웃 나도 무방

        print()
        print("=" * 44)
        print(" cs co-pilot 실행 중 (읽기 전용)")
        print("=" * 44)
        print(" cs 티켓 상세 페이지를 열면 자동으로 분석합니다.")
        print(" 종료: Ctrl+C")
        print()

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
                        _handle_ticket(page, tid, service)
                    except Exception as e:
                        print(f"[오류] 티켓 #{tid} 처리 실패: {e}")
        except KeyboardInterrupt:
            print("\n[종료] co-pilot을 종료합니다.")
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
