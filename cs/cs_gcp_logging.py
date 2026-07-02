# -*- coding: utf-8 -*-
"""cs/console 공용 GCP Logging 읽기 helper."""

import os

LOGGING_READ_SCOPE = "https://www.googleapis.com/auth/logging.read"

# Cloud Logging entries.list는 검색을 다 끝내지 못하면 entries=[] 인데도 nextPageToken을
# 돌려주며(공식 문서 명시), 클라이언트가 토큰으로 계속 넘겨야 실제 로그를 만난다.
# 첫 페이지만 읽고 "로그없음"으로 단정하면 실제 로그가 있는 유저를 false negative로 놓친다
# (BattleScore처럼 최근 접속이 몇 주 전인 유저에서 결정적으로 재현됨).
# 빈 페이지를 몇 번까지 따라갈지 상한. 소진 시 "로그없음"이 아니라 불확실(에러)로 반환한다.
GCP_MAX_EMPTY_PAGES = max(1, int(os.environ.get("GCP_MAX_EMPTY_PAGES", "20")))


def load_logging_credentials(key_path):
    """서비스계정 키 파일 → credentials(logging.read scope). 실패 시 None.

    credentials 로드(RSA 키 파싱)는 비싸므로 한 번만 로드해 공유하고,
    스레드별로는 build_logging_service_from_credentials 로 service(회선)만 만든다.
    """
    try:
        from google.oauth2 import service_account
    except ImportError:
        return None
    try:
        return service_account.Credentials.from_service_account_file(
            str(key_path), scopes=[LOGGING_READ_SCOPE]
        )
    except Exception:
        return None


def build_logging_service_from_credentials(credentials):
    """credentials → GCP Cloud Logging v2 service. 실패 시 None.

    googleapiclient service(httplib2/SSL)는 thread-safe하지 않으므로 스레드별로 호출한다.
    credentials 는 thread-safe하여 여러 스레드가 공유해도 된다.
    """
    if credentials is None:
        return None
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return None
    try:
        return build("logging", "v2", credentials=credentials, cache_discovery=False)
    except Exception:
        return None


def build_logging_service(key_path):
    """GCP Cloud Logging v2 service (읽기 전용). 실패 시 None. (키 로드+build 일괄)"""
    return build_logging_service_from_credentials(load_logging_credentials(key_path))


def fetch_recent_log_entry(logging_service, project, filter_expr, max_empty_pages=None):
    """filter_expr 조건에 맞는 가장 최근 로그 1건 조회 (orderBy timestamp desc).

    로그 종류·SUB_CATEGORY 등 세부 조회 조건은 filter_expr로 호출부가 결정한다.
    읽기 전용(entries.list). 반환: (entry_dict | None, error | None).

    ★ 페이징 처리: entries.list는 검색을 다 끝내지 못하면 entries=[] 인데도 nextPageToken을
      돌려준다(공식 문서). 이때 토큰으로 계속 넘겨야 실제 로그를 만난다. 첫 페이지가 비었다고
      바로 "로그없음"으로 단정하면 실제 로그가 있는 유저를 놓친다. 따라서 entries가 채워진
      페이지를 만나거나(→ 최신 1건 반환), nextPageToken이 사라질 때까지(→ 진짜 로그없음) 넘긴다.
    - 진짜 로그가 없으면(빈 페이지 + 토큰 없음) (None, None) — 정상 상태(에러 아님).
    - 빈 페이지 상한(max_empty_pages)을 넘도록 토큰이 계속 남으면, 검색이 미완결이라
      "로그없음"으로 단정할 수 없으므로 에러로 반환한다(false negative 방지).
    """
    if max_empty_pages is None:
        max_empty_pages = GCP_MAX_EMPTY_PAGES

    body = {
        "resourceNames": [f"projects/{project}"],
        "filter": filter_expr,
        "orderBy": "timestamp desc",
        "pageSize": 1,
    }

    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = None

    empty_pages = 0
    page_token = None
    while True:
        if page_token:
            body["pageToken"] = page_token
        else:
            body.pop("pageToken", None)

        try:
            resp = logging_service.entries().list(body=body).execute()
        except Exception as exc:  # noqa: BLE001
            if HttpError is not None and isinstance(exc, HttpError):
                status = getattr(getattr(exc, "resp", None), "status", "?")
                return None, f"HTTP {status}"
            return None, str(exc)

        entries = resp.get("entries", [])
        if entries:
            return entries[0], None

        page_token = resp.get("nextPageToken")
        if not page_token:
            # 빈 페이지 + 토큰 없음 = 검색 완료, 실제로 로그 없음
            return None, None

        empty_pages += 1
        if empty_pages >= max_empty_pages:
            # 토큰이 계속 남아 검색이 미완결 → "로그없음"으로 단정 불가. 불확실로 반환한다.
            return None, f"empty pages exhausted (>{max_empty_pages}, nextPageToken 잔존)"


def fetch_recent_shop_click_log(logging_service, project, log_name, uuid):
    """해당 유저(uuid)의 가장 직전 log_shop_click 로그 1건 조회.

    읽기 전용(entries.list). 반환: (entry_dict | None, error | None)
    """
    if not (project and log_name and uuid):
        return None, "project/log_name/uuid 부족"

    log_path = f"projects/{project}/logs/{log_name}"
    filt = (
        f'logName="{log_path}" '
        f'AND jsonPayload._user_id="{uuid}" '
        f'AND jsonPayload.SUB_CATEGORY="log_shop_click"'
    )

    # fetch_recent_log_entry가 nextPageToken 페이징(빈 페이지+토큰)을 처리하므로 재사용한다.
    entry, err = fetch_recent_log_entry(logging_service, project, filt)
    if err:
        return None, err
    if entry is None:
        return None, "해당 유저의 log_shop_click 로그 없음"
    return entry, None
