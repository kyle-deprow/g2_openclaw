---
name: g2-display-ui
description: Design and verify G2 OpenClaw container layouts, transcript rendering, text upgrades, images, bounds, and display behavior.
---

# G2 display UI

Read the canonical `.agents/skills/g2-display-ui/SKILL.md` before non-trivial work.

- Use a 576×288 absolute-positioned, 4-bit monochrome container canvas; no CSS/flex/DOM rendering exists on the glasses.
- Locked SDK `0.0.11` allows 1-12 total containers: at most 8 text/list and 4 image containers.
- Use unique IDs and names (names at most 16 chars), exact `containerTotalNum`, in-bounds rectangles, and exactly one text/list event-capture target.
- Use `borderRadius`. SDK `0.0.11` has no `zOrderIndex`; declaration order controls overlap. All-or-none unique z-order is only for a future `0.0.12` migration.
- Text limits: 1,000 chars on startup/rebuild and 2,000 on upgrade. Prefer `textContainerUpgrade` for status, transcript, and footer.
- Lists support at most 20 items with 64-character labels and require rebuild to change contents.
- Images are 20-288×20-144; create placeholders first and queue raw-data updates sequentially.
- Preserve the idle-first status/transcript/footer layout and newest-first transcript unless product requirements change.
- Validate screenshots as RGBA, then verify fonts, greyscale, scrolling, image transfer, and flicker on hardware.

Repository implementation: `g2_app/src/display.ts`, `g2_app/src/conversation.ts`, and `g2_app/src/utils.ts`.
