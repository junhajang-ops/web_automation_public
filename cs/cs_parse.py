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
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
ENV_PATH = ROOT_DIR / ".env"
_DOTENV_LOADED = False


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

# '보낸 사람' 필드는 "표시명 <email>" 형식. UUID 오탈자 대조용 닉네임 claim이
# 티켓 폼에 아예 없는 브랜드(2026-07-08 실측: "게임타이틀" 문의 양식엔 닉네임
# 항목 자체가 없음)의 폴백으로 쓴다 — 사용자가 cs 표시명을 이메일 앞에
# 실제 닉네임이 오도록 별도로 설정해뒀다고 확인(2026-07-08)해 이 표시명은
# 인게임 닉네임과 동일하게 취급한다(추측성 폴백이 아니라 정확한 값).
_SENDER_DISPLAY_NAME_RE = re.compile(r"^(.*?)\s*<[^<>]*>\s*$")

_CUSTOM_FIELD_RULES_DEFAULT_ENV = "CS_CUSTOM_FIELD_RULES_DEFAULT"
_CUSTOM_FIELD_RULES_BRAND_ENV = "CS_CUSTOM_FIELD_RULES_BY_BRAND"
_PACKAGE_BRAND_RULES_ENV = "CS_PACKAGE_BRAND_RULES"
_REQUIRED_PACKAGE_RULE_KEYS = ("uuid", "order_id", "order_meta", "order_time")
_WARNED_ENV_NAMES = set()


def _clean_text(value: str):
    return re.sub(r"\s+", " ", (value or "")).strip()


def _warn_env_once(name: str, message: str):
    if name in _WARNED_ENV_NAMES:
        return
    _WARNED_ENV_NAMES.add(name)
    print(f"[경고] {name}: {message}", file=sys.stderr)


def _load_dotenv_once():
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    if not ENV_PATH.exists():
        return

    # 역슬래시(\) 로 끝나는 줄은 다음 줄과 연결 (긴 JSON 값의 줄바꿈 지원)
    raw_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    joined: list[str] = []
    buf = ""
    for raw_line in raw_lines:
        if raw_line.endswith("\\"):
            buf += raw_line[:-1]
        else:
            buf += raw_line
            joined.append(buf)
            buf = ""
    if buf:
        joined.append(buf)

    for line in joined:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        os.environ[key] = value


def _load_json_env(name: str):
    _load_dotenv_once()
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn_env_once(name, f"JSON 파싱 실패 ({exc})")
        return {}
    if not isinstance(parsed, dict):
        _warn_env_once(name, "JSON object 형식이 아니어서 무시합니다.")
        return {}
    return parsed


def _normalize_rule_map(raw) -> dict:
    if not isinstance(raw, dict):
        return {}

    normalized = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            items = [_clean_text(value)] if _clean_text(value) else []
        elif isinstance(value, list):
            items = [
                _clean_text(item)
                for item in value
                if isinstance(item, str) and _clean_text(item)
            ]
        else:
            continue
        if key == "order_item_line":
            key = "order_meta"
        normalized[key] = items
    return normalized


def _merge_rule_maps(base: dict, override: dict) -> dict:
    merged = {key: list(value) for key, value in base.items()}
    for key, value in override.items():
        merged[key] = list(value)
    return merged


def _normalize_brand_key(brand: str):
    return re.sub(r"[^0-9a-z]+", "_", (brand or "").lower()).strip("_")


def _normalize_package_brand_rules(raw) -> dict:
    if not isinstance(raw, dict):
        return {}

    normalized = {}
    for package_name, package_block in raw.items():
        if not isinstance(package_name, str):
            continue

        if isinstance(package_block, dict) and isinstance(package_block.get("brands"), dict):
            brand_map = package_block.get("brands", {})
        elif isinstance(package_block, dict):
            brand_map = package_block
        else:
            continue

        normalized_brand_map = {}
        for brand_name, rule_map in brand_map.items():
            if not isinstance(brand_name, str):
                continue
            normalized_rules = _normalize_rule_map(rule_map)
            if normalized_rules:
                normalized_brand_map[brand_name] = normalized_rules

        if normalized_brand_map:
            normalized[package_name] = normalized_brand_map

    return normalized


