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
import ctypes
import os
import re
import sys
import time
import unicodedata
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STEP_WAIT_MS = 1_000
POLL_INTERVAL_MS = 500  # wait_until 폴링(요소 대기) 재확인 주기 — 조작 전 사람 확인 대기(STEP_WAIT_MS)와 무관
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
    if (/^\d+\+?$/.test(text)) return;
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


ANSI_RED = "\033[91m"
ANSI_RESET = "\033[0m"


def _supports_color() -> bool:
    # 예약 실행 래퍼(run_*_scheduled.ps1)는 `*>> $LogFile`로 표준출력을 파일 리다이렉트한다.
    # 이때 stdout은 터미널이 아니므로(isatty()=False) 색상 코드를 넣지 않아야
    # 로그 파일에 이스케이프 문자가 그대로 남지 않는다.
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _red(text: str) -> str:
    if not _supports_color():
        return text
    return f"{ANSI_RED}{text}{ANSI_RESET}"


def configure_console_output() -> str:
    encoding = "utf-8"
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
            # ANSI 색상 코드가 이스케이프 문자 그대로 찍히지 않고 실제 색으로
            # 렌더링되도록 콘솔 가상 터미널 처리를 켠다(구형 conhost 대응).
            STD_OUTPUT_HANDLE = -11
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        except Exception:
            pass

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            # line_buffering=True: 표준출력이 실제 콘솔이 아니라 파이프로 연결되면(예:
            # run_leaderboard_scheduled.ps1의 `... | Tee-Object`) 파이썬이 자동으로
            # 완전 버퍼링으로 전환해, 내부 버퍼가 다 차거나 프로세스가 끝날 때까지
            # 화면·로그 파일 어디에도 아무 줄도 안 나가는 문제가 있었다(실측: 예약
            # 실행이 실제로 돌고 있었는데 터미널에 아무것도 안 보여 사용자가 수동 종료).
            # 줄바꿈마다 강제로 flush하도록 고정해 콘솔 실행과 동일하게 실시간으로 보이게 한다.
            stream.reconfigure(encoding=encoding, errors="replace", line_buffering=True)
        except Exception:
            continue
    return encoding


def display_width(value) -> int:
    text = "" if value is None else str(value)
    width = 0
    for ch in text:
        if ch in "\r\n":
            continue
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in {"W", "F"}:
            width += 2
        else:
            width += 1
    return width


def fit_display_text(value, width: int, ellipsis: str = "...") -> str:
    text = "" if value is None else str(value)
    if display_width(text) <= width:
        return text

    ellipsis_width = display_width(ellipsis)
    if ellipsis_width >= width:
        return "." * max(1, width)

    parts: list[str] = []
    current_width = 0
    for ch in text:
        ch_width = display_width(ch)
        if current_width + ch_width + ellipsis_width > width:
            break
        parts.append(ch)
        current_width += ch_width
    return "".join(parts) + ellipsis


def pad_display(value, width: int, align: str = "left") -> str:
    text = fit_display_text(value, width)
    padding = max(0, width - display_width(text))
    if align == "right":
        return (" " * padding) + text
    return text + (" " * padding)


def step_pause(page, wait_ms: int | None = None) -> None:
    page.wait_for_timeout(get_step_wait_ms() if wait_ms is None else wait_ms)


def wait_until(page, predicate, timeout_ms: int = 10_000, wait_ms: int = POLL_INTERVAL_MS):
    """predicate()가 truthy 값을 반환할 때까지 wait_ms 간격으로 반복 대기한다.

    UI 요소가 렌더 지연으로 늦게 나타나는 경우의 공용 반복 대기 헬퍼.
    폴링 주기(wait_ms)는 조작 전 사람 확인 대기(step_pause / STEP_WAIT_MS)와는
    무관한 별도 값이다 — 폴링은 요소 등장을 빠르게 재확인하기 위한 것이며,
    기본값은 POLL_INTERVAL_MS이고 호출부가 wait_ms로 개별 지정할 수 있다.
    반환: predicate의 truthy 결과(요소/locator 등). 시간 초과 시 None.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(wait_ms)


def retry_with_recovery(action, recovery, label: str, recovery_desc: str, max_retries: int):
    """action()을 최대 max_retries회 시도한다. 실패마다 recovery()를 호출한 뒤 재시도하고,

    마지막 시도까지 실패하면 마지막 예외를 그대로 올린다(재시도 소진 후 이 대상을
    스킵할지 최종 실패로 볼지는 이 함수가 아니라 호출자/상위 배치 계층이 결정한다 —
    검증은 fail-fast로 끝내고 재시도 정책은 상위에 위임하는 공용 원칙에 따른 것).
    action()의 반환값을 그대로 돌려준다. label/recovery_desc는 실패 시 출력할
    로그 문구("    [{label}] N/max 실패: 에러 -> {recovery_desc}")에 쓰인다.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_retries:
                break
            print(f"    [{label}] {attempt}/{max_retries} 실패: {exc} -> {recovery_desc}")
            recovery()

    raise last_exc


def get_retry_max_retries(env_key: str = "RETRY_MAX_RETRIES", default: int = 3) -> int:
    """Return the shared whole-procedure retry budget from env."""
    raw_value = os.environ.get(env_key, str(default))
    try:
        return max(1, int(raw_value))
    except ValueError as exc:
        raise RuntimeError(f"{env_key} must be an integer: {raw_value}") from exc


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


_project_label_provider: list = [None]  # [Callable[[Any], str] | None]


