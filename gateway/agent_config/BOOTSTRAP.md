# Bootstrap — Environment Context

## User Environment

- `~/repos/` is the standard project directory
- `~/repos/ai_scaffolding/` contains reusable agent and skill templates for new projects
- The user wears G2 AR smart glasses — responses via this channel should be concise
- Tools available: `exec` (shell), `copilot` (code delegation), `copilot_sessions` (session management)
- File tools available: `Read`, `Write`, `Glob`, `Grep`

## ai_scaffolding Contents

Agents (in `~/repos/ai_scaffolding/agents/`):
- `orchestrator.agent.md` — Required for all projects. Coordinates specialist agents.
- `react-best-practices.agent.md` — React/Next.js specialist
- `backend-python.agent.md` — Python backend specialist
- `composition-patterns.agent.md` — Component architecture specialist
- `react-native-skills.agent.md` — React Native specialist

Skills (in `~/repos/ai_scaffolding/skills/`):
- `react-best-practices/` — React patterns, hooks, performance
- `backend-python/` — Python TDD, typed dataclasses, clean architecture
- `composition-patterns/` — Component structure, compound patterns
- Plus 20+ more domain-specific skills (G2, OpenClaw, Azure, etc.)

## Workflow

1. User sends a request → you scaffold the workspace
2. You delegate to Copilot → Copilot's orchestrator handles the rest
3. You present results → user approves or asks for changes
