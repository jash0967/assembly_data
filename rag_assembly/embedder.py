"""Vertex AI gemini-embedding-001 임베딩 wrapper.

google-genai SDK (신규) 사용. ADC 인증 자동.

- batch 처리 (config.EMBED_BATCH_SIZE)
- 429 / 503 / 504 backoff retry
- multi-region rotation (config.ENABLED_REGIONS): 각 region 독립 quota 활용
- 429 발생 시 다른 region으로 즉시 fallback
- task_type 분리 (DOCUMENT vs QUERY)
"""
import _bootstrap  # noqa: F401

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

# google-genai 사용 전 환경변수 설정 필요
import config as cfg
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", cfg.GCP_PROJECT)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

from google import genai
from google.genai.types import EmbedContentConfig
from google.genai import errors as genai_errors


log = logging.getLogger(__name__)


class RateLimiter:
    """초당 RPM/60 만큼만 통과시키는 단순 토큰버킷."""
    def __init__(self, rpm: int):
        self.interval = 60.0 / max(rpm, 1)
        self.lock = threading.Lock()
        self.next_time = 0.0

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = self.next_time - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self.next_time = now + self.interval


class Embedder:
    """gemini-embedding-001 사용. batch + retry + multi-region rotation 통합.

    각 region은 독립 1M tokens/min quota를 가짐. 7 region 사용 시 7M TPM.
    region별로 별도 Client 생성, round-robin으로 분산.
    """
    def __init__(self, regions: list[str] = None):
        self.regions = regions or cfg.ENABLED_REGIONS or [cfg.GCP_LOCATION]
        # region별 Client 생성 (각각 다른 location으로)
        self.clients = {}
        for r in self.regions:
            self.clients[r] = genai.Client(
                vertexai=True,
                project=cfg.GCP_PROJECT,
                location=r,
            )
        self._region_idx = 0
        self._region_lock = threading.Lock()
        # 일시 차단된 region 추적 (429 quota burst 시)
        self._region_cooldown: dict[str, float] = {}

        self.model = cfg.EMBED_MODEL
        self.dim = cfg.EMBED_DIM
        self.batch_size = cfg.EMBED_BATCH_SIZE
        # rate limiter는 region별로 별도가 더 정확하지만,
        # multi-region에서 글로벌 제한은 거의 무의미 (region별 quota가 진짜 제약)
        self.limiter = RateLimiter(cfg.RATE_LIMIT_RPM * len(self.regions))

        log.info("Embedder ready: %d regions = %s",
                 len(self.regions), self.regions)

    def _next_region(self) -> str:
        """현재 시점에 사용 가능한 region 중 하나를 round-robin 선택."""
        now = time.monotonic()
        with self._region_lock:
            tries = 0
            while tries < len(self.regions):
                r = self.regions[self._region_idx % len(self.regions)]
                self._region_idx += 1
                tries += 1
                cd_until = self._region_cooldown.get(r, 0)
                if cd_until <= now:
                    return r
            # 전부 cooldown 중이면 가장 빨리 풀리는 것 선택
            r, _ = min(self._region_cooldown.items(), key=lambda x: x[1])
            return r

    def _set_cooldown(self, region: str, seconds: float) -> None:
        with self._region_lock:
            self._region_cooldown[region] = time.monotonic() + seconds

    def _call_with_retry(self, texts: list[str], task_type: str,
                         max_retries: int = 12) -> list[list[float]]:
        """Multi-region 회전 + retry. 429 발생 시 region을 60초 cooldown 처리하고 다음 region으로."""
        cfg_obj = EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.dim,
        )
        last_err = None
        for attempt in range(max_retries):
            self.limiter.acquire()
            region = self._next_region()
            client = self.clients[region]
            try:
                resp = client.models.embed_content(
                    model=self.model,
                    contents=texts,
                    config=cfg_obj,
                )
                return [e.values for e in resp.embeddings]
            except genai_errors.ClientError as e:
                last_err = e
                code = getattr(e, "status_code", None) or getattr(e, "code", None)
                if code == 429:
                    # 이 region은 60초 cooldown, 다음 region으로 즉시 시도
                    self._set_cooldown(region, 60.0)
                    log.warning("429 [%s] cooldown 60s; try %d/%d next region",
                                region, attempt + 1, max_retries)
                    # 짧은 jitter 후 즉시 재시도
                    time.sleep(random.uniform(0.1, 0.5))
                    continue
                if code is not None and 500 <= code < 600:
                    wait = min(2.0 * (attempt + 1), 30.0)
                    log.warning("server %d [%s]; retry %d/%d after %.1fs",
                                code, region, attempt + 1, max_retries, wait)
                    time.sleep(wait)
                    continue
                log.error("permanent client error %s [%s]: %s", code, region, e)
                raise
            except genai_errors.ServerError as e:
                last_err = e
                wait = min(2.0 * (attempt + 1), 30.0)
                log.warning("server error [%s]; retry %d/%d after %.1fs",
                            region, attempt + 1, max_retries, wait)
                time.sleep(wait)
            except (ConnectionError, TimeoutError) as e:
                last_err = e
                wait = min(2.0 * (attempt + 1), 30.0)
                log.warning("network %s [%s]; retry %d/%d after %.1fs",
                            type(e).__name__, region, attempt + 1,
                            max_retries, wait)
                time.sleep(wait)
        raise RuntimeError(f"embed failed after {max_retries} retries (last: {last_err})")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """RETRIEVAL_DOCUMENT task type."""
        return self._embed_in_batches(texts, cfg.EMBED_TASK_TYPE_DOC)

    def embed_query(self, query: str) -> list[float]:
        """RETRIEVAL_QUERY task type — 쿼리 시 사용."""
        out = self._embed_in_batches([query], cfg.EMBED_TASK_TYPE_QUERY)
        return out[0]

    def _embed_in_batches(self, texts: list[str], task_type: str
                          ) -> list[list[float]]:
        # 단일 호출 (1 batch)
        if len(texts) <= self.batch_size:
            return self._call_with_retry(texts, task_type)
        # 다중 batch — 병렬 처리
        batches = [texts[i:i + self.batch_size]
                   for i in range(0, len(texts), self.batch_size)]
        results: list[list[list[float]]] = [None] * len(batches)
        max_workers = min(cfg.EMBED_CONCURRENCY, len(batches))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(self._call_with_retry, b, task_type): i
                for i, b in enumerate(batches)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                results[i] = fut.result()
        # flatten
        out: list[list[float]] = []
        for r in results:
            out.extend(r)
        return out
