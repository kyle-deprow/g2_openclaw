# Agents — Operational Rules

## Orchestration Loop

Every coding task follows this sequence. No exceptions.

1. **PLAN** — Call `copilot` with a planner persona. Get a phased plan with file paths, complexity, and dependencies.
2. **PRESENT** — Show the plan to the user. List phases, scope, and risks. Then stop. Wait for explicit approval before proceeding.
3. **IMPLEMENT** — For each approved phase: destroy the Copilot session, call `copilot` with an implementer persona. One phase per cycle.
4. **REVIEW** — After implementation: destroy session, call `copilot` with a reviewer persona. Get a structured verdict.
5. **FIX** — If review finds issues: destroy session, call `copilot` with a fixer persona. Address each finding.
6. **REPEAT** — Loop review → fix until the verdict is clean, then move to the next phase.
7. **REPORT** — After all phases complete, summarize what changed and ask if the user wants modifications.

## Human-in-the-Loop Gates

- **Before implementation:** Always present the full plan and wait. "Looks good" or equivalent = approval. Silence ≠ approval.
- **After all phases:** Summarize changes, ask if anything needs adjustment.
- **On unexpected errors:** Stop immediately. Report what happened and what you recommend before continuing.

## Session Management

- Each `workingDir` = one Copilot session with shared file context.
- Persona is locked at session creation. To switch persona, destroy the session first with `copilot_sessions(action: "destroy", ...)`.
- Copilot retains file reads, edits, and conversation within a session — don't repeat context unnecessarily.

## Handle Directly (no copilot)

- Conversational questions, clarifications, planning decisions
- Presenting plans and summaries to the user
- Memory queries and context recall
- Choosing which persona fits each step

## Delegate to Copilot

- All code writing, generation, and scaffolding
- Code review and security analysis
- Test writing and execution
- Multi-file refactoring and restructuring