def set_project_label_provider(fn) -> None:
    """스텝 이름에 자동으로 붙일 프로젝트 라벨 제공 함수를 등록한다.

    같은 스크립트를 서로 다른 프로젝트(--title)로 반복 실행하면 화면 구성(사이드바 등)이
    실제로 다른데도 스텝 이름이 프로젝트와 무관하게 고정돼 있어 baseline이 서로 다른
    프로젝트끼리 비교되며 오탐한다. fn(page) -> str 을 한 번 등록해두면 이후 모든
    record_step_dump/step_and_verify_ui 호출에 자동으로 라벨이 반영되어, 개별 호출부마다
    프로젝트명을 손으로 엮을 필요가 없다.
    """
    _project_label_provider[0] = fn


def _next_tag(page, name: str = "") -> str:
    tag = name if name else f"step_{_step_seq[0]:03d}"
    _step_seq[0] += 1
    provider = _project_label_provider[0]
    if provider is not None:
        try:
            label = provider(page)
        except Exception:
            label = ""
        if label:
            tag = f"{tag}_{label}"
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
            if re.match(r"^:[a-z0-9]+:$", item["name"]):
                # React useId() 등이 렌더될 때마다 재발급하는 동적 id. 콘솔 콘솔 전반에서
                # radio/label 그룹 name으로 흔히 쓰여, 실제 구조 변경이 아닌데도 매번 다른
                # 값이 되어 오탐을 유발한다. id 폴백 경로와 동일한 패턴으로 정규화한다.
                return f"{item['type']}|name=:react-id:"
            return f"{item['type']}|name={item['name']}"
        stable_id = item["id"] if item["id"] and not re.match(r"^:[a-z0-9]+:$", item["id"]) else ""
        return f"{item['type']}|id={stable_id}"

    list_diff("input", prev.get("inputs", []), curr.get("inputs", []), input_key)

    def button_key(item):
        text = item["text"]
        # 알림 개수 배지(예: '99', '99+')는 수시로 바뀌어 구조 감시에 무의미 → 정규화
        if re.fullmatch(r"\d+\+?", text):
            return f"badge|type={item['type']}"
        return f"{text}|type={item['type']}"

    list_diff("button", prev.get("buttons", []), curr.get("buttons", []), button_key)
    list_diff("sidebar a#id", prev.get("sidebarLinks", []), curr.get("sidebarLinks", []))
    list_diff("role", prev.get("roles", []), curr.get("roles", []))
    list_diff("listbox[name]", prev.get("listboxNames", []), curr.get("listboxNames", []))
    list_diff("accordion", prev.get("accordionTitles", []), curr.get("accordionTitles", []))

    def structural_text_key(item):
        # 사이드바 nav 항목(예: "신고 및 제재")에는 미확인 건수 배지가 텍스트에 그대로
        # 붙어 나온다("신고 및 제재0" -> "신고 및 제재42"). 실제 구조 변경이 아니라 건수만
        # 바뀐 것이므로 button 배지 정규화(badge|type=...)와 동일한 취지로 끝자리 숫자를 제거한다.
        if item.startswith("nav:"):
            return re.sub(r"\d+$", "", item)
        return item

    list_diff(
        "structural_text",
        prev.get("structuralTexts", []),
        curr.get("structuralTexts", []),
        structural_text_key,
    )
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
            print(_red(f"  [UI change] '{name}' changed."))
            for diff in kept_diffs:
                print(_red(diff))
            if ignored_diffs:
                print(f"  [UI ignore] {len(ignored_diffs)} whitelisted diffs skipped.")
            print(_red("  -> See console/ui_fingerprints/ui_change_log.txt"))
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
    # 행동(조작) 전 대기: 사람이 현재 화면을 볼 수 있게 한 번만 대기한 뒤 기록/지문 비교.
    # 실제 조작은 이 함수 반환 후 호출부에서 진행된다.
    tag = _next_tag(page, name)
    step_pause(page)
    _save_dump(page, tag)
    snap_and_check_ui(page, name=tag, ignore_patterns=ignore_patterns)
    return tag


def record_final_step_state(
    page,
    name: str = "",
    ignore_patterns=None,
) -> str:
    # 마지막 단계 기록: 안정화 고정 대기(step_pause)는 두지 않는다.
    # 조작 후 안정화는 조작 측 폴링(wait_until 등)이 이미 보장한 상태에서 호출되므로
    # 여기서는 기록 덤프와 지문 비교만 수행한다.
    tag = _next_tag(page, name)
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


def save_page_artifacts(page, out_dir, basename, summary_lines=None):
    """최종 화면 아티팩트(전체 스크린샷 + HTML + 요약 txt)를 out_dir에 저장한다.

    각 console 스크립트가 공통으로 갖고 있던 저장 보일러플레이트(스크린샷/HTML/
    요약 txt/완료 출력)를 한 곳으로 모은 것. 요약 내용은 화면마다 다르므로
    호출부가 summary_lines(문자열 리스트)로 만들어 넘긴다(None이면 txt 생략).
    반환: 확장자 없는 저장 경로 stem(Path).
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"{basename}_{ts}"

    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
    except Exception as exc:
        print(f"  (스크린샷 저장 실패: {exc})")

    try:
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  (HTML 저장 실패: {exc})")

    if summary_lines is not None:
        try:
            Path(f"{stem}.txt").write_text("\n".join(summary_lines), encoding="utf-8")
        except Exception as exc:
            print(f"  (요약 저장 실패: {exc})")

    print(f"\n아티팩트 저장 완료: {stem}.png / .html / .txt")
    return stem
