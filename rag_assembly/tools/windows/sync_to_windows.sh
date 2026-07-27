#!/usr/bin/env bash
# sync_to_windows.sh — WSL(정본) → Windows(서빙) 코드·데이터 복사
#
#   사용법:  bash rag_assembly/tools/windows/sync_to_windows.sh [옵션] <대상경로>
#   예:      bash rag_assembly/tools/windows/sync_to_windows.sh /mnt/d/assembly_serving
#            bash rag_assembly/tools/windows/sync_to_windows.sh 'D:\assembly_serving'
#
# 만드는 배치 (SETUP_WINDOWS.md §2 와 동일):
#
#   <대상>/
#     repo/                       코드 (서빙에 필요한 .py 만)
#     data/bills_kr/*.duckdb      ← ASSEMBLY_DATA_DIR
#     data/news/*.duckdb
#     rag_data/                   ← RAG_DATA_DIR  (= rag_assembly/data 내용)
#     hf_cache/hub/models--...    ← HF_HOME       (--model 일 때만)
#     venv/                       (Windows에서 직접 만든다 — 이 스크립트는 안 건드림)
#
# 성질:
#   - 읽기 전용 소스. 저장소·인덱스에 아무것도 쓰지 않는다.
#   - 재개 가능 (rsync --partial-dir). 중간에 끊기면 같은 명령을 다시.
#   - 복사 후 (1) rsync 재대조 --dry-run 으로 전 파일 크기·시각 확인,
#              (2) 표본 sha256 바이트 대조.
#   - 마지막에 .mcp.json 에 넣을 env 블록을 Windows 경로로 출력.
#
# ⚠ Windows 쪽 MCP 서버가 떠 있으면 DuckDB·LanceDB·manifest.sqlite 파일이
#   잠겨 있어 덮어쓰기가 실패한다. 재동기화 전에 Claude Code(또는 서버)를 종료할 것.

set -euo pipefail

# ── 위치 ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

HF_MODEL_DIR_NAME="models--dragonkue--snowflake-arctic-embed-l-v2.0-ko"
HF_HUB_SRC="${HF_HUB_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}/hub}"

# ── 기본값 ───────────────────────────────────────────────────────────────────
DO_CODE=0; DO_DUCKDB=0; DO_RAG=0; DO_MODEL=0; GROUPS_SET=0
DRY_RUN=0
DO_DELETE=0
VERIFY=1
VERIFY_N=10
VERIFY_FULL=0
HASH_CAP=$((2 * 1024 * 1024 * 1024))   # 표본 해시 대상 최대 파일 크기 (2 GiB)
ALLOW_ANY_DEST=0
ASSUME_YES=0
DEST_RAW=""

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

옵션:
  --code            코드만
  --duckdb          DuckDB 4개만
  --rag             RAG 인덱스(rag_assembly/data)만
  --model           HF 임베딩 모델 캐시도 함께 (기본 그룹에는 없음)
  --all             code + duckdb + rag + model
                    (그룹 옵션을 하나도 안 주면 code + duckdb + rag)

  --dry-run         무엇이 복사될지만 출력 (아무것도 쓰지 않음)
  --delete          소스에 없는 파일을 대상에서 지운다.
                    인덱스를 **재빌드**한 뒤에는 사실상 필수다 — LanceDB 조각
                    파일명이 통째로 바뀌므로, 안 지우면 옛 21GB가 대상에 그대로
                    남는다. 지우는 범위는 각 전송의 대상 하위 디렉터리뿐이다.
  --no-verify       복사 후 검증 생략
  --verify-n N      표본 sha256 개수 (기본 10, 전송 단위별)
  --verify-full     모든 파일 sha256 대조 (수십 분~수 시간)
  --verify-big      2 GiB 초과 파일도 표본 대상에 포함 (assembly_raw.duckdb 등)
  --allow-any-dest  /mnt/ 밖 경로 허용 (기본은 Windows 드라이브만)
  -y, --yes         확인 프롬프트 생략
  -h, --help        이 도움말
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --code)   DO_CODE=1;   GROUPS_SET=1 ;;
    --duckdb) DO_DUCKDB=1; GROUPS_SET=1 ;;
    --rag)    DO_RAG=1;    GROUPS_SET=1 ;;
    --model)  DO_MODEL=1;  GROUPS_SET=1 ;;
    --all)    DO_CODE=1; DO_DUCKDB=1; DO_RAG=1; DO_MODEL=1; GROUPS_SET=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --delete)     DO_DELETE=1 ;;
    --no-verify)  VERIFY=0 ;;
    --verify-full) VERIFY_FULL=1 ;;
    --verify-big) HASH_CAP=$((1024 * 1024 * 1024 * 1024)) ;;
    --verify-n)   shift; VERIFY_N="${1:?--verify-n 뒤에 숫자}" ;;
    --allow-any-dest) ALLOW_ANY_DEST=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "알 수 없는 옵션: $1" >&2; echo "--help 참조" >&2; exit 2 ;;
    *)  if [[ -n "$DEST_RAW" ]]; then echo "대상경로가 두 개다: $DEST_RAW / $1" >&2; exit 2; fi
        DEST_RAW="$1" ;;
  esac
  shift
