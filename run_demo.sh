#!/usr/bin/env bash
# Web Agent demo runner.
#
# Default: kill any existing gateway, start fresh, launch UI on :8090.
# Ctrl+C stops both UI and gateway.
#
# Usage:
#   ./run_demo.sh              gateway + UI (one command)
#   ./run_demo.sh ui           same as default
#   ./run_demo.sh tests        only pytest
#   ./run_demo.sh hello        run single query (restarts gateway first)
#   ./run_demo.sh comparison   5-tool AI coding pricing comparison
#   ./run_demo.sh wipe         clear state/sessions + logs
#   ./run_demo.sh all          pytest + canonical queries

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CODE_DIR="$SCRIPT_DIR/code"
GATEWAY_DIR="$SCRIPT_DIR/llm_gatewayV9"
LOG_DIR="$SCRIPT_DIR/logs"
GATEWAY_PID=""

mkdir -p "$LOG_DIR"

usage() {
  sed -n '2,16p' "$0"
}

query_for() {
  case "$1" in
    hello)        echo "hello" ;;
    shannon)      echo "When was Claude Shannon born and when did he die? Name three of his contributions to information theory." ;;
    populations)  echo "Find the populations of London, Paris, Berlin and tell me which two are closest in size." ;;
    structured)   echo "Compare the populations of Mumbai, Cairo, and Lagos and identify which is growing fastest. Return structured fields per city." ;;
    fail)         echo "Summarise the contents of /nonexistent/path.txt for me." ;;
    browser)      echo "What are the top 3 most-liked open-source LLM releases on Hugging Face from the past week? For each give model name, parameter count, and one-line description." ;;
    comparison)   echo "Compare GitHub Copilot, Cursor, Claude Code, Windsurf, and Tabnine as AI coding tools. For each, open its pricing page, switch the billing toggle if present, and report the free plan, the cheapest paid plan with its price, and three headline features. Give me a single comparison table." ;;
    *) return 1 ;;
  esac
}

describe() {
  case "$1" in
    hello)        echo "webagent query: hello — planner -> formatter" ;;
    shannon)      echo "webagent query: shannon — planner -> researcher -> formatter" ;;
    populations)  echo "webagent query: populations — parallel researchers" ;;
    structured)   echo "webagent query: structured — distiller + critic" ;;
    fail)         echo "webagent query: fail — graceful planning failure" ;;
    browser)      echo "webagent query: browser — browser cascade" ;;
    comparison)   echo "webagent query: comparison — browser x5 + comparator" ;;
  esac
}

stop_gateway() {
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -ti :8109 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      kill -9 $pids 2>/dev/null || true
    fi
  fi
  if [[ -n "$GATEWAY_PID" ]]; then
    kill -9 "$GATEWAY_PID" 2>/dev/null || true
    GATEWAY_PID=""
  fi
  sleep 0.4
}

gateway_online() {
  curl -sf http://localhost:8109/v1/routers >/dev/null 2>&1
}

start_gateway_fresh() {
  stop_gateway
  echo "[webagent] Starting gateway on http://localhost:8109 ..."
  ( cd "$GATEWAY_DIR" && uv run main.py >> "$LOG_DIR/gateway.log" 2>&1 ) &
  GATEWAY_PID=$!
  for _ in $(seq 1 45); do
    if gateway_online; then
      echo "[webagent] Gateway ready (pid $GATEWAY_PID)"
      return 0
    fi
    sleep 1
  done
  echo "[webagent] Gateway failed to start; see $LOG_DIR/gateway.log" >&2
  exit 1
}

sync_code() {
  ( cd "$CODE_DIR" && uv sync --quiet )
}

start_webagent_stack() {
  sync_code
  start_gateway_fresh
  trap 'echo "[webagent] Stopping gateway..."; stop_gateway' INT TERM EXIT
  echo "[webagent] UI      -> http://127.0.0.1:8090"
  echo "[webagent] Gateway -> http://localhost:8109/"
  echo "[webagent] Press Ctrl+C to stop UI and gateway."
  ( cd "$CODE_DIR" && uv run python -m ui.server )
}

run_pytest() {
  echo
  echo "===================================================================="
  echo "  Unit tests"
  echo "===================================================================="
  ( cd "$CODE_DIR" && uv run --quiet pytest tests/ -v --no-header )
}

run_one() {
  local id="$1"
  local q log sid
  q=$(query_for "$id") || { echo "[webagent] unknown query: $id" >&2; usage; exit 2; }

  echo
  echo "===================================================================="
  describe "$id"
  echo "===================================================================="
  log="$LOG_DIR/$id.log"
  ( cd "$CODE_DIR" && uv run python flow.py "$q" 2>&1 ) | tee "$log"

  sid=$(ls -t "$CODE_DIR/state/sessions" 2>/dev/null | head -1)
  if [[ -n "$sid" ]]; then
    ( cd "$CODE_DIR" && uv run python comparison_report.py "$sid" ) || true
    echo
    echo "[webagent] log     -> $log"
    echo "[webagent] session -> $CODE_DIR/state/sessions/$sid/"
    echo "[webagent] report  -> $CODE_DIR/state/sessions/$sid/REPORT.html"
  fi
}

case "${1:-ui}" in
  -h|--help|help) usage; exit 0 ;;
  ui|"")
    start_webagent_stack
    ;;
  tests)          sync_code; run_pytest ;;
  wipe)
    stop_gateway
    rm -rf \
      "$CODE_DIR/state/sessions" \
      "$CODE_DIR/state/artifacts" \
      "$CODE_DIR/state/index.faiss" \
      "$CODE_DIR/state/index_ids.json" \
      "$CODE_DIR/state/memory.json" \
      "$LOG_DIR"
    mkdir -p "$LOG_DIR"
    echo "[webagent] cleared sessions, artifacts, FAISS index, memory.json, logs/"
    ;;
  hello|shannon|populations|structured|fail|browser|comparison)
    sync_code
    start_gateway_fresh
    trap 'stop_gateway' EXIT
    run_one "$1"
    stop_gateway
    ;;
  all)
    sync_code
    start_gateway_fresh
    trap 'stop_gateway' EXIT
    run_pytest
    for id in hello shannon populations structured fail; do
      run_one "$id"
    done
    stop_gateway
    echo
    echo "[webagent] Done. Also try: ./run_demo.sh browser | comparison | ui"
    ;;
  *)
    echo "[webagent] unknown command: $1" >&2
    usage
    exit 2
    ;;
esac
