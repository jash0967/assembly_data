"""소스별 청킹 전략.

각 청크는 (chunk_id, text, metadata) 튜플로 반환.
chunk_id = sha256(text)[:16]  - 동일 텍스트는 동일 ID, 증분 sync에 활용.

소스 종류:
  - bill          : raw.bill_text — full_text와 reason_and_content 둘 다 임베딩
  - bill_meta     : raw.v_bill — 제목·발의자·위원회·처리결과 짧은 카드
  - document      : raw.document_text — 회의록·연구단체보고서·정책여론조사
  - speech        : raw.speeches — 발언자별 turn (대부분 단일 청크)
  - member        : raw.v_member — 의원 프로필 카드
"""
import _bootstrap  # noqa: F401

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterator

import config as cfg


def _chunk_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict = field(default_factory=dict)


# ── 일반 char-based 청킹 ──────────────────────────────────

def char_chunks(text: str, chunk_size: int = None,
                overlap: int = None) -> Iterator[str]:
    """문자 기반 청킹. 한국어 문장 경계 가능하면 우선."""
    chunk_size = chunk_size or cfg.CHUNK_SIZE
    overlap = overlap or cfg.CHUNK_OVERLAP
    if not text:
        return
    if len(text) <= cfg.SHORT_DOC_THRESHOLD:
        yield text
        return
    step = chunk_size - overlap
    for start in range(0, len(text), step):
        end = start + chunk_size
        chunk = text[start:end]
        if not chunk.strip():
            continue
        yield chunk
        if end >= len(text):
            break


# ── 회의록 발언자 마커 기반 청킹 ───────────────────────

# ◯위원장 김OO ... ◯의장 이OO ... ◯국회의원 박OO 같은 발언 시작 마커
_SPEAKER_MARKER = re.compile(r"◯[^\n]{0,40}")


def speaker_split(text: str, target_size: int = None) -> Iterator[str]:
    """발언자 마커로 1차 분할 후, 너무 크면 char_chunks로 추가 분할.

    회의록(minutes_*)에 적합. 발언 단위로 보존하려는 시도.
    """
    target_size = target_size or cfg.CHUNK_SIZE
    if not text:
        return

    # 발언자 마커 위치 찾기
    matches = list(_SPEAKER_MARKER.finditer(text))
    if not matches:
        # 마커 없음 → 일반 char_chunks
        yield from char_chunks(text)
        return

    # 마커 위치로 segment 분할
    boundaries = [m.start() for m in matches] + [len(text)]
    segments = [text[boundaries[i]:boundaries[i + 1]]
                for i in range(len(boundaries) - 1)]

    # 너무 작은 segment 병합, 너무 큰 segment 분할
    buf = ""
    for seg in segments:
        if len(buf) + len(seg) <= target_size * 1.5:
            buf += seg
        else:
            if buf:
                yield buf
            if len(seg) > target_size * 2:
                # 단일 발언이 너무 길면 char_chunks
                yield from char_chunks(seg)
                buf = ""
            else:
                buf = seg
    if buf:
        yield buf


# ── 소스별 청크 빌더 ──────────────────────────────────

def chunks_from_bill(bill_id: str, age: int, bill_name: str,
                     reason_and_content: str | None,
                     full_text: str | None,
                     proposer: str | None,
                     committee: str | None,
                     propose_date: str | None) -> list[Chunk]:
    """bill_text의 두 컬럼을 모두 청킹.

    reason_and_content와 full_text는 99% 같지만 둘 다 임베딩 (사용자 요청).
    각각 별도 sub_source 메타로 구분.
    """
    out = []
    base_meta = dict(
        source="bill", bill_id=bill_id, age=int(age) if age else None,
        bill_name=bill_name or "", proposer=proposer or "",
        committee=committee or "", propose_date=str(propose_date or ""),
    )
    for sub_source, body in [("reason", reason_and_content), ("full", full_text)]:
        if not body:
            continue
        for i, chunk_text in enumerate(char_chunks(body)):
            cid = _chunk_id(f"bill:{bill_id}:{sub_source}:{i}:{chunk_text[:80]}")
            meta = {**base_meta, "sub_source": sub_source, "chunk_idx": i}
            out.append(Chunk(cid, chunk_text, meta))
    return out


