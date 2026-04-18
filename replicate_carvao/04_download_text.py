"""Step 4: 법안 전문 텍스트 다운로드.

bills_detail.json의 text_versions URL에서 법안 전문을 다운로드하고
HTML 태그를 제거하여 순수 텍스트로 저장.

Usage:
    python replicate_carvao/04_download_text.py
"""
import json
import os
import re
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("CONGRESS_API_KEY", "")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DETAIL_FILE = os.path.join(DATA_DIR, "bills_detail.json")
TEXT_DIR = os.path.join(DATA_DIR, "bills_text")

SESSION = requests.Session()


def strip_html(html: str) -> str:
    """HTML 태그 제거 → 순수 텍스트."""
    # Remove style/script blocks
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Replace common block elements with newlines
    text = re.sub(r"<(?:p|div|br|hr|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&nbsp;", " ").replace("&#x2019;", "'")
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_text_url(detail: dict) -> str:
    """가장 최신 Formatted Text URL 반환."""
    versions = detail.get("text_versions", [])
    if not versions:
        return ""

    # Prefer the latest version
    for tv in versions:
        url = tv.get("url", "")
        if url:
            return url
    return ""


def download_bill_text(url: str) -> str:
    """URL에서 법안 텍스트 다운로드."""
    # Congress.gov text URLs need api_key
    params = {}
    if "api.congress.gov" in url:
        params["api_key"] = API_KEY
        params["format"] = "json"

    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code == 429:
                time.sleep(10)
                continue
            else:
                return ""
        except Exception:
            time.sleep(2)
    return ""


def main():
    os.makedirs(TEXT_DIR, exist_ok=True)

    with open(DETAIL_FILE, encoding="utf-8") as f:
        details = json.load(f)

    print(f"법안: {len(details)}건")

    downloaded = 0
    skipped = 0
    failed = 0

    for i, (bill_id, detail) in enumerate(details.items(), 1):
        safe_name = bill_id.replace(" ", "_").replace(".", "")
        out_path = os.path.join(TEXT_DIR, f"{safe_name}.txt")

        if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
            skipped += 1
            continue

        url = get_text_url(detail)
        if not url:
            print(f"[{i}/{len(details)}] {bill_id}: no text URL")
            failed += 1
            continue

        print(f"[{i}/{len(details)}] {bill_id}...", end=" ", flush=True)

        html = download_bill_text(url)
        time.sleep(0.5)

        if not html:
            print("FAIL")
            failed += 1
            continue

        text = strip_html(html)

        if len(text) < 50:
            print(f"too short ({len(text)} chars)")
            failed += 1
            continue

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)

        downloaded += 1
        print(f"OK ({len(text):,} chars)")

        if downloaded % 20 == 0:
            print(f"  --- {downloaded} downloaded, {skipped} skipped, {failed} failed ---")

    print(f"\n완료: downloaded={downloaded}, skipped={skipped}, failed={failed}")

    # Stats
    total_chars = 0
    file_count = 0
    for fname in os.listdir(TEXT_DIR):
        if fname.endswith(".txt"):
            fpath = os.path.join(TEXT_DIR, fname)
            total_chars += os.path.getsize(fpath)
            file_count += 1
    avg = total_chars // file_count if file_count else 0
    print(f"텍스트 파일: {file_count}개, 총 {total_chars:,} bytes, 평균 {avg:,} bytes")


if __name__ == "__main__":
    main()
