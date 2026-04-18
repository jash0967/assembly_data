import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from config import ApiSpec, AGE_YEAR_RANGE, MAX_AGE
from api_client import fetch_all_pages, MandatoryParamError

logger = logging.getLogger(__name__)

_MAX_WORKERS = 4


def _fetch_task(api_id: str, task_key: str, params: dict) -> tuple[str, list[dict]]:
    """단일 task 수집. (task_key, rows) 반환. MandatoryParamError는 상위로 전파."""
    try:
        rows = fetch_all_pages(api_id, extra_params=params or None)
        return (task_key, rows)
    except MandatoryParamError:
        raise
    except Exception as e:
        logger.error(f"  {task_key}: {e}")
        return (task_key, [])


def _generate_tasks(
    spec: ApiSpec,
    age_range: range,
    con=None,
) -> list[tuple[str, dict]]:
    """strategy에 따라 [(task_key, extra_params)] 목록 생성."""
    aid = spec.api_id
    strategy = spec.strategy

    if strategy == "none":
        return [(aid, {**spec.required_params})]

    if strategy == "age":
        # required_params에 대수 키가 있으면 그 키 사용, 없으면 "AGE"
        age_key = "AGE"
        for k in spec.required_params:
            if k in ("AGE", "DAE_NUM", "REGDAESU"):
                age_key = k
                break
        return [
            (f"{aid}__AGE_{age}", {**spec.required_params, age_key: age})
            for age in age_range
        ]

    if strategy == "age_year":
        tasks = []
        for age in age_range:
            yr_range = AGE_YEAR_RANGE.get(age)
            if not yr_range:
                continue
            start_yr, end_yr = yr_range
            for year in range(start_yr, end_yr + 1):
                tasks.append((
                    f"{aid}__AGE_{age}__YEAR_{year}",
                    {**spec.required_params, "DAE_NUM": age, "CONF_DATE": str(year)},
                ))
        return tasks

    if strategy == "year":
        current_year = date.today().year
        return [
            (f"{aid}__YEAR_{year}",
             {**spec.required_params, "TAKING_DATE": str(year)})
            for year in range(2000, current_year + 1)
        ]

    if strategy == "age_unit":
        return [
            (f"{aid}__AGE_{age}",
             {**spec.required_params, "UNIT_CD": f"1000{age}"})
            for age in age_range
        ]

    if strategy == "year_host":
        current_year = date.today().year
        return [
            (f"{aid}__YEAR_{year}",
             {**spec.required_params, "HOST_DT": str(year)})
            for year in range(2000, current_year + 1)
        ]

    if strategy == "daily":
        tasks = []
        for age in age_range:
            yr_range = AGE_YEAR_RANGE.get(age)
            if not yr_range:
                continue
            start_yr, end_yr = yr_range
            d = date(start_yr, 1, 1)
            end_d = min(date(end_yr, 12, 31), date.today())
            while d <= end_d:
                dt_str = d.strftime("%Y-%m-%d")
                tasks.append((
                    f"{aid}__AGE_{age}__DT_{dt_str}",
                    {**spec.required_params, "AGE": age, "DT": dt_str},
                ))
                d += timedelta(days=1)
        return tasks

    if strategy == "committee":
        if con is None:
            logger.warning("  committee strategy: DB 연결 없음, 건너뜀")
            return []
        try:
            rows = con.execute(
                'SELECT DISTINCT "COMMITTEE_NAME" FROM nxrvzonlafugpqjuh'
            ).fetchall()
            names = [r[0] for r in rows if r[0]]
        except Exception as e:
            logger.warning(f"  위원회 목록 조회 실패: {e}")
            return []
        return [
            (f"{aid}__CMIT_{name}", {**spec.required_params, "CURR_COMMITTEE": name})
            for name in names
        ]

    if strategy == "lookup_bill":
        if con is None:
            logger.warning("  lookup_bill strategy: DB 연결 없음, 건너뜀")
            return []
        try:
            age_filters = " OR ".join(f"\"ERACO\" = '제{a}대'" for a in age_range)
            rows = con.execute(
                f'SELECT DISTINCT "BILL_ID" FROM billrcp WHERE {age_filters}'
            ).fetchall()
            ids = [r[0] for r in rows if r[0]]
        except Exception as e:
            logger.warning(f"  BILL_ID 목록 조회 실패: {e}")
            return []
        return [
            (f"{aid}__BILL_{bid}", {**spec.required_params, "BILL_ID": bid})
            for bid in ids
        ]

    if strategy == "lookup_bill_age":
        # AGE + BILL_ID 둘 다 필수 (예: 국회의원 본회의 표결정보)
        if con is None:
            logger.warning("  lookup_bill_age strategy: DB 연결 없음, 건너뜀")
            return []
        try:
            age_list = ", ".join(f"'{a}'" for a in age_range)
            rows = con.execute(
                f'SELECT DISTINCT "BILL_ID", "AGE" FROM ncocpgfiaoituanbr '
                f'WHERE "AGE" IN ({age_list}) AND "BILL_ID" IS NOT NULL'
            ).fetchall()
        except Exception as e:
            logger.warning(f"  BILL_ID+AGE 목록 조회 실패: {e}")
            return []
        return [
            (f"{aid}__AGE_{age}__BILL_{bid}",
             {**spec.required_params, "AGE": age, "BILL_ID": bid})
            for bid, age in rows
        ]

    if strategy == "lookup_conf":
        if con is None:
            logger.warning("  lookup_conf strategy: DB 연결 없음, 건너뜀")
            return []
        try:
            age_filters = " OR ".join(f"\"ERACO\" = '제{a}대'" for a in age_range)
            rows = con.execute(
                f'SELECT DISTINCT "CONF_ID" FROM vconfsubcconflist WHERE {age_filters}'
            ).fetchall()
            ids = [r[0] for r in rows if r[0]]
        except Exception as e:
            logger.warning(f"  CONF_ID 목록 조회 실패: {e}")
            return []
        return [
            (f"{aid}__CONF_{cid}", {**spec.required_params, "CONF_ID": cid})
            for cid in ids
        ]

    raise ValueError(f"알 수 없�� strategy: {strategy}")


