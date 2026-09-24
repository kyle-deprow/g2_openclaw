# H — completion ownership across owner turns

Observed failure: a native Luna follow-up completed in its existing thread and its completion reached the owner rollout, but no new owner turn started. The preceding `sessions_yield` explicitly reported no pending completion owned by that turn. Source work was preserved; this is a dispatch-ownership gap, not lost output or grounds for replay/provider fallback.

Luna owns exactly four existing Markdown files on main:

- `gateway/agent_config/skills/research-loop/SKILL.md`
- `.claude/skills/research-loop/SKILL.md`
- `gateway/agent_config/skills/codex-subagents/SKILL.md`
- `gateway/agent_config/skills/autoresearch/SKILL.md`

Add only a compact rule in the relevant dispatch section: a new owner turn needing a completion-required coding or runner stage spawns a fresh configured native child bound to the same hypothesis/attempt/worktree/checkpoint. A new child is not a new admission/attempt and does not authorize replay. Do not reactivate a child completed in a prior owner turn via `followup_task` and assume its completion is owned by the new turn. Preserve communication/followups to a live child already owned by the current turn; do not ban all followups. If `sessions_yield` reports no owned pending completion, report the exact durable checkpoint, do not claim an owned wait/autowake, and do not fabricate callbacks, replay, restart, or switch routes/providers.

No persona/config/provider/runtime/script/test-file changes or new abstraction. Preserve all unrelated work. Workers do not commit, deploy, or mutate live state. Root owns those actions at an idle checkpoint.

Validation: existing `tests/gateway/test_agent_config_docs.py` and `tests/gateway/test_research_persona_docs.py`, skill-creator quick validation of all four directories, and diff check. Do not add wording/regex-only tests. Sol checks scope and mirror consistency. A fresh independent evaluator receives only the updated skill and neutral snapshots for prior-turn terminal child, current-turn live owned child, and the no-owned-pending yield error, then reports its next action without executing it. Confirm actual next fresh-child callback in the live loop as operational evidence; do not claim a runtime code defect was patched.

Sol round-one finding: distinguish forbidding fabricated claims from forbidding actual duplicate/replay/restart/provider actions. Make explicit that a fresh child does not authorize replay and a no-owned-pending error grants no permission for those actions in that turn. Do not block later explicitly authorized bounded new work or current-owned live-child communication. Add blank-line separation around the mirror's inserted list item. The fresh scenario evaluator otherwise selected the intended actions in all three cases; repeat focused regressions after this wording correction.
