"""진행 표시 및 ETA 계산 모듈.

전략별 EMA(지수이동평균) 속도 추적으로 정확한 ETA를 제공하고,
API 단위 요약 출력으로 노이즈를 줄입니다.
"""
import shutil
from datetime import datetime, timedelta

# 전략별 초기 추정치 (초/task). 관측 데이터 없을 때 사용.
_FALLBACK_SEC = {
    "none":            3.0,
    "age":             5.0,
    "age_year":        3.0,
    "year":            3.0,
    "year_host":       3.0,
    "daily":           0.8,
    "committee":       8.0,
    "lookup_bill":     1.5,
    "lookup_bill_age": 2.0,
    "lookup_conf":     1.5,
    "age_unit":        3.0,
}


class PerStrategyETA:
    """전략별 실측 속도를 EMA로 추적하여 ETA를 계산."""

    def __init__(self):
        self.t0 = datetime.now()
        self._observed: dict[str, list[float]] = {}   # strategy -> [sec/task, ...]
        self._remaining: dict[str, int] = {}           # strategy -> remaining count
        self._done_count = 0
        self._last_activity = datetime.now()

    def set_plan(self, remaining_by_strategy: dict[str, int]):
        self._remaining = dict(remaining_by_strategy)

    def record(self, strategy: str, elapsed_sec: float):
        self._observed.setdefault(strategy, []).append(elapsed_sec)
        self._remaining[strategy] = max(0, self._remaining.get(strategy, 0) - 1)
        self._done_count += 1
        self._last_activity = datetime.now()

    def skip(self, strategy: str, count: int = 1):
        self._remaining[strategy] = max(0, self._remaining.get(strategy, 0) - count)
        self._done_count += count
        self._last_activity = datetime.now()

    def _ema(self, strategy: str) -> float:
        obs = self._observed.get(strategy)
        if not obs:
            return _FALLBACK_SEC.get(strategy, 3.0)
        alpha = 0.3
        ema = obs[0]
        for v in obs[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema

    def eta_seconds(self) -> float:
        return sum(
            self._ema(s) * cnt
            for s, cnt in self._remaining.items()
            if cnt > 0
        )

    def eta_range_str(self) -> str:
        """불확실성을 반영한 ETA 범위 문자열."""
        total_obs = sum(len(v) for v in self._observed.values())

        # 관측 데이터가 너무 적으면 ETA 대신 규모감만 표시
        if total_obs < 5:
            remaining = self.total_remaining
            if remaining > 10000:
                return "대규모 작업 (수시간 이상)"
            if remaining > 1000:
                return "대규모 작업"
            return "계산중..."

        eta = self.eta_seconds()
        if eta <= 0:
            return "곧 완료"

        if total_obs >= 50:
            margin = 0.2
        elif total_obs >= 20:
            margin = 0.35
        else:
            margin = 0.5

        lo = max(0, eta * (1 - margin))
        hi = eta * (1 + margin)
        return f"약 {_fmt_duration(lo)}~{_fmt_duration(hi)}"

    def elapsed(self) -> timedelta:
        return datetime.now() - self.t0

    def last_activity_ago(self) -> str:
        sec = (datetime.now() - self._last_activity).total_seconds()
        if sec < 5:
            return "방금"
        if sec < 60:
            return f"{int(sec)}초 전"
        return f"{int(sec // 60)}분 전"

    @property
    def done_count(self) -> int:
        return self._done_count

    @property
    def total_remaining(self) -> int:
        return sum(self._remaining.values())


def _fmt_duration(seconds: float) -> str:
    """초를 사람이 읽기 좋은 형태로 변환."""
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분"
    h = s // 3600
    m = (s % 3600) // 60
    if m == 0:
        return f"{h}시간"
    return f"{h}시간 {m}분"


def progress_bar(current: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return ""
    ratio = min(current / total, 1.0)
    filled = int(width * ratio)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def print_job_header(api_count: int, total_tasks: int, cached_tasks: int,
                     new_tasks: int, age_range: range, db_path: str, db_mb: float):
    cols = shutil.get_terminal_size().columns
    print("=" * min(cols, 60))
    print(f"  국회 Open API 수집기")
    print(f"  대상: {api_count}개 API  |  대수: {age_range.start}~{age_range.stop - 1}대")
    print(f"  캐시: {cached_tasks:,}/{total_tasks:,} tasks  →  신규 수집: {new_tasks:,} tasks")
    print(f"  DB: {db_path} ({db_mb:.1f} MB)")
    print("=" * min(cols, 60))


def print_api_start(api_idx: int, api_total: int, name_kr: str,
                    strategy: str, expected: int, cached: int,
                    task_idx: int, total_tasks: int, elapsed: timedelta):
    new = max(0, expected - cached)
    big = "  ⚠ 대규모" if new >= 1000 else ""
    pct = task_idx / total_tasks * 100 if total_tasks else 0
    print()
    print(f"[{api_idx}/{api_total}] {name_kr} [{strategy}] {new:,} tasks{big}")
    print(f"  전체: {task_idx:,}/{total_tasks:,} ({pct:.1f}%)  |  경과: {_fmt_duration(elapsed.total_seconds())}")


def print_api_progress(task_in_api: int, tasks_in_api: int,
                       rows_in_api: int, eta_tracker: PerStrategyETA):
    """API 내 task 진행률을 같은 줄에 덮어쓰기."""
    if tasks_in_api <= 0:
        return
    bar = progress_bar(task_in_api, tasks_in_api, 15)
    pct = task_in_api / tasks_in_api * 100
    eta_str = eta_tracker.eta_range_str()
    last = eta_tracker.last_activity_ago()

    cols = shutil.get_terminal_size().columns
    line = (f"\r  {bar} {task_in_api}/{tasks_in_api} ({pct:.0f}%)"
            f"  {rows_in_api:,}건  |  {eta_str}  |  응답: {last}")
    print(line[:cols].ljust(cols), end="", flush=True)


def print_api_done(api_idx: int, api_total: int, name_kr: str,
                   rows: int, empty: int, errors: int, duration_sec: float):
    parts = [f"{rows:,}건"]
    if empty:
        parts.append(f"빈 응답 {empty}")
    if errors:
        parts.append(f"오류 {errors}")
    summary = ", ".join(parts)
    dur = _fmt_duration(duration_sec)
    # \r로 진행 바 줄 지우고 확정 출력
    cols = shutil.get_terminal_size().columns
    line = f"\r  ✓ {name_kr}  {summary}  ({dur})"
    print(line.ljust(cols))


def print_api_cached(api_idx: int, api_total: int, name_kr: str,
                     cached_count: int):
    print(f"\r  ● {name_kr}  캐시 ({cached_count} tasks)")


def print_api_error(api_idx: int, api_total: int, name_kr: str, error: str):
    print(f"\r  ✗ {name_kr}  오류: {error}")


def print_phase_separator(phase: int):
    cols = shutil.get_terminal_size().columns
    print()
    print(f"{'─' * min(cols, 60)}")
    print(f"  Phase {phase} 시작 (Phase 1 데이터 참조)")
    print(f"{'─' * min(cols, 60)}")


def print_midpoint_summary(task_idx: int, total_tasks: int,
                           elapsed: timedelta, eta_tracker: PerStrategyETA):
    pct = task_idx / total_tasks * 100 if total_tasks else 0
    eta_str = eta_tracker.eta_range_str()
    cols = shutil.get_terminal_size().columns
    print(f"\n{'─' * min(cols, 60)}")
    print(f"  전체: {task_idx:,}/{total_tasks:,} ({pct:.1f}%)  |  경과: {_fmt_duration(elapsed.total_seconds())}  |  {eta_str}")
    print(f"{'─' * min(cols, 60)}")


def print_job_footer(done: int, skipped_cached: int, skipped_empty: int,
                     errors: int, elapsed: timedelta, db_mb: float,
                     db_mb_start: float, error_details: list[str]):
    cols = shutil.get_terminal_size().columns
    print()
    print("=" * min(cols, 60))
    print(f"  완료  |  소요: {_fmt_duration(elapsed.total_seconds())}")
    print(f"  저장: {done:,}건  |  캐시: {skipped_cached:,}  |  빈 응답: {skipped_empty:,}  |  오류: {errors}")
    delta = db_mb - db_mb_start
    sign = "+" if delta >= 0 else ""
    print(f"  DB: {db_mb:.1f} MB ({sign}{delta:.1f} MB)")
    if error_details:
        print()
        print("  오류 목록:")
        for e in error_details:
            print(f"    - {e}")
    print("=" * min(cols, 60))
