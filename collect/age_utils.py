"""Derive `age` (Korean Assembly term) from various source fields.

Used by:
- download_all.save_rows  — write-time injection on new collection
- backfill_ages.py        — one-shot retro-fill on existing data
- validate_collection.py  — drift detection

Each ApiSpec declares an `age_source` string (see config.py) that this
module knows how to resolve into an integer age.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- adds repo root + collect/ to sys.path

import re
from datetime import date as _date

from config import AGE_YEAR_RANGE

_ERACO_RE = re.compile(r"^제(\d+)대$")
# Alternative ERACO formats seen in the wild:
#   nzivskufaliivfhpb (역대 의안 통계): "N대 국회" or "N대"
_ERACO_ALT_RE = re.compile(r"^(\d+)대(?:\s*국회)?$")
_KOREAN_AGE_RE = re.compile(r"(\d+)대")

# Special bodies that appear in BILLRCP.ERACO instead of a normal "제N대".
# Stored as negative ages so analysis queries can filter with `WHERE age >= 1`.
ERACO_SPECIAL: dict[str, int] = {
    "국가보위입법회의": -3,  # 1980-1981
    "국가재건최고회의": -2,  # 1961-1963
    "비상국무회의":      -1,  # 1972-1973
}
# 제헌국회 = the very first National Assembly (1948), often labeled differently.
ERACO_NAMED_AGES: dict[str, int] = {
    "제헌": 1,
    "제헌국회": 1,
}

# (start_date, age) sorted ascending. Each Assembly term begins May 30 of the
# inauguration year. AGE_YEAR_RANGE provides the inauguration year for every term.
_TERM_STARTS: list[tuple[_date, int]] = sorted(
    [(_date(start_y, 5, 30), age) for age, (start_y, _) in AGE_YEAR_RANGE.items()],
    key=lambda t: t[0],
)


def parse_eraco(value) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _ERACO_RE.match(s)
    if m:
        return int(m.group(1))
    m = _ERACO_ALT_RE.match(s)
    if m:
        return int(m.group(1))
    if s in ERACO_NAMED_AGES:
        return ERACO_NAMED_AGES[s]
    return ERACO_SPECIAL.get(s)


def parse_unit_cd(value) -> int | None:
    """100013-100022 → 13-22."""
    if value is None:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    n = int(s)
    if 100001 <= n <= 100099:
        return n - 100000
    return None


def parse_age_korean(value) -> int | None:
    """'22대' or '제22대 국회' → 22."""
    if value is None:
        return None
    m = _KOREAN_AGE_RE.search(str(value))
    return int(m.group(1)) if m else None


def parse_int(value) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def date_to_age(value) -> int | None:
    """Map a date or year value to the active Assembly term.

    Boundary: May 30 of the inauguration year. Bare 4-digit years map to mid-year.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    if s.isdigit() and len(s) == 4:
        return _lookup_active_term(_date(int(s), 6, 30))

    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        try:
            return _lookup_active_term(_date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])))
        except ValueError:
            return None
    return None


def _lookup_active_term(d: _date) -> int | None:
    active = None
    for start_d, age in _TERM_STARTS:
        if start_d <= d:
            active = age
        else:
            break
    return active


def parse_param_age_from_task_key(task_key: str, field: str) -> int | None:
    """Extract `__{FIELD}_{n}` from a task_key like 'nzmi__AGE_22' or '__DAE_NUM_22__YEAR_2024'."""
    m = re.search(rf"__{re.escape(field)}_(-?\d+)", task_key)
    return int(m.group(1)) if m else None


_COLUMN_PARSERS = {
    "ERACO": parse_eraco,
    "UNIT_CD": parse_unit_cd,
    "PROFILE_UNIT_CD": parse_unit_cd,
    "ORD_NUM": parse_age_korean,
    "DIV": parse_age_korean,
    "AGE": parse_int,
    "DAE_NUM": parse_int,
    "REGDAESU": parse_int,
    "dae_num": parse_eraco,  # speeches.dae_num: '22' or '제22대' both occur
    "age": parse_int,
}


def derive_age(age_source: str, *, task_key: str | None = None,
               row: dict | None = None) -> int | None:
    """Resolve age from a row + task_key per a spec's age_source.

    Returns None for `join:...` (resolved post-insert in Phase 2) and `none`.
    """
    if age_source in ("none", ""):
        return None

    kind, _, arg = age_source.partition(":")

    if kind == "constant":
        try:
            return int(arg)
        except ValueError:
            return None

    if kind == "param":
        return parse_param_age_from_task_key(task_key or "", arg)

    if kind == "column":
        if row is None:
            return None
        parser = _COLUMN_PARSERS.get(arg, parse_int)
        return parser(row.get(arg))

    if kind == "date":
        if row is None:
            return None
        return date_to_age(row.get(arg))

    if kind == "join":
        return None  # post-insert, see Phase 2

    return None


if __name__ == "__main__":
    # Quick smoke test
    cases = [
        ("constant:22", {}, None, 22),
        ("column:ERACO", {"ERACO": "제21대"}, None, 21),
        ("column:ERACO", {"ERACO": "국가재건최고회의"}, None, -2),
        ("column:UNIT_CD", {"UNIT_CD": "100020"}, None, 20),
        ("column:PROFILE_UNIT_CD", {"PROFILE_UNIT_CD": "100022"}, None, 22),
        ("column:ORD_NUM", {"ORD_NUM": "22대"}, None, 22),
        ("column:DIV", {"DIV": "제21대 국회"}, None, 21),
        ("column:AGE", {"AGE": "20"}, None, 20),
        ("date:HOST_DT", {"HOST_DT": "2024-04-15"}, None, 21),  # before May 30
        ("date:HOST_DT", {"HOST_DT": "2024-06-01"}, None, 22),  # after
        ("date:YR", {"YR": "2026"}, None, 22),
        ("param:AGE", {}, "nzmi__AGE_19", 19),
        ("param:DAE_NUM", {}, "ngyto__AGE_21__YEAR_2022", None),  # field is DAE_NUM not AGE
        ("param:DAE_NUM", {}, "ngyto__DAE_NUM_22__CONF_DATE_2024", 22),
        ("join:billrcp.BILL_ID", {}, None, None),
        ("none", {}, None, None),
    ]
    for src, row, tk, expected in cases:
        got = derive_age(src, task_key=tk, row=row)
        ok = "✓" if got == expected else "✗"
        print(f"  {ok} {src!r} row={row} tk={tk} → {got} (expected {expected})")
