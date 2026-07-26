"""stdin으로 query 받고 stdout으로 JSON vector 반환 (질의 임베딩 격리 실행).

2026-07-27: Vertex AI raw HTTP → 로컬 arctic-ko 모델 호출로 교체.

**기본 경로가 아니다.** duckdb_mcp_server.py 는 이제 in-process
`embedder.embed_query()` 를 쓴다 — 옛 subprocess 우회의 원인이던 hang은
google-genai 의 네트워크 호출이었고, 로컬 모델에는 그 원인이 없다.
이 스크립트가 남아 있는 이유:

  1. `MCP_EMBED_SUBPROC=1` 로 강제할 수 있는 격리 fallback
     (in-process 로드가 특정 환경에서 문제를 일으킬 때의 탈출구)
  2. 다른 프로세스·언어에서 질의 벡터를 얻는 진입점

주의:
  - 호출마다 568M 모델을 새로 로드하므로 질의당 수 초가 붙는다. 상시 사용 금지.
  - "query: " prefix는 여기서 붙이지 않는다. embedder.embed_query() 안에서만
    붙는다 (주입 지점 단일화).
  - 모델 로드가 stdout에 뭔가 뱉어도 JSON이 오염되지 않도록, 로드·인코딩
    구간 동안 fd 1을 stderr로 돌린 뒤 마지막에 복구하고 JSON만 쓴다.
    (전용 subprocess라 프로세스 전역 fd 조작이 안전하다 — MCP 서버 본체에서
     같은 짓을 하면 FastMCP 응답과 경합한다.)
"""
import json
import os
import sys

import _bootstrap  # noqa: F401


def main() -> None:
    query = sys.stdin.read().strip()

    saved_fd = os.dup(1)
    os.dup2(2, 1)          # stdout → stderr (로드 노이즈 격리)
    try:
        import embedder
        vec = embedder.embed_query(query)
    finally:
        sys.stdout.flush()
        os.dup2(saved_fd, 1)
        os.close(saved_fd)

    sys.stdout.write(json.dumps({"vector": vec}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
