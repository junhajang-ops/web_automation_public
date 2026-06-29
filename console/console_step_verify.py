# -*- coding: utf-8 -*-
"""
console_step_verify.py - console 조작 공용 step/fingerprint/dump 유틸리티

규칙:
- 조작 전에는 `record_step_dump()`를 호출해 pre 대기 + pre dump + pre fingerprint 비교/저장을 수행한다.
- 다음 조작이 없는 최종 안정 상태에서는 `step_and_verify_ui()`를 호출해
  post 대기 + post dump + post fingerprint 비교/저장을 수행한다.
- fingerprint 비교는 dump 파일끼리가 아니라 같은 이름의 이전 JSON fingerprint와 비교한다.
"""

import datetime
import json
import os
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STEP_WAIT_MS = 1_000
STEP_WAIT_ENV_KEYS = ("CONSOLE_STEP_WAIT_MS", "STEP_WAIT_MS")
FINGERPRINT_SCHEMA_VERSION = 2

_step_seq = [0]
_dump_counter = [0]
_dump_dir: list = [None]  # [Path | None]

_FINGERPRINT_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const stableCls = cls =>
    (cls || "").split(/\s+/).filter(t => t && !/^css-/.test(t) && !/^Mui[A-Z]/.test(t) && t.length < 25);

  const inputs = [];
  document.querySelectorAll("input, select, textarea").forEach(el => {
    const type = (el.getAttribute("type") || el.tagName).toLowerCase();
    if (["hidden", "submit", "button"].includes(type)) return;
    const name = el.getAttribute("name") || "";
    const id = el.id || "";
    if (name || id) inputs.push({ type, name, id });
  });

  const buttons = [];
  document.querySelectorAll("button").forEach(el => {
    const text = clean(el.innerText);
    const type = el.getAttribute("type") || "";
    if (text || type === "submit") buttons.push({ text, type });
  });

  const sidebarLinks = [];
  document.querySelectorAll("a[id]").forEach(el => {
    sidebarLinks.push(el.id);
  });

  const roles = [...new Set(
    [...document.querySelectorAll("[role]")].map(el => el.getAttribute("role")).filter(Boolean)
  )].sort();

  const listboxNames = [...document.querySelectorAll("[role='listbox']")]
    .map(el => el.getAttribute("name")).filter(Boolean);

  const accordionTitles = [...document.querySelectorAll(".accordion .title")]
    .map(el => clean(el.innerText)).filter(Boolean);

  const structuralTexts = [];
  const inDataCell = el => !!el.closest(
    "[role='gridcell'], [role='row'], td, .MuiDataGrid-cell, .MuiDataGrid-row"
  );

  document.querySelectorAll("[role='columnheader']").forEach(el => {
    const t = clean(el.innerText);
    if (t) structuralTexts.push("col:" + t);
  });

  document.querySelectorAll("[role='tab']").forEach(el => {
    const t = clean(el.innerText);
    if (t) structuralTexts.push("tab:" + t);
  });

  document.querySelectorAll("label").forEach(el => {
    if (inDataCell(el)) return;
    const t = clean(el.innerText);
    if (t && t.length < 40) structuralTexts.push("label:" + t);
  });

  document.querySelectorAll("a[id]").forEach(el => {
    const t = clean(el.innerText);
    if (t) structuralTexts.push("nav:" + t);
  });

  return { inputs, buttons, sidebarLinks, roles, listboxNames, accordionTitles, structuralTexts };
}
"""


def get_step_wait_ms() -> int:
    for key in STEP_WAIT_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if not value:
            continue
        try:
            parsed = int(value)
        except ValueError:
            continue
        if parsed >= 0:
            return parsed
    return DEFAULT_STEP_WAIT_MS


def step_pause(page, wait_ms: int | None = None) -> None:
    page.wait_for_timeout(get_step_wait_ms() if wait_ms is None else wait_ms)


def init_dump_dir(path: Path) -> None:
    _dump_dir[0] = path
    path.mkdir(parents=True, exist_ok=True)
    cleanup_old_dumps(path)


def cleanup_old_dumps(dump_root: Path, max_age_days: int = 30) -> None:
    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
    deleted = 0
    for f in dump_root.glob("dump_*.html"):
        try:
            if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                png = f.with_suffix(".png")
                if png.exists():
                    png.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        print(f"    {deleted}개의 {max_age_days}일 초과 덤프 삭제됨.")


def _save_dump(page, tag: str) -> None:
    dump_root = _dump_dir[0]
    if dump_root is None:
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    n = _dump_counter[0]
    _dump_counter[0] += 1
    stem = dump_root / f"dump_{ts}_{n:04d}_{tag}"
    try:
        page.screenshot(path=str(stem.with_suffix(".png")))
    except Exception:
        pass
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _next_tag(name: str = "") -> str:
    tag = name if name else f"step_{_step_seq[0]:03d}"
    _step_seq[0] += 1
    return tag


def _get_fp_dir() -> Path:
    fp_dir = BASE_DIR / "ui_fingerprints"
    fp_dir.mkdir(parents=True, exist_ok=True)
    return fp_dir


def _extract_page_fingerprint(page) -> dict:
    try:
        return page.evaluate(_FINGERPRINT_JS)
    except Exception as exc:
        print(f"  [UI감시] fingerprint 추출 실패: {exc}")
        return {}


def _load_last_fingerprint(name: str) -> dict:
    path = _get_fp_dir() / f"{name}_last.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    if loaded.get("__schema_version") != FINGERPRINT_SCHEMA_VERSION:
        return {}
    fingerprint = loaded.get("fingerprint", {})
    return fingerprint if isinstance(fingerprint, dict) else {}


def _save_fingerprint(name: str, fp: dict) -> None:
    path = _get_fp_dir() / f"{name}_last.json"
    payload = {
        "__schema_version": FINGERPRINT_SCHEMA_VERSION,
        "fingerprint": fp,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _diff_fingerprints(prev: dict, curr: dict) -> list[str]:
    changes: list[str] = []

    def list_diff(label, prev_list, curr_list, key_fn=lambda x: x):
        a = {key_fn(x) for x in prev_list}
        b = {key_fn(x) for x in curr_list}
        for k in sorted(a - b):
            changes.append(f"  [-] {label}: {k}")
        for k in sorted(b - a):
            changes.append(f"  [+] {label}: {k}")

    def _input_key(x):
        if x["name"]:
            if re.match(r"^dataSet\.\d+\.dataValue$", x["name"]):
                return f"{x['type']}|name=dataSet.*.dataValue"
            return f"{x['type']}|name={x['name']}"
        stable_id = x["id"] if x["id"] and not re.match(r"^:[a-z0-9]+:$", x["id"]) else ""
        return f"{x['type']}|id={stable_id}"

    list_diff("input", prev.get("inputs", []), curr.get("inputs", []), _input_key)
    list_diff(
        "button",
        prev.get("buttons", []),
        curr.get("buttons", []),
        lambda x: f"{x['text']}|type={x['type']}",
    )
    list_diff("sidebar a#id", prev.get("sidebarLinks", []), curr.get("sidebarLinks", []))
    list_diff("role", prev.get("roles", []), curr.get("roles", []))
    list_diff("listbox[name]", prev.get("listboxNames", []), curr.get("listboxNames", []))
    list_diff("accordion", prev.get("accordionTitles", []), curr.get("accordionTitles", []))
    list_diff("구조텍스트", prev.get("structuralTexts", []), curr.get("structuralTexts", []))
    return changes


def _append_change_log(name: str, diffs: list[str]) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_path = _get_fp_dir() / "ui_change_log.txt"
    lines = [
        f"\n[{ts}] [{name}] 구조 변경 감지 ({len(diffs)}건)",
        *diffs,
        "  → 셀렉터 재점검 권장.",
    ]
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def snap_and_check_ui(page, name: str) -> bool:
    curr = _extract_page_fingerprint(page)
    if not curr:
        return False

    prev = _load_last_fingerprint(name)
    changed = False

    if not prev:
        print(f"  [UI감시] '{name}' 최초 기준 저장 — 다음 실행부터 비교합니다.")
    else:
        diffs = _diff_fingerprints(prev, curr)
        if diffs:
            changed = True
            print(f"\n  [UI변경감지] '{name}' 구조 변경 감지:")
            for diff in diffs:
                print(diff)
            print("  → 셀렉터 재점검 권장. (기록: ui_fingerprints/ui_change_log.txt)\n")
            _append_change_log(name, diffs)
        else:
            print(f"  [UI감시] '{name}' 구조 동일.")

    _save_fingerprint(name, curr)
    return changed


def record_step_dump(page, name: str = "") -> str:
    """
    조작 직전 pre 상태를 기록한다.

    순서:
    1. env 기반 대기
    2. pre dump 저장
    3. pre fingerprint 비교/저장
    """
    tag = _next_tag(name)
    step_pause(page)
    _save_dump(page, tag)
    snap_and_check_ui(page, name=tag)
    return tag


def record_final_step_state(page, name: str = "") -> str:
    """
    다음 조작이 없는 최종 안정 상태를 기록한다.

    순서:
    1. env 기반 대기
    2. post dump 저장
    3. post fingerprint 비교/저장
    """
    tag = _next_tag(name)
    step_pause(page)
    _save_dump(page, tag)
    snap_and_check_ui(page, name=tag)
    return tag


def step_and_verify_ui(page, name: str = "") -> str:
    """호환용 wrapper. 최종 안정 상태 기록에만 사용한다."""
    return record_final_step_state(page, name=name)
