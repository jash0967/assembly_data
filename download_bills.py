"""국회 법안 의안원문 PDF 다운로드 및 텍스트 추출.

Usage:
    python download_bills.py --age 22           # 22대 전체
    python download_bills.py --age 21           # 21대 전체
    python download_bills.py --age 22 --limit 10  # 테스트 10건
    python download_bills.py --age 22 --extract-only  # 텍스트 추출만
    python download_bills.py --age 22 --status  # 진행 현황
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb
import fitz
import requests

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BILL_PDF_DIR = ""
BILL_TXT_DIR = ""

FILELIST_URL = "https://likms.assembly.go.kr/bill/bi/dwld/selectFileList.do"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
RETRY = 3
RETRY_DELAY = 2


# ── DB에서 법안 목록 ──────────────────────────────────────

def _cache_path(age: int) -> str:
    return os.path.join(DATA_DIR, f"bill_list_{age}.json")


def get_bill_list(age: int, **_kwargs) -> list[dict]:
    """국회 법안 목록. DB 잠금 시 캐시 파일 사용."""
    cache = _cache_path(age)
    try:
        con = duckdb.connect(config.DB_PATH, read_only=True)
        rows = con.execute(f"""
            SELECT bill_id, bill_no, bill_name, lead_proposer, propose_date, committee
            FROM v_bill
            WHERE age = {age}
            ORDER BY propose_date DESC
        """).fetchall()
        con.close()

        bills = []
        for bill_id, bill_no, name, proposer, date, committee in rows:
            bills.append({
                "bill_id": bill_id,
                "bill_no": bill_no or "",
                "bill_name": name,
                "proposer": proposer or "",
                "propose_date": str(date or ""),
                "committee": committee or "",
            })

        with open(cache, "w", encoding="utf-8") as f:
            json.dump(bills, f, ensure_ascii=False)
        logger.info(f"{age}대 법안 목록 캐시 저장: {len(bills)}건")
        return bills

    except Exception as e:
        if os.path.exists(cache):
            logger.info(f"DB 잠금, 캐시 사용: {e}")
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        raise


# ── PDF URL 조회 ──────────────────────────────────────────

def get_pdf_url(bill_id: str) -> str | None:
    """의안원문 PDF 다운로드 URL을 가져온다."""
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
    """PDF 다운로드."""
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
        except Exception as e:
            if attempt < RETRY - 1:
                time.sleep(RETRY_DELAY)
    return False


# ── 텍스트 추출 ──────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> str:
    """PDF에서 텍스트 추출."""
    doc = fitz.open(pdf_path)
    text = "".join(page.get_text() for page in doc)
    doc.close()
    return text


def parse_bill_sections(text: str) -> dict:
    """제안이유, 주요내용 등 섹션 분리."""
    result = {"full_text": text}

    # 제안이유 추출
    m = re.search(r"제\s*안\s*이\s*유(?:\s*및\s*주\s*요\s*내\s*용)?\s*\n(.*)", text, re.DOTALL)
    if m:
        result["reason_and_content"] = m.group(1).strip()

    return result


# ── 단일 법안 처리 ────────────────────────────────────────

def process_bill(bill: dict, download: bool = True, extract: bool = True) -> str:
    """단일 법안 처리. 반환: 'ok', 'skip', 'no_pdf', 'dl_fail', 'ext_fail'"""
    bill_id = bill["bill_id"]
    safe_id = re.sub(r'[\\/:*?"<>|]', '_', bill_id)
    pdf_path = os.path.join(BILL_PDF_DIR, f"{safe_id}.pdf")
    txt_path = os.path.join(BILL_TXT_DIR, f"{safe_id}.json")

    # 이미 완료된 경우
    if os.path.exists(txt_path) and not download:
        return "skip"
    if os.path.exists(txt_path) and os.path.exists(pdf_path):
        return "skip"

    # 다운로드
    if download and not os.path.exists(pdf_path):
        url = get_pdf_url(bill_id)
        if not url:
            return "no_pdf"
        if not download_pdf(url, pdf_path):
            return "dl_fail"

    # 텍스트 추출
    if extract and not os.path.exists(txt_path) and os.path.exists(pdf_path):
        try:
            text = extract_text_from_pdf(pdf_path)
            sections = parse_bill_sections(text)
            record = {
                "bill_id": bill_id,
                "bill_no": bill["bill_no"],
                "bill_name": bill["bill_name"],
                "proposer": bill["proposer"],
                "propose_date": bill["propose_date"],
                "committee": bill["committee"],
                "text_length": len(text),
                **sections,
            }
            with open(txt_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"추출 실패 {bill_id}: {e}")
            return "ext_fail"

    return "ok"


# ── 메인 ─────────────────────────────────────────────────

def show_status():
    pdf_count = len([f for f in os.listdir(BILL_PDF_DIR) if f.endswith(".pdf")]) if os.path.exists(BILL_PDF_DIR) else 0
    txt_count = len([f for f in os.listdir(BILL_TXT_DIR) if f.endswith(".json")]) if os.path.exists(BILL_TXT_DIR) else 0

    pdf_size = 0
    if os.path.exists(BILL_PDF_DIR):
        for f in os.listdir(BILL_PDF_DIR):
            pdf_size += os.path.getsize(os.path.join(BILL_PDF_DIR, f))

    print("=" * 50)
    print(" 법안 PDF 수집 현황")
    print(f"  PDF 다운로드: {pdf_count}건 ({pdf_size / 1024 / 1024:.1f} MB)")
    print(f"  텍스트 추출:  {txt_count}건")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="국회 법안 PDF 다운로드")
    parser.add_argument("--age", type=int, required=True, help="국회 대수 (예: 21, 22)")
    parser.add_argument("--limit", type=int, default=0, help="테스트용 N건만")
    parser.add_argument("--extract-only", action="store_true", help="텍스트 추출만")
    parser.add_argument("--download-only", action="store_true", help="다운로드만")
    parser.add_argument("--workers", type=int, default=8, help="동시 다운로드 수 (기본: 8)")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    global BILL_PDF_DIR, BILL_TXT_DIR
    BILL_PDF_DIR = os.path.join(DATA_DIR, f"bill_pdf_{args.age}")
    BILL_TXT_DIR = os.path.join(DATA_DIR, f"bill_txt_{args.age}")

    if args.status:
        show_status()
        return

    os.makedirs(BILL_PDF_DIR, exist_ok=True)
    os.makedirs(BILL_TXT_DIR, exist_ok=True)

    print(f"{args.age}대 국회 법안 수집")
    print(f"  PDF: {BILL_PDF_DIR}")
    print(f"  TXT: {BILL_TXT_DIR}")

    bills = get_bill_list(age=args.age)
    if args.limit:
        bills = bills[:args.limit]

    total = len(bills)
    print(f"대상: {total}건")

    counts = {"ok": 0, "skip": 0, "no_pdf": 0, "dl_fail": 0, "ext_fail": 0}
    t0 = datetime.now()

    do_dl = not args.extract_only
    do_ext = not args.download_only
    workers = args.workers

    # ── 병렬 처리 ──
    done = 0
    import threading
    lock = threading.Lock()

    def _work(bill):
        return bill, process_bill(bill, download=do_dl, extract=do_ext)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_work, b): b for b in bills}
        for future in as_completed(futures):
            bill, result = future.result()
            with lock:
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

    elapsed = datetime.now() - t0
    print()
    print("=" * 50)
    print(f"  완료 | 소요: {str(elapsed).split('.')[0]}")
    print(f"  성공: {counts['ok']}  스킵: {counts['skip']}  PDF없음: {counts['no_pdf']}  "
          f"다운로드실패: {counts['dl_fail']}  추출실패: {counts['ext_fail']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
