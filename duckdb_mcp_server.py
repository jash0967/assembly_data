"""국회 데이터 DuckDB MCP 서버.

Claude Code에서 자연어로 국회 데이터를 질의할 수 있도록
DuckDB 접근 도구를 제공하는 MCP 서버.
"""
import json
import os

import duckdb
from mcp.server.fastmcp import FastMCP

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "assembly.duckdb")

mcp = FastMCP("assembly-db")


@mcp.tool()
def list_tables() -> str:
    """DB의 모든 테이블과 뷰 목록을 반환합니다."""
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute("""
        SELECT table_name, table_type
        FROM information_schema.tables
        ORDER BY table_type, table_name
    """).fetchall()
    con.close()
    result = []
    for name, ttype in rows:
        result.append(f"{'[VIEW]' if ttype == 'VIEW' else '[TABLE]'} {name}")
    return "\n".join(result)


@mcp.tool()
def describe_table(table_name: str) -> str:
    """특정 테이블/뷰의 컬럼 정보와 샘플 데이터를 반환합니다.

    Args:
        table_name: 테이블 또는 뷰 이름 (예: v_bill, v_member, speeches)
    """
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        # 컬럼 정보
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table_name],
        ).fetchall()
        if not cols:
            return f"테이블 '{table_name}'을 찾을 수 없습니다."

        # 행 수
        count = con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]

        # 샘플 3행
        sample = con.execute(f'SELECT * FROM "{table_name}" LIMIT 3').fetchall()
        col_names = [c[0] for c in cols]

        result = f"=== {table_name} ({count:,}건) ===\n\n"
        result += "컬럼:\n"
        for cname, ctype in cols:
            result += f"  {cname} ({ctype})\n"
        result += f"\n샘플 ({min(3, count)}건):\n"
        for row in sample:
            result += "  " + json.dumps(
                dict(zip(col_names, [str(v)[:100] if v else None for v in row])),
                ensure_ascii=False,
            ) + "\n"
    finally:
        con.close()
    return result


@mcp.tool()
def query(sql: str) -> str:
    """DuckDB SQL을 실행하고 결과를 반환합니다. SELECT만 허용.

    Args:
        sql: 실행할 SQL 쿼리 (SELECT만 가능)
    """
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT") and not sql_stripped.startswith("WITH"):
        return "오류: SELECT/WITH 문만 실행 가능합니다."

    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        result = con.execute(sql).fetchall()
        columns = [desc[0] for desc in con.description]
        rows = [dict(zip(columns, row)) for row in result]

        if not rows:
            return "결과 없음 (0건)"

        output = f"결과: {len(rows)}건\n\n"
        output += json.dumps(rows[:50], ensure_ascii=False, default=str, indent=2)
        if len(rows) > 50:
            output += f"\n\n... 외 {len(rows) - 50}건 생략"
        return output
    except Exception as e:
        return f"SQL 오류: {e}"
    finally:
        con.close()


@mcp.tool()
def get_overview() -> str:
    """국회 데이터베이스 개요를 반환합니다. 어떤 데이터가 있는지 파악할 때 사용하세요."""
    return """13~22대 국회 Open API 데이터 (DuckDB)

=== 주요 분석용 뷰 ===
v_member: 현재(22대) 국회의원 인적사항 (590명). 조인키: mona_cd
v_bill: 발의법률안 (113,698건). age=22로 필터. 조인키: lead_mona_cd → mona_cd
v_vote: 의원별 본회의 표결 (403,077건). 찬성/반대/기권
v_vote_summary: 의안별 표결 집계 (9,468건)
v_bill_detail: 의안 상세정보 (4,936건)
v_plenary_conf: 본회의 회의록 메타 (26,330건)
v_committee_conf: 위원회 회의록 메타 (41,306건)
v_plenary_bill: 본회의 처리안건 (74,636건)

=== 원본 회의·발언 ===
speeches: 회의록 발언 전문 (84,137건, 2000-2026)
speech_issues: 27개 카테고리 키워드 태깅 (96,622건)

=== 법안 원본 테이블 ===
billinfodetail: 의안 상세정보 원본 (107,300건). 대수 구분 없이 통합
nzmimeepazxkubdpn: 발의법률안 raw 테이블 (v_bill의 소스)

=== AI 정책 분석 ===
(DuckDB에는 분류 결과 없음. 정본 분류는 data/bills_classified_kr_{age}.json이며
 bill_loaders.load_kr_bills()로 로드. bill 원문은 data/bill_txt_{age}/*.json 파일.)

=== 참고 ===
- v_member에는 age 컬럼 없음 (22대 의원만 있음)
- gender 값: '남', '여'
- reelect 값: '초선', '재선', '3선', '4선', '5선', '6선'
- speaker 값에 '의원', '위원' 접미사 포함될 수 있음 → LIKE '%이름%' 사용
"""


if __name__ == "__main__":
    mcp.run()
