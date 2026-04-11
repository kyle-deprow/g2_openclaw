---
name: autoresearch
description: Autonomous research loop with Copilot-based multi-agent ideation. PM delegates research debate to Copilot researcher agent, selects winner, delegates implementation to Copilot orchestrator, measures, keeps/reverts. Gateway-monitored self-continuation.
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
7. **Check MemPalace** — `mempalace_status` to verify connectivity, then `mempalace_diary_read(agent_name: "autoresearch", last_n: 5)` for session continuity.
8. **Check scaffolding** — verify the target repo has `.github/agents/researcher.agent.md` + contrarian + explorer + theorist. If missing, copy from `~/repos/ai_scaffolding/agents/`.

## The Loop

```
LOOP (until goal met or user interrupts):

  Phase 1 — REVIEW (build situational awareness)
    - Read current state of in-scope files
    - Read last 10-20 entries from experiment log (RESEARCH_LOG.md)
    - Run: git log --oneline -20
    - Run: memory_search for related past experiments AND prior research rounds
    - Query MemPalace: mempalace_search for prior experiments, failure modes, successful features
    - Query MemPalace: mempalace_kg_query for entity relationships and temporal context
    - Identify: what worked, what failed, what's untried (combining RESEARCH_LOG + MemPalace results)
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
       - Available REAL data: any ticker (1-min to daily, 2021-2026). `qp.prices()` auto-fetches missing data — just request the full range.

    b) Delegate to Copilot researcher agent:

       exec bash pty:true workdir:<repo> background:true command:"copilot --agent researcher -p \"
         CURRENT RESEARCH DIRECTION (from the human — NON-NEGOTIABLE):
         Simple indicator intraday trading on small/mid cap equities + Reddit sentiment correlation.
         ALL proposals MUST use these building blocks:
         - Indicators: Moving Averages, Bollinger Bands, OBV. Iterate on parameters/combinations.
         - Sentiment: Reddit post sentiment from DB (analyzed_posts, ticker_sentiments tables).
         - Universe: 4-10 small/mid cap equities (\$1B-\$20B market cap). You choose which tickers.
         - Holding: Intraday ONLY. Flat by 15:50 ET. No entries before 9:45.
         - ML model: freely choose, compare, iterate. No restrictions on model type or complexity.
         HARD REJECT any proposal using exotic/novel features (wavelets, entropy, topology, etc).
         HARD REJECT any proposal on mega-caps (AAPL, NVDA, TSLA, MSFT, GOOG, etc).
         The thesis: simple indicators work better on less-efficient small/mid caps.

         Context:
         - Current best Sharpe: <N>, baseline: <N>
         - Experiments tried: <list>
         - Data available: ANY ticker (1-min to daily bars, 2021-2026). Auto-fetched by `qp.prices()` on first call.
           Also: Reddit sentiment (2021-2026) from r/wallstreetbets, r/stocks, r/investing + news sentiment.
           Sentiment SQL: SELECT * FROM analyzed_posts / ticker_sentiments (localhost:5433)
           Load via: import quantipy as qp; df = qp.prices('<TICKER>', '<start>', '<end>')

         REAL DATA ONLY (NON-NEGOTIABLE):
         - ALL experiments MUST use real OHLCV data from the database. ZERO synthetic data.
         - Read the experiment-data skill (.github/skills/experiment-data/SKILL.md) for data loading patterns.

         INTRADAY FOCUS (NON-NEGOTIABLE):
         - ALL strategies MUST target sub-day holding periods (minutes to hours). No overnight positions.
         - We have 1-MINUTE OHLCV bars. Transaction costs MUST be modeled.

         ASSET CLASS & UNIVERSE DESIGN (MANDATORY in every proposal):
         Every proposal MUST explicitly address:
         1. WHAT to trade: small/mid cap equities (\$1B-\$20B market cap, >5M avg daily volume).
         2. WHY that universe: justify by volatility, spread, liquidity, Reddit coverage.
         3. HOW to evaluate: walk-forward CV config, OOS holdout period, transaction cost model.
         4. DATA SPLIT: Use multi-year data (2021-2026). OOS at least 60 trading days.
         5. HYPERPARAMETER TUNING: RandomizedSearchCV + TimeSeriesSplit.
         Proposals without explicit universe/evaluation design are REJECTED.

         Run the research debate. Delegate to contrarian, explorer, and theorist.
         Each proposal MUST use MA + Bollinger + OBV as the core feature set, with Reddit sentiment.
         Iterate on: which MAs, which BB params, how to combine with sentiment, ML model choice.
         HARD REJECT any proposal with overnight holding periods.
         HARD REJECT any proposal using synthetic data.
         HARD REJECT any proposal on mega-cap tickers.
         HARD REJECT any proposal using exotic features instead of simple indicators.
         Output a structured research report with the winner and all proposals.
       \" --yolo --model claude-opus-4.6 --no-auto-update"

    c) Wait for completion via process action:log
    d) Read the research report from the session output
    e) Extract the winning idea AND the full ranked list (all proposals with scores)
    f) Write ALL proposals (ranked, with scores) to RESEARCH_LOG.md and memory
       This is your implementation queue — you'll work through them in order.

  Phase 3 — IMPLEMENT (Copilot orchestrator — quantitative strategy)
    Pick the top-ranked UNIMPLEMENTED proposal from RESEARCH_LOG.md.

    **CRITICAL: Extract walk-forward parameters from the proposal.** Each proposal in RESEARCH_LOG.md
    specifies its own walk-forward design (min train window, test block size, slide step, purge gap,
    expected fold count). You MUST read these values and inject them into the implementation prompt.
    If the proposal doesn't specify, use the defaults from experiment-data skill (train_min=630 days,
    test=21 days, step=21 days, purge=390 bars).

    Build a detailed implementation prompt that includes:
    - The proposal name, description, and ML model type
    - Feature engineering pipeline: raw data → features → model input
    - Universe: which tickers and why (from the proposal's asset class design)
    - Data loading: MUST use real data via qp.prices() or direct SQL. Read the experiment-data skill.
    - Model training approach (walk-forward CV with hyperparameter tuning)
    - **Walk-forward parameters extracted from the proposal** (min train days, test days, step, purge bars)
    - Transaction cost model (spread + slippage per ticker)
    - Where to put the code: src/quantipy/alpha/<strategy_name>/
    - Backtest requirements: use BacktestRunner, compare vs buy-and-hold baseline
    - Test requirements: pytest, all existing tests must still pass
    - Dependencies: only add deps already in the stack (scikit-learn, xgboost, etc.) or lightweight

    **NOTEBOOK OUTPUT (mandatory):**
    Every experiment MUST produce a Jupyter notebook as its primary output.
    The notebook is the deliverable — module code supports it, not the other way around.

    Delegate to Copilot orchestrator:

    exec bash pty:true workdir:<repo> background:true command:"copilot --agent orchestrator -p \"
      IMPLEMENT INTRADAY STRATEGY: <proposal name>

      READ THE experiment-data SKILL FIRST: .github/skills/experiment-data/SKILL.md
      It defines data loading, methodology, evaluation, and decision thresholds.

      Description: <full proposal description from research report>
      ML Model: <model type and approach>
      Universe: <tickers and justification from proposal>
      Holding Period: INTRADAY ONLY — entry and exit within the same trading day. No overnight positions.

      REAL DATA ONLY — load via qp.prices() or direct SQL from PostgreSQL (localhost:5433).
      Before starting, ensure API server is running:
        uv run uvicorn 'quantipy.api.main:create_app' --factory --host 127.0.0.1 --port 8000 &
      NEVER generate synthetic OHLCV data. All prior synthetic experiments were DISCARDED.

      Feature Engineering:
      <feature list from proposal — map each to existing data services>
      MUST INCLUDE time-of-day features (hour, minutes-since-open, session half).

      Implementation has TWO parts:

      PART 1 — Module code in src/quantipy/alpha/<strategy_name>/:
        - features.py — feature extraction pipeline using real OHLCV data
        - model.py — ML model training and prediction with hyperparameter tuning
        - strategy.py — QuantiPyStrategy subclass that wraps the model for backtesting

      PART 2 — Experiment notebook at notebooks/experiments/<strategy_name>.ipynb:
        This is the PRIMARY deliverable. The notebook MUST contain:
        1. Data inventory — run the inventory cell from experiment-data skill, print actual date ranges
        2. Hypothesis — what we expect, which tickers, why this universe
        3. Data loading — qp.prices() for real OHLCV, show shape and sample
        4. Feature engineering — compute features on real data, show distributions/correlations
        5. Hyperparameter tuning — RandomizedSearchCV with TimeSeriesSplit (min 5 splits)
           Report best params and CV scores. NEVER hardcode model hyperparameters.
        6. Walk-forward backtest — expanding window, min 630-day train, 21-day test blocks,
           390-bar purge gap, 21-day slide, minimum 20 folds on the training period.
           If RESEARCH_LOG specifies different walk-forward params, use those exact values.
           Report per-fold metrics.
        7. Transaction costs — apply realistic spread + slippage per ticker
           Report BOTH gross and net Sharpe. If net < 0 but gross > 0, strategy trades too much.
        8. OOS evaluation — run final model on held-out period (NEVER touched during training, min 120 days)
           Report OOS Sharpe (gross + net), accuracy, trades/day, max drawdown.
        9. Null tests — at least 3: shuffled labels, random features, bootstrap Sharpe CI
        10. Conclusion — keep/iterate/discard decision with reasoning

        Create notebooks/ and notebooks/experiments/ dirs if they don't exist.

      CRITICAL BACKTEST RULE — holding period MUST match prediction horizon:
      If the model predicts a 15-bar forward return, the backtest MUST hold the position for 15 bars — NOT 1 bar.
      Holding for 1 bar and recalculating every bar inflates Sharpe by diversifying across many tiny bets.
      Implement a cooldown: after taking a position, DO NOT trade again for `horizon` bars.
      This is the most common backtesting bug — it produces impossible Sharpe values (>10).

      METRIC SANITY CHECK before reporting results:
      Sharpe > 10 = BUG. Win rate = 1.0 = BUG. Max drawdown = 0.00% = BUG. Profit factor = inf = BUG.
      If any of these trigger, find and fix the bug before reporting.

      Tests: write unit tests in tests/unit/alpha/test_<strategy_name>.py
      After: run uv run pytest -q --tb=short --ignore=tests/integration
      All existing tests MUST still pass.

      MANDATORY FINAL STEPS (do ALL of these before exiting):
      1. Execute the notebook end-to-end:
         uv run jupyter execute notebooks/experiments/<strategy_name>.ipynb --ExecutePreprocessor.timeout=600
         If execution fails, fix the error and re-run. Do NOT exit with a broken notebook.
      2. Verify the notebook has outputs — open it and confirm cells produced printed results.
      3. Run the sanity checks above on the actual output. If any trigger, fix the bug and re-execute.
      4. Only after the notebook executes cleanly with valid metrics:
         Commit with message: 'experiment: <strategy_name> — <one-line description>'
      DO NOT commit a notebook without outputs. DO NOT exit without executing the notebook.
    \" --yolo --model claude-opus-4.6 --no-auto-update"

    Wait for completion via process action:log

  Phase 4 — VERIFY (run in YOUR turn with exec commands — do NOT delegate to Copilot)
    **DO NOT WAIT FOR HUMAN. DO NOT DELEGATE TO COPILOT.** Evaluation is lightweight — you do it directly.
    The gateway's [TASK:complete] includes metrics. Parse them first.
    If metrics are present and sufficient → use them directly for Phase 5.
    If metrics are missing or insufficient → run these checks yourself with exec:

    a0) SANITY CHECK FIRST — before interpreting any metrics, check for impossible values:
    - Sharpe > 10 → BUG (horizon mismatch, look-ahead bias, or PnL calculation error)
    - Win rate = 1.0 → BUG (no losing days means signal leaks future data)
    - Max drawdown = 0.00% → BUG (impossible in real trading)
    - Profit factor = inf → BUG (no losing trades)
    - Accuracy < 50% but Sharpe > 5 → BUG (return calculation inflates gains)
    If ANY sanity check triggers: verdict is BUG, not KEEP/DISCARD.
    Delegate a fix to Copilot: "Fix the backtesting bug: <specific issue>. The holding period must match the prediction horizon."
    Re-run evaluation after the fix. Do NOT report bugged metrics as results.

    a) Tests pass:
    exec bash pty:true workdir:<repo> command:"uv run pytest -q --tb=short --ignore=tests/integration 2>&1 | tail -10"

    b) Notebook exists and executes:
    exec bash pty:true workdir:<repo> command:"ls notebooks/experiments/<strategy_name>.ipynb && uv run jupyter nbconvert --execute --inplace --ExecutePreprocessor.timeout=300 notebooks/experiments/<strategy_name>.ipynb 2>&1 | tail -20"
    If the notebook doesn't exist → CRASH.
    If the notebook fails to execute → CRASH (attempt fix, max 3 tries).

    c) Extract metrics from notebook output:
    exec bash workdir:<repo> command:"python3 -c \"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])) if nbs else {}; [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\" 2>&1 | tail -30"

    Extract: IS walk-forward Sharpe (the primary metric), OOS Sharpe, max drawdown, win rate, trade count, OOS accuracy, OOS trading days, feature importances.
    If the strategy can't be backtested yet (missing data, import errors), treat as CRASH.

  Phase 4.5 — ADVERSARIAL REVIEW (Copilot reviewer — isolated, skeptical)
    **Every experiment gets reviewed by a dedicated adversarial agent before any keep/discard decision.**
    This catches statistical issues that threshold checks miss: inflated OOS on short periods,
    feature importance anomalies, OOS >> IS inversions, leakage patterns, holding period mismatches.

    Delegate to Copilot reviewer agent:

    exec bash pty:true workdir:<repo> background:true command:"copilot --agent reviewer -p \"
      REVIEW EXPERIMENT: <strategy_name>

      Read the experiment-data skill: .github/skills/experiment-data/SKILL.md
      Read the reviewer agent instructions: .github/agents/reviewer.agent.md

      Notebook: notebooks/experiments/<strategy_name>.ipynb
      Module code: src/quantipy/alpha/<strategy_dir>/
      Tests: tests/unit/test_<strategy_name>*.py

      Metrics from Phase 4 extraction:
      - IS walk-forward Sharpe (net): <value>
      - IS bootstrap CI: [<low>, <high>]
      - OOS Sharpe (net): <value>
      - OOS trading days: <value>
      - Trades/day IS: <value>
      - Trades/day OOS: <value>
      - Feature importances: <list top 5 with values>
      - Null test results: <pass/fail summary>

      Run ALL 8 checks from your review protocol.
      Read the actual source code, not just notebook output.
      Output the structured review with verdict and recommended action.
    \" --yolo --model claude-opus-4.6 --no-auto-update"

    Wait for completion via process monitor notification.
    Parse the reviewer's output. Extract:
    - Verdict: PASS / CONDITIONAL PASS / FAIL
    - Recommended decision metric (usually IS walk-forward Sharpe)
    - Issues found (with severity)
    - Recommended action

    **The reviewer's recommended metric overrides raw OOS claims.**
    If reviewer says "use IS Sharpe 2.13, not OOS 5.90" → Phase 5 uses 2.13.

  Phase 5 — DECIDE (against thresholds — autonomous, no human input)
    **You make this decision. Not the human.**
    **Use the reviewer's recommended decision metric, NOT raw OOS.**
    If the reviewer was not available or crashed, use IS walk-forward Sharpe (net).

    Hard thresholds for quant strategies (applied to the decision metric):
    - Tests pass? If no → CRASH (attempt fix, max 3 tries, then revert)
    - Decision Sharpe > -0.5? If no → DISCARD (too bad to keep)
    - Decision Sharpe > SMA baseline? If yes → KEEP (improvement)
    - Decision Sharpe > 0.5? → SIGNIFICANT KEEP (flag as promising)
    - Decision Sharpe > 1.0? → STRONG KEEP (prioritize for further optimization)
    - Max drawdown (IS) < 30%? If no → DISCARD regardless of Sharpe
    - Reviewer verdict FAIL with CRITICAL issues? → BUG FIX first (delegate to Copilot, re-review)

    Decision:
    - KEEP / SIGNIFICANT KEEP → keep commit, record metrics + reviewer verdict, mark as "implemented" in RESEARCH_LOG.md
    - DISCARD → git revert HEAD, record why + reviewer issues, consider: can features be improved?
      If the model architecture is sound but features are weak, try ONE feature iteration before moving on.
    - CRASH → attempt fix (max 3 tries), else revert. Mark as "crashed" in RESEARCH_LOG.md.
    - BUG FIX → delegate fix to Copilot orchestrator, then re-run Phase 4 + 4.5.

    After DISCARD with decent architecture (Decision Sharpe > -1.0):
    - Try one feature engineering iteration: add/remove features, retrain, retest
    - If still DISCARD after feature iteration → move to next proposal

  Phase 6 — LOG
    - Append to experiments.jsonl (see Results Logging below)
    - Update RESEARCH_LOG.md: mark strategy status (implemented/discarded/crashed) with metrics
    - Write to memory/YYYY-MM-DD.md: strategy name, outcome, Sharpe, what worked/failed
    - If SIGNIFICANT KEEP or STRONG KEEP → update MEMORY.md with the strategy as a milestone
    - Write to knowledge graph (MANDATORY):
      mempalace_add_drawer(
        wing: "wing_quantipy",
        room: "room_<experiment_id>",
        content: "<experiment name> used <features> with <model> on <tickers> <timeframe>.
          IS Sharpe: <value>. OOS Sharpe: <value>. Reviewer: <PASS/FAIL>. Decision: <KEEP/DISCARD>.
          Failure modes: <any encountered>. Key insight: <what was learned>."
      )
      Then add structured KG triples:
      mempalace_kg_add(subject: "<experiment_name>", predicate: "achieved_is_sharpe", object: "<value>", valid_from: "<date>")
      mempalace_kg_add(subject: "<experiment_name>", predicate: "used_feature", object: "<feature>", valid_from: "<date>")
      mempalace_kg_add(subject: "<experiment_name>", predicate: "used_model", object: "<model>", valid_from: "<date>")
      mempalace_kg_add(subject: "<experiment_name>", predicate: "decision", object: "<KEEP/DISCARD>", valid_from: "<date>")

  Phase 7 — REFLECT (after every 3 implementations or end of research round)
    This phase runs after implementing 3 strategies from a round OR when all proposals are done.

    a0) MemPalace meta-analysis — before reading logs:
       - mempalace_search for all experiment results → build success/failure panorama
       - mempalace_kg_query for FailureMode entities → identify recurring anti-patterns
       - mempalace_kg_query for Feature entities used in successful experiments → identify winning features
       - mempalace_search for cross-experiment relationships (which features + models co-occur in KEEPs?)

    a) Pattern analysis — read RESEARCH_LOG.md and extract (augmented with graph results):
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

    f) Write MemPalace session diary:
       mempalace_diary_write(
         agent_name: "autoresearch",
         entry: "Reflection after N implementations. Success rate: X/N kept. Best features: <list>. Best model: <type>. Key insights: <what was learned>. Next direction: <what to try>.",
         topic: "autoresearch-session"
       )

  Phase 8 — CONTINUE (autonomous progression — NEVER STOP)
    **THIS IS THE MOST IMPORTANT PHASE. YOU MUST EXECUTE IT. NEVER SKIP IT.**
    Do NOT stop. Do NOT ask "should I continue?" Do NOT wait for the human.
    **The loop NEVER terminates on its own.** Even after finding a STRONG KEEP strategy,
    continue exploring for orthogonal strategies to build a robust portfolio.
    Only the human saying "stop" halts the loop.

    Posting [TASK:complete] after evaluation of a single experiment is WRONG — that means
    the individual task finished, not the autonomous loop.
    Posting GOAL MET based on raw OOS Sharpe without reviewer validation is WRONG.

    Decision tree (you decide, not the human):
    a) Current strategy had a BACKTESTING BUG (sanity check triggered)?
       → Delegate bug fix to Copilot orchestrator → re-evaluate. This does NOT count as an iteration.
    b) Current strategy was KEEP and Sharpe < 1.0?
       → Try optimizing: feature iteration, hyperparameter tuning, ensemble with prior keeps
    c) Current strategy was STRONG KEEP (IS walk-forward Sharpe > 1.0, reviewer PASS)?
       → Log as "significant alpha candidate" in RESEARCH_LOG.md and memory.
       → Post a brief status update: "[PORTFOLIO] <strategy> added — IS Sharpe X.XX (reviewer PASS)"
       → Then CONTINUE to next proposal. Explore orthogonal strategies (different signal families,
         different holding periods, different asset pairs) to diversify the portfolio.
    d) Current strategy was DISCARD?
       → Move to next ranked proposal from RESEARCH_LOG.md
    e) All proposals from current research round implemented?
       → Run Phase 7 REFLECT first, then Phase 2 for new ideation with updated context
       → Include reflection insights + current portfolio composition in the next researcher prompt
       → Explicitly ask the researcher to propose strategies ORTHOGONAL to existing keeps
         (different signal families, uncorrelated return streams)

    **Portfolio-aware ideation:** When launching Phase 2, include the list of all KEPT strategies
    with their signal type, holding period, and correlation profile. Ask for strategies that
    would ADD diversification, not duplicate existing alpha sources.

    **After deciding, IMMEDIATELY launch the next Copilot process.**
    The loop continues until the human explicitly says "stop."
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

## Loop Recovery (Preventing Stalls)

The most dangerous failure mode is NOT a crash — it's a silent stall where no Copilot is running. The loop just stops and nobody notices.

**The gateway's process monitor handles this automatically.** It polls Copilot sessions every 30 seconds and sends `[TASK:complete]` or `[TASK:failed]` to your session when a process exits.

### On Reconnect / Session Resume
When the human connects (or you receive a `connected` frame), run a health check:
1. `pgrep -fa 'copilot.*-p' || echo NO_COPILOT` — any Copilots running?
2. If NO_COPILOT and autoresearch was active → **loop stalled. Re-enter at Phase 1.**
3. If Copilot IS running → the gateway monitor will notify you when it exits. Wait.

### Never Silently Acknowledge Staleness
When you see a `[TASK:*]` message, you MUST act:
- Evaluate the results (Phase 4)
- Continue the loop (Phase 8)
- **Never respond "noted" and stop.** That's how loops die.

## Execution Model

**This is a behavioral mode, NOT a separate agent.** When activated, you (the default agent) follow this protocol.

### Delegation
**Only Phase 2 and Phase 3 delegate to Copilot CLI.** Everything else is YOUR work:
- Phase 2 (IDEATE) → delegate to Copilot `--agent researcher` in background
- Phase 3 (IMPLEMENT) → delegate to Copilot `--agent orchestrator` in background
- Phase 4-5 (VERIFY + DECIDE) → YOU run exec commands directly
- Phase 6-7 (LOG + REFLECT) → YOU write to files and memory directly
- Phase 8 (CONTINUE) → YOU decide next action and launch the appropriate Copilot agent

**The entire Phase 4 → Phase 8 sequence runs in ONE turn** after the gateway's process monitor reports completion.

### Self-Continuation via Gateway Process Monitor

**Read the `copilot-cli` skill** for the full launch sequence. Key points:
- The gateway automatically detects Copilot processes working on target repos
- When a process exits, the gateway sends `[TASK:complete]` or `[TASK:failed]` to your session
- Includes git log, notebook sanity check output, and dirty-tree detection
- On `[TASK:complete]`, continue the autoresearch loop (Phase 4+) in YOUR turn
- On `[TASK:failed]` (dirty tree), check uncommitted changes then evaluate

### Status Reporting
- Post `[TASK:running] autoresearch iteration N` after each launch
- Post `[TASK:complete] autoresearch — N iterations, metric: X → Y` when goal met
- Every 10 iterations, post a progress summary (see Results Logging)
- Write significant findings to MEMORY.md via `memory_search` context
