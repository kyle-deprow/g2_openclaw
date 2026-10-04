# Tools

## Allowed use

- Read existing research status/control surfaces and their durable evidence.
- Use only the approved native implementer and experiment runner plus the
  read-only native Sol reviewer. Reserve the immutable review bundle before the
  one native Sol review; reconcile the official child run only after the
  original owner turn ends, binding task identity, commit, spec digest, and
  verdict.
- Use the exact cancel operation once, after rereading the current task. Keep
  an unknown response pending and pause; do not repeat it or invent a result.
- Return every refusal and outcome to Astra through the owner wake. Main
  controls remain read-only.

## Command evidence

Use the installed CLI's live help to discover research operations.
Start with:

`uv run gateway-cli research --help`

The top-level help enumerates subcommands. Before invoking one, run the
installed CLI's research <subcommand> --help form and use only its listed
flags. The frozen appendix is not an exhaustive CLI
allowlist. Review collection through the existing review-collect operation
is authorized; consult its live help before using the invocation below.
Live help defines syntax only and grants no additional permission. Owner actions
remain limited to the operations authorized by this contract (including
review-collect); policy, ledger, runtime-registration,
run-release-unstarted, and run-reverify mutations remain operator-only.
Do not invent flags, providers, routes, or command names. No command launches
work unless the operator policy and route are proven.

Freeze sequence (`compute-probe` first, then `hypothesis-set-compute`, then freeze): see the
autoresearch skill.

Reserve with `review-reserve ATTEMPT_ID --root ROOT --bundle-dir BUNDLE_DIR --wake-key WAKE_KEY
--owner-key OWNER_KEY --openclaw-database
/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`.
The reservation output's
top-level `model`, `effort`, and `prompt_sha256` are authoritative. Its
`spawn_arguments` contains only `task_name`, `message`, `agent_type`, and
`fork_turns` (`none`); there is no `cwd` argument; `message` contains the
immutable absolute bundle path. Invoke exactly the returned native
`collaboration.spawn_agent` arguments. Reserve binds the wake run as active owner writer; reconcile binds the thread from the flushed official event. Reserve
and spawn exactly once in this owner turn. After the normal native spawn ACK,
STOP and call `sessions_yield` for the owned completion. Do not reconcile or
collect in this owner turn: exact owner run/thread correlation requires the
original owner turn to end. The completion callback after that turn ends may
reconcile and collect exactly once from the official child-thread ACK; it must
never respawn. Those later reconcile/collect/cancel operations use these same
managed-root database paths
`/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`
and
`/home/dev/.openclaw/agents/research-orchestrator/agent/codex-home/state_5.sqlite`,
and pass the latter as `--codex-state-database` to review-reconcile,
review-collect, and review-cancel. The CLI derives these paths from the managed OpenClaw root that contains `--root` (the wake text renders them concretely) and rejects any other path. `prompt_sha256` is
reservation-side only: the host spawn message is encrypted, so binding rests on
the exact task name, host role/model/effort, and the strict verdict. Collect trusts the child's rollout; an `announce:codex-native` callback is optional but must agree. Unfinished stays pending, never FAIL. A failed, errored, or aborted reviewer child makes the attempt a terminal REVIEW_FAILED with the host reason as the sole finding, with no supersession or re-review; it is distinct from a reviewer FAIL verdict but final for the attempt. Limitation:
after the owner yields, `chat.abort` cannot stop a running reviewer
(`owner_run_inactive_no_supported_child_cancel`); it ends on its own terminal
evidence. Do not use the retired `--core-database`, ACPX, or Claude-project
flags, infer paths, search arbitrary records, or repair a verdict. If
correlation remains unavailable, preserve the refusal.
