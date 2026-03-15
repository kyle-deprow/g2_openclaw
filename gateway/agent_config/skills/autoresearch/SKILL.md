---
name: autoresearch
description: Autonomous research loop with Copilot-based multi-agent ideation. PM delegates research debate to Copilot researcher agent, selects winner, delegates implementation to Copilot orchestrator, measures, keeps/reverts. Cron-driven self-continuation.
version: 4.0.0
---

# Autoresearch — Autonomous Iteration Protocol v4

**Behavioral mode with Copilot-based research agents.** The PM agent orchestrates an iterative research loop. Novel experiment ideas come from delegating to the Copilot `researcher` agent, which orchestrates a structured debate among `contrarian`, `explorer`, and `theorist` agents. The PM evaluates the winning idea and delegates implementation to the Copilot `orchestrator` agent.

**EXECUTION MODEL:** You (the PM agent) run this loop in YOUR turn using your own tools. Phases 1, 6, 7 use `read`, `write`, `exec`, `memory_search` directly. Phase 2 (ideation) delegates to Copilot `--agent researcher`. Phase 3 (implementation) delegates to Copilot `--agent orchestrator`. Both run in background via `exec bash pty:true background:true`. Do NOT wrap the entire loop in a single `exec copilot` call.

## When to Activate

- User says "autoresearch", "iterate autonomously", "keep improving", "run overnight", "research loop"
- Any task requiring repeated modify → verify → measure cycles with a mechanical metric
- User wants hands-off autonomous improvement of code, tests, performance, or any quantifiable target

## Setup Phase (Do Once)

1. **Read all in-scope files** for full context before any modification
2. **Define the goal** — extract or ask for a mechanical metric:
   - Code: tests pass, coverage %, performance benchmark
   - ML/quant: Sharpe ratio, val_loss, hit rate
   - Content: word count, readability score, SEO score
   - If no metric exists → define one (simplest proxy: "compiles without errors")
3. **Define scope** — which files can be modified? Which are read-only?
4. **Define direction** — higher is better (coverage, Sharpe) or lower is better (latency, loss)
5. **Establish baseline** — run verification, record as iteration #0 in experiment log
6. **Check prior work** — use `memory_search` to find previous attempts on this goal. Don't repeat failed ideas.
7. **Check scaffolding** — verify the target repo has `.github/agents/researcher.agent.md` + contrarian + explorer + theorist. If missing, copy from `~/repos/ai_scaffolding/agents/`.

## The Loop

```
LOOP (until goal met or user interrupts):

  Phase 1 — REVIEW (build situational awareness)
    - Read current state of in-scope files
    - Read last 10-20 entries from experiment log
    - Run: git log --oneline -20
    - Run: memory_search for related past experiments
    - Identify: what worked, what failed, what's untried

  Phase 2 — IDEATE (Copilot research agents)
    Delegate the entire research debate to Copilot's researcher agent:

    a) Build context block:
       - Current best metric and baseline
       - Last 10 experiment log entries (what worked, what didn't)
       - List of all strategies/approaches already tried
       - Available data sources in the codebase

    b) Delegate to Copilot researcher agent:

       exec bash pty:true workdir:<repo> background:true command:"copilot --agent researcher -p \"
         Context:
         - Current best Sharpe: <N>, baseline: <N>
         - Experiments tried: <list>
         - Data available: <list>
         - Generic indicators (SMA, RSI, MACD, Bollinger, OBV) are BANNED.

         Run the research debate. Delegate to contrarian, explorer, and theorist
         agents. Each should propose 2-3 graded ideas. Evaluate all proposals
         against filters (data available, testable, novel, not tried). Pick the
         single best idea. Output a structured research report with the winner
         and all proposals.
       \" --yolo --model claude-opus-4.6 --no-auto-update"

    c) Wait for completion via process action:log
    d) Read the research report from the session output
    e) Extract the winning idea
    f) Write the winning idea to the shared experiment log before proceeding

  Phase 3 — MODIFY (one atomic change)
    Delegate implementation to Copilot orchestrator:

    exec bash pty:true workdir:<repo> background:true command:"copilot --agent orchestrator -p \"
      Implement this experiment: <winning idea from Phase 2>
      Affected files: <list>
      Test command: <command>
      One focused change. Run tests after. Commit with message 'experiment: <description>'.
    \" --yolo --model claude-opus-4.6 --no-auto-update"

    Wait for completion via process action:log

  Phase 4 — VERIFY (mechanical only)
    - exec: run the verification command (tests, benchmark, backtest)
    - Extract the metric number from output
    - Timeout: if verification exceeds 2x normal time, stop and treat as crash

  Phase 5 — DECIDE (no ambiguity)
    - IMPROVED → keep commit, status = "keep"
    - SAME/WORSE → exec: git reset --hard HEAD~1, status = "discard"
    - CRASHED → attempt fix (max 3 tries), else revert, status = "crash"
    - Simplicity override: barely improved + complex → discard.
      Unchanged metric + simpler code → keep.

  Phase 6 — LOG
    - Append to experiments.jsonl (see Results Logging below)
    - If meaningful finding → write insight to MEMORY.md
    - Print one-line status every ~5 iterations

  Phase 7 — REPEAT
    - Go to Phase 1. Do NOT stop. Do NOT ask "should I continue?"
    - If goal achieved → print final summary and stop
```

