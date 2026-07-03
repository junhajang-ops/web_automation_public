# -*- coding: utf-8 -*-
"""cs/console 공용 BigQuery 읽기 helper (cs_gcp_logging.py의 BigQuery 버전).

Cloud Logging(entries.list) 대신 BigQuery로 로그/유저 데이터를 내보내는 프로젝트용.
읽기 전용(SELECT)만 수행한다.
"""

BIGQUERY_READONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"


def load_bigquery_credentials(key_path):
    """서비스계정 키 파일 → credentials(bigquery.readonly scope). 실패 시 None.

    cs_gcp_logging.load_logging_credentials와 동일한 이유로 credentials(RSA 키 파싱)는
    한 번만 로드해 공유하고, client(연결)는 build_bigquery_client_from_credentials로
    스레드별로 만든다.
    """
    try:
        from google.oauth2 import service_account
    except ImportError:
        return None
    try:
        return service_account.Credentials.from_service_account_file(
            str(key_path), scopes=[BIGQUERY_READONLY_SCOPE]
        )
    except Exception:
        return None


def build_bigquery_client_from_credentials(credentials, project):
    """credentials → BigQuery Client. 실패 시 None.

    google-cloud-bigquery Client도 내부적으로 커넥션을 들고 있으므로 cs_gcp_logging의
    logging service와 동일하게 스레드별로 새로 만든다.
    """
    if credentials is None:
        return None
    try:
        from google.cloud import bigquery
    except ImportError:
        return None
    try:
        return bigquery.Client(project=project, credentials=credentials)
    except Exception:
        return None


def fetch_min_date_for_user(bq_client, project, dataset, table, user_col, date_col, uuid):
    """dc_all처럼 유저당 레코드가 존재하는 테이블에서 uuid의 최초 날짜(date_col 최솟값)를 조회한다.

    읽기 전용(SELECT MIN(...)). 반환: (datetime | None, error | None).
    해당 uuid 행이 없으면 (None, None) — 정상 상태(레코드 없음)이며, 그 의미 판단은 호출부가 한다.
    """
    if not (project and dataset and table and user_col and date_col and uuid):
        return None, "project/dataset/table/컬럼/uuid 부족"

    try:
        from google.cloud import bigquery
    except ImportError:
        return None, "google-cloud-bigquery 미설치"

    query = (
        f"SELECT MIN(`{date_col}`) AS min_date "
        f"FROM `{project}.{dataset}.{table}` "
        f"WHERE `{user_col}` = @uuid"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("uuid", "STRING", uuid)]
    )

    try:
        rows = list(bq_client.query(query, job_config=job_config).result())
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    if not rows or rows[0]["min_date"] is None:
        return None, None
    return rows[0]["min_date"], None
