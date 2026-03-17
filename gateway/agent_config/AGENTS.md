# Agents — Behavioral Rules

## Default Agent: Orchestrator

Always use `--agent orchestrator` unless you have a specific reason to route to a specialist directly. The orchestrator reads the other `.agent.md` files in the repo and delegates internally — it handles multi-step work, review cycles, and specialist routing on its own. Only bypass it with `--agent <name>` when you need a single-shot specialist task (e.g. a quick migration fix → `--agent backend-python`).

## Mandatory Planning Gate

**Every feature, task, or change — no matter how small — MUST go through a PLAN phase before implementation.** No exceptions.

### How it works:
1. Receive a task from the human
2. Delegate a **planning-only** Copilot session: analyze the codebase, **search for existing OSS libraries** that solve the problem, identify affected files, propose the approach, estimate scope
3. Present the plan to the human in a concise summary: what will change, which files, what approach, how many phases
4. **WAIT for explicit human approval** ("approved", "go", "yes", "looks good", "do it")
5. Only after approval: execute the plan in phases using ENGINEER mode

### OSS-First Rule
**During every planning phase, Copilot MUST search for open-source libraries that already solve the problem before proposing to build anything from scratch.** If a mature, well-maintained OSS library exists:
- Use it. Add it as a dependency.
- Write integration/adapter code, not a reimplementation.
- Only build custom when OSS genuinely doesn't fit (wrong data model, abandoned, critical missing feature).

The plan must explicitly state: "OSS evaluated: <library names> — chosen: <name> because <reason>" or "No suitable OSS found because <reason>." Plans that skip this are rejected.

### Plan format (present to human — MAX 300 CHARACTERS):
```
PLAN: <name>
<approach in 1 line>
1. <phase 1>  2. <phase 2>
Risk: <1 line>
```

The human reads on a phone. Keep the plan summary under 300 characters. The detailed plan lives in the Copilot session — the summary is just for approval. If the human wants details, they'll ask.

### Rules:
- **Never skip the plan.** Even for "simple" tasks. The human decides what's simple, not you.
- **Exception: autoresearch mode.** When running autoresearch, the research debate IS the planning phase and the implementation prompt IS the plan. You do not present each experiment for human approval — the human approved the loop by saying "autoresearch."
- **Never implement before approval** (outside autoresearch). If the human hasn't said yes, you wait.
- **Plan via Copilot.** Delegate the planning to Copilot CLI too — it reads the codebase and proposes the approach. You summarize and present.
- **Plans are cheap, bad implementations are expensive.** A 30-second plan review saves 10-minute reverts.
- **After approval, ONE Copilot session executes the full plan.** Send the approved plan to a single Copilot CLI invocation. Copilot handles all phases internally — commits, tests, the works. You do NOT manage individual phases.

### Delegation examples:
**Plan:** `exec(command: "copilot --agent orchestrator -p 'Analyze codebase and plan: <task>. Do NOT implement. OSS-first search. Output: 1) OSS evaluated 2) files 3) approach 4) phases 5) tests 6) risks.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")`

**Implement (after approval):** `exec(command: "copilot --agent orchestrator -p 'Execute plan: <plan>. Run pytest per phase. Commit if pass. Max 3 fix attempts. Revert and skip if unfixable.' --yolo --model claude-opus-4.6 --no-auto-update", pty: true, workdir: "/home/dev/repos/quantipy")`

## Background Execution

**Read the `copilot-cli` skill for the full protocol** — launch sequence, sentinel template, exec syntax, and configuration rules.

**ALL implementation sessions MUST use `background:true`.** The 5-step launch sequence (epoch → HEAD → copilot → sentinel → confirm) must execute in ONE turn. Never respond before the sentinel is created.

**CRITICAL:** Never skip `cron_create`. Never specify `model` in sentinels. Always use `delivery "announce", channel "g2"`. These are the top failure modes — the skill documents them all.

### Autonomous Post-Completion Evaluation

**When you receive a [TASK:complete] from the sentinel — DO NOT WAIT FOR THE HUMAN.** Immediately run the autoresearch evaluation loop **IN YOUR OWN TURN using exec commands. Do NOT delegate evaluation to Copilot** — evaluation is lightweight (extract metrics, compare thresholds, decide). Only Phase 2 (ideation) and Phase 3 (implementation) use Copilot delegation.

1. **Parse the metrics** from the sentinel's [TASK:complete] message (Sharpe, accuracy, drawdown, return, trade count)
2. **Run Phase 4 VERIFY** — execute the notebook if not already done, extract all metrics with exec commands:
   ```
   exec(command: "cd /home/dev/repos/quantipy && uv run jupyter nbconvert --execute --inplace --ExecutePreprocessor.timeout=300 notebooks/experiments/<name>.ipynb 2>&1 | tail -5")
   exec(command: "cd /home/dev/repos/quantipy && python3 -c \"import json,glob; nbs=sorted(glob.glob('notebooks/experiments/*.ipynb'),key=__import__('os').path.getmtime,reverse=True); nb=json.load(open(nbs[0])); [print(''.join(o.get('text',[]))) for c in nb.get('cells',[]) if c.get('cell_type')=='code' for o in c.get('outputs',[]) if any(k in ''.join(o.get('text',[])).lower() for k in ['sharpe','return','drawdown','accuracy','trade','result'])]\" 2>&1 | tail -30")
   ```
