### Runtime mechanics: codex

Children are native Codex subagents, never OpenClaw sessions. The owner writes
the implementer and runner spawns (`agent_type` `implementer` or
`experiment_runner`). The reviewer spawn comes from `review-reserve --openclaw-database
/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`:
`spawn_arguments` contains only `task_name`, `message`, `agent_type`
(`reviewer`, read-only), and `fork_turns` (`none`), with no `cwd` argument;
`message` contains the immutable absolute bundle path. Invoke exactly the
returned native `collaboration.spawn_agent` arguments. Reserve binds the wake
run as active owner writer; reconcile binds the thread from the flushed official
event. The completion callback reconciles from the official child-thread ACK.

Later reconcile/collect/cancel use the same managed-root database paths
`/home/dev/.openclaw/agents/research-orchestrator/agent/openclaw-agent.sqlite`
and
`/home/dev/.openclaw/agents/research-orchestrator/agent/codex-home/state_5.sqlite`,
passing the latter as `--codex-state-database` to review-reconcile,
review-collect, and review-cancel; the CLI derives them from the managed
OpenClaw root containing `--root` and rejects any other path.

The spawn message is encrypted on the host, so `prompt_sha256` is
reservation-side only and is never host-verified; binding rests on the task
name, host role/model/effort, and the strict verdict. Collect trusts the child's
official terminal rollout; an `announce:codex-native` callback is optional
corroboration that must agree. Keep the requested `fast` service tier separate
from the observed `unknown` one. After the owner yields, `chat.abort` cannot
stop a running reviewer (`owner_run_inactive_no_supported_child_cancel`).
