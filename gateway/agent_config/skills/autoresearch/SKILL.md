---
name: autoresearch
description: Autonomous research loop with Copilot-based multi-agent ideation. PM delegates research debate to Copilot researcher agent, selects winner, delegates implementation to Copilot orchestrator, measures, keeps/reverts. Cron-driven self-continuation.
version: 5.0.0
---

# Autoresearch — Autonomous Iteration Protocol v5

**Behavioral mode with Copilot-based research agents.** The PM agent orchestrates an iterative research loop. Novel experiment ideas come from delegating to the Copilot `researcher` agent, which orchestrates a structured debate among `contrarian`, `explorer`, and `theorist` agents. The PM evaluates the winning idea and delegates implementation to the Copilot `orchestrator` agent.

**EXECUTION MODEL:** You (the PM agent) run this loop in YOUR turn using your own tools. Phases 1, 6, 7 use `read`, `write`, `exec`, `memory_search` directly. Phase 2 (ideation) delegates to Copilot `--agent researcher`. Phase 3 (implementation) delegates to Copilot `--agent orchestrator`. Both run in background via `exec bash pty:true background:true`. Do NOT wrap the entire loop in a single `exec copilot` call.

**AUTONOMOUS BY DEFAULT:** Once activated, you run the full loop without waiting for human approval at each step. The human steers with high-level direction ("autoresearch", "focus on sentiment", "try HMM approaches") and disconnects. You continue working.

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
    - Read last 10-20 entries from experiment log (RESEARCH_LOG.md)
    - Run: git log --oneline -20
    - Run: memory_search for related past experiments AND prior research rounds
    - Identify: what worked, what failed, what's untried
    - Check: do we have UNIMPLEMENTED proposals from a prior research round?
      If yes → skip Phase 2, go straight to Phase 3 with the top-ranked unimplemented proposal.
      If no → proceed to Phase 2 for new ideation.

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

         INTRADAY FOCUS (NON-NEGOTIABLE):
         - ALL strategies MUST target sub-day holding periods (minutes to hours). No overnight positions.
         - We have 1-MINUTE OHLCV bars — exploit this granularity. Time-of-day features, volume profiles,
           VWAP dynamics, opening range patterns, session segmentation are all fair game.
         - Transaction costs MUST be modeled — at intraday frequency, slippage destroys alpha.
         - Intraday patterns to explore: opening range, VWAP reversion, volume-at-price, lunch hour effects,
           power hour momentum, intraday sentiment spikes, session-over-session regime persistence.

         CONSTRAINTS:
         - Generic indicators (SMA, RSI, MACD, Bollinger, OBV) are BANNED as primary signals.
         - MANDATORY: Every proposal MUST include a machine learning / learning component.
           Reject any idea that relies solely on hand-tuned thresholds or fixed rules.
           Minimum: supervised learning, unsupervised clustering, online learning, or learned features.
         - Tech stack: Python 3.13, scikit-learn, pandas, numpy, backtesting.py, SQLAlchemy.
         - Scoring weight: (novelty × 2) + feasibility + (persistence × 1.5). Favor ambitious ML ideas.

         Run the research debate. Delegate to contrarian, explorer, and theorist
         agents. Each should propose 2-3 ML-grade INTRADAY ideas with learned parameters.
         HARD REJECT any proposal without a learning component.
         HARD REJECT any proposal with overnight holding periods.
         Evaluate remaining proposals against filters. Pick the single best ML idea.
         Output a structured research report with the winner and all proposals.
       \" --yolo --model claude-opus-4.6 --no-auto-update"

    c) Wait for completion via process action:log
    d) Read the research report from the session output
    e) Extract the winning idea AND the full ranked list (all proposals with scores)
    f) Write ALL proposals (ranked, with scores) to RESEARCH_LOG.md and memory
       This is your implementation queue — you'll work through them in order.

  Phase 3 — IMPLEMENT (Copilot orchestrator — quantitative strategy)
    Pick the top-ranked UNIMPLEMENTED proposal from RESEARCH_LOG.md.

    Build a detailed implementation prompt that includes:
    - The proposal name, description, and ML model type
    - Feature engineering pipeline: raw data → features → model input
    - Which existing data services to use (PriceDataService, NewsSentimentService, etc.)
    - Model training approach (walk-forward, cross-validation, etc.)
    - Where to put the code: src/quantipy/alpha/<strategy_name>/
    - Backtest requirements: use BacktestRunner, compare vs SMA crossover baseline
    - Test requirements: pytest, all existing tests must still pass
    - Dependencies: only add deps already in the stack (scikit-learn, xgboost, etc.) or lightweight

    **NOTEBOOK OUTPUT (mandatory):**
    Every experiment MUST produce a Jupyter notebook as its primary output.
    The notebook is the deliverable — module code supports it, not the other way around.

    Delegate to Copilot orchestrator:

    exec bash pty:true workdir:<repo> background:true command:"copilot --agent orchestrator -p \"
      IMPLEMENT INTRADAY STRATEGY: <proposal name>

      Description: <full proposal description from research report>
      ML Model: <model type and approach>
      Holding Period: INTRADAY ONLY — entry and exit within the same trading day. No overnight positions.

      Feature Engineering:
      <feature list from proposal — map each to existing data services>
      MUST INCLUDE time-of-day features (hour, minutes-since-open, session half).
      MUST MODEL transaction costs in backtest (slippage + commissions).

      Implementation has TWO parts:

      PART 1 — Module code in src/quantipy/alpha/<strategy_name>/:
        - features.py — feature extraction pipeline using existing data services
        - model.py — ML model training and prediction (scikit-learn/xgboost)
        - strategy.py — QuantiPyStrategy subclass that wraps the model for backtesting

      PART 2 — Experiment notebook at notebooks/experiments/<strategy_name>.ipynb:
        This is the PRIMARY deliverable. The notebook MUST contain:
        1. Hypothesis — what we expect and why (2-3 sentences)
        2. Data loading — pull from existing data services, show shape and sample
        3. Feature engineering — compute features, show distributions/correlations
        4. Model training — walk-forward CV (8-week train, 1-week validate, 7-day embargo, 52 rolls)
        5. Backtest execution — run via BacktestRunner, compare vs SMA baseline
        6. Results — Sharpe ratio, max drawdown, win rate, profit factor (as printed output)
        7. Visualizations — equity curve, drawdown chart, feature importance
        8. Conclusion — 2-3 sentences: keep/discard decision with reasoning

        The notebook must be EXECUTABLE: `uv run jupyter execute notebooks/experiments/<strategy_name>.ipynb`
        Use papermill-compatible parameterization where possible.
        Create notebooks/ and notebooks/experiments/ dirs if they don't exist.

      Tests: write unit tests in tests/unit/alpha/test_<strategy_name>.py
      After: run uv run pytest -q --tb=short --ignore=tests/integration
      All existing tests MUST still pass.
      Commit with message: 'experiment: <strategy_name> — <one-line description>'
    \" --yolo --model claude-opus-4.6 --no-auto-update"

    Wait for completion via process action:log

  Phase 4 — VERIFY (run in YOUR turn with exec commands — do NOT delegate to Copilot)
    **DO NOT WAIT FOR HUMAN. DO NOT DELEGATE TO COPILOT.** Evaluation is lightweight — you do it directly.
    The sentinel's [TASK:complete] includes metrics. Parse them first.
    If metrics are present and sufficient → use them directly for Phase 5.
    If metrics are missing or insufficient → run these checks yourself with exec:

    a) Tests pass:
    exec bash pty:true workdir:<repo> command:"uv run pytest -q --tb=short --ignore=tests/integration 2>&1 | tail -10"

    b) Notebook exists and executes:
    exec bash pty:true workdir:<repo> command:"ls notebooks/experiments/<strategy_name>.ipynb && uv run jupyter nbconvert --execute --inplace --ExecutePreprocessor.timeout=300 notebooks/experiments/<strategy_name>.ipynb 2>&1 | tail -20"
    If the notebook doesn't exist → CRASH.
    If the notebook fails to execute → CRASH (attempt fix, max 3 tries).

    c) Extract metrics from notebook output:
    exec bash workdir:<repo> command:"python3 -c \"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])) if nbs else {}; [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\" 2>&1 | tail -30"

    Extract: Sharpe ratio, max drawdown, win rate, total return, trade count, OOS accuracy.
    If the strategy can't be backtested yet (missing data, import errors), treat as CRASH.

  Phase 5 — DECIDE (against thresholds — autonomous, no human input)
    **You make this decision. Not the human.**
    Hard thresholds for quant strategies:
    - Tests pass? If no → CRASH (attempt fix, max 3 tries, then revert)
    - Sharpe > -0.5? If no → DISCARD (too bad to keep)
    - Sharpe > SMA baseline? If yes → KEEP (improvement)
    - Sharpe > 0.5? → SIGNIFICANT KEEP (flag as promising)
    - Sharpe > 1.0? → STRONG KEEP (prioritize for further optimization)
    - Max drawdown < 30%? If no → DISCARD regardless of Sharpe

    Decision:
    - KEEP / SIGNIFICANT KEEP → keep commit, record metrics, mark strategy as "implemented" in RESEARCH_LOG.md
    - DISCARD → git revert HEAD, record why, consider: can features be improved?
      If the model architecture is sound but features are weak, try ONE feature iteration before moving on.
    - CRASH → attempt fix (max 3 tries), else revert. Mark as "crashed" in RESEARCH_LOG.md.

    After DISCARD with decent architecture (Sharpe > -1.0):
    - Try one feature engineering iteration: add/remove features, retrain, retest
    - If still DISCARD after feature iteration → move to next proposal

  Phase 6 — LOG
    - Append to experiments.jsonl (see Results Logging below)
    - Update RESEARCH_LOG.md: mark strategy status (implemented/discarded/crashed) with metrics
    - Write to memory/YYYY-MM-DD.md: strategy name, outcome, Sharpe, what worked/failed
    - If SIGNIFICANT KEEP or STRONG KEEP → update MEMORY.md with the strategy as a milestone

  Phase 7 — REFLECT (after every 3 implementations or end of research round)
    This phase runs after implementing 3 strategies from a round OR when all proposals are done.

    a) Pattern analysis — read RESEARCH_LOG.md and extract:
       - Success rate: how many KEEPs vs DISCARDs vs CRASHes?
       - Did high-ranked proposals actually perform better than low-ranked ones?
       - Which agent source (contrarian/explorer/theorist) produced the best ideas?
       - Which model types worked (tree-based, Bayesian, HMM, etc.)?
       - Which feature categories drove success (sentiment, volume, price patterns)?

    b) Copilot session forensics — for each CRASH or bad DISCARD:
       - Read the Copilot session log: `exec bash command:"ls -t /home/dev/.copilot/session-state/ | head -10"`
       - Check: did Copilot misunderstand the prompt? Use wrong data services? Skip tests?
       - Identify repeating failure patterns (e.g., "always imports wrong module", "forgets walk-forward")

    c) Scaffolding update — if patterns found in (b):
       Delegate to Copilot to update the scaffolding:
       ```
       exec bash pty:true workdir:/home/dev/repos/quantipy command:"copilot --agent orchestrator -p \"
         Read .github/copilot-instructions.md and .github/agents/.
         Based on these observed failure patterns: <patterns from b>
         Update the instructions to prevent these. Add specific warnings, examples, or rules.
         Also check if any agent file is stale or never triggered — remove it if so.
         Keep it lean. Run git diff to show what changed.
       \" --yolo --model claude-opus-4.6 --no-auto-update"
       ```

    d) Research quality adjustment — write to memory:
       - If high-ranked proposals consistently underperform: note that scoring weights may need adjustment
       - If one agent source dominates successes: note to weight that source higher next round
       - If a model type consistently fails: add it to the avoid list for next ideation prompt

    e) Write a reflection note to memory/YYYY-MM-DD.md:
       ```
       ## Reflection (after N implementations)
       - Success rate: X/N kept, Y discarded, Z crashed
       - Best source: <agent> (N/M kept)
       - Best model type: <type>
       - Scaffolding updated: yes/no (what changed)
       - Next round adjustments: <what to emphasize/avoid>
       ```

  Phase 8 — CONTINUE (autonomous progression — NO HUMAN INPUT NEEDED)
    Do NOT stop. Do NOT ask "should I continue?" Do NOT wait for the human. Just continue.

    Decision tree (you decide, not the human):
    a) Current strategy was KEEP and Sharpe < 1.0?
       → Try optimizing: feature iteration, hyperparameter tuning, ensemble with prior keeps
    b) Current strategy was STRONG KEEP (Sharpe > 1.0)?
       → Move to next proposal. Log this as a "significant alpha candidate."
    c) Current strategy was DISCARD?
       → Move to next ranked proposal from RESEARCH_LOG.md
    d) All proposals from current research round implemented?
       → Run Phase 7 REFLECT first, then Phase 2 for new ideation with updated context
       → Include reflection insights in the next researcher prompt
    e) Goal met (Sharpe > 1.5 sustained across walk-forward)?
       → Post [TASK:complete], print final summary, stop

    Go to Phase 1.
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
**Only Phase 2 and Phase 3 delegate to Copilot CLI.** Everything else is YOUR work:
- Phase 2 (IDEATE) → delegate to Copilot `--agent researcher` in background
- Phase 3 (IMPLEMENT) → delegate to Copilot `--agent orchestrator` in background
- Phase 4-5 (VERIFY + DECIDE) → YOU run exec commands directly (notebook execution, metric extraction, threshold comparison)
- Phase 6-7 (LOG + REFLECT) → YOU write to files and memory directly
- Phase 8 (CONTINUE) → YOU decide next action and launch the appropriate Copilot agent