def collect_api(
    spec: ApiSpec,
    age_range: range | None = None,
    skip_keys: set[str] | None = None,
    con=None,
    on_task_done=None,
) -> dict[str, list[dict]]:
    """API 하나의 전체 데이터 수집.

    strategy에 따라 task 목록을 생성하고 병렬 호출.
    skip_keys에 포함된 task_key는 API 호출 자체를 건너뜀.
    on_task_done(task_key, rows): task 완료 콜백 (메인 스레드에서 호출).

    Returns: {task_key: [row, ...]}
    Raises: MandatoryParamError (필수 파라미터 누락 시)
    """
    if age_range is None:
        age_range = range(1, MAX_AGE + 1)

    tasks = _generate_tasks(spec, age_range, con)
    todo = [(tk, params) for tk, params in tasks
            if not (skip_keys and tk in skip_keys)]

    results: dict[str, list[dict]] = {}

    # task가 적으면 직렬 처리 (오버헤드 방지)
    if len(todo) <= 2:
        for task_key, params in todo:
            try:
                task_key, rows = _fetch_task(spec.api_id, task_key, params)
                results[task_key] = rows
                if on_task_done:
                    on_task_done(task_key, rows)
            except MandatoryParamError:
                raise
            except Exception as e:
                logger.error(f"  {task_key}: {e}")
                results[task_key] = []
                if on_task_done:
                    on_task_done(task_key, [])
        return results

    # 병렬 처리
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_task, spec.api_id, tk, params): tk
            for tk, params in todo
        }
        for future in as_completed(futures):
            tk = futures[future]
            try:
                task_key, rows = future.result()
                results[task_key] = rows
                if on_task_done:
                    on_task_done(task_key, rows)
            except MandatoryParamError:
                raise
            except Exception as e:
                logger.error(f"  {tk}: {e}")
                results[tk] = []
                if on_task_done:
                    on_task_done(tk, [])

    return results
