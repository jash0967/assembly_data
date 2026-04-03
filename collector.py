import logging

from config import ApiSpec, MAX_AGE
from api_client import fetch_all_pages, MandatoryParamError

logger = logging.getLogger(__name__)


def collect_api(
    spec: ApiSpec,
    age_range: range | None = None,
    skip_keys: set[str] | None = None,
) -> dict[str, list[dict]]:
    """API 하나의 전체 데이터 수집.

    iterate_age API는 AGE별로 반복, 아닌 경우 1회 호출.
    skip_keys에 포함된 task_key는 API 호출 자체를 건너뜀.

    Returns: {task_key: [row, ...]}
    Raises: MandatoryParamError (필수 파라미터 누락 시)
    """
    if age_range is None:
        age_range = range(1, MAX_AGE + 1)

    results: dict[str, list[dict]] = {}

    if spec.iterate_age:
        for age in age_range:
            task_key = f"{spec.api_id}__AGE_{age}"
            if skip_keys and task_key in skip_keys:
                logger.debug(f"  skip {task_key}")
                continue
            params = {**spec.required_params, "AGE": age}
            try:
                rows = fetch_all_pages(spec.api_id, extra_params=params)
                results[task_key] = rows
                logger.info(f"  {spec.name_kr} AGE={age}: {len(rows)}건")
            except MandatoryParamError:
                raise
            except Exception as e:
                logger.error(f"  {spec.name_kr} AGE={age}: {e}")
                results[task_key] = []
    else:
        task_key = spec.api_id
        if skip_keys and task_key in skip_keys:
            logger.debug(f"  skip {task_key}")
            return results
        params = spec.required_params or None
        try:
            rows = fetch_all_pages(spec.api_id, extra_params=params)
            results[task_key] = rows
            logger.info(f"  {spec.name_kr}: {len(rows)}건")
        except MandatoryParamError:
            raise
        except Exception as e:
            logger.error(f"  {spec.name_kr}: {e}")
            results[task_key] = []

    return results