done

if [[ $GROUPS_SET -eq 0 ]]; then DO_CODE=1; DO_DUCKDB=1; DO_RAG=1; fi
[[ -n "$DEST_RAW" ]] || { usage; echo; echo "오류: 대상경로가 없다." >&2; exit 2; }

command -v rsync >/dev/null || { echo "오류: rsync 가 없다 (sudo apt install rsync)" >&2; exit 1; }

# ── 대상 경로 정규화 (D:\... 도 받는다) ──────────────────────────────────────
DEST="$DEST_RAW"
if [[ "$DEST" =~ ^[A-Za-z]:[\\/] ]]; then
  command -v wslpath >/dev/null || { echo "오류: Windows 경로를 주려면 wslpath 가 필요하다" >&2; exit 1; }
  DEST="$(wslpath -u "$DEST_RAW")"
fi
DEST="${DEST%/}"
# 존재하지 않아도 되므로 realpath -m (그러나 부모는 있어야 한다)
DEST="$(realpath -m "$DEST")"

# ── 안전장치 ─────────────────────────────────────────────────────────────────
case "$DEST" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "거부: 대상이 저장소 안이다 ($DEST). 정본 인덱스를 덮어쓸 수 있다." >&2; exit 1 ;;
  "/"|"/home"|"/mnt"|"$HOME")
    echo "거부: 위험한 대상 경로 ($DEST)" >&2; exit 1 ;;
