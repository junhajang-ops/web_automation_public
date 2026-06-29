# -*- coding: utf-8 -*-
"""
Shared console step dump and UI fingerprint helpers.

Rules:
- Call record_step_dump() before a visible UI action.
- Call step_and_verify_ui() only for the final stable state when there is
  no immediate next action.
- Fingerprints are compared by stable step name, not by dump filename.
- Full-page fingerprinting is the default rule. Step-local ignore patterns
  can suppress known false positives without weakening the shared baseline.
"""

import datetime
import json
import os
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STEP_WAIT_MS = 1_000
STEP_WAIT_ENV_KEYS = ("CONSOLE_STEP_WAIT_MS", "STEP_WAIT_MS")
FINGERPRINT_SCHEMA_VERSION = 4

_step_seq = [0]
_dump_counter = [0]
_dump_dir: list = [None]  # [Path | None]

_FINGERPRINT_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const isVisible = el => {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const inputs = [];
  document.querySelectorAll("input, select, textarea").forEach(el => {
    if (!isVisible(el)) return;
    const type = (el.getAttribute("type") || el.tagName).toLowerCase();
    if (["hidden", "submit", "button"].includes(type)) return;
    const name = el.getAttribute("name") || "";
    const id = el.id || "";
    if (name || id) inputs.push({ type, name, id });
  });

  const buttons = [];
  document.querySelectorAll("button").forEach(el => {
    if (!isVisible(el)) return;
    const text = clean(el.innerText);
    const type = el.getAttribute("type") || "";
    if (text || type === "submit") buttons.push({ text, type });
  });

  const sidebarLinks = [];
  document.querySelectorAll("a[id]").forEach(el => {
    if (!isVisible(el)) return;
    sidebarLinks.push(el.id);
  });

  const roles = [...new Set(
    [...document.querySelectorAll("[role]")]
      .filter(isVisible)
      .map(el => el.getAttribute("role"))
      .filter(Boolean)
  )].sort();

  const listboxNames = [...document.querySelectorAll("[role='listbox']")]
    .filter(isVisible)
    .map(el => el.getAttribute("name"))
    .filter(Boolean);

  const accordionTitles = [...document.querySelectorAll(".accordion .title")]
    .filter(isVisible)
    .map(el => clean(el.innerText))
    .filter(Boolean);

  const structuralTexts = [];
  const inDataCell = el => !!el.closest(
    "[role='gridcell'], [role='row'], td, .MuiDataGrid-cell, .MuiDataGrid-row"
  );

  document.querySelectorAll("[role='columnheader']").forEach(el => {
    if (!isVisible(el)) return;
    const text = clean(el.innerText);
    if (text) structuralTexts.push("col:" + text);
  });

  document.querySelectorAll("[role='tab']").forEach(el => {
    if (!isVisible(el)) return;
    const text = clean(el.innerText);
    if (text) structuralTexts.push("tab:" + text);
  });

  document.querySelectorAll("label").forEach(el => {
    if (!isVisible(el)) return;
    if (inDataCell(el)) return;
    const text = clean(el.innerText);
    if (text && text.length < 40) structuralTexts.push("label:" + text);
  });

  document.querySelectorAll("a[id]").forEach(el => {
    if (!isVisible(el)) return;
    const text = clean(el.innerText);
    if (text) structuralTexts.push("nav:" + text);
  });

  return {
    inputs,
    buttons,
    sidebarLinks,
    roles,
    listboxNames,
    accordionTitles,
    structuralTexts,
  };
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
    for file_path in dump_root.glob("dump_*.html"):
        try:
            if datetime.datetime.fromtimestamp(file_path.stat().st_mtime) < cutoff:
                file_path.unlink()
                png_path = file_path.with_suffix(".png")
                if png_path.exists():
                    png_path.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        print(f"    {deleted} old dumps removed ({max_age_days}+ days).")


def _save_dump(page, tag: str) -> None:
    dump_root = _dump_dir[0]
    if dump_root is None:
        return
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    index = _dump_counter[0]
    _dump_counter[0] += 1
    stem = dump_root / f"dump_{timestamp}_{index:04d}_{tag}"
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
        print(f"  [UI monitor] fingerprint extraction failed: {exc}")
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

    def list_diff(label, prev_list, curr_list, key_fn=lambda item: item):
        prev_keys = {key_fn(item) for item in prev_list}
        curr_keys = {key_fn(item) for item in curr_list}
        for key in sorted(prev_keys - curr_keys):
            changes.append(f"  [-] {label}: {key}")
        for key in sorted(curr_keys - prev_keys):
            changes.append(f"  [+] {label}: {key}")

    def input_key(item):
        if item["name"]:
            if re.match(r"^dataSet\.\d+\.dataValue$", item["name"]):
                return f"{item['type']}|name=dataSet.*.dataValue"
            return f"{item['type']}|name={item['name']}"
        stable_id = item["id"] if item["id"] and not re.match(r"^:[a-z0-9]+:$", item["id"]) else ""
        return f"{item['type']}|id={stable_id}"

    list_diff("input", prev.get("inputs", []), curr.get("inputs", []), input_key)
    list_diff(
        "button",
        prev.get("buttons", []),
        curr.get("buttons", []),
        lambda item: f"{item['text']}|type={item['type']}",
    )
    list_diff("sidebar a#id", prev.get("sidebarLinks", []), curr.get("sidebarLinks", []))
    list_diff("role", prev.get("roles", []), curr.get("roles", []))
    list_diff("listbox[name]", prev.get("listboxNames", []), curr.get("listboxNames", []))
    list_diff("accordion", prev.get("accordionTitles", []), curr.get("accordionTitles", []))
    list_diff("structural_text", prev.get("structuralTexts", []), curr.get("structuralTexts", []))
    return changes


def _compile_ignore_patterns(ignore_patterns) -> list[re.Pattern]:
    compiled = []
    for pattern in ignore_patterns or []:
        compiled.append(re.compile(pattern))
    return compiled


def _split_ignored_diffs(
    diffs: list[str],
    ignore_patterns=None,
) -> tuple[list[str], list[str]]:
    compiled = _compile_ignore_patterns(ignore_patterns)
    if not compiled:
        return diffs, []

    kept: list[str] = []
    ignored: list[str] = []
    for diff in diffs:
        if any(pattern.search(diff) for pattern in compiled):
            ignored.append(diff)
        else:
            kept.append(diff)
    return kept, ignored


def _append_change_log(
    name: str,
    diffs: list[str],
    ignored_diffs: list[str] | None = None,
) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_path = _get_fp_dir() / "ui_change_log.txt"
    lines = [
        "",
        f"[{timestamp}] [{name}] UI structure change detected (items={len(diffs)})",
        *diffs,
    ]
    if ignored_diffs:
        lines.extend(
            [
                "  [ignored diffs]",
                *ignored_diffs,
            ]
        )
    lines.append("  -> Re-check selectors if this was not intentional.")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def snap_and_check_ui(
    page,
    name: str,
    ignore_patterns=None,
) -> bool:
    curr = _extract_page_fingerprint(page)
    if not curr:
        return False

    prev = _load_last_fingerprint(name)
    changed = False

    if not prev:
        print(f"  [UI monitor] '{name}' baseline saved.")
    else:
        diffs = _diff_fingerprints(prev, curr)
        kept_diffs, ignored_diffs = _split_ignored_diffs(diffs, ignore_patterns=ignore_patterns)
        if kept_diffs:
            changed = True
            print(f"\n  [UI change] '{name}' changed.")
            for diff in kept_diffs:
                print(diff)
            if ignored_diffs:
                print(f"  [UI ignore] {len(ignored_diffs)} whitelisted diffs skipped.")
            print("  -> See console/ui_fingerprints/ui_change_log.txt\n")
            _append_change_log(name, kept_diffs, ignored_diffs)
        else:
            print(f"  [UI monitor] '{name}' unchanged.")
            if ignored_diffs:
                print(f"  [UI ignore] {len(ignored_diffs)} whitelisted diffs skipped.")

    _save_fingerprint(name, curr)
    return changed


def record_step_dump(
    page,
    name: str = "",
    ignore_patterns=None,
) -> str:
    tag = _next_tag(name)
    step_pause(page)
    _save_dump(page, tag)
    snap_and_check_ui(page, name=tag, ignore_patterns=ignore_patterns)
    return tag


def record_final_step_state(
    page,
    name: str = "",
    ignore_patterns=None,
) -> str:
    tag = _next_tag(name)
    step_pause(page)
    _save_dump(page, tag)
    snap_and_check_ui(page, name=tag, ignore_patterns=ignore_patterns)
    return tag


def step_and_verify_ui(
    page,
    name: str = "",
    ignore_patterns=None,
) -> str:
    return record_final_step_state(
        page,
        name=name,
        ignore_patterns=ignore_patterns,
    )
