#!/usr/bin/env bash
# autoresearch-loop.sh — Persistent autoresearch loop controller
#
# Bridges the single-turn `openclaw agent --local` limitation by:
# 1. Watching for Copilot process completion
# 2. Collecting results (git log, notebook metrics, dirty tree state)
# 3. Feeding results back to OpenClaw for the next phase
# 4. Repeating until the human says stop
#
# Usage:
#   bash scripts/autoresearch-loop.sh [--kickstart]
#
# --kickstart: Start a fresh autoresearch loop (Phase 1+2), otherwise
#              assumes a Copilot session is already running and monitors it.

set -uo pipefail
# NOTE: do NOT use -e here — pgrep/ss commands can return non-zero legitimately

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QUANTIPY_DIR="$HOME/repos/quantipy"
LOG_DIR="$REPO_ROOT/.archive"
LOOP_LOG="$LOG_DIR/autoresearch-loop.log"
PIDFILE="$LOG_DIR/autoresearch-loop.pid"
POLL_INTERVAL=30  # seconds between checks
SCOPE_ENABLED="${SCOPE_ENABLED:-true}"  # wrap openclaw calls in systemd scope for process isolation
COPILOT_MODEL="${COPILOT_MODEL:-claude-sonnet-4.6}"  # model for Copilot CLI sessions

mkdir -p "$LOG_DIR"

# Single-instance guard
if [[ -f "$PIDFILE" ]]; then
  old_pid=$(cat "$PIDFILE")
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "Loop controller already running (PID $old_pid). Exiting."
    exit 0
  fi
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

log() {
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "[$ts] $*" >> "$LOOP_LOG"
}

MEM_PRESSURE_THRESHOLD=85  # percent used RAM before triggering cleanup

# ── Helpers ──────────────────────────────────────────────────────────────────

copilot_is_running() {
  pgrep -f 'copilot.*--agent' >/dev/null 2>&1
}

get_copilot_pid() {
  pgrep -f 'node /usr/bin/copilot' 2>/dev/null | head -1 || echo ""
}

openclaw_is_running() {
  # ss -tlnp can fail in headless/nohup context; use nc as fallback
  nc -z 127.0.0.1 18789 2>/dev/null || ss -tlnp 2>/dev/null | grep -q ':18789'
}

mem_used_percent() {
  # Read from /proc/meminfo — no external deps
  awk '/^MemTotal:/{total=$2} /^MemAvailable:/{avail=$2} END{if(total>0) printf "%d", ((total-avail)/total)*100; else print 0}' /proc/meminfo
}

check_memory_pressure() {
  # Returns 0 (true) if RAM usage exceeds threshold
  local used
  used=$(mem_used_percent)
  [[ "$used" -ge "$MEM_PRESSURE_THRESHOLD" ]]
}

kill_zombie_kernels() {
  # Kill orphaned python processes (ppid=1) matching jupyter/ipykernel patterns
  # that are using >200 MB RSS.  These are zombie kernels leftover from crashed
  # Copilot sessions that ran `jupyter nbconvert --execute`.
  local killed=0
  local freed_mb=0
  while IFS= read -r line; do
    local pid ppid rss args
    pid=$(echo "$line" | awk '{print $1}')
    ppid=$(echo "$line" | awk '{print $2}')
    rss=$(echo "$line" | awk '{print $3}')
    args=$(echo "$line" | awk '{for(i=4;i<=NF;i++) printf "%s ", $i}')

    [[ "$ppid" -ne 1 ]] && continue
    [[ "$rss" -lt 204800 ]] && continue  # <200 MB

    # Only kill python/node processes, never openclaw/vscode/gateway
    if echo "$args" | grep -qiE "python|ipykernel|jupyter|nbconvert|loky|joblib"; then
      if ! echo "$args" | grep -qiE "openclaw|gateway|vscode"; then
        kill "$pid" 2>/dev/null
        local mb=$((rss / 1024))
        log "Killed zombie PID $pid (${mb} MB): ${args:0:120}"
        freed_mb=$((freed_mb + mb))
        killed=$((killed + 1))
      fi
    fi
  done < <(ps -eo pid,ppid,rss,args --no-headers 2>/dev/null)

  if [[ "$killed" -gt 0 ]]; then
    log "Memory cleanup: killed $killed zombie(s), freed ~${freed_mb} MB"
  fi
  echo "$killed"
}

get_quantipy_state() {
  cd "$QUANTIPY_DIR"
  echo "HEAD=$(git rev-parse --short HEAD)"
  echo "DIRTY=$(git status --short | wc -l)"
  echo "LAST_COMMIT=$(git log --oneline -1)"
  
  # Check for new notebooks
  local nb
  nb=$(git status --short | grep '\.ipynb' | head -1 | awk '{print $2}')
  if [[ -n "$nb" ]]; then
    echo "NEW_NOTEBOOK=$nb"
  fi
}