## Critical Rules

1. **Loop until done** — never ask "should I keep going?" Just keep iterating.
2. **Read before write** — always understand full context before modifying.
3. **One change per iteration** — atomic changes. If it breaks, you know exactly why.
4. **Mechanical verification only** — no "looks good." Use numbers. If you can't extract a metric from a command, you can't iterate.
5. **Automatic rollback** — failed changes revert instantly. No "maybe if we tweak it."
6. **Never re-try failed ideas** — check `memory_search` and experiment log before proposing.
7. **Simplicity wins** — equal results + less code = KEEP. Tiny gain + ugly complexity = DISCARD.
8. **Git is memory** — every kept change committed. Read your own git history to learn patterns.
9. **When stuck, think harder** — re-read files, re-read goal, combine near-misses, try radical changes. Don't give up without trying the opposite of what hasn't been working.

## Core Principles

Seven universal principles from Karpathy's autoresearch, applicable to any autonomous work:

1. **Constraint = Enabler** — Bounded scope that fits context, fixed iteration cost, single mechanical metric. Constraints enable confidence, simplicity, and velocity.

2. **Separate Strategy from Tactics** — The user sets direction ("improve page load speed"). The agent executes iterations ("lazy-load images, code-split routes"). Don't mix roles.

3. **Metrics Must Be Mechanical** — If you can't verify with a shell command that outputs a number, you can't iterate autonomously. Tests pass/fail, benchmark ms, coverage %, file size bytes. "Looks better" breaks autonomous loops.

4. **Verification Must Be Fast** — If verification takes longer than the work itself, incentives misalign. Use the fastest verification that catches real problems. Unit tests (seconds) > E2E suite (minutes) > manual QA (hours).

5. **Iteration Cost Shapes Behavior** — Cheap iteration → bold exploration. Expensive iteration → conservative. Minimize cost: fast tests, incremental builds, targeted verification.

6. **Git as Memory and Audit Trail** — Commit before verify. Revert on failure. Every kept change stacks. Agent reads its own git history to inform next experiment.

7. **Honest Limitations** — If you hit a wall (missing permissions, external dependency, needs human judgment), say so clearly. Don't guess or hallucinate solutions.

## Results Logging

Track every iteration in `results/experiments.jsonl` (or project-appropriate path). One JSON object per line:

