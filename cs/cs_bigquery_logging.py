# -*- coding: utf-8 -*-
"""cs/console 공용 BigQuery 읽기 helper (cs_gcp_logging.py의 BigQuery 버전).

Cloud Logging(entries.list) 대신 BigQuery로 로그/유저 데이터를 내보내는 프로젝트용.
읽기 전용(SELECT)만 수행한다.
"""

BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery"
# 주의: bigquery.readonly 스코프로는 쿼리 실행(jobs.insert)이 거부된다
# (403 ACCESS_TOKEN_SCOPE_INSUFFICIENT, 라이브 테스트로 확인 2026-07-03).
# BigQuery는 SELECT도 내부적으로 job을 생성해 실행하는 구조라 job 생성 자체에
# 더 넓은 스코프가 필요함. 실제 쓰기/삭제 가능 여부는 스코프가 아니라 서비스계정에
# 부여된 IAM 역할(BigQuery 데이터 뷰어 + BigQuery 작업 사용자)로 제한되므로,
# 스코프를 넓혀도 이 서비스계정이 실제로 할 수 있는 일은 여전히 조회뿐이다.


def load_bigquery_credentials(key_path):
    """서비스계정 키 파일 → credentials(bigquery scope). 실패 시 None.

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
            str(key_path), scopes=[BIGQUERY_SCOPE]
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


def run_readonly_query(bq_client, query, query_parameters, maximum_bytes_billed, location=None):
    """읽기 전용 쿼리를 **dry-run 예상 처리량 확인 + maximum_bytes_billed 강제** 하에 실행한다.

    BIGQUERY_COST_GUARDRAILS.md §4-C(dry run)·§4-D(강제 상한)의 코드 강제 지점.
    반환: (rows | None, error | None).

    fail-closed 원칙:
      - maximum_bytes_billed가 없거나(0/None) 양수가 아니면 **쿼리를 실행하지 않고** 오류를 반환한다.
      - dry-run 예상 스캔량이 상한을 초과하면 실제 쿼리를 실행하지 않고 오류를 반환한다.
      - 실제 쿼리에도 maximum_bytes_billed를 걸어, dry-run 이후 데이터 증가로 상한을 넘으면
        BigQuery가 과금 전에 job을 실패시킨다(경쟁 상태 방어).
    """
    if not maximum_bytes_billed or maximum_bytes_billed <= 0:
        return None, (
            "maximum_bytes_billed 미설정(fail-closed): "
            "BQ_MAXIMUM_BYTES_BILLED(또는 {TITLE}_BQ_MAXIMUM_BYTES_BILLED)를 양수로 설정해야 쿼리를 실행합니다"
        )

    try:
        from google.cloud import bigquery
    except ImportError:
        return None, "google-cloud-bigquery 미설치"

    params = list(query_parameters or [])

    # 1) dry-run: 실제 스캔·과금 없이 예상 처리량만 확인(INFORMATION_SCHEMA 없이 저비용 확인 경로).
    dry_config = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        query_parameters=params,
    )
    try:
        dry_job = bq_client.query(query, job_config=dry_config, location=location)
    except Exception as exc:  # noqa: BLE001
        return None, f"dry-run 실패: {exc}"

    estimated = int(getattr(dry_job, "total_bytes_processed", 0) or 0)
    if estimated > maximum_bytes_billed:
        return None, (
            f"예상 스캔 {estimated:,}B > 상한 {maximum_bytes_billed:,}B — 쿼리 중단(fail-closed). "
            "쿼리에 파티션(날짜) 조건을 추가하거나 상한을 재검토하세요"
        )

    # 2) 실제 실행: 상한을 job에도 강제해 초과 시 과금 전에 실패시킨다.
    real_config = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=int(maximum_bytes_billed),
    )
    try:
        rows = list(bq_client.query(query, job_config=real_config, location=location).result())
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    return rows, None


def fetch_min_date_for_user(
    bq_client, project, dataset, table, user_col, date_col, uuid, maximum_bytes_billed
):
    """dc_all처럼 유저당 레코드가 존재하는 테이블에서 uuid의 최초 날짜(date_col 최솟값)를 조회한다.

    읽기 전용(SELECT MIN(...)). 모든 실행은 dry-run 예상 처리량 확인 + maximum_bytes_billed
    강제를 거친다(run_readonly_query). 반환: (datetime | None, error | None).
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
    params = [bigquery.ScalarQueryParameter("uuid", "STRING", uuid)]

    rows, err = run_readonly_query(bq_client, query, params, maximum_bytes_billed)
    if err:
        return None, err

    if not rows or rows[0]["min_date"] is None:
        return None, None
    return rows[0]["min_date"], None
