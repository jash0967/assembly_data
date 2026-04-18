import logging
import threading
import time

import requests
from requests.adapters import HTTPAdapter

import config

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 2

# ── 글로벌 Session (커넥션 풀 재사용) ──────────────────────
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# ── 글로벌 Rate Limiter (10 req/s) ─────────────────────────
_rate_lock = threading.Lock()
_last_request_time = 0.0
_MIN_INTERVAL = 0.1  # 초 (10 req/s)


def _rate_limit():
    """글로벌 rate limit 적용. 스레드 안전."""
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


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
            _rate_limit()
            resp = _session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return _parse(resp.json(), api_id)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                logger.warning(f"{api_id} p={page} attempt {attempt}: {exc}")
                time.sleep(_RETRY_DELAY * attempt)

    raise AssemblyAPIError("NETWORK", f"{_MAX_RETRIES}회 재시도 실패: {last_exc}")


def _parse(data: dict, api_id: str) -> dict:
    # 최상위 에러 (envelope 없음)
    if "RESULT" in data:
        code = data["RESULT"].get("CODE", "UNKNOWN")
        msg = data["RESULT"].get("MESSAGE", "")
        if code.startswith("INFO"):
            return {"total_count": 0, "rows": []}
        if "300" in code:
            raise MandatoryParamError(code, msg)
        raise AssemblyAPIError(code, msg)

    envelope = data.get(api_id)
    if not envelope or not isinstance(envelope, list) or len(envelope) == 0:
        raise AssemblyAPIError("PARSE", f"Unexpected response for {api_id}")

    head_part = envelope[0]
    if not isinstance(head_part, dict):
        raise AssemblyAPIError("PARSE", f"Unexpected head format for {api_id}")

    total_count = 0
    for item in head_part.get("head", []):
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

    # PAGE_SIZE fallback: 요청한 크기보다 적게 왔는데 더 있으면 → 100으로 재시도
    if page_size > 100 and len(first["rows"]) < page_size and total > len(first["rows"]):
        logger.info(f"  {api_id}: pSize={page_size} 거부됨, 100으로 fallback")
        return fetch_all_pages(api_id, extra_params=extra_params, page_size=100)

    total_pages = (total + page_size - 1) // page_size
    if total_pages > 1:
        logger.info(f"  {api_id}: {total:,}건 / {total_pages}페이지")

    for p in range(2, total_pages + 1):
        if len(all_rows) >= total:
            break
        result = fetch_page(api_id, page=p, page_size=page_size, extra_params=extra_params)
        all_rows.extend(result["rows"])
        if p % 50 == 0 or p == total_pages:
            logger.info(f"  {api_id}: {p}/{total_pages} 페이지 ({len(all_rows):,}건)")

    return all_rows
