"""국회 법안 의안원문 PDF 다운로드 및 텍스트 추출 → DuckDB.bill_text.

Phase 3 이후: 추출 결과는 DuckDB의 `bill_text` 테이블에 저장됨.
PDF는 파일시스템(`data/bills_kr/pdf_archives/{age}/`)에 그대로 보관,
`bill_text.pdf_path` 컬럼에 절대경로 기록.

Usage:
    python download_bills.py --age 22                # 22대 전체
    python download_bills.py --age 22 --limit 10     # 테스트 10건
    python download_bills.py --age 22 --extract-only # PDF는 이미 있다고 가정, 추출만
    python download_bills.py --age 22 --status       # 진행 현황
"""
import argparse
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb
import fitz
import requests

import _bootstrap  # noqa: F401  -- adds repo root + collect/ to sys.path

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BILL_PDF_DIR = ""

FILELIST_URL = "https://likms.assembly.go.kr/bill/bi/dwld/selectFileList.do"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
RETRY = 3
RETRY_DELAY = 2

BILL_TEXT_CREATE = """
CREATE TABLE IF NOT EXISTS bill_text (
    bill_id            VARCHAR PRIMARY KEY,
    age                INTEGER NOT NULL,
    reason_and_content TEXT,
    full_text          TEXT,
    pdf_path           VARCHAR,
    extractor_version  VARCHAR DEFAULT 'fitz-1.0',
    extracted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

BILL_TEXT_INSERT = """
INSERT INTO bill_text (bill_id, age, reason_and_content, full_text, pdf_path)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (bill_id) DO UPDATE SET
    age = EXCLUDED.age,
    reason_and_content = EXCLUDED.reason_and_content,
    full_text = EXCLUDED.full_text,
    pdf_path = EXCLUDED.pdf_path,
    extracted_at = now();
