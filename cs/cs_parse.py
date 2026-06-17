# -*- coding: utf-8 -*-
"""
cs_parse.py — cs dump에서 claim(고객 입력 주장) 추출 + 구글 주문번호 정규화
===================================================================================

원칙 (DECISIONS.md "cs 입력 신뢰 원칙"):
  - cs의 모든 값은 미검증 claim. 이 모듈은 **추출만** 하고 진실 판정 안 함.
  - 라벨은 다국어(EN/KO)라 한국어 라벨 하드코딩 금지 → 값 패턴 앵커로 파싱.
  - 채널 판정 안 함 (category는 claim으로만 보관).
  - 네트워크 호출 없음 (완전 오프라인).

CLI:
  python cs_parse.py <dump.json>       # 단일 파일 파싱 결과 출력
  python cs_parse.py --test            # 간이 자체 테스트 실행
"""

import re
import sys
import json
from pathlib import Path


# ────────────────────────────────────────────────────────────────────────────
# 정규식 상수
# ────────────────────────────────────────────────────────────────────────────

# 구글 주문번호 앵커: GPA 또는 GAP(오타) + 점 + 숫자·하이픈 나열
_GPA_ANCHOR = re.compile(r"(?:GPA|GAP)[.\s][0-9][0-9\-]{16,}", re.IGNORECASE)