**The entire Phase 4 → Phase 8 sequence runs in ONE turn** after sentinel reports completion. You do not stop between phases. You do not ask the human what to do next.

### Self-Continuation via Copilot Process Sentinel
The loop persists across turns using a cheap monitoring sentinel. Uses `azure-oai-g2-mini/gpt-5-mini` for ~$0.001/tick instead of burning GPT-5.4 on `ps -p PID` checks.

1. Before creating the sentinel, capture the expiry epoch:
   `exec bash command:"echo $(( $(date +%s) + 7200 ))"`

2. Create the Copilot Process Sentinel (fill in PID, REPO, and EXPIRY):
```
cron_create: schedule "every 5m", delivery "announce", prompt "COPILOT SENTINEL. PID=<PID>. Repo=/home/dev/repos/quantipy. Expiry=<EXPIRY_EPOCH>.
Step 1: exec bash command:\"ps -p <PID> -o pid= 2>/dev/null || echo EXITED\"
Step 2: exec bash command:\"date +%s\"
If output does NOT contain EXITED → respond 'PID <PID> alive'. STOP. No other tool calls.
If current epoch > Expiry → respond '[TASK:timeout] Copilot PID <PID> exceeded 2h TTL'. Delete this cron with cron_delete. STOP.
If EXITED → run THREE commands:
  exec bash command:\"cd /home/dev/repos/quantipy && git log --oneline -3\"
  exec bash command:\"cd /home/dev/repos/quantipy && uv run pytest -q --tb=line 2>&1 | tail -5\"
  exec bash command:\"cd /home/dev/repos/quantipy && python3 -c \\\"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])) if nbs else {}; [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\\\" 2>&1 | tail -20\"
Respond: '[TASK:complete] Copilot PID <PID> exited. Commits: <git log>. Tests: <test summary>. Notebook metrics: <extracted metrics>'. Delete this cron with cron_delete. STOP."
```