def _merge_package_brand_rules(base: dict, override: dict) -> dict:
    merged = {
        package_name: {
            brand_name: {key: list(values) for key, values in rule_map.items()}
            for brand_name, rule_map in brand_map.items()
        }
        for package_name, brand_map in base.items()
    }

    for package_name, brand_map in override.items():
        if package_name not in merged:
            merged[package_name] = {}
        for brand_name, rule_map in brand_map.items():
            merged[package_name][brand_name] = {
                key: list(values) for key, values in rule_map.items()
            }

    return merged


def _load_package_brand_rules() -> dict:
    return _normalize_package_brand_rules(_load_json_env(_PACKAGE_BRAND_RULES_ENV))


def _resolve_brand_package_entry(brand: str):
    package_brand_rules = _load_package_brand_rules()
    if not brand:
        return None, {}

    normalized_brand = _normalize_brand_key(brand)
    for package_name, brand_map in package_brand_rules.items():
        if brand in brand_map:
            return package_name, brand_map[brand]
        for brand_name, rule_map in brand_map.items():
            if _normalize_brand_key(brand_name) == normalized_brand:
                return package_name, rule_map

    return None, {}


def _load_custom_field_rules(brand: str) -> dict:
    rules = _normalize_rule_map(_load_json_env(_CUSTOM_FIELD_RULES_DEFAULT_ENV))

    _package_name, package_brand_rules = _resolve_brand_package_entry(brand)
    if package_brand_rules:
        rules = _merge_rule_maps(rules, package_brand_rules)

    by_brand = _load_json_env(_CUSTOM_FIELD_RULES_BRAND_ENV)
    if isinstance(by_brand, dict):
        candidates = []
        if brand:
            candidates.extend([brand, _normalize_brand_key(brand)])
        for candidate in candidates:
            if candidate in by_brand:
                rules = _merge_rule_maps(
                    rules,
                    _normalize_rule_map(by_brand.get(candidate, {})),
                )
                break

    return rules


def _collect_config_errors(brand: str):
    config_errors = []
    package_brand_rules = _load_package_brand_rules()
    if not package_brand_rules:
        config_errors.append(f"env_missing:{_PACKAGE_BRAND_RULES_ENV}")
        return None, {}, config_errors

    if not brand:
        return None, {}, config_errors

    package_name, brand_rules = _resolve_brand_package_entry(brand)
    if not package_name:
        config_errors.append(f"brand_not_configured:{brand}")
        return None, {}, config_errors

    for key in _REQUIRED_PACKAGE_RULE_KEYS:
        if not brand_rules.get(key):
            config_errors.append(f"field_not_configured:{key}")

    return package_name, brand_rules, config_errors


def _normalize_custom_field_map(dump_json: dict) -> dict:
    field_map = {}

    raw_map = dump_json.get("customFieldMap")
    if isinstance(raw_map, dict):
        for label, value in raw_map.items():
            if not isinstance(label, str):
                continue
            clean_label = _clean_text(label)
            clean_value = _clean_text(str(value))
            if clean_label and clean_value:
                field_map[clean_label] = clean_value

    if field_map:
        return field_map

    raw_pairs = dump_json.get("customFields")
    if isinstance(raw_pairs, list):
        for pair in raw_pairs:
            if not isinstance(pair, dict):
                continue
            clean_label = _clean_text(pair.get("label", ""))
            clean_value = _clean_text(pair.get("value", ""))
            if clean_label and clean_value:
                field_map[clean_label] = clean_value

    return field_map


def _resolve_custom_field(field_map: dict, aliases: list[str]):
    if not field_map or not aliases:
        return None

    normalized_index = {
        _clean_text(label).lower(): (label, value)
        for label, value in field_map.items()
    }

    for alias in aliases:
        if alias in field_map:
            return {"label": alias, "value": field_map[alias]}

    for alias in aliases:
        matched = normalized_index.get(_clean_text(alias).lower())
        if matched:
            return {"label": matched[0], "value": matched[1]}

    return None


def _select_custom_field_claims(field_map: dict, brand: str):
    rules = _load_custom_field_rules(brand)
    resolved = {}
    selected = {}

    for key in ("uuid", "order_id", "order_time", "order_meta", "nickname"):
        matched = _resolve_custom_field(field_map, rules.get(key, []))
        if not matched:
            continue
        resolved[key] = matched
        selected[matched["label"]] = matched["value"]

    return resolved, selected


