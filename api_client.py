import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 2


class AssemblyAPIError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class MandatoryParamError(AssemblyAPIError):
    """ERROR-300: 필수 파라미터 누락."""
    pass


def fetch_page(
    api_id: str,
    page: int = 1,
    page_size: int = config.PAGE_SIZE,
    extra_params: dict | None = None,
) -> dict:
    """한 페이지 조회. 반환: {"total_count": int, "rows": list[dict]}"""
    if not config.ASSEMBLY_API_KEY:
        raise AssemblyAPIError("CONFIG", "ASSEMBLY_API_KEY not set in .env")

    url = f"{config.BASE_URL}/{api_id}"
    params = {
        "Key": config.ASSEMBLY_API_KEY,
        "Type": "json",
        "pIndex": page,
        "pSize": page_size,
    }
    if extra_params:
        params.update(extra_params)

    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return _parse(resp.json(), api_id)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                logger.warning(f"{api_id} p={page} attempt {attempt}: {exc}")
                time.sleep(_RETRY_DELAY)

    raise AssemblyAPIError("NETWORK", f"{_MAX_RETRIES}회 재시도 실패: {last_exc}")


def _parse(data: dict, api_id: str) -> dict:
    # 최상위 에러 (envelope 없음)
    if "RESULT" in data:
        code = data["RESULT"].get("CODE", "UNKNOWN")
        msg = data["RESULT"].get("MESSAGE", "")
        if "300" in code:
            raise MandatoryParamError(code, msg)
        raise AssemblyAPIError(code, msg)

    envelope = data.get(api_id)
    if not envelope or not isinstance(envelope, list):
        raise AssemblyAPIError("PARSE", f"Unexpected response for {api_id}")

    total_count = 0
    for item in envelope[0].get("head", []):
        if "list_total_count" in item:
            total_count = int(item["list_total_count"])
        if "RESULT" in item:
            code = item["RESULT"].get("CODE", "")
            if code and not code.startswith("INFO"):
                msg = item["RESULT"].get("MESSAGE", "")
                if "300" in code:
                    raise MandatoryParamError(code, msg)
                raise AssemblyAPIError(code, msg)

    rows = []
    if total_count > 0 and len(envelope) > 1 and "row" in envelope[1]:
        rows = envelope[1]["row"]

    return {"total_count": total_count, "rows": rows}


def fetch_all_pages(
    api_id: str,
    extra_params: dict | None = None,
    page_size: int = config.PAGE_SIZE,
) -> list[dict]:
    """전체 페이지 순회하여 모든 row 반환."""
    first = fetch_page(api_id, page=1, page_size=page_size, extra_params=extra_params)
    total = first["total_count"]
    all_rows = list(first["rows"])

    if total == 0:
        return []

    total_pages = (total + page_size - 1) // page_size

    for p in range(2, total_pages + 1):
        if len(all_rows) >= total:
            break
        result = fetch_page(api_id, page=p, page_size=page_size, extra_params=extra_params)
        all_rows.extend(result["rows"])

    return all_rows