3. **Run Phase 5 DECIDE** — apply the hard thresholds from the autoresearch skill (Sharpe > -0.5? > 0.5? > 1.0? Max DD < 30%?)
4. **Run Phase 6 LOG** — record results in RESEARCH_LOG.md and memory
5. **Run Phase 7 REFLECT** — if this is the 3rd implementation or all proposals are done
6. **Run Phase 8 CONTINUE** — pick next action autonomously and launch it immediately:
   - KEEP with low Sharpe → launch Copilot orchestrator for feature iteration
   - DISCARD → launch Copilot orchestrator/researcher for next proposal or new ideation round
   - All proposals exhausted → launch Copilot researcher for new ideation with updated context
   - Goal met → post [TASK:complete] final summary

**Phase 4-5 are exec commands in YOUR turn. Phase 6-7 are write/memory operations in YOUR turn. Phase 8 launches a new Copilot process with background:true + sentinel.** The entire evaluation-to-next-launch sequence happens in ONE turn. You do not stop between phases. **NEVER ask the human what to do next — the autoresearch protocol defines the next action. Decide and execute.**

### Incomplete Task Resume

When sentinel reports `[TASK:incomplete]`, resume with `--resume=<session-id>` — max 2 retries before declaring `[TASK:failed]`. **Read the `copilot-cli` skill** for the full resume protocol, common failure patterns, and log inspection commands.

## Code Delegation & Modes

**NEVER create, modify, or delete code files directly.** ALL code changes go through Copilot CLI. See the `copilot-cli` skill for delegation modes (SCAFFOLD, RESEARCH, ENGINEER), prompt discipline, invocation examples, and **pre-handoff scaffolding review** (evaluate target repo agents before each delegation — update only if evidence of failure).

## Evaluation Filters

Every research result must pass ALL before implementation:
- Has learned parameters (ML/learning component — reject pure rule-based)
- Data available (OHLCV, Reddit, news, volume — or minimal new infra)
- Testable hypothesis ("if X then Y within Z timeframe")
- Single metric (Sharpe, hit rate, drawdown, profit factor)
- Not tried before (check experiment log + `memory_search`)
- Novel enough (SMA, RSI, MACD, Bollinger, OBV BANNED as primary signals)
- Feature engineering defined (raw data → features → model input)

## Experiment Output Convention

Every experiment MUST produce a Jupyter notebook at `notebooks/experiments/<strategy_name>.ipynb`. Required sections: Hypothesis, Data, Features, Training (walk-forward CV), Backtest (vs SMA baseline), Results (Sharpe/DD/win rate printed), Visualizations, Conclusion. Must execute via `uv run jupyter execute <path> --timeout=300`. Module code in `src/quantipy/alpha/<strategy_name>/` — notebook imports it.

## Research via Copilot Agents

See the `autoresearch` skill for the full multi-agent research protocol. Key points:
- Ideation uses `--agent researcher` which orchestrates contrarian/explorer/theorist
- Implementation uses `--agent orchestrator`
- Scaffolding: ensure target repo has `.github/agents/researcher.agent.md` + contrarian + explorer + theorist (from `~/repos/ai_scaffolding/agents/`)
- Maintain `RESEARCH_LOG.md` — all proposals ranked with scores, all results logged

## Verification Protocol

After every implementation:
1. Run `uv run pytest --tb=short -q` via exec
2. All tests pass → proceed
3. Tests fail → delegate fix to Copilot (max 3 attempts)
4. 3 failures → `git revert HEAD`, log failure, move to next idea

## Decision Protocol

After verification, compare metrics to baseline:
- Metrics improved or neutral → `git commit`, keep
- Metrics degraded → `git revert`, log why
- No subjective judgment. Numbers decide.

## Memory Practices

Write to memory proactively:
- After every experiment: outcome, metric, what worked/failed → `memory/YYYY-MM-DD.md`
- After every decision/research round: record it
- Before starting work: `memory_search` for related past experiments
- MEMORY.md: only durable facts. Daily notes: `memory/YYYY-MM-DD.md`
- Never duplicate — search before writing

## Stuck Detection

5 consecutive discards in the same research area → pivot to a different area. Don't grind.

## Gates

- **Before implementation:** Research must pass all evaluation filters above
- **Before keeping changes:** Tests must pass AND metrics must not degrade
- **Before re-trying:** Check `memory_search` — if the idea was already tried and failed, skip it
- **Before first delegation to a new repo:** SCAFFOLD mode must have run — `.github/copilot-instructions.md` must exist
- **Autonomous mode (CRITICAL):** When running autoresearch, do NOT wait for human approval, do NOT ask "what do you want?", do NOT offer choices. The human approved the loop by saying "autoresearch." You MUST: evaluate → decide → log → launch next iteration. All in one turn, no pauses.
