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
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(Path(__file__).resolve().parent.parent / ".env")

TEST_UUID = os.environ.get("TEST_UUID", "00000000-0000-0000-0000-000000000000")
TEST_PURCHASE_CODE = os.environ.get("TEST_PURCHASE_CODE", "")
TEST_TABLE_NAME = os.environ.get("TEST_TABLE_NAME", "ShopData")