def _extract_sender_display_name(sender: str):
    """'표시명 <email>'에서 표시명만 뽑는다. 형식이 안 맞으면 원문 그대로(트림)."""
    if not sender:
        return None
    stripped = sender.strip()
    match = _SENDER_DISPLAY_NAME_RE.match(stripped)
    name = match.group(1).strip() if match else stripped
    return name or None


def resolve_brand_package(brand: str):
    package_name, _rules = _resolve_brand_package_entry(brand)
    return package_name


def list_known_packages():
    return list(_load_package_brand_rules().keys())


def resolve_brand_gcp_log(brand: str):
    """브랜드 → (gcp_project, gcp_log_name).

    `CS_PACKAGE_BRAND_RULES`의 **패키지 레벨** `gcp_project`/`gcp_log_name`을 읽는다
    (두 브랜드가 같은 패키지/프로젝트를 공유). 정규화 로직을 거치지 않고 raw에서 직접 찾는다.
    없으면 (None, None).
    """
    raw = _load_json_env(_PACKAGE_BRAND_RULES_ENV)
    if not isinstance(raw, dict) or not brand:
        return None, None
    normalized_brand = _normalize_brand_key(brand)
    for _package_name, block in raw.items():
        if not isinstance(block, dict):
            continue
        brands = block.get("brands") if isinstance(block.get("brands"), dict) else block
        if not isinstance(brands, dict):
            continue
        matched = brand in brands or any(
            isinstance(b, str) and _normalize_brand_key(b) == normalized_brand
            for b in brands
        )
        if matched:
            project = block.get("gcp_project")
            log_name = block.get("gcp_log_name")
            return (
                project if isinstance(project, str) and project else None,
                log_name if isinstance(log_name, str) and log_name else None,
            )
    return None, None