**Sentinel design (two-stage cost optimization):**
- **Alive ticks (~90%):** One `ps` command + "alive". Announced to channel — main agent ignores.
- **Exit tick (once):** Runs git log + pytest + notebook metrics, formats [TASK:complete] summary. Announced to channel.
- **Full evaluation:** Happens in your NEXT turn (GPT-5.4) when you process the announced [TASK:complete] and continue the autoresearch loop.

**Sentinel rules:**
- **Always specify `delivery: "announce"`** — so the main agent sees [TASK:complete] when Copilot exits.
- **Do NOT specify `execution`** — default (isolated) is correct. `execution: "main"` FAILS for named agents.
- **Do NOT pass `context`** — not a valid cron_create field. Valid: schedule, delivery, prompt, model, agent, thinking.
- **Prompt must be self-contained** — include PID, repo path, and expiry. Isolated crons have NO conversation history.
- **Hard TTL: 2 hours** — creation epoch + 7200. Sentinel self-deletes on expiry.
- **Reusable** — this same sentinel pattern works for researcher, orchestrator, or any Copilot background process.

3. Each tick runs independently with all context in the prompt
4. When Copilot exits, the sentinel does a lightweight summary and self-deletes
5. To continue the loop: after sentinel reports completion, launch NEXT iteration from main conversation, create new sentinel for new PID

### Status Reporting
- Post `[TASK:running] autoresearch iteration N` after each launch
- Post `[TASK:complete] autoresearch — N iterations, metric: X → Y` when goal met
- Every 10 iterations, post a progress summary (see Results Logging)
- Write significant findings to MEMORY.md via `memory_search` context