extract_notebook_metrics() {
  local notebook="$1"
  if [[ ! -f "$notebook" ]]; then
    echo "NO_NOTEBOOK"
    return
  fi
  python3 -c "
import json, sys
try:
    nb = json.load(open(sys.argv[1]))
    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'code':
            continue
        for output in cell.get('outputs', []):
            text = ''.join(output.get('text', []))
            tl = text.lower()
            if any(k in tl for k in ['sharpe', 'return', 'drawdown', 'accuracy', 'trade', 'result', 'oos', 'walk-forward']):
                print(text.strip()[:500])
except Exception as e:
    print(f'ERROR: {e}')
" "$notebook" 2>&1 | tail -30
}

collect_death_report() {
  cd "$QUANTIPY_DIR"
  local head_before="$1"
  
  echo "=== COPILOT SESSION COMPLETE ==="
  echo ""
  
  # Always show recent commits for context (guards against stale head_before)
  echo "RECENT COMMITS (last 5):"
  git log --oneline -5 2>/dev/null
  echo ""
  
  # Git changes since launch
  local head_after
  head_after=$(git rev-parse --short HEAD)
  if [[ "$head_before" != "$head_after" ]]; then
    echo "NEW COMMITS (since $head_before):"
    git log --oneline "${head_before}..HEAD" 2>/dev/null || echo "(none)"
  else
    echo "NO NEW COMMITS (head unchanged: $head_after)"
    echo "NOTE: head_before may be stale — check RECENT COMMITS above."
  fi
  echo ""
  
  # Dirty tree
  local dirty
  dirty=$(git status --short)
  if [[ -n "$dirty" ]]; then
    echo "DIRTY TREE:"
    echo "$dirty"
  fi
  echo ""
  
  # Find newest notebook: check git diff first, then mtime fallback
  local newest_nb=""
  if [[ "$head_before" != "$head_after" ]]; then
    newest_nb=$(git diff --name-only "${head_before}..HEAD" 2>/dev/null | grep '\.ipynb' | head -1)
  fi
  if [[ -z "$newest_nb" ]]; then
    newest_nb=$(find notebooks/experiments/ -name '*.ipynb' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}')
  fi
  if [[ -z "$newest_nb" ]]; then
    newest_nb=$(git status --short | grep '\.ipynb' | head -1 | awk '{print $2}')
  fi
  
  if [[ -n "$newest_nb" ]]; then
    echo "NEWEST NOTEBOOK: $newest_nb"
    echo ""
    echo "NOTEBOOK METRICS:"
    extract_notebook_metrics "$newest_nb"
  else
    echo "NO NOTEBOOK FOUND"
  fi
}

send_to_openclaw() {
  local message="$1"
  local session_id="autoresearch-loop-$(date +%Y%m%d-%H%M%S)"
  
  # Inject model override into every message
  message="[MODEL: Use --model $COPILOT_MODEL for ALL Copilot sessions. Do NOT use any other model.]

$message"

  log "Sending to OpenClaw (session: $session_id)..."
  
  if [[ "$SCOPE_ENABLED" == "true" ]] && command -v systemd-run &>/dev/null; then
    # Scope-wrapped: memory limit + automatic child cleanup on exit
    "$REPO_ROOT/scripts/run-in-scope.sh" --mem 40G --timeout 3600 --name "autoresearch-$session_id" -- \
      openclaw agent --local --agent main --session-id "$session_id" \
      -m "$message" 2>&1 | tee -a "$LOG_DIR/openclaw-responses.log"
  else
    # Direct execution (no systemd available)
    openclaw agent --local --agent main --session-id "$session_id" \
      -m "$message" 2>&1 | tee -a "$LOG_DIR/openclaw-responses.log"
  fi
  
  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    log "WARNING: openclaw exited with code $exit_code"
  fi
  
  log "OpenClaw responded."
}

# ── Main Loop ────────────────────────────────────────────────────────────────