def resolve_brand_console_project(brand: str):
    """브랜드 → 콘솔 콘솔 프로젝트 표시명(console_project_name).

    `CS_PACKAGE_BRAND_RULES`의 **패키지 레벨** `console_project_name`을 읽는다
    (resolve_brand_gcp_log와 동일한 패턴 — 같은 패키지의 언어별 브랜드는 같은 콘솔
    프로젝트를 공유). 설정 없으면 None(호출자가 하드코딩 기본값으로 폴백하면 안
    된다 — 2026-07-07 대상 선택 모호성 금지 원칙, 폴백이 실제로 후보 다건 오류를 낸 사례).
    """
    raw = _load_json_env(_PACKAGE_BRAND_RULES_ENV)
    if not isinstance(raw, dict) or not brand:
        return None
    normalized_brand = _normalize_brand_key(brand)
    for _package_name, block in raw.items():
        if not isinstance(block, dict):
            continue
        brands = block.get("brands") if isinstance(block.get("brands"), dict) else block
        if not isinstance(brands, dict):
            continue
        matched = brand in brands or any(
            isinstance(b, str) and _normalize_brand_key(b) == normalized_brand
            for b in brands
        )
        if matched:
            project_name = block.get("console_project_name")
            return project_name if isinstance(project_name, str) and project_name else None
    return None


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
      nickname       : 오탈자 대조 폴백용 닉네임 후보 (str | None) — uuid가 콘솔에서 무효
                       판정될 때만 쓰임. 닉네임 커스텀 필드가 있으면 그 값, 없으면
                       '보낸 사람' 표시명(이메일 앞부분 — 사용자가 실제 닉네임이 오도록
                       설정해둔 값이라 정확함, 2026-07-08 확인)
      nickname_source: nickname의 출처 — "custom_field" | "sender_display_name" | None
      brand          : 브랜드 (str | None)
      category       : 문의유형 claim (str | None) — 채널 판정 금지
      ticket_status  : 티켓 상태 (str | None)
      sender         : 보낸 사람 (str | None)
      body           : 최초 문의 메시지 본문 (str | None)
      attachments    : 첨부 파일명 목록 (list[str])
      flags          : 누락·형식불일치 신호 목록 (list[str])
    """
    flags = []
    custom_field_map = _normalize_custom_field_map(dump_json)

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

    app_package = None
    config_errors = []
    if brand:
        app_package, _brand_rules, config_errors = _collect_config_errors(brand)
    else:
        _unused_package, _unused_rules, config_errors = _collect_config_errors("")

    resolved_custom_fields, selected_custom_fields = _select_custom_field_claims(
        custom_field_map,
        brand or "",
    )

    # ── 2) GPA 주문번호 후보 추출 ────────────────────────────────────────
    order_id_raw = None
    if resolved_custom_fields.get("order_id"):
        order_id_raw = resolved_custom_fields["order_id"]["value"]

    order_id = order_norm_status = None
    if order_id_raw:
        order_id, order_norm_status = normalize_gpa_order(order_id_raw)
        if order_id is None:
            flags.append("order_invalid")
    else:
        flags.append("order_missing")

    # ── 3) UUID 추출 ──────────────────────────────────────────────────────
    uuid = None
    if resolved_custom_fields.get("uuid"):
        uuid = resolved_custom_fields["uuid"]["value"].lower()
    if not uuid:
        flags.append("uuid_missing")

    # ── 3-1) 닉네임 추출(선택) — uuid가 콘솔에서 무효 판정될 때 오탈자 대조용
    # 폴백으로만 쓰인다(console_user_search.ensure_uuid_registered 참고). 브랜드별
    # 별칭은 uuid/order_id와 동일하게 CS_*_CUSTOM_FIELD_RULES* env로 설정한다.
    # 커스텀 필드에 닉네임 항목이 없는 브랜드(2026-07-08 실측: "게임타이틀" 문의
    # 양식엔 닉네임 항목 자체가 없음)는 '보낸 사람' 표시명을 쓴다 — 사용자가 이메일
    # 앞에 실제 닉네임이 오도록 cs 표시명을 별도로 설정해뒀다고 확인(2026-07-08)
    # 했으므로 정확한 값으로 취급한다. nickname_source는 참고용 출처 표기일 뿐이다.
    nickname = None
    nickname_source = None
    if resolved_custom_fields.get("nickname"):
        nickname = resolved_custom_fields["nickname"]["value"] or None
        if nickname:
            nickname_source = "custom_field"
    if not nickname:
        nickname = _extract_sender_display_name(sender)
        if nickname:
            nickname_source = "sender_display_name"

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
        "nickname": nickname,
        "nickname_source": nickname_source,
        "brand": brand,
        "app_package": app_package,
        "category": category,
        "ticket_status": ticket_status,
        "sender": sender,
        "custom_fields": custom_field_map,
        "selected_custom_fields": selected_custom_fields,
        "config_errors": config_errors,
        "body": body_msg,
        "attachments": attachments,
        "flags": flags,
    }


# ────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ────────────────────────────────────────────────────────────────────────────

def _find_gpa_raw(dump_json: dict):
    """tables 값 셀(row[1])에서 GPA 패턴 탐색. 원문 문자열 반환.

    인식 위치: tables 의 값 셀만. 라벨(row[0])·직원 답변·bodyText 전체 탐색 금지.
    1차: GPA/GAP 접두사 포함 패턴
    2차: 접두사 없이 17자리 숫자만 입력한 경우 (normalize_gpa_order 검증)
    3차: 최초 문의 메시지 (고객이 본문에 직접 기재한 경우)
    """
    # 1) GPA/GAP 접두사 패턴
    for table in dump_json.get("tables", []):
        for row in table:
            if len(row) >= 2:
                m = _GPA_ANCHOR.search(row[1])
                if m:
                    return m.group(0)

    # 2) 접두사 없이 17자리 숫자만 입력한 경우 — UUID와 구별 후 digit count 검증
    for table in dump_json.get("tables", []):
        for row in table:
            if len(row) >= 2:
                v = row[1].strip()
                if v and not _UUID_RE.search(v):
                    _, status = normalize_gpa_order(v)
                    if status == "ok":
                        return v

    # 3) 최초 문의 메시지
    first_msg = _extract_first_message(dump_json.get("bodyText", ""))
    if first_msg:
        m = _GPA_ANCHOR.search(first_msg)
        if m:
            return m.group(0)

    return None


def _find_uuid(dump_json: dict):
    """tables 값 셀(row[1])에서 UUID 탐색. 소문자 정규화해 반환."""
    for table in dump_json.get("tables", []):
        for row in table:
            if len(row) >= 2:
                m = _UUID_RE.search(row[1])
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
