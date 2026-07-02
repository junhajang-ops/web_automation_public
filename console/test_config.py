# -*- coding: utf-8 -*-
"""
테스트 환경 변수 로더.

프로젝트 루트의 .env 파일을 읽어 테스트용 상수를 제공합니다.
.env 파일이 없으면 플레이스홀더 값을 사용합니다.
"""

import os
from pathlib import Path


def _load_env_file(env_path):
    if not env_path.exists():
        return
    # 역슬래시(\)로 끝나는 줄은 다음 줄과 연결 (긴 JSON 값의 줄바꿈 지원 — cs_parse와 동일)
    raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    joined = []
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
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(Path(__file__).resolve().parent.parent / ".env")

TEST_UUID = os.environ.get("TEST_UUID", "00000000-0000-0000-0000-000000000000")
TEST_PURCHASE_CODE = os.environ.get("TEST_PURCHASE_CODE", "")
TEST_TABLE_NAME = os.environ.get("TEST_TABLE_NAME", "ShopData")
TEST_CHART_NAME = os.environ.get("TEST_CHART_NAME", "Shop")
STEP_WAIT_MS = int(os.environ.get("STEP_WAIT_MS", "1000"))


def apply_title_profile(
    args,
    *,
    default_project_name: str = "",
    require_project_name: bool = False,
    include_key_file: bool = False,
    include_gcp: bool = False,
    default_block_reason: str = "",
    include_block_reason: bool = False,
):
    if getattr(args, "gametitle", False) and not getattr(args, "title", ""):
        args.title = "gametitle"

    title = (getattr(args, "title", "") or "").strip()
    if not title:
        if require_project_name:
            raise SystemExit(
                "[오류] --title 또는 --gametitle 옵션이 필요합니다. "
                "실수로 잘못된 프로젝트를 대상으로 실행되는 것을 막기 위해 "
                "옵션 없이는 진행하지 않습니다."
            )
        return args

    prefix = title.upper()
    project_name_env = os.environ.get(f"{prefix}_PROJECT_NAME", "").strip()

    if hasattr(args, "project_name"):
        current_project_name = getattr(args, "project_name", "")
        if current_project_name == default_project_name:
            if project_name_env:
                setattr(args, "project_name", project_name_env)
            elif require_project_name:
                raise SystemExit(
                    f"[오류] --title {title}: env '{prefix}_PROJECT_NAME' 이 비어 있습니다."
                )

    if include_key_file and hasattr(args, "key"):
        key_file = os.environ.get(f"{prefix}_KEY_FILE", "").strip()
        if key_file and not Path(key_file).is_absolute():
            # 상대경로는 실행 시 cwd에 따라 못 찾을 수 있으므로(예: 작업 스케줄러 실행)
            # .env와 같은 기준(프로젝트 루트)의 절대경로로 고정한다.
            key_file = str(Path(__file__).resolve().parent.parent / key_file)
        if not getattr(args, "key", ""):
            setattr(args, "key", key_file)

    if include_gcp:
        if hasattr(args, "gcp_project") and not getattr(args, "gcp_project", ""):
            setattr(
                args,
                "gcp_project",
                os.environ.get(f"{prefix}_GCP_PROJECT", "").strip(),
            )
        if hasattr(args, "gcp_log") and not getattr(args, "gcp_log", ""):
            setattr(
                args,
                "gcp_log",
                os.environ.get(f"{prefix}_LOGNAME", "").strip(),
            )

    if include_block_reason and hasattr(args, "reason"):
        current_reason = getattr(args, "reason", "")
        if current_reason == default_block_reason:
            reason_env = os.environ.get(f"{prefix}_BLOCK_REASON", "").strip()
            if reason_env:
                setattr(args, "reason", reason_env)

    return args
