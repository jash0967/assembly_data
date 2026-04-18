"""Phase 5: create v_kr_bills_analysis view in assembly.duckdb.

Joins v_bill + bill_text + bill_classifications (current version) + bill_ai_filter.
Idempotent (CREATE OR REPLACE).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

import config

VIEW_DDL = """
CREATE OR REPLACE VIEW v_kr_bills_analysis AS
SELECT
    b.bill_id,
    b.age,
    b.bill_name,
    b.proposer,
    b.lead_proposer,
    b.propose_date,
    b.committee,
    b.proc_result,
    t.reason_and_content,
    t.full_text,
    t.pdf_path,
    c.primary_attr,
    c.secondary_attr,
    c.tertiary_attr,
    c.title         AS classified_title,
    c.prompt_version,
    f.classification AS ai_relevance,
    f.is_ai_bill,
    f.gpt_reason,
    f.ai_provisions
FROM v_bill b
LEFT JOIN bill_text t
       ON t.bill_id = b.bill_id
LEFT JOIN v_bill_classifications_current c
       ON c.bill_id = b.bill_id AND c.source LIKE 'kr_%'
LEFT JOIN bill_ai_filter f
       ON f.bill_id = b.bill_id;
"""

con = duckdb.connect(config.DB_PATH)
try:
    con.execute(VIEW_DDL)
    print("v_kr_bills_analysis created")
    n = con.execute(
        "SELECT COUNT(*) FROM v_kr_bills_analysis WHERE primary_attr IS NOT NULL"
    ).fetchone()[0]
    print(f"  classified rows reachable via view: {n}")
    by_age = con.execute(
        "SELECT age, COUNT(*) FROM v_kr_bills_analysis "
        "WHERE primary_attr IS NOT NULL GROUP BY age ORDER BY age"
    ).fetchall()
    for a, c in by_age:
        print(f"  age {a}: {c}")
finally:
    con.close()
