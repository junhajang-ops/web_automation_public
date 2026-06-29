# -*- coding: utf-8 -*-
"""cs/console 공용 GCP Logging 읽기 helper."""


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
