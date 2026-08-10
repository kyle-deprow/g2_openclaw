---
name: g2-simulator-automation
description: Use EvenHub Simulator 0.8.0 native HTTP automation for G2 OpenClaw screenshots, console logs, and injected glasses input.
---

# G2 official simulator automation

Read the canonical `.agents/skills/g2-simulator-automation/SKILL.md` before non-trivial work.

- Start Vite with `npm run dev:sim`, then launch simulator `0.8.0` with `--automation-port 9898`.
- Keep the control plane on `127.0.0.1`; supervise exact child processes or use `make stop`.
- Poll console without `since_id` first, then use the latest non-negative ID until `[Display] _createStartup complete` appears.
- Native endpoints provide ping, RGBA glasses/WebView screenshots, incremental console logs, and click/double-click/up/down input.
- Input before the event-capture container exists is dropped.
- Detect lit pixels through screenshot alpha, compare semantic regions, and retain before/after frames plus error logs.
- Use `g2-sim-automation` alongside this skill for exact app state, conversation, and gateway control.
- The simulator emits 100 ms/3,200-byte PCM and fixed right-glasses input; remain chunk-agnostic and test real sources, permissions, BLE, lifecycle, and release behavior on hardware.