def chunks_from_bill_meta(bill_id: str, age: int, bill_name: str,
                          proposer: str | None, lead_proposer: str | None,
                          committee: str | None, propose_date: str | None,
                          proc_result: str | None) -> list[Chunk]:
    """법안 메타데이터 카드 (제목 + 발의자 + 위원회 + 결과)."""
    parts = [
        f"법안명: {bill_name or ''}",
        f"대수: {age}대" if age else "",
        f"발의자: {proposer or lead_proposer or ''}",
        f"위원회: {committee or ''}",
        f"제안일: {propose_date or ''}",
        f"처리결과: {proc_result or ''}",
    ]
    text = "\n".join(p for p in parts if p)
    if not text:
        return []
    cid = _chunk_id(f"bill_meta:{bill_id}:{text[:80]}")
    meta = dict(
        source="bill_meta", bill_id=bill_id,
        age=int(age) if age else None,
        bill_name=bill_name or "",
        committee=committee or "",
        propose_date=str(propose_date or ""),
    )
    return [Chunk(cid, text, meta)]


def chunks_from_document(doc_id: str, doc_source: str,
                         full_text: str) -> list[Chunk]:
    """document_text 청킹. minutes_*는 발언자 마커 우선, 그 외는 char."""
    if not full_text:
        return []
    if doc_source.startswith("minutes_"):
        chunker = speaker_split
    else:
        chunker = char_chunks
    out = []
    base_meta = dict(source="document", doc_id=doc_id,
                     doc_source=doc_source)
    for i, ct in enumerate(chunker(full_text)):
        cid = _chunk_id(f"doc:{doc_id}:{doc_source}:{i}:{ct[:80]}")
        meta = {**base_meta, "chunk_idx": i}
        out.append(Chunk(cid, ct, meta))
    return out


def chunks_from_speech(conf_id: str, speech_idx: int, speaker: str,
                       conf_date: str, source: str, dae_num: str,
                       text: str) -> list[Chunk]:
    """speeches 청킹. 대부분 짧아 단일 청크로 처리."""
    if not text:
        return []
    base_meta = dict(
        source="speech", conf_id=conf_id, speech_idx=int(speech_idx),
        speaker=speaker or "", conf_date=str(conf_date or ""),
        speech_source=source or "", dae_num=str(dae_num or ""),
    )
    out = []
    for i, ct in enumerate(char_chunks(text)):
        cid = _chunk_id(f"sp:{conf_id}:{speech_idx}:{i}:{ct[:80]}")
        meta = {**base_meta, "chunk_idx": i}
        out.append(Chunk(cid, ct, meta))
    return out


def chunks_from_member(mona_cd: str, name: str, party: str | None,
                       district: str | None, committee: str | None,
                       reelect: str | None, terms_list: str | None,
                       gender: str | None) -> list[Chunk]:
    """의원 프로필 카드."""
    parts = [
        f"이름: {name or ''}",
        f"정당: {party or ''}",
        f"지역구: {district or ''}",
        f"위원회: {committee or ''}",
        f"당선횟수: {reelect or ''}",
        f"역대대수: {terms_list or ''}",
        f"성별: {gender or ''}",
    ]
    text = "\n".join(p for p in parts if p and not p.endswith(": "))
    if not text:
        return []
    cid = _chunk_id(f"member:{mona_cd}:{text[:80]}")
    meta = dict(
        source="member", mona_cd=mona_cd, name=name or "",
        party=party or "", district=district or "",
    )
    return [Chunk(cid, text, meta)]
