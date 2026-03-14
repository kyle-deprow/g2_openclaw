---
name: autoresearch
description: Autonomous goal-directed iteration. Modify → Verify → Keep/Discard → Repeat. Karpathy-inspired constraint-driven research loop.
version: 2.0.0
---

# Autoresearch — Autonomous Iteration Protocol

Adapted from [Karpathy's autoresearch](https://github.com/karpathy/autoresearch). Applies constraint-driven autonomous iteration to ANY measurable improvement task. Core idea: define a mechanical metric, make one atomic change, verify, keep or revert, repeat. Autonomy scales when you constrain scope, clarify success, and mechanize verification.

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

## The Loop

```
LOOP (until goal met or user interrupts):

  Phase 1 — REVIEW (build situational awareness)
    - Read current state of in-scope files
    - Read last 10-20 entries from experiment log
    - Run: git log --oneline -20
    - Run: memory_search for related past experiments
    - Identify: what worked, what failed, what's untried

  Phase 2 — IDEATE (pick next change, priority order)
    1. Fix crashes/failures from previous iteration
    2. Exploit successes — variants of what improved the metric
    3. Explore untried approaches from experiment log gaps
    4. Combine near-misses — two changes that individually didn't help
    5. Simplify — remove code while maintaining metric
    6. Radical experiments — when incremental changes stall

  Phase 3 — MODIFY (one atomic change)
    - Delegate implementation to Copilot CLI: exec with bash pty:true
    - One focused change, describable in one sentence
    - Write description BEFORE making the change

  Phase 4 — COMMIT (before verification)
    - exec: git add <files> && git commit -m "experiment: <description>"
    - Commit BEFORE verify so rollback is clean: git reset --hard HEAD~1

  Phase 5 — VERIFY (mechanical only)
    - exec: run the verification command (tests, benchmark, backtest)
    - Extract the metric number from output
    - Timeout: if verification exceeds 2x normal time, kill and treat as crash

  Phase 6 — DECIDE (no ambiguity)
    - IMPROVED → keep commit, status = "keep"
    - SAME/WORSE → exec: git reset --hard HEAD~1, status = "discard"
    - CRASHED → attempt fix (max 3 tries), else revert, status = "crash"
    - Simplicity override: barely improved + complex → discard.
      Unchanged metric + simpler code → keep.

  Phase 7 — LOG
    - Append to experiments.jsonl (see Results Logging below)
    - If meaningful finding → write insight to MEMORY.md
    - Print one-line status every ~5 iterations

  Phase 8 — REPEAT
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

3. **Metrics Must Be Mechanical** — If you can't verify with a shell command that outputs a number, you can't iterate autonomously. Tests pass/fail, benchmark ms, coverage %, file size bytes. "Looks better" kills autonomous loops.

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
- Infinite loop/hang → kill after timeout, revert, avoid that approach

## Execution Model

This agent orchestrates. Implementation work is delegated:
- Use `exec` with `bash pty:true` to delegate coding tasks to Copilot CLI
- Use `exec` for running tests, benchmarks, git commands
- Use `memory_search` to check for prior experiments and insights
- Write findings to MEMORY.md after significant discoveries
- For scheduling repeated runs, use cron jobs
