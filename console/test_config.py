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
