# Review findings output contract clarification

## Objective and ownership

In the isolated G2 repair worktree based on f55193aeef99c0dc5fefc246e30c07e2f074f632,
clarify generated reviewer instructions so actual Opus emits the strict existing
verdict schema. H4-A001 returned object findings while the collector requires
nonempty string elements; prompt merely said findings. Preserve that immutable
closed attempt and strict enforcement. No lifecycle/schema expansion or salvage.

Worker owns only gateway/research/review_evidence.py and
tests/gateway/research/test_review_evidence.py. This plan is read-only and may be
committed unchanged. You are not alone; preserve all unrelated edits. No changes
to Quantipy, research methods, contracts.py, runtime skills/config, live state,
review artifacts, auth, services, models, or deployment.

## Requirements

- Change only the generated new-bundle reviewer instruction text and focused tests.
- Specify terminal output is exactly one bare JSON object with exactly verdict,
  attempt_id, commit, spec_sha256, findings keys; no markdown or surrounding prose.
- Verdict is PASS or FAIL. Include actual attempt/commit/spec binding values.
- findings is an array of nonempty strings, never objects/nested arrays/null.
  Severity, location and explanation can be encoded within each string. An empty
  array is permitted when there are no findings. Do not add a new verdict rule.
- Include a valid machine-parseable example with actual binding values, produced
  with json.dumps rather than manual escaping. Prefer compact addition to current
  instructions; no helpers/refactor unless genuinely needed.
- Keep collector behavior, strict JSON/provenance checks, bundle limits, hashes,
  immutable existing-bundle validation, and reservation/ACK lifecycle unchanged.
- Existing immutable bundles are never rewritten; future attempts get clarified
  instructions normally. Do not pretend the closed H4-A001 can be recollected.

## Sequence and verification

Read AGENTS.md and applicable backend Python/TDD rules. Add failing prompt-contract
test first, then minimal fix. Exercise example against current strict parser with
matching reservation fixture; retain/add rejection test for object-valued findings.
Run actual commands in this worktree using existing environment, no new deps:

    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/pytest tests/gateway/research/test_review_evidence.py -q
    /home/dev/repos/g2_openclaw/.venv/bin/ruff check gateway/research/review_evidence.py tests/gateway/research/test_review_evidence.py
    /home/dev/repos/g2_openclaw/.venv/bin/ruff format --check gateway/research/review_evidence.py tests/gateway/research/test_review_evidence.py
    PYTHONPATH=. /home/dev/repos/g2_openclaw/.venv/bin/mypy gateway/research/review_evidence.py
    git diff --check

No parallel pytest in same worktree. Commit owned two files plus unchanged plan.
Return exact commit/diff SHA, actual red/green/check results, risks, and DONE only
complete. Root/broker independent verification and actual Opus review precede
integration. Root owns supported owner resume/restart and infrastructure handoff.