esac
if [[ $ALLOW_ANY_DEST -eq 0 && "$DEST" != /mnt/* ]]; then
  echo "거부: 대상이 Windows 드라이브(/mnt/*)가 아니다: $DEST" >&2
  echo "      의도한 것이라면 --allow-any-dest" >&2; exit 1
fi

# ── 전송 목록 만들기 ─────────────────────────────────────────────────────────
# 각 항목: "라벨|타입(dir|file|filter)|소스|대상|추가 rsync 인자"
TRANSFERS=()
add() { TRANSFERS+=("$1|$2|$3|$4|${5:-}"); }

if [[ $DO_CODE -eq 1 ]]; then
  for f in duckdb_mcp_server.py config.py prompts.py bill_loaders.py; do
    [[ -f "$REPO_ROOT/$f" ]] || { echo "오류: 없는 코드 파일 $REPO_ROOT/$f" >&2; exit 1; }
    add "code:$f" file "$REPO_ROOT/$f" "$DEST/repo/$f"
  done
  # rag_assembly 최상위 .py 만 (data/, tools/*.py, __pycache__ 제외)
  add "code:rag_assembly/*.py" filter "$REPO_ROOT/rag_assembly/" "$DEST/repo/rag_assembly/" \
      "--include=*.py --exclude=*"
  # 이 번들(문서·requirements·템플릿)도 Windows 쪽에 둔다
  add "code:tools/windows" dir "$SCRIPT_DIR/" "$DEST/repo/rag_assembly/tools/windows/" \
      "--exclude=__pycache__/"
fi

if [[ $DO_DUCKDB -eq 1 ]]; then
  add "duckdb:assembly_raw"      file "$REPO_ROOT/data/bills_kr/assembly_raw.duckdb"      "$DEST/data/bills_kr/assembly_raw.duckdb"
  add "duckdb:assembly_analysis" file "$REPO_ROOT/data/bills_kr/assembly_analysis.duckdb" "$DEST/data/bills_kr/assembly_analysis.duckdb"
  add "duckdb:news_raw"          file "$REPO_ROOT/data/news/news.duckdb"                  "$DEST/data/news/news.duckdb"
  add "duckdb:news_analysis"     file "$REPO_ROOT/data/news/news_analysis.duckdb"         "$DEST/data/news/news_analysis.duckdb"
fi

if [[ $DO_RAG -eq 1 ]]; then
  add "rag:index" dir "$REPO_ROOT/rag_assembly/data/" "$DEST/rag_data/"
fi

if [[ $DO_MODEL -eq 1 ]]; then
  src="$HF_HUB_SRC/$HF_MODEL_DIR_NAME"
  [[ -d "$src" ]] || { echo "오류: HF 모델 캐시가 없다: $src" >&2; exit 1; }
  # blobs/ 를 빼고 snapshots/ 의 심볼릭 링크를 실체화(-L)한다.
  #  · NTFS(drvfs)에 심볼릭 링크를 만들려면 개발자 모드/관리자 권한이 필요해 실패하기 쉽다.
  #  · blobs 를 함께 실체화하면 같은 내용이 2벌(≈4.4GB)이 된다.
  #  · huggingface_hub 은 snapshots/<rev>/<파일> 이 실파일이어도 그대로 인식한다
  #    (Windows에서 개발자 모드가 꺼져 있을 때 HF 자신이 만드는 형태와 동일).
  add "model:$HF_MODEL_DIR_NAME" dir "$src/" "$DEST/hf_cache/hub/$HF_MODEL_DIR_NAME/" \
      "-L --exclude=blobs/"
fi

# ── 용량 점검 ────────────────────────────────────────────────────────────────
total_bytes=0
echo "=== 전송 목록 ==="
for t in "${TRANSFERS[@]}"; do
  IFS='|' read -r label kind src dst extra <<<"$t"
  if [[ "$kind" == "file" ]]; then
    b=$(stat -c %s "$src")
  elif [[ "$kind" == "filter" ]]; then
    # 현재 filter 전송은 `--include=*.py --exclude=*` 하나뿐이다.
    # du 로 재면 rag_assembly/data(24GB)까지 세어 버리므로 필터와 같은 조건으로 잰다.
    b=$(find "$src" -maxdepth 1 -type f -name '*.py' -printf '%s\n' 2>/dev/null \
        | awk '{s+=$1} END{print s+0}')
  else
    b=$(du -sbL --exclude=blobs "$src" 2>/dev/null | cut -f1 || true)
  fi
  b=${b:-0}
  total_bytes=$((total_bytes + b))
  printf '  %-28s %10s  %s\n' "$label" "$(numfmt --to=iec --suffix=B "$b")" "$dst"
done
echo "  ─────────────────────────────────────────────"
printf '  %-28s %10s\n' "합계(원본 기준)" "$(numfmt --to=iec --suffix=B "$total_bytes")"

dest_mount_probe="$DEST"
while [[ ! -d "$dest_mount_probe" && "$dest_mount_probe" != "/" ]]; do
  dest_mount_probe="$(dirname "$dest_mount_probe")"
done
avail=$(df --output=avail -B1 "$dest_mount_probe" 2>/dev/null | tail -1 | tr -d ' ' || echo 0)
if [[ -n "$avail" && "$avail" =~ ^[0-9]+$ ]]; then
  echo "  대상 여유 공간: $(numfmt --to=iec --suffix=B "$avail")  ($dest_mount_probe)"
  need=$((total_bytes + total_bytes / 20))    # 5% 여유
  if (( avail < need )); then
    echo "거부: 여유 공간 부족 (필요 ≈ $(numfmt --to=iec --suffix=B "$need"))" >&2
    exit 1
  fi
fi

if [[ $DO_DELETE -eq 1 ]]; then
  echo
  echo "  ⚠ --delete: 위 대상 디렉터리들에서 소스에 없는 파일을 삭제한다."
fi

if [[ $DRY_RUN -eq 0 && $ASSUME_YES -eq 0 ]]; then
  echo
  read -r -p "위 내용을 $DEST 로 복사한다. 계속? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "취소."; exit 0; }
fi

# ── rsync 공통 플래그 ────────────────────────────────────────────────────────
# -a 를 쓰지 않는 이유: drvfs(NTFS)에 POSIX 퍼미션·소유자를 심으려다 실패한다.
# --modify-window=2: 파일시스템 간 타임스탬프 해상도 차이 흡수(재개·재대조 안정).
# --partial-dir: 중간에 끊긴 조각을 별도 디렉터리에 남겨 다음 실행에서 이어받는다
#                (완성본으로 오인되지 않는다).
RS_BASE=(-rlt --no-perms --no-owner --no-group --modify-window=2
         --partial-dir=.rsync-partial)
# --delete 는 각 전송의 대상 하위 디렉터리에만 적용된다 (전송 단위로 호출하므로).
# 이어받기용 조각 디렉터리는 지우지 않는다.
if [[ $DO_DELETE -eq 1 ]]; then
  RS_BASE+=(--delete --exclude=.rsync-partial/)
fi
RS_RUN=("${RS_BASE[@]}" --info=progress2 --human-readable)
RS_CHECK=("${RS_BASE[@]}" --dry-run --itemize-changes)

run_one() {   # $1=kind $2=src $3=dst $4=extra ; 나머지: rsync 플래그
  local kind="$1" src="$2" dst="$3" extra="$4"; shift 4
  local -a xargs_=()
  [[ -n "$extra" ]] && read -r -a xargs_ <<<"$extra"
  # dry-run 에서는 디렉터리조차 만들지 않는다 (정말 아무것도 쓰지 않기 위해).
  if [[ "$kind" == "file" ]]; then
    [[ $DRY_RUN -eq 1 ]] || mkdir -p "$(dirname "$dst")"
  else
    [[ $DRY_RUN -eq 1 ]] || mkdir -p "$dst"
  fi
  rsync "$@" "${xargs_[@]}" "$src" "$dst"
}

# ── 실행 ─────────────────────────────────────────────────────────────────────
if [[ $DRY_RUN -eq 1 ]]; then
  echo; echo "=== DRY-RUN (아무것도 쓰지 않는다) ==="
  for t in "${TRANSFERS[@]}"; do
    IFS='|' read -r label kind src dst extra <<<"$t"
    echo "--- $label"
    # 단일 파일 전송은 대상 디렉터리가 없으면 rsync 가 dry-run 에서도 에러를 낸다.
    # (디렉터리 전송은 "created directory ..." 로 보고만 하고 실제로 만들지 않는다.)
    if [[ "$kind" == "file" && ! -d "$(dirname "$dst")" ]]; then
      echo "    >f+++++++++ $(basename "$dst")   (대상 디렉터리 신규 생성 예정)"
      continue
    fi
    run_one "$kind" "$src" "$dst" "$extra" "${RS_CHECK[@]}" || true
  done
  echo; echo "DRY-RUN 끝."
  exit 0
fi

t_start=$(date +%s)
for t in "${TRANSFERS[@]}"; do
  IFS='|' read -r label kind src dst extra <<<"$t"
  echo; echo "=== 복사: $label ==="
  run_one "$kind" "$src" "$dst" "$extra" "${RS_RUN[@]}"
done
echo; echo "복사 완료 ($(( $(date +%s) - t_start ))초)"

# ── 검증 1: rsync 재대조 (전 파일 크기·mtime) ────────────────────────────────
verify_failed=0
if [[ $VERIFY -eq 1 ]]; then
  echo; echo "=== 검증 1/2: rsync 재대조 (남은 차이가 없어야 한다) ==="
  for t in "${TRANSFERS[@]}"; do
    IFS='|' read -r label kind src dst extra <<<"$t"
    out="$(run_one "$kind" "$src" "$dst" "$extra" "${RS_CHECK[@]}" \
           | grep -E '^[<>ch]' || true)"
    if [[ -n "$out" ]]; then
      echo "  ✗ $label — 아직 차이가 있다:"
      echo "$out" | head -20 | sed 's/^/      /'
      verify_failed=1
    else
      echo "  ✓ $label"
    fi
  done
fi

# ── 검증 2: 표본 sha256 ──────────────────────────────────────────────────────
# 전송의 필터를 그대로 반영해 소스 파일을 나열한다.
#   ⚠ 이걸 kind 와 무관하게 짜면(= 그냥 find) filter 전송에서 복사되지 **않는**
#     파일(rag_assembly/data/** 등)까지 표본에 들어와 검증이 거짓 실패한다.
list_files() {   # $1=root $2=kind $3=크기상한(0=무제한) ; stdout: 상대경로 정렬 목록
  local root="$1" kind="$2" cap="$3"
  if [[ "$kind" == "filter" ]]; then       # --include=*.py --exclude=*
    (cd "$root" && find . -maxdepth 1 -type f -name '*.py' -printf '%P\n' 2>/dev/null | sort)
  elif (( cap > 0 )); then
    (cd "$root" && find . -type f -size -$((cap / 1024))k -printf '%P\n' 2>/dev/null | sort)
  else
    (cd "$root" && find . -type f -printf '%P\n' 2>/dev/null | sort)
  fi
}

sample_files() {   # $1=src_root $2=kind ; stdout: 상대경로 목록
  local root="$1" kind="$2"
  local -a crit=()
  local f p
  if [[ "$kind" != "filter" ]]; then
    for f in manifest.json embed_config.json build_state.json params.index.json; do
      while IFS= read -r p; do crit+=("$p"); done \
        < <(cd "$root" && find . -maxdepth 3 -type f -name "$f" -printf '%P\n' 2>/dev/null | head -2)
    done
  fi
  # 크기 상한 이하만 대상으로, 경로 정렬 후 균등 간격 추출
  local -a all=()
  while IFS= read -r p; do all+=("$p"); done < <(list_files "$root" "$kind" "$HASH_CAP")
  local n=${#all[@]}
  (( n == 0 )) && { printf '%s\n' "${crit[@]}"; return; }
  local want=$VERIFY_N step i
  (( want > n )) && want=$n
  step=$(( n / want )); (( step < 1 )) && step=1
  for (( i=0; i<n && ${#crit[@]}<VERIFY_N+4; i+=step )); do crit+=("${all[$i]}"); done
  printf '%s\n' "${crit[@]}" | awk 'NF' | sort -u
}

hash_pair() {   # $1=src $2=dst $3=label  → 0 일치 / 1 불일치 / 2 건너뜀
  local s="$1" d="$2" lbl="$3" sz
  [[ -f "$s" ]] || return 2
  sz=$(stat -c %s "$s")
  if (( sz > HASH_CAP )) && [[ $VERIFY_FULL -eq 0 ]]; then
    printf '    - %s (%s, 상한 초과 — --verify-big 로 포함)\n' \
           "$lbl" "$(numfmt --to=iec --suffix=B "$sz")"
    return 2
  fi
  if [[ ! -f "$d" ]]; then printf '    ✗ %s — 대상에 파일이 없다\n' "$lbl"; return 1; fi
  local hs hd
  hs=$(sha256sum "$s" | cut -d' ' -f1)
  hd=$(sha256sum "$d" | cut -d' ' -f1)
  if [[ "$hs" == "$hd" ]]; then
    printf '    ✓ %s (%s)\n' "$lbl" "$(numfmt --to=iec --suffix=B "$sz")"; return 0
  fi
  printf '    ✗ %s — sha256 불일치\n        src %s\n        dst %s\n' "$lbl" "$hs" "$hd"; return 1
}

if [[ $VERIFY -eq 1 ]]; then
  echo; echo "=== 검증 2/2: sha256 바이트 대조 ==="
  if [[ $VERIFY_FULL -eq 1 ]]; then
    echo "  (--verify-full: 모든 파일. 오래 걸린다)"
  else
    echo "  (표본 ${VERIFY_N}개/전송, ≤$(numfmt --to=iec --suffix=B "$HASH_CAP") 파일 대상)"
  fi
  for t in "${TRANSFERS[@]}"; do
    IFS='|' read -r label kind src dst extra <<<"$t"
    echo "  $label"
    if [[ "$kind" == "file" ]]; then
      hash_pair "$src" "$dst" "$(basename "$src")" || [[ $? -eq 2 ]] || verify_failed=1
    else
      if [[ $VERIFY_FULL -eq 1 ]]; then
        mapfile -t picks < <(list_files "$src" "$kind" 0)
      else
        mapfile -t picks < <(sample_files "$src" "$kind")
      fi
      for rel in "${picks[@]}"; do
        [[ -n "$rel" ]] || continue
        hash_pair "$src$rel" "$dst$rel" "$rel" || [[ $? -eq 2 ]] || verify_failed=1
      done
    fi
  done
fi

# ── 마무리: .mcp.json env 블록 출력 ──────────────────────────────────────────
win() { command -v wslpath >/dev/null && wslpath -w "$1" 2>/dev/null || echo "$1"; }
jsonpath() { win "$1" | sed 's/\\/\\\\/g'; }

echo
echo "════════════════════════════════════════════════════════════════════"
if [[ $verify_failed -eq 1 ]]; then
  echo "⚠ 검증에서 차이가 나왔다. 위 항목을 확인하고 같은 명령을 다시 실행할 것."
else
  echo "✓ 복사·검증 완료."
fi
echo
echo "Windows .mcp.json 에 넣을 값 (경로만 발췌):"
cat <<EOF
      "command": "$(jsonpath "$DEST")\\\\venv\\\\Scripts\\\\python.exe",
      "args": ["$(jsonpath "$DEST/repo/duckdb_mcp_server.py")"],
      "env": {
        "ASSEMBLY_DATA_DIR": "$(jsonpath "$DEST/data")",
        "RAG_DATA_DIR": "$(jsonpath "$DEST/rag_data")",
        "HF_HOME": "$(jsonpath "$DEST/hf_cache")",
        "PYTHONUTF8": "1"
      }
EOF
echo
echo "다음: $(win "$DEST")\\repo\\rag_assembly\\tools\\windows\\SETUP_WINDOWS.md  §4 부터."
echo "════════════════════════════════════════════════════════════════════"
if [[ $verify_failed -eq 1 ]]; then exit 3; fi
exit 0
