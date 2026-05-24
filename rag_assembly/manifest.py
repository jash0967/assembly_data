"""SQLite 기반 청크·임베딩 추적.

각 청크의 (chunk_id, source, ref_id, embed_config_version, embedded_at)을 기록.
indexer.py가 시작할 때 이미 임베딩된 chunk_id 집합을 먼저 조회 → skip 처리.
sync.py가 변경된 source 행만 골라낼 때 활용.

DB 위치: config.MANIFEST_DB
"""
import _bootstrap  # noqa: F401

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config as cfg


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id          TEXT PRIMARY KEY,
    source            TEXT NOT NULL,        -- bill / bill_meta / document / speech / member
    ref_id            TEXT,                 -- bill_id / doc_id / conf_id+speech_idx / mona_cd
    sub_source        TEXT,                 -- bill: reason/full / document: minutes_committee 등
    embed_config_ver  TEXT NOT NULL,
    text_len          INTEGER,
    embedded_at       TIMESTAMP DEFAULT (CURRENT_TIMESTAMP)
);

CREATE INDEX IF NOT EXISTS ix_chunks_source ON chunks(source);
CREATE INDEX IF NOT EXISTS ix_chunks_ref_id ON chunks(ref_id);
CREATE INDEX IF NOT EXISTS ix_chunks_embed_ver ON chunks(embed_config_ver);

CREATE TABLE IF NOT EXISTS source_state (
    source            TEXT NOT NULL,        -- bill / document / speech / member / bill_meta
    ref_id            TEXT NOT NULL,
    sha               TEXT NOT NULL,        -- content hash for change detection
    last_indexed_at   TIMESTAMP DEFAULT (CURRENT_TIMESTAMP),
    PRIMARY KEY (source, ref_id)
);

CREATE INDEX IF NOT EXISTS ix_source_state_source ON source_state(source);

CREATE TABLE IF NOT EXISTS run_log (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TIMESTAMP DEFAULT (CURRENT_TIMESTAMP),
    finished_at       TIMESTAMP,
    sources           TEXT,
    chunks_added      INTEGER,
    chunks_skipped    INTEGER,
    embed_config_ver  TEXT,
    notes             TEXT
);
"""


class Manifest:
    def __init__(self, path: Path | str = None):
        self.path = Path(path or cfg.MANIFEST_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path))
        self.con.executescript(_SCHEMA)
        self.con.commit()

    def existing_chunk_ids(self, embed_config_ver: str = None) -> set[str]:
        ver = embed_config_ver or cfg.EMBED_CONFIG_VERSION
        rows = self.con.execute(
            "SELECT chunk_id FROM chunks WHERE embed_config_ver = ?",
            [ver],
        ).fetchall()
        return {r[0] for r in rows}

    def register_chunks(self, items: list[dict]) -> None:
        """items: [{chunk_id, source, ref_id, sub_source, text_len}]"""
        if not items:
            return
        ver = cfg.EMBED_CONFIG_VERSION
        rows = [
            (it["chunk_id"], it["source"], it.get("ref_id"),
             it.get("sub_source"), ver, it.get("text_len", 0))
            for it in items
        ]
        self.con.executemany(
            "INSERT OR IGNORE INTO chunks "
            "(chunk_id, source, ref_id, sub_source, embed_config_ver, text_len) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.con.commit()

    def update_source_state(self, source: str, ref_id: str, sha: str) -> None:
        self.con.execute(
            "INSERT INTO source_state (source, ref_id, sha) VALUES (?, ?, ?) "
            "ON CONFLICT(source, ref_id) DO UPDATE SET "
            "sha=excluded.sha, last_indexed_at=CURRENT_TIMESTAMP",
            [source, ref_id, sha],
        )
        self.con.commit()

    def known_source_state(self, source: str) -> dict[str, str]:
        rows = self.con.execute(
            "SELECT ref_id, sha FROM source_state WHERE source = ?",
            [source],
        ).fetchall()
        return {ref_id: sha for ref_id, sha in rows}

    def chunk_counts_by_source(self) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT source, COUNT(*) FROM chunks "
            "WHERE embed_config_ver = ? GROUP BY source",
            [cfg.EMBED_CONFIG_VERSION],
        ).fetchall()
        return {s: n for s, n in rows}

    def open_run(self, sources: list[str]) -> int:
        cur = self.con.execute(
            "INSERT INTO run_log (sources, embed_config_ver) VALUES (?, ?)",
            [",".join(sources), cfg.EMBED_CONFIG_VERSION],
        )
        self.con.commit()
        return cur.lastrowid

    def close_run(self, run_id: int, chunks_added: int,
                  chunks_skipped: int, notes: str = "") -> None:
        self.con.execute(
            "UPDATE run_log SET finished_at=CURRENT_TIMESTAMP, "
            "chunks_added=?, chunks_skipped=?, notes=? WHERE run_id=?",
            [chunks_added, chunks_skipped, notes, run_id],
        )
        self.con.commit()

    def close(self):
        self.con.close()
