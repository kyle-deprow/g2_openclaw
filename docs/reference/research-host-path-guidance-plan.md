# Deployed research host path guidance

## Objective and scope

Prevent repeated caller-side contract mistakes before the next hypothesis.
Docs-only change at baseline c54b7e40f9b47fc2924deedc4b29d1f051de85a5.
No code, security, review schema, models, auth, hypothesis, or evidence changes.
You are not alone: preserve all other edits and prior immutable records.

Worker owns only:
- gateway/agent_config/research-orchestrator/TOOLS.md
- gateway/agent_config/skills/research-loop/SKILL.md
- .claude/skills/research-loop/SKILL.md

This plan is read-only and may be committed unchanged. No other source edits,
live files, configuration deployment, services, RPCs, database writes, new
dependencies, or downloads. Root/broker own reviewed deployment separately.

## Required guidance

1. Document this installation's exact review collector paths consistently:
   --core-database /home/dev/.openclaw/state/openclaw.sqlite
   --acpx-sessions /home/dev/.openclaw/workspace/state/sessions
   --claude-projects /home/dev/.claude/projects
   These are verified local paths, not portable upstream defaults. Do not guess
   ~/.acpx/sessions or infer missing environment variables. Replace placeholders
   in the existing TOOLS example. If correlation remains unavailable, preserve
   the refusal; never search arbitrary alternative records or repair a verdict.
2. State actual analysis-stage mount contract in active skill and concise mirror:
   --scenarios /scenarios --inputs /inputs --out /stage/analysis;
   cost specs /inputs/evaluation-specs/{spec_id}.json, NOT /evaluation-specs.
   Frozen future specs, RunPlans, synthetic fixtures and reviewed analysis must
   agree with this host contract. Do not rewrite already frozen hypotheses.
3. State canonical EvaluationSpecSet and RunPlan themselves are host-attested,
   not automatically mounted analysis inputs. No invented mounts/args or host
   path leakage. Retain existing scenario-specific target-output guidance.
4. Keep edits compact, no new framework/role/policy/fallback. Preserve models,
   three-attempt rule, strict review, no-repair runner and scientific boundaries.

## Sequence and verification

Read AGENTS and applicable OpenClaw tools/skills instructions, then only the
owned files and relevant authoritative containment.py/worker.py contract lines.
Apply minimal docs edits, inspect diff, run git diff --check and verify the
three exact collector paths and stage mounts agree across owned guidance.
Do not run pytest for a prose-only change; no concurrent test suite.
Commit owned files plus unchanged plan; report actual checks, commit, diffSHA,
changedfiles, risks and DONE. Independent native Luna and actual Opus review
precede root ff-only integration and guarded config push+gateway restart.
