# Tools

## Allowed use

- Read existing research status/control surfaces and their durable evidence.
- Use only the approved native implementer and experiment runner. Reserve the
  immutable review bundle before the one ACP Opus review, then verify its ACK,
  task identity, commit, spec digest, and verdict.
- Use the exact cancel operation once, after rereading the current task. Keep
  an unknown response pending and pause; do not repeat it or invent a result.
- Return every refusal and outcome to Astra through the owner wake. Main
  controls remain read-only.

## Command evidence

Use the installed CLI's live help to discover supported research operations.
Start with the top-level command:

`uv run gateway-cli research --help`

The top-level help enumerates subcommands. Before invoking one, run the
installed CLI's research <subcommand> --help form and use only its listed
flags. The historical frozen appendix is not an exhaustive current CLI
allowlist. Review collection through the existing review-collect operation
is authorized; consult its live help before using the exact invocation below.
Live help defines syntax only and grants no additional permission. Owner actions
remain limited to the operations authorized by this contract (including
review-collect); operator-only policy, ledger, and runtime-registration
mutations remain operator-only.
Do not invent flags, providers, routes, or command names. No command launches
work unless the operator policy and route are proven.

When collecting review evidence, use the required --core-database option with
/home/dev/.openclaw/state/openclaw.sqlite:

    gateway-cli research review-collect ATTEMPT_ID --root ROOT --core-database /home/dev/.openclaw/state/openclaw.sqlite --acpx-sessions /home/dev/.openclaw/workspace/state/sessions --claude-projects /home/dev/.claude/projects

That path is the owner environment's RESEARCH_CORE_DATABASE value; model
shells may not inherit the variable, so do not infer or substitute a database,
ACPX sessions, or Claude projects path. These are verified local paths, not
portable upstream defaults; if correlation remains unavailable, preserve the
refusal and never search arbitrary records or repair a verdict.