"""

# Single shared write connection with a lock — DuckDB does not allow concurrent
# writes from the same process. PDF download/extract still parallelize freely.
_db_lock = threading.Lock()
_db_con: duckdb.DuckDBPyConnection | None = None


# ── 법안 목록 ─────────────────────────────────────────────

def get_bill_list(age: int) -> list[dict]:
    """국회 법안 목록 (DB의 v_bill 뷰에서)."""
    con = duckdb.connect(config.RAW_DB_PATH, read_only=True)
    try:
        rows = con.execute(f"""
            SELECT bill_id, bill_no, bill_name, lead_proposer, propose_date, committee
            FROM v_bill
            WHERE age = {int(age)}
            ORDER BY propose_date DESC
        """).fetchall()
    finally:
        con.close()

    return [
        {
            "bill_id": bid,
            "bill_no": bno or "",
            "bill_name": name,
            "proposer": prop or "",
            "propose_date": str(date or ""),
            "committee": committee or "",
        }
        for bid, bno, name, prop, date, committee in rows
    ]


# ── PDF URL 조회 ──────────────────────────────────────────

def get_pdf_url(bill_id: str) -> str | None:
    for attempt in range(RETRY):
        try:
            resp = requests.post(
                FILELIST_URL,
                data={"billId": bill_id},
                headers=HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for f in data.get("billFileList", []):
                if f.get("docKindCd") == "의안원문" and f.get("pdfFileDwnldUrl"):
                    return f["pdfFileDwnldUrl"]
            return None
        except Exception as e:
            if attempt < RETRY - 1:
                time.sleep(RETRY_DELAY)
            else:
                logger.debug(f"PDF URL 조회 실패 {bill_id}: {e}")
    return None


# ── PDF 다운로드 ──────────────────────────────────────────

def download_pdf(url: str, filepath: str) -> bool:
    for attempt in range(RETRY):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60, stream=True)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            if os.path.getsize(filepath) < 500:
                os.remove(filepath)
                return False
            return True
        except Exception:
            if attempt < RETRY - 1:
                time.sleep(RETRY_DELAY)
    return False


# ── 텍스트 추출 ──────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    try:
        return "".join(page.get_text() for page in doc)
    finally:
        doc.close()


def parse_bill_sections(text: str) -> dict:
    result = {"full_text": text}
    m = re.search(r"제\s*안\s*이\s*유(?:\s*및\s*주\s*요\s*내\s*용)?\s*\n(.*)", text, re.DOTALL)
    if m:
        result["reason_and_content"] = m.group(1).strip()
    return result


# ── DB 기반 bill_text 저장 ────────────────────────────────

def save_bill_text(bill_id: str, age: int, reason: str | None,
                   full_text: str, pdf_path: str | None) -> None:
    with _db_lock:
        _db_con.execute(BILL_TEXT_INSERT, [bill_id, age, reason, full_text, pdf_path])


def _reader_con():
    """Return the shared write connection (DuckDB forbids mixing RO+RW in same process)."""
    return _db_con if _db_con is not None else duckdb.connect(config.RAW_DB_PATH, read_only=True)


def load_existing_bill_ids(age: int) -> set[str]:
    con = _reader_con()
    try:
        rows = con.execute(
            "SELECT bill_id FROM bill_text WHERE age = ?", [age]
        ).fetchall()
    finally:
        if con is not _db_con:
            con.close()
    return {r[0] for r in rows}


# ── 단일 법안 처리 ────────────────────────────────────────

def process_bill(bill: dict, age: int, *, download: bool, extract: bool,
                 existing_ids: set[str]) -> str:
    """반환: 'ok' | 'skip' | 'no_pdf' | 'dl_fail' | 'ext_fail'."""
    bill_id = bill["bill_id"]

    if bill_id in existing_ids:
        return "skip"

    safe_id = re.sub(r'[\\/:*?"<>|]', "_", bill_id)
    pdf_path = os.path.join(BILL_PDF_DIR, f"{safe_id}.pdf")

    if download and not os.path.exists(pdf_path):
        url = get_pdf_url(bill_id)
        if not url:
            return "no_pdf"
        if not download_pdf(url, pdf_path):
            return "dl_fail"

    if not extract:
        return "ok"

    if not os.path.exists(pdf_path):
        return "no_pdf"

    try:
        text = extract_text_from_pdf(pdf_path)
        sections = parse_bill_sections(text)
        save_bill_text(
            bill_id=bill_id,
            age=age,
            reason=sections.get("reason_and_content"),
            full_text=text,
            pdf_path=pdf_path,
        )
        existing_ids.add(bill_id)
    except Exception as e:
        logger.debug(f"추출 실패 {bill_id}: {e}")
        return "ext_fail"

    return "ok"


# ── 메인 ─────────────────────────────────────────────────

def show_status(age: int) -> None:
    pdf_count = len([f for f in os.listdir(BILL_PDF_DIR) if f.endswith(".pdf")]) \
        if os.path.exists(BILL_PDF_DIR) else 0
    pdf_size = 0
    if os.path.exists(BILL_PDF_DIR):
        for f in os.listdir(BILL_PDF_DIR):
            pdf_size += os.path.getsize(os.path.join(BILL_PDF_DIR, f))

    con = duckdb.connect(config.RAW_DB_PATH, read_only=True)
    try:
        db_count = con.execute(
            "SELECT COUNT(*) FROM bill_text WHERE age = ?", [age]
        ).fetchone()[0]
    finally:
        con.close()

    print("=" * 50)
    print(f" {age}대 법안 수집 현황")
    print(f"  PDF 다운로드: {pdf_count}건 ({pdf_size / 1024 / 1024:.1f} MB)")
    print(f"  bill_text DB: {db_count}건")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="국회 법안 PDF 다운로드 → DuckDB.bill_text")
    parser.add_argument("--age", type=int, required=True, help="국회 대수 (예: 21, 22)")
    parser.add_argument("--limit", type=int, default=0, help="테스트용 N건만")
    parser.add_argument("--extract-only", action="store_true", help="PDF가 이미 있다고 가정, 추출+DB 저장만")
    parser.add_argument("--download-only", action="store_true", help="PDF 다운로드만, DB 저장 안 함")
    parser.add_argument("--workers", type=int, default=8, help="동시 다운로드 수")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    global BILL_PDF_DIR
    BILL_PDF_DIR = os.path.join(config.BILLS_KR_DIR, "pdf_archives", str(args.age))

    if args.status:
        show_status(args.age)
        return

    os.makedirs(BILL_PDF_DIR, exist_ok=True)

    print(f"{args.age}대 국회 법안 수집")
    print(f"  PDF: {BILL_PDF_DIR}")
    print(f"  DB:  {config.RAW_DB_PATH}::bill_text")

    bills = get_bill_list(age=args.age)
    if args.limit:
        bills = bills[:args.limit]
    total = len(bills)
    print(f"대상: {total}건")

    global _db_con
    _db_con = duckdb.connect(config.RAW_DB_PATH)
    _db_con.execute(BILL_TEXT_CREATE)

    existing_ids = load_existing_bill_ids(args.age)
    print(f"기존 bill_text: {len(existing_ids)}건 → skip 처리")

    counts = {"ok": 0, "skip": 0, "no_pdf": 0, "dl_fail": 0, "ext_fail": 0}
    t0 = datetime.now()
    do_dl = not args.extract_only
    do_ext = not args.download_only

    done = 0
    print_lock = threading.Lock()

    def _work(bill):
        return bill, process_bill(bill, args.age,
                                  download=do_dl, extract=do_ext,
                                  existing_ids=existing_ids)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_work, b): b for b in bills}
            for future in as_completed(futures):
                _, result = future.result()
                with print_lock:
                    counts[result] += 1
                    done += 1
                    if done % 100 == 0 or done == total:
                        elapsed = (datetime.now() - t0).total_seconds()
                        speed = done / elapsed if elapsed > 0 else 0
                        remaining = total - done
                        eta = int(remaining / speed) if speed > 0 else 0
                        eta_str = f"{eta // 60}분 {eta % 60}초" if eta >= 60 else f"{eta}초"
                        print(f"  [{done}/{total}] ok={counts['ok']} skip={counts['skip']} "
                              f"no_pdf={counts['no_pdf']} fail={counts['dl_fail']} | ETA {eta_str}")
    finally:
        _db_con.close()

    elapsed = datetime.now() - t0
    print()
    print("=" * 50)
    print(f"  완료 | 소요: {str(elapsed).split('.')[0]}")
    print(f"  성공: {counts['ok']}  스킵: {counts['skip']}  PDF없음: {counts['no_pdf']}  "
          f"다운로드실패: {counts['dl_fail']}  추출실패: {counts['ext_fail']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