main() {
  local kickstart=false
  if [[ "${1:-}" == "--kickstart" ]]; then
    kickstart=true
  fi
  
  log "=== AUTORESEARCH LOOP CONTROLLER STARTED ==="
  log "Quantipy dir: $QUANTIPY_DIR"
  log "Kickstart: $kickstart"
  
  # Verify OpenClaw is running
  if ! openclaw_is_running; then
    log "ERROR: OpenClaw daemon not running on :18789"
    exit 1
  fi
  log "OpenClaw daemon: OK (:18789)"
  
  # If kickstart, launch the full loop from scratch
  if [[ "$kickstart" == "true" ]]; then
    log "Kickstarting fresh autoresearch loop..."
    
    send_to_openclaw "autoresearch

You are starting a fresh autonomous research loop. This is a persistent loop — I will keep feeding you results as phases complete. Do NOT stop after one turn.

IMMEDIATE ACTIONS (do all of these NOW):
1. mempalace_status — verify memory is live
2. mempalace_diary_read(agent_name: 'autoresearch', last_n: 5) — check for continuity
3. Read the current state: exec 'cd ~/repos/quantipy && git log --oneline -10 && cat RESEARCH_LOG.md | tail -80'
4. mempalace_search(query: 'experiment results') — find prior experiment data
5. Based on what you find, decide: are there UNIMPLEMENTED proposals? If yes, implement the next one. If no, launch a new ideation round.
6. Launch the next Copilot session (researcher or orchestrator) with background:true
7. Report what you launched and the PID

The human wants profitable strategies found. Keep going until Sharpe > 0.5 net OOS is achieved. Do NOT stop. Do NOT ask for permission. The loop IS the approval."
    
    log "Kickstart message sent. Waiting for Copilot to launch..."
    sleep 30
  fi
  
  # Main monitoring loop
  local head_before_file="$LOG_DIR/.head_before"
  rm -f "$head_before_file"
  while true; do
    if copilot_is_running; then
      local pid
      pid=$(get_copilot_pid)
      # Capture HEAD when we first see Copilot running (file-backed for reliability)
      if [[ ! -f "$head_before_file" ]]; then
        local captured_head
        captured_head=$(cd "$QUANTIPY_DIR" && git rev-parse --short HEAD)
        echo "$captured_head" > "$head_before_file"
        log "Tracking Copilot PID $pid (HEAD before: $captured_head)"
      fi
      log "Copilot running (PID: $pid). Polling in ${POLL_INTERVAL}s..."

      # Memory pressure check on every poll
      if check_memory_pressure; then
        local mem_pct
        mem_pct=$(mem_used_percent)
        log "WARNING: Memory pressure detected (${mem_pct}% used, threshold ${MEM_PRESSURE_THRESHOLD}%)"
        kill_zombie_kernels
      fi

      sleep "$POLL_INTERVAL"
      continue
    fi
    
    # Copilot is NOT running — collect results and continue
    log "Copilot process EXITED. Collecting results..."
    
    cd "$QUANTIPY_DIR"
    local head_now
    head_now=$(git rev-parse --short HEAD)
    
    # Use head_before if we captured it, otherwise use head_now
    local head_before=""
    if [[ -f "$head_before_file" ]]; then
      head_before=$(cat "$head_before_file")
    fi
    local baseline="${head_before:-$head_now}"
    rm -f "$head_before_file"  # Reset for next cycle
    
    # Build death report
    local report
    report=$(collect_death_report "$baseline")
    log "Death report collected."
    log "$report"
    
    # Feed results back to OpenClaw for the next phase
    send_to_openclaw "autoresearch — COPILOT SESSION COMPLETED

The Copilot session has finished. Here are the results:

$report

YOUR NEXT ACTIONS (do all NOW, do NOT wait for human):
1. Parse the results above — did the experiment succeed (commit made) or fail (dirty tree, no commit)?
2. If COMMITTED:
   a) Read the notebook and extract metrics (exec jupyter nbconvert or read the output above)
   b) Run Phase 4 VERIFY — sanity check the metrics
   c) Run Phase 4.5 ADVERSARIAL REVIEW — launch Copilot reviewer with background:true
   d) After review completes, run Phase 5 DECIDE (keep/discard threshold)
   e) Run Phase 6 LOG — log to RESEARCH_LOG.md AND MemPalace (mempalace_add_drawer + mempalace_kg_add)
   f) Run Phase 8 CONTINUE — pick next action and launch next Copilot session
3. If DIRTY TREE (no commit):
   a) Check if work is salvageable: exec 'cd ~/repos/quantipy && git diff --stat'
   b) If salvageable, commit it and evaluate
   c) If not, exec 'cd ~/repos/quantipy && git checkout .' and move to next proposal
4. If NO CHANGES:
   a) The Copilot session likely planned but didn't implement. Launch a new implementation session.

CRITICAL: Launch the next Copilot session before responding. The loop must continue.
Post [TASK:running] with the PID when you launch.
Target: Find strategies with net OOS Sharpe > 0.5. Do not stop until found."
    
    log "Waiting for new Copilot launch..."
    # Give OpenClaw time to process and launch next session
    sleep 60
    
    # Check if OpenClaw successfully launched a new Copilot
    if copilot_is_running; then
      log "New Copilot session detected. Loop continues."
    else
      log "WARNING: No new Copilot launched after 60s. Retrying..."
      # Try again with a more forceful message
      send_to_openclaw "autoresearch — CONTINUATION REQUIRED

The previous turn did not launch a new Copilot session. The loop MUST continue.

Check:
1. Are there unimplemented proposals in RESEARCH_LOG.md? If yes, implement the next one.
2. If all proposals are done, launch a new ideation round with Copilot researcher.
3. If you're stuck, read the autoresearch skill for guidance.

Launch a Copilot session NOW with background:true. Report the PID."
      
      sleep 60
      
      if ! copilot_is_running; then
        log "ERROR: Failed to launch Copilot after retry. Loop stalled."
        log "Attempting full kickstart..."
        # Do a full kickstart
        send_to_openclaw "autoresearch

Fresh start. The loop stalled. Begin from Phase 1:
1. mempalace_status
2. Read RESEARCH_LOG.md
3. Check git log in ~/repos/quantipy
4. Launch the next experiment (ideation or implementation)
5. Post [TASK:running] with PID

Do NOT stop. Do NOT ask for permission."
        sleep 60
      fi
    fi
  done
}

main "$@"
