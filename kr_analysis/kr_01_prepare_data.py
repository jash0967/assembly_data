"""한국 국회 Step 1: AI 법안 필터링 + 메타데이터 구축.

1차 필터: 제안이유/주요내용에 인공지능/AI 3회 이상 언급
2차 필터: GPT-4.1로 AI 관련성 판별
중복 제거: 동일 의원 + 동일 제목 → 최신 1건만

Usage:
    python replicate_carvao/kr_01_prepare_data.py              # 22대 (기본)
    python replicate_carvao/kr_01_prepare_data.py --age 21     # 21대
"""
import argparse
import json
import os
import re
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── CLI 인자 파싱 (모듈 레벨) ──
_parser = argparse.ArgumentParser()
_parser.add_argument("--age", type=int, default=22, help="국회 대수 (기본: 22)")
_args = _parser.parse_args()
AGE = _args.age

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEXT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", f"bill_txt_{AGE}")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "assembly.duckdb")
OUTPUT_CANDIDATES = os.path.join(DATA_DIR, f"kr{AGE}_ai_candidates.json")
OUTPUT_FINAL = os.path.join(DATA_DIR, f"kr{AGE}_ai_bills.json")

client = OpenAI()

GPT_SYSTEM = """You are an expert Korean legislative analyst.
You will be given the title and proposal reason (제안이유) of a Korean National Assembly bill.
Classify this bill's relationship to AI (artificial intelligence).

Classification:
1. "core" — AI가 법안의 주된 목적 (AI 기본법, AI 산업육성법, AI 책임법 등)
2. "adjacent" — AI가 법안의 핵심 trigger이거나, AI 관련 실질적 조항을 포함
   - AI 기술이 야기한 문제를 해결하기 위한 법안 (딥페이크 규제, AI 허위광고 등)
   - AI 인프라/생태계 조성을 위한 법안 (데이터센터 전력, AI 학습데이터 개방 등)
   - AI 활용에 대한 구체적 규정을 신설하는 법안 (AI 채용시스템 투명성 등)
3. "unrelated" — AI가 배경으로만 언급되거나, AI를 제거해도 법안의 본질이 변하지 않음
   - "AI 시대에...", "인공지능 등 첨단기술의 발전으로..." 식의 배경 언급
   - 지역발전/행정구역 법안에서 AI를 미래 비전으로 나열
   - 세법/에너지법 등에서 AI를 여러 기술 중 하나로 언급

핵심 판단 기준: "이 법안에서 AI 관련 내용을 삭제하면 법안의 존재 이유가 사라지는가?"
- 사라진다 → core 또는 adjacent
- 사라지지 않는다 → unrelated

Respond ONLY with valid JSON:
{"classification": "core|adjacent|unrelated", "reason": "한국어 한 문장 설명", "ai_provisions": "adjacent일 경우 AI 관련 구체 조항 요약 (core/unrelated는 빈 문자열)"}"""


def step1_keyword_filter():
    """1차: 텍스트에서 AI 3회+ 언급 법안 추출."""
    print("=== Step 1: 키워드 필터 (AI/인공지능 3회+) ===")

    candidates = []
    for fname in os.listdir(TEXT_DIR):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(TEXT_DIR, fname), encoding="utf-8") as f:
            bill = json.load(f)

        reason = bill.get("reason_and_content", "") or bill.get("full_text", "") or ""
        text = bill.get("bill_name", "") + " " + reason
        cnt = len(re.findall(r"인공지능|AI|A\.I", text))

        if cnt >= 3:
            candidates.append({
                "bill_id": bill.get("bill_id", fname.replace(".json", "")),
                "bill_name": bill.get("bill_name", ""),
                "proposer": bill.get("proposer", ""),
                "propose_date": bill.get("propose_date", ""),
                "ai_mention_count": cnt,
                "reason_snippet": reason[:500],
            })

    candidates.sort(key=lambda x: x["propose_date"])
    print(f"  1차 후보: {len(candidates)}건")

    with open(OUTPUT_CANDIDATES, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2, ensure_ascii=False)
    print(f"  저장: {OUTPUT_CANDIDATES}")

    return candidates


