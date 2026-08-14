#!/bin/zsh
# Full pipeline: enrich -> bundle -> index.
#
# Every stage resumes, so this script is safe to re-run after an interrupt,
# a crash, or a reboot. Nothing already finished is redone.
#
# Run it by hand:   ./scripts/run-pipeline.sh
# Or let the LaunchAgent fire it at 18:00.

set -u

REPO="${0:A:h:h}"
KB="$REPO/.venv/bin/kb"
LOG_DIR="$REPO/data/logs"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG="$LOG_DIR/pipeline_$STAMP.log"
SENTINEL="$REPO/data/.pipeline-complete"

mkdir -p "$LOG_DIR"

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG" }

# The LaunchAgent fires daily. This makes the run one-shot: once the chain has
# completed, later firings exit immediately instead of re-running.
if [[ -f "$SENTINEL" ]]; then
  log "sentinel present, nothing to do. Delete $SENTINEL to run again."
  exit 0
fi

log "=== pipeline start ==="
log "repo:  $REPO"
log "model: $(grep '^model = ' "$REPO/config.toml" | head -1)"

# Keep the Mac awake for the duration. Idle sleep would stall an 11-hour run.
# This does NOT defeat lid-close sleep: the lid must stay open, or the machine
# must be on the power adapter with clamshell display attached.
caffeinate -dimsu -w $$ &
CAFFEINATE_PID=$!
log "caffeinate holding wake (pid $CAFFEINATE_PID)"

run_stage() {
  local name="$1"; shift
  log "--- $name: starting"
  local t0=$SECONDS
  "$@" >> "$LOG" 2>&1
  local rc=$?
  local elapsed=$((SECONDS - t0))
  if [[ $rc -ne 0 ]]; then
    log "--- $name: FAILED (exit $rc) after ${elapsed}s"
    return $rc
  fi
  log "--- $name: done in ${elapsed}s"
  return 0
}

# Stage 1: the long one. Local LLM writes a catalogue record per document.
run_stage "enrich" "$KB" enrich
if [[ $? -ne 0 ]]; then
  log "=== pipeline aborted at enrich ==="
  kill $CAFFEINATE_PID 2>/dev/null
  exit 1
fi

# Stage 2: render the OKF bundle and validate it.
run_stage "bundle" "$KB" bundle
if [[ $? -ne 0 ]]; then
  log "=== pipeline aborted at bundle ==="
  kill $CAFFEINATE_PID 2>/dev/null
  exit 1
fi

# Stage 3: chunk, embed, and build both indexes.
run_stage "index" "$KB" index
if [[ $? -ne 0 ]]; then
  log "=== pipeline aborted at index ==="
  kill $CAFFEINATE_PID 2>/dev/null
  exit 1
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$SENTINEL"
log "=== pipeline complete ==="
kill $CAFFEINATE_PID 2>/dev/null
exit 0
