#!/bin/zsh
# Duty-cycled enrichment: 2 hours off, 2 hours on, repeating until the queue
# is empty, then bundle, index and graph.
#
# Starts with an OFF window so the machine gets a rest immediately.
#
# Everything resumes. Killing this script at any point loses at most the one
# document in flight, and rerunning picks up exactly where it stopped.
#
#   ./scripts/run-duty-cycle.sh          run in the foreground
#   ./scripts/run-duty-cycle.sh --now    skip the first off window
#
# Detached (survives the terminal closing):
#   nohup ./scripts/run-duty-cycle.sh > /dev/null 2>&1 & disown

set -u

REPO="${0:A:h:h}"
KB="$REPO/.venv/bin/kb"
LOG_DIR="$REPO/data/logs"
LOG="$LOG_DIR/duty-cycle.log"
LOCK="$REPO/data/.duty-cycle.pid"
SENTINEL="$REPO/data/.pipeline-complete"

OFF_SECONDS=7200      # 2 hours resting
ON_SECONDS=7200       # 2 hours working
CHARGE_FLOOR=30       # refuse to start an on window below this, on AC or battery

mkdir -p "$LOG_DIR"
log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S')  $*" | tee -a "$LOG" }

# One owner only. A second copy would double-enrich and fight for the GPU.
if [[ -f "$LOCK" ]] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
  log "another duty cycle is already running (pid $(cat "$LOCK")). Exiting."
  exit 1
fi
print -r -- $$ > "$LOCK"
cleanup() { rm -f "$LOCK"; pkill -P $$ caffeinate 2>/dev/null; }
trap cleanup EXIT INT TERM

remaining() {
  MANIFEST_DB="$REPO/data/manifest.db" "$REPO/.venv/bin/python" - <<'PY'
import os
import sqlite3
c = sqlite3.connect("file:" + os.environ["MANIFEST_DB"] + "?mode=ro", uri=True)
total = c.execute("""SELECT COUNT(*) FROM (SELECT DISTINCT hash FROM files
    WHERE scan_status='included' AND extract_status='ok')""").fetchone()[0]
done = c.execute("SELECT COUNT(*) FROM concepts WHERE enrich_status='ok'").fetchone()[0]
print(max(0, total - done))
PY
}

power_state() { pmset -g batt | head -1 | grep -q "AC Power" && print -r -- "AC" || print -r -- "BATT" }
charge_pct()  { pmset -g batt | grep -Eo '[0-9]+%;' | head -1 | tr -d '%;' }

log "=== duty cycle starting (off ${OFF_SECONDS}s / on ${ON_SECONDS}s, charge floor ${CHARGE_FLOOR}%) ==="
log "remaining: $(remaining) documents"

if [[ "${1:-}" == "--now" ]]; then
  log "--now given, skipping the first off window"
  SKIP_FIRST_OFF=1
else
  SKIP_FIRST_OFF=0
fi

cycle=0
while true; do
  left=$(remaining)
  if [[ "$left" -eq 0 ]]; then
    log "queue empty after $cycle cycles"
    break
  fi

  if [[ $cycle -gt 0 || $SKIP_FIRST_OFF -eq 0 ]]; then
    log "--- OFF for ${OFF_SECONDS}s. $left documents remaining. Power: $(power_state) $(charge_pct)%"
    sleep "$OFF_SECONDS"
  fi

  # The floor applies on AC as well as on battery. Under sustained GPU load
  # this machine drained 61% to 4% in about 33 minutes on 2026-08-08, so a
  # low battery is not safe to work from even while charging: the adapter
  # barely keeps ahead of the draw.
  pct=$(charge_pct)
  if [[ -z "$pct" ]]; then pct=100; fi
  if [[ "$pct" -lt "$CHARGE_FLOOR" ]]; then
    log "charge at ${pct}% ($(power_state)), below the ${CHARGE_FLOOR}% floor. Resting another ${OFF_SECONDS}s."
    ((cycle++))
    continue
  fi

  ((cycle++))
  log "--- ON  cycle $cycle for ${ON_SECONDS}s. Power: $(power_state) $(charge_pct)%. $left remaining."

  # Hold the machine awake only while working. This does NOT defeat lid-close
  # sleep on battery: that is what cost 8.8 hours on the night of 2026-08-07.
  caffeinate -dimsu &
  CAF=$!

  "$KB" enrich --max-seconds "$ON_SECONDS" >> "$LOG" 2>&1
  rc=$?
  kill $CAF 2>/dev/null

  after=$(remaining)
  log "--- cycle $cycle done (exit $rc). $((left - after)) enriched, $after remaining."
  if [[ $rc -ne 0 ]]; then
    log "enrich exited non-zero. Resting, then retrying."
  fi
done

log "=== enrichment complete, finishing the pipeline ==="
caffeinate -dimsu &
CAF=$!
"$KB" bundle >> "$LOG" 2>&1 && log "bundle ok" || log "bundle FAILED"
"$KB" index  >> "$LOG" 2>&1 && log "index ok"  || log "index FAILED"
"$KB" graph  >> "$LOG" 2>&1 && log "graph ok"  || log "graph FAILED"
kill $CAF 2>/dev/null

date -u +%Y-%m-%dT%H:%M:%SZ > "$SENTINEL"
log "=== all done ==="
