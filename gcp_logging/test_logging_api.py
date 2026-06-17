# -*- coding: utf-8 -*-
"""
test_logging_api.py — GCP Cloud Logging 읽기 권한 확인 (읽기 전용)
================================================================

기존 서비스 계정 키로 Cloud Logging entries.list를 실제로 호출해
읽기 권한이 정상인지 판정합니다.

이 스크립트는 "읽기(조회)"만 합니다. 로그 쓰기·삭제는 하지 않습니다.

실행:
  .venv\\Scripts\\python.exe test_logging_api.py --key <JSON키파일>
  .venv\\Scripts\\python.exe test_logging_api.py --key <JSON키파일> --project <프로젝트ID>
  .venv\\Scripts\\python.exe test_logging_api.py --key <JSON키파일> --filter "resource.type=\\"gce_instance\\"" --limit 10
  .venv\\Scripts\\python.exe test_logging_api.py --key <JSON키파일> --show-payload

옵션:
  --key <경로>           Google 서비스 계정 JSON 키 파일 (필수)
  --project <id>         GCP 프로젝트 ID (없으면 키 파일의 project_id 자동 사용)
  --filter <문자열>      Cloud Logging 필터 (없으면 프로젝트 전체 최근 N건)
  --limit N              가져올 로그 항목 수 (기본: 5)
  --show-payload         로그 본문 표시 (민감정보 포함 가능 — 주의)
  --payload-chars N      본문 표시 최대 글자 수 (기본: 200, 0 = 제한 없음)

필터 예시:
  # 특정 로그 + UUID 조회 (타임스탬프 연산자 앞뒤 공백 필수)
  logName="projects/<id>/logs/gametitle-log" AND jsonPayload._user_id="<uuid>" \
    AND timestamp >= "2026-06-15T00:30:00Z" AND timestamp <= "2026-06-15T02:00:00Z"
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[설치 필요] google-api-python-client google-auth가 없습니다:")
    print("    .venv\\Scripts\\python.exe -m pip install google-api-python-client google-auth")
    sys.exit(1)

SCOPE = "https://www.googleapis.com/auth/logging.read"


def _extract_project_id(key_path: str):
    """키 파일에서 project_id 필드만 읽어 반환 (비밀 필드는 읽지 않음)."""
    try:
        with open(key_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("project_id")
    except Exception:
        return None


def _explain_http_error(e, indent="  "):
    status = getattr(getattr(e, "resp", None), "status", None)
    detail = ""
    try:
        detail = e.error_details or e._get_reason()
    except Exception:
        detail = str(e)
    print(f"{indent}[오류] HTTP {status}")
    print(f"{indent}   구글 원본 메시지: {detail}")
    if str(status) in ("401", "403"):
        print(f"{indent}-> '로그 뷰어' 권한이 아직 반영되지 않았거나 부족합니다.")
        print(f"{indent}   1) 위 서비스계정 이메일이 로그 프로젝트 IAM에 추가됐는지 확인")
        print(f"{indent}      (GCP Console → 해당 프로젝트 → IAM 및 관리자 → IAM)")
        print(f"{indent}   2) 역할이 'roles/logging.viewer'(로그 뷰어)인지 확인")
        print(f"{indent}   3) 방금 권한을 부여했다면 1~2분 대기 후 재시도")
        print(f"{indent}   4) 비공개 감사 로그라면 'roles/logging.privateLogViewer' 필요")
        print(f"{indent}   5) 프로젝트 ID가 올바른지 확인 (--project 인자 또는 키 파일의 project_id)")
    elif str(status) == "404":
        print(f"{indent}-> 프로젝트를 찾을 수 없습니다.")
        print(f"{indent}   프로젝트 ID가 맞는지 확인하세요 (--project 또는 키 파일의 project_id).")
    elif str(status) == "400":
        print(f"{indent}-> 요청 형식 오류입니다. --filter 문법을 확인하세요.")
        print(f"{indent}   자주 발생하는 원인:")
        print(f"{indent}   1) 타임스탬프 연산자 앞뒤 공백 누락")
        print(f'{indent}      ✗ timestamp>="2026-06-15T00:30:00Z"')
        print(f'{indent}      ✓ timestamp >= "2026-06-15T00:30:00Z"')
        print(f"{indent}   2) 필드·값 따옴표 누락 또는 중첩 오류")
    else:
        print(f"{indent}-> 상세: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="GCP Cloud Logging 읽기 권한 확인 (읽기 전용)"
    )
    parser.add_argument("--key", required=True, help="Google 서비스 계정 JSON 키 파일 경로")
    parser.add_argument("--project", help="GCP 프로젝트 ID (없으면 키 파일에서 자동 추출)")
    parser.add_argument("--filter", dest="log_filter", default="",
                        help="Cloud Logging 필터 문자열")
    parser.add_argument("--limit", type=int, default=5,
                        help="가져올 로그 항목 수 (기본: 5)")
    parser.add_argument("--show-payload", action="store_true",
                        help="로그 본문 표시 (민감정보 포함 가능)")
    parser.add_argument("--payload-chars", type=int, default=200,
                        help="본문 표시 최대 글자 수 (기본: 200, 0 = 제한 없음)")
    args = parser.parse_args()

    key_path = Path(args.key)
    if not key_path.exists():
        print(f"[오류] 키 파일을 찾을 수 없습니다: {key_path}")
        sys.exit(1)

    print("=" * 64)
    print(" GCP Cloud Logging — 읽기 권한 확인 (읽기 전용)")
    print("=" * 64)

    # ── 프로젝트 ID 결정 ──────────────────────────────────────────────────────
    project_id = args.project or _extract_project_id(str(key_path))
    if not project_id:
        print("[오류] 프로젝트 ID를 특정할 수 없습니다.")
        print("       --project <id> 인자를 직접 넣어주세요.")
        sys.exit(1)

    print(f"  프로젝트 ID : {project_id}")
    print(f"  키 파일     : {key_path.name}")
    print(f"  최대 건수   : {args.limit}")
    if args.log_filter:
        print(f"  필터        : {args.log_filter}")
    print("-" * 64)

    # ── 1) 인증 ───────────────────────────────────────────────────────────────
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=[SCOPE]
        )
        service = build("logging", "v2", credentials=creds, cache_discovery=False)
        sa_email = getattr(creds, "service_account_email", "(확인불가)")
        print("[1/2] 인증: OK")
        print(f"       서비스계정: {sa_email}")
        print("       ↑ 이 이메일이 로그 프로젝트 IAM에 'roles/logging.viewer'로 추가돼 있어야 합니다.")
    except FileNotFoundError:
        print(f"[실패] 키 파일을 찾을 수 없습니다: {key_path}")
        sys.exit(1)
    except Exception as e:
        print(f"[실패] 키 파일이 올바르지 않습니다: {e}")
        sys.exit(1)

    # ── 2) entries.list 호출 (읽기 전용) ─────────────────────────────────────
    print(f"[2/2] 로그 조회(entries.list) — 프로젝트 {project_id} ...")

    body = {
        "resourceNames": [f"projects/{project_id}"],
        "orderBy": "timestamp desc",
        "pageSize": args.limit,
    }
    if args.log_filter:
        body["filter"] = args.log_filter

    try:
        resp = service.entries().list(body=body).execute()
    except HttpError as e:
        _explain_http_error(e)
        sys.exit(1)
    except Exception as e:
        print(f"  [오류] 예상치 못한 오류: {e}")
        sys.exit(1)

    entries = resp.get("entries", [])
    count = len(entries)

    print()
    print("-" * 64)

    if count == 0:
        print("[OK] 권한 정상 — 해당 조건의 로그 없음")
        print("     (필터 조건에 맞는 로그가 없거나, 해당 기간 로그가 없습니다.)")
        if not args.log_filter:
            print("     --filter 옵션으로 특정 로그를 지정하거나,")
            print("     --limit를 늘려 더 많은 항목을 요청해 보세요.")
    else:
        print(f"[OK] 로그 읽기 권한 정상 — {count}건 조회됨")
        if args.show_payload:
            print()
            print("  ※ --show-payload 모드: 로그 본문 포함 (민감정보 포함 가능 — 주의)")
        print()
        for i, entry in enumerate(entries, 1):
            ts = entry.get("timestamp", "-")
            log_name = entry.get("logName", "-")
            severity = entry.get("severity", "-")
            res_type = entry.get("resource", {}).get("type", "-")

            print(f"  [{i}] {ts}")
            print(f"       logName  : {log_name}")
            print(f"       severity : {severity}")
            print(f"       resource : {res_type}")

            if args.show_payload:
                payload = (
                    entry.get("jsonPayload")
                    or entry.get("textPayload")
                    or entry.get("protoPayload")
                )
                if payload:
                    raw = str(payload)
                    n = args.payload_chars
                    truncated = raw if n == 0 else (raw[:n] + ("..." if len(raw) > n else ""))
                    print(f"       payload  : {truncated}")
            print()

    print("-" * 64)
    print()
    print("=" * 64)
    if count > 0:
        print(" [OK] Cloud Logging 읽기 권한 확인 완료.")
    else:
        print(" [OK] 권한 확인 완료 (조회 성공, 해당 조건 로그 없음).")
    print("=" * 64)


if __name__ == "__main__":
    main()