def step2_dedup(candidates):
    """중복 제거: 동일 의원 + 동일 법안명 → 최신 1건만."""
    print(f"\n=== Step 2: 중복 제거 ===")

    # Group by (bill_name, proposer)
    groups = {}
    for c in candidates:
        # proposer에서 대표발의자만 추출
        lead = c["proposer"].split(",")[0].strip() if c["proposer"] else ""
        key = (c["bill_name"], lead)
        if key not in groups:
            groups[key] = c
        else:
            # 더 최신 것으로 교체
            if c["propose_date"] > groups[key]["propose_date"]:
                groups[key] = c

    deduped = sorted(groups.values(), key=lambda x: x["propose_date"])
    removed = len(candidates) - len(deduped)
    print(f"  {len(candidates)}건 → {len(deduped)}건 (제거: {removed}건)")

    return deduped


def step3_gpt_filter(candidates):
    """2차: GPT로 AI 관련성 판별."""
    print(f"\n=== Step 3: GPT AI 관련성 판별 ({len(candidates)}건) ===")

    # Load existing
    if os.path.exists(OUTPUT_FINAL):
        with open(OUTPUT_FINAL, encoding="utf-8") as f:
            existing = {b["bill_id"]: b for b in json.load(f)}
        print(f"  기존 판별: {len(existing)}건")
    else:
        existing = {}

    results = []
    new_classified = 0

    for i, c in enumerate(candidates, 1):
        bid = c["bill_id"]

        # 이미 판별된 건 재사용
        if bid in existing:
            results.append(existing[bid])
            continue

        print(f"  [{i}/{len(candidates)}] {c['bill_name'][:40]}...", end=" ", flush=True)

        user_msg = f"법안 제목: {c['bill_name']}\n\n제안이유 및 주요내용:\n{c['reason_snippet']}"

        try:
            response = client.chat.completions.create(
                model="gpt-4.1",
                messages=[
                    {"role": "system", "content": GPT_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            classification = result.get("classification", "unrelated")
            c["classification"] = classification
            c["is_ai_bill"] = classification in ("core", "adjacent")
            c["gpt_reason"] = result.get("reason", "")
            c["ai_provisions"] = result.get("ai_provisions", "")
            new_classified += 1

            label_map = {"core": "CORE", "adjacent": "ADJ", "unrelated": "no"}
            print(f"{label_map.get(classification, '?')}")

        except Exception as e:
            print(f"ERROR: {e}")
            c["is_ai_bill"] = None
            c["classification"] = None
            c["gpt_reason"] = str(e)

        results.append(c)
        time.sleep(0.3)

        # Save every 30
        if new_classified % 30 == 0 and new_classified > 0:
            with open(OUTPUT_FINAL, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)

    return results


def step4_enrich(ai_bills):
    """DB에서 메타데이터 보강: 정당, 위원회, 처리결과."""
    print(f"\n=== Step 4: DB 메타데이터 보강 (제{AGE}대) ===")

    con = duckdb.connect(DB_PATH, read_only=True)

    # v_bill은 22대 전용 뷰이므로, 원본 테이블(nzmimeepazxkubdpn)도 폴백으로 사용
    def query_bill(bid):
        # 먼저 v_bill 시도
        row = con.execute("""
            SELECT bill_name, lead_proposer, lead_mona_cd, co_proposers,
                   propose_date, committee, proc_result
            FROM v_bill WHERE bill_id = ?
        """, [bid]).fetchone()
        if row:
            return row
        # 폴백: 원본 발의법률안 테이블
        row = con.execute("""
            SELECT BILL_NAME, RST_PROPOSER, RST_MONA_CD, MEMBER_LIST,
                   PROPOSE_DT, COMMITTEE, PROC_RESULT
            FROM nzmimeepazxkubdpn WHERE BILL_ID = ? LIMIT 1
        """, [bid]).fetchone()
        return row

    for bill in ai_bills:
        bid = bill["bill_id"]
        row = query_bill(bid)

        if row:
            bill["bill_name"] = row[0] or bill["bill_name"]
            bill["lead_proposer"] = row[1] or ""
            bill["lead_mona_cd"] = row[2] or ""
            bill["co_proposers"] = row[3] or ""
            bill["propose_date"] = str(row[4] or "")
            bill["committee"] = row[5] or ""
            bill["proc_result"] = row[6] or ""

            # 정당 정보: v_member → 인적사항 → 표결정보 순으로 폴백
            mona = row[2]
            party = ""
            if mona:
                # 1) v_member (22대 뷰)
                party_row = con.execute("""
                    SELECT party FROM v_member WHERE mona_cd = ? LIMIT 1
                """, [mona]).fetchone()
                if party_row:
                    party = party_row[0] or ""
                if not party:
                    # 2) 인적사항 테이블 (현재 의원)
                    party_row = con.execute("""
                        SELECT POLY_NM FROM nwvrqwxyaytdsfvhu
                        WHERE MONA_CD = ? LIMIT 1
                    """, [mona]).fetchone()
                    if party_row:
                        party = party_row[0] or ""
                if not party:
                    # 3) 표결정보 테이블 (역대 의원 정당 확인)
                    party_row = con.execute("""
                        SELECT POLY_NM FROM nojepdqqaweusdfbi
                        WHERE MONA_CD = ? AND AGE = ? LIMIT 1
                    """, [mona, str(AGE)]).fetchone()
                    if party_row:
                        party = party_row[0] or ""
            bill["party"] = party

            # 공동발의자 수
            co = row[3] or ""
            bill["cosponsor_count"] = len([x for x in co.split(",") if x.strip()]) if co else 0

    con.close()
    print(f"  보강 완료: {len(ai_bills)}건")
    return ai_bills


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Step 1: 키워드 필터
    candidates = step1_keyword_filter()

    # Step 2: 중복 제거
    deduped = step2_dedup(candidates)

    # Step 3: GPT 필터
    all_results = step3_gpt_filter(deduped)

    # 3단계 분류 집계
    core = [b for b in all_results if b.get("classification") == "core"]
    adjacent = [b for b in all_results if b.get("classification") == "adjacent"]
    unrelated = [b for b in all_results if b.get("classification") == "unrelated"]
    ai_bills = [b for b in all_results if b.get("is_ai_bill") is True]  # core + adjacent
    non_ai = [b for b in all_results if b.get("is_ai_bill") is False]

    print(f"\n=== GPT 판별 결과 (3단계) ===")
    print(f"  core (AI 주목적):     {len(core)}건")
    print(f"  adjacent (AI 관련):   {len(adjacent)}건")
    print(f"  unrelated (비-AI):    {len(unrelated)}건")
    print(f"  ─────────────────────")
    print(f"  AI 법안 합계:         {len(ai_bills)}건 (core + adjacent)")
    print(f"  미판별:               {len(all_results) - len(ai_bills) - len(non_ai)}건")

    # Step 4: 메타데이터 보강
    ai_bills = step4_enrich(ai_bills)

    # 최종 저장
    with open(OUTPUT_FINAL, "w", encoding="utf-8") as f:
        json.dump(ai_bills, f, indent=2, ensure_ascii=False)

    print(f"\n최종: {len(ai_bills)}건 → {OUTPUT_FINAL}")

    # 통계
    from collections import Counter
    party_counts = Counter(b.get("party", "") for b in ai_bills)
    committee_counts = Counter(b.get("committee", "") for b in ai_bills)

    print(f"\n정당별:")
    for p, c in party_counts.most_common():
        print(f"  {p or '미상'}: {c}")

    print(f"\n위원회별 (상위 10):")
    for cm, c in committee_counts.most_common(10):
        print(f"  {cm or '미상'}: {c}")

    # adjacent 예시
    print(f"\nadjacent 법안 예시:")
    for b in adjacent[:10]:
        prov = b.get('ai_provisions', '')[:50]
        print(f"  {b['bill_name'][:45]} — {prov}")

    # 비-AI 예시
    print(f"\nunrelated 법안 예시:")
    for b in non_ai[:10]:
        print(f"  {b['bill_name'][:50]} — {b.get('gpt_reason','')[:50]}")


if __name__ == "__main__":
    main()
