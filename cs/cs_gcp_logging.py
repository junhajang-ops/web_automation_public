# -*- coding: utf-8 -*-
"""cs/console 공용 GCP Logging 읽기 helper."""

LOGGING_READ_SCOPE = "https://www.googleapis.com/auth/logging.read"


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


def fetch_pvp_match_logs(logging_service, project, log_name, uuid, since_utc_iso, until_utc_iso):
    """기간 내 해당 유저의 log_pvp_match 로그 전체 조회 (페이지네이션 포함).

    반환: (entries_list, error | None)
    """
    if not (project and log_name and uuid):
        return [], "project/log_name/uuid 부족"

    log_path = f"projects/{project}/logs/{log_name}"
    filt = (
        f'logName="{log_path}" '
        f'AND jsonPayload._user_id="{uuid}" '
        f'AND jsonPayload.SUB_CATEGORY="log_pvp_match" '
        f'AND timestamp >= "{since_utc_iso}" '
        f'AND timestamp <= "{until_utc_iso}"'
    )

    all_entries = []
    page_token = None
    try:
        from googleapiclient.errors import HttpError
        while True:
            body = {
                "resourceNames": [f"projects/{project}"],
                "filter": filt,
                "orderBy": "timestamp desc",
                "pageSize": 1000,
            }
            if page_token:
                body["pageToken"] = page_token
            resp = logging_service.entries().list(body=body).execute()
            all_entries.extend(resp.get("entries", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", "?")
        return all_entries, f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001
        return all_entries, str(exc)

    return all_entries, None


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
    body = {
        "resourceNames": [f"projects/{project}"],
        "filter": filt,
        "orderBy": "timestamp desc",
        "pageSize": 1,
    }

    try:
        from googleapiclient.errors import HttpError

        resp = logging_service.entries().list(body=body).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", "?")
        return None, f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    entries = resp.get("entries", [])
    if not entries:
        return None, "해당 유저의 log_shop_click 로그 없음"
    return entries[0], None
