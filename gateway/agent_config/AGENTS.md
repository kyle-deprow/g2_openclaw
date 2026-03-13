# Agents — Operational Rules

## Orchestration Flow

Every coding task follows four steps. No exceptions.

### 1. SCAFFOLD

Set up the workspace using `exec` before calling Copilot:

```bash
mkdir -p <user-specified-path>
cd <user-specified-path> && git init
mkdir -p .github/agents .github/skills
```

Examine `~/repos/ai_scaffolding/` — glob agents/ and skills/, read descriptions, pick what fits.
Always include `orchestrator.agent.md`. Copy selected agents and skills to the target repo.

**Scaffolding MUST complete before the first `copilot()` call.** Agents and skills are discovered at session creation.

### 2. PLAN

Call `copilot()` with `workingDir` = the user's specified path. The prompt MUST:
- Include the user's requirements VERBATIM — every tech choice, API, directory, and constraint quoted directly from their words. Do not paraphrase or substitute.
- Include: "The directory already contains .github/agents/ and .github/skills/ with orchestrator config. Preserve these — initialize the project around them (e.g. use --force or init in-place). Do NOT delete or recreate the directory."

Read the result. Distill into a brief summary: phases, key decisions, risks.

**Stop. Wait for explicit approval before building.**

### 3. IMPLEMENT

Same Copilot session (do NOT create a new one). Send a single follow-up that:
- Includes the approved plan
- Quotes the user's original requirements again verbatim
- Reminds: "The directory contains .github/ scaffolding — preserve it. Initialize the project around it."
- Instructs: "Implement ALL phases end-to-end in one pass. For each phase: implement → review → fix. Do not advance until review passes. After all phases, run a final integration review across the entire codebase. Do NOT ask for confirmation, approval, or clarification — implement everything immediately without stopping."
- Do NOT break implementation into separate copilot() calls per phase. One call, all phases.
- Uses timeout 0 (no timeout) so the full build can complete.

### 4. REPORT

Summarize what was built, list key files, explain how to run it. Ask if the user wants changes.

## Gates

- **Before implementation:** Present plan, wait for "go." Silence ≠ approval.
- **During implementation:** No gates. Run all phases continuously without asking for approval between phases.
- **After completion:** Summarize, ask for adjustments.
- **On errors:** Stop. Report what happened. Don't retry silently.

## Handle Directly

Conversations, clarifications, plan summaries, agent/skill selection, memory queries.

## Delegate to Copilot

All code writing, review, testing, refactoring.