# UUID (표준 하이픈 형식, 대소문자 무관)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# 첨부 파일명 앵커 (흔한 확장자)
_ATTACH_EXT = re.compile(
    r"\.(png|jpg|jpeg|gif|pdf|mp4|zip|txt|webp|bmp|mov|avi)$",
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────────────────────────
# 공개 함수 1: normalize_gpa_order
# ────────────────────────────────────────────────────────────────────────────

def normalize_gpa_order(raw: str):
    """구글 주문번호를 표준형 GPA.{4}-{4}-{4}-{5}으로 정규화.

    - GPA./GAP./점 생략/하이픈 생략/공백 등 다양한 입력을 흡수.
    - 숫자가 정확히 17자리일 때만 표준형 반환, status="ok".
    - 17자리가 아니면 (None, "digit_count=N") 반환 — 임의 보정 금지.

    반환: (표준형_문자열 | None, status_문자열)
    """
    digits = re.sub(r"\D", "", raw)
    n = len(digits)
    if n == 17:
        norm = f"GPA.{digits[0:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:17]}"
        return (norm, "ok")
    return (None, f"digit_count={n}")


# ────────────────────────────────────────────────────────────────────────────
# 공개 함수 2: parse_ticket
# ────────────────────────────────────────────────────────────────────────────

def parse_ticket(dump_json: dict) -> dict:
    """dump JSON(cs_field_dump.py 출력)에서 claim을 추출한다.

    입력 키: tables / formFields / definitionLists / labeledLines / bodyText

    반환 dict 키:
      order_id_raw   : 원문 그대로의 주문번호 후보 (str | None)
      order_id       : 정규화된 GPA.xxxx-… (str | None)
      order_norm_status : "ok" 또는 "digit_count=N" (str | None)
      uuid           : UUID 소문자 정규화 (str | None)
      brand          : 브랜드 (str | None)
      category       : 문의유형 claim (str | None) — 채널 판정 금지
      ticket_status  : 티켓 상태 (str | None)
      sender         : 보낸 사람 (str | None)
      body           : 최초 문의 메시지 본문 (str | None)
      attachments    : 첨부 파일명 목록 (list[str])
      flags          : 누락·형식불일치 신호 목록 (list[str])
    """
    flags = []

    # ── 1) ticket_info_* formFields ─────────────────────────────────────
    brand = category = ticket_status = sender = None
    _INFO_MAP = {
        "ticket_info_brand":    "brand",
        "ticket_info_category": "category",
        "ticket_info_status":   "ticket_status",
        "ticket_info_sender":   "sender",
    }
    for fld in dump_json.get("formFields", []):
        key = _INFO_MAP.get(fld.get("id", ""))
        if key:
            val = fld.get("value", "").strip()
            if key == "brand":
                brand = val or None
            elif key == "category":
                category = val or None
            elif key == "ticket_status":
                ticket_status = val or None
            elif key == "sender":
                sender = val or None

    if not brand:
        flags.append("brand_missing")
    if not category:
        flags.append("category_missing")

    # ── 2) GPA 주문번호 후보 추출 ────────────────────────────────────────
    order_id_raw = _find_gpa_raw(dump_json)

    order_id = order_norm_status = None
    if order_id_raw:
        order_id, order_norm_status = normalize_gpa_order(order_id_raw)
        if order_id is None:
            flags.append("order_invalid")
    else:
        flags.append("order_missing")

    # ── 3) UUID 추출 ──────────────────────────────────────────────────────
    uuid = _find_uuid(dump_json)
    if not uuid:
        flags.append("uuid_missing")

    # ── 4) 최초 문의 메시지 본문 ──────────────────────────────────────────
    body_text = dump_json.get("bodyText", "")
    body_msg = _extract_first_message(body_text)

    # ── 5) 첨부 파일명 목록 ───────────────────────────────────────────────
    attachments = _extract_attachments(body_text)

    return {
        "order_id_raw": order_id_raw,
        "order_id": order_id,
        "order_norm_status": order_norm_status,
        "uuid": uuid,
        "brand": brand,
        "category": category,
        "ticket_status": ticket_status,
        "sender": sender,
        "body": body_msg,
        "attachments": attachments,
        "flags": flags,
    }


# ────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ────────────────────────────────────────────────────────────────────────────

def _find_gpa_raw(dump_json: dict):
    """tables → bodyText 순으로 GPA 패턴 탐색. 원문 문자열 반환."""
    # tables: 2열 행의 값 셀
    for table in dump_json.get("tables", []):
        for row in table:
            if len(row) >= 2:
                m = _GPA_ANCHOR.search(row[1])
                if m:
                    return m.group(0)

    # bodyText 전체 탐색 (tables에 없으면)
    m = _GPA_ANCHOR.search(dump_json.get("bodyText", ""))
    if m:
        return m.group(0)

    return None


def _find_uuid(dump_json: dict):
    """tables → bodyText 순으로 UUID 탐색. 소문자 정규화해 반환."""
    for table in dump_json.get("tables", []):
        for row in table:
            for cell in row:
                m = _UUID_RE.search(cell)
                if m:
                    return m.group(0).lower()

    m = _UUID_RE.search(dump_json.get("bodyText", ""))
    if m:
        return m.group(0).lower()

    return None


def _extract_first_message(body_text: str):
    """bodyText에서 '최초 문의 메시지' 이후 고객 입력 본문 추출."""
    marker = "최초 문의 메시지"
    idx = body_text.find(marker)
    if idx < 0:
        return None

    segment = body_text[idx + len(marker):].lstrip("\n")

    # UI 버튼 "번역"은 항상 마커 바로 뒤에 등장 → 건너뜀
    if segment.startswith("번역\n"):
        segment = segment[len("번역\n"):]

    # 본문 끝 기준: 첨부파일 블록 / 날짜스탬프 / UI 영역
    end_patterns = ["첨부된 파일", "\n20", "\n답장\n", "\n티켓 메모\n", "\n티켓 정보\n"]
    end_idx = len(segment)
    for pat in end_patterns:
        pos = segment.find(pat)
        if 0 <= pos < end_idx:
            end_idx = pos

    result = segment[:end_idx].strip()
    return result or None


def _extract_attachments(body_text: str):
    """bodyText에서 첨부 파일명 목록 추출."""
    attachments = []
    marker = "첨부된 파일"
    idx = body_text.find(marker)
    if idx < 0:
        return attachments

    seg = body_text[idx + len(marker):].lstrip("\n")
    if seg.startswith("전체 다운로드\n"):
        seg = seg[len("전체 다운로드\n"):]

    for line in seg.splitlines():
        line = line.strip()
        if not line:
            continue
        if _ATTACH_EXT.search(line):
            attachments.append(line)
        elif re.match(r"\d{4}-\d{2}-\d{2}$", line):
            break  # 날짜 구분선 = 첨부 파일 섹션 끝

    return attachments


# ────────────────────────────────────────────────────────────────────────────
# 간이 자체 테스트 (python cs_parse.py --test)
# ────────────────────────────────────────────────────────────────────────────

def _run_tests():
    """오프라인 간이 테스트. 실패 시 AssertionError로 중단."""
    passed = 0
    failed = 0

    GPA_FMT = re.compile(r"^GPA\.\d{4}-\d{4}-\d{4}-\d{5}$")
    UUID_FMT = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    def check(name, cond, msg=""):
        nonlocal passed, failed
        if cond:
            print(f"  [OK] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name}" + (f" — {msg}" if msg else ""))
            failed += 1

    # ── normalize_gpa_order 단위 테스트 (합성 입력) ──────────────────────
    print("\n[1] normalize_gpa_order 단위 테스트")

    # 표준형 → 왕복(round-trip)
    synthetic_17 = "12345678901234567"
    norm, st = normalize_gpa_order(synthetic_17)
    check("17자리 순자→표준형", st == "ok" and GPA_FMT.match(norm or ""))
    if norm:
        back = re.sub(r"\D", "", norm)
        check("표준형→숫자 왕복", back == synthetic_17, f"{back!r} != {synthetic_17!r}")

    # GPA./GAP. 접두사 / 하이픈 생략 흡수
    variants = [
        "GAP.1234-5678-9012-34567",
        "GPA.1234-5678-9012-34567",
        "12345678901234567",
        "GPA12345678901234567",    # 점 생략
        "1234-5678-9012-34567",    # GPA 없이 하이픈만
    ]
    for v in variants:
        n2, s2 = normalize_gpa_order(v)
        check(f"흡수: {v!r}", s2 == "ok" and GPA_FMT.match(n2 or ""))

    # 자리수 불일치 → None 반환
    bad_cases = [
        ("abc",                  0),
        ("GPA.1234-5678-9012-3456",  16),   # 1자리 부족
        ("GPA.1234-5678-9012-345678", 18),  # 1자리 초과
    ]
    for raw, expected_n in bad_cases:
        n3, s3 = normalize_gpa_order(raw)
        check(f"불일치→None: {raw!r}", n3 is None and s3 == f"digit_count={expected_n}")

    # ── dump 3건 파싱 테스트 (형식 검증, 실값 하드코딩 금지) ─────────────
    print("\n[2] dump 파일 파싱 테스트 (형식·왕복 검증)")

    base = Path(__file__).resolve().parent / "dumps"
    fixtures = [
        "cs_20260615_185836.json",
        "cs_20260615_190850.json",
        "cs_20260615_190904.json",
    ]

    for fname in fixtures:
        fpath = base / fname
        if not fpath.exists():
            print(f"  [SKIP] {fname} — 파일 없음")
            continue
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
        result = parse_ticket(data)
        label = fname[:24]

        # order_id: 있으면 표준형, 없으면 flags에 order_* 있어야 함
        oid = result["order_id"]
        if oid:
            check(f"{label} order_id 형식", bool(GPA_FMT.match(oid)), oid)
            # 왕복: 정규화된 값을 다시 normalize 해도 동일해야 함
            oid2, _ = normalize_gpa_order(oid)
            check(f"{label} order_id 왕복", oid == oid2)
        else:
            check(
                f"{label} order_id 누락 플래그",
                any(f.startswith("order_") for f in result["flags"]),
                str(result["flags"]),
            )

        # uuid: 있으면 UUID 형식
        uuid = result["uuid"]
        if uuid:
            check(f"{label} uuid 형식", bool(UUID_FMT.match(uuid)), uuid)
        else:
            check(
                f"{label} uuid 누락 플래그",
                "uuid_missing" in result["flags"],
                str(result["flags"]),
            )

        # brand / category: 있으면 비어 있지 않은 문자열
        if result["brand"]:
            check(f"{label} brand 비어있지 않음", bool(result["brand"].strip()))
        if result["category"]:
            check(f"{label} category 비어있지 않음", bool(result["category"].strip()))

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    print(f"\n결과: 통과 {passed}건 / 실패 {failed}건")
    if failed:
        sys.exit(1)


# ────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ────────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print("사용법:")
        print("  python cs_parse.py <dump.json>   # 파싱 결과 출력")
        print("  python cs_parse.py --test        # 자체 테스트 실행")
        sys.exit(0)

    if args[0] == "--test":
        _run_tests()
        return

    path = Path(args[0])
    if not path.exists():
        print(f"[오류] 파일을 찾을 수 없습니다: {path}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    result = parse_ticket(data)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