```jsonl
{"iteration": 0, "commit": "a1b2c3d", "metric": 85.2, "delta": 0.0, "status": "baseline", "description": "initial state — test coverage 85.2%"}
{"iteration": 1, "commit": "b2c3d4e", "metric": 87.1, "delta": 1.9, "status": "keep", "description": "add tests for auth middleware edge cases"}
{"iteration": 2, "commit": "-", "metric": 86.5, "delta": -0.6, "status": "discard", "description": "refactor test helpers (broke 2 tests)"}
{"iteration": 3, "commit": "-", "metric": 0.0, "delta": 0.0, "status": "crash", "description": "add integration tests (DB connection failed)"}
```

Fields: `iteration` (int), `commit` (short hash or "-"), `metric` (float), `delta` (change from previous best), `status` (baseline/keep/discard/crash), `description` (one sentence).

First line records metric direction:
```jsonl
{"meta": true, "metric_direction": "higher_is_better", "metric_name": "test_coverage_pct", "goal": "95%"}
```

**Progress reports** — every 10 iterations, print:
```
=== Autoresearch Progress (iteration 20) ===
Baseline: 85.2% → Current best: 92.1% (+6.9%)
Keeps: 8 | Discards: 10 | Crashes: 2
```

After meaningful findings, write a summary to MEMORY.md so future runs benefit.

## Stuck Detection & Recovery

Trigger: >5 consecutive discards in the same area.

Recovery protocol:
1. Re-read ALL in-scope files from scratch
2. Re-read the original goal
3. Review entire experiment log for patterns — what KIND of changes succeed?
4. Try combining 2-3 previously successful changes
5. Try the OPPOSITE of what hasn't been working
6. Try a radical architectural change
7. Rotate to a different improvement area if available

**Crash recovery:**
- Syntax error → fix immediately, don't count as separate iteration
- Runtime error → attempt fix (max 3 tries), then move on
- Resource exhaustion (OOM) → revert, try smaller variant
- Infinite loop/hang → stop after timeout, revert, avoid that approach

## Execution Model

**This is a behavioral mode, NOT a separate agent.** When activated, you (the default agent) follow this protocol. You do not spawn a subagent.

### Delegation
All ideation and coding go through Copilot CLI via `exec bash pty:true background:true`:
- Phase 2 (IDEATE) → delegate to Copilot `--agent researcher` in background
- Phase 3 (MODIFY) → delegate to Copilot `--agent orchestrator` in background
- Phase 4 (VERIFY) → you run tests directly or check Copilot results via `process action:log`

### Self-Continuation via Cron
The loop persists across agent turns using cron self-nudging:

1. At loop start, create a continuation cron:
```
cron_create: schedule "every 3m", delivery "none", execution "main", prompt "AUTORESEARCH MONITOR (tick N of max 40). Check background Copilot via process action:log. If DONE → evaluate results, launch next iteration, reset tick to 0. If STILL RUNNING → reply with only 'still running' (no tool calls, no analysis). If tick >= 40 (2 hours) → delete this cron, post [TASK:timeout]. If goal met or user said stop → delete this cron."
```

**CRITICAL cron rules:**
- `execution: "main"` — the cron MUST share the main session context to see process IDs and conversation history. Never use isolated.
- **Hard TTL: 40 ticks (2 hours).** If the cron fires 40 times without completing, it self-deletes. This prevents runaway token burn.
- **Still-running ticks are FREE.** If the background process isn't done, respond with just "still running" — no tool calls, no reasoning, no analysis. This costs ~200 tokens instead of ~15K.
- Track the tick count in memory or the cron prompt itself. Increment on each fire.

2. Each cron trigger resumes the loop at the appropriate phase
3. When the goal is met or user says stop, delete the cron with `cron_delete`
4. After 40 ticks with no goal completion, the cron self-deletes as a safety valve

### Status Reporting
- Post `[TASK:running] autoresearch iteration N` after each launch
- Post `[TASK:complete] autoresearch — N iterations, metric: X → Y` when goal met
- Every 10 iterations, post a progress summary (see Results Logging)
- Write significant findings to MEMORY.md via `memory_search` context
