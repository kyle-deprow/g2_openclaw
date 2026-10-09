"""Focused contract checks for the route-neutral research persona docs."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONA_ROOT = REPO_ROOT / "gateway" / "agent_config" / "research-orchestrator"
RUNTIME_SKILL = REPO_ROOT / "gateway" / "agent_config" / "skills" / "research-loop" / "SKILL.md"
CLAUDE_MIRROR = REPO_ROOT / ".claude" / "skills" / "research-loop" / "SKILL.md"
FROZEN_HELP_TEXT = """Command: `uv run gateway-cli research --help`
Captured in `/home/dev/repos/g2_openclaw`, 2026-09-10.

                                                                                
 Usage: gateway-cli research [OPTIONS] COMMAND [ARGS]...                        
                                                                                
 Durable, bounded Quantipy research driver.                                     
                                                                                
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ init                                                                         │
│ hypothesis-create                                                            │
│ hypothesis-freeze                                                            │
│ attempt-open                                                                 │
│ implementation-submit                                                        │
│ review-submit                                                                │
│ run                                                                          │
│ reconcile                                                                    │
│ cancel                                                                       │
│ attempt-close                                                                │
│ hypothesis-decide                                                            │
│ pause                                                                        │
│ resume                                                                       │
│ status                                                                       │
│ serve                                                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
"""

BOOTSTRAP_FILES = tuple(
    PERSONA_ROOT / name for name in ("AGENTS.md", "SOUL.md", "TOOLS.md", "BOOTSTRAP.md")
)

RULE_STATEMENTS = (
    "Astra is the research owner",
    "Native Luna is the implementer and runner",
    "Native OpenAI/Codex Sol (`gpt-5.6-sol`, xhigh, fast) is reviewer-only",
    "OpenAI/Codex remains the provider; no fallback or route switching",
    "hypothesis equals iteration",
    "each attempt is code → review → run",
    "maximum three attempts",
    "FINISH, ABANDON, or PAUSE",
    "reserve review evidence before the one native Sol review",
    "Reserve and spawn exactly once",
    "After the normal native spawn ACK, STOP and call",
    "original owner turn to end",
    "review completion callback",
    "Never retype a verdict or respawn after an unknown result",
    "typed admission refusals",
    "policy-unset pause",
    "exhausted-attempt completion",
    "exact cancel",
    "owner wake",
    "read-only main controls",
    "price-panel-only capability",
    "ETF scope",
    "stocks are refused unless a hypothesis-bound EDGAR rule-D earnings snapshot "
    "and membership file are present",
    "results stay `exploratory_snapshot` (never a single-stock acceptance claim)",
    "unknown earnings fail closed",
    "trusted panel sessions",
    "immutable receipts, ledger, and evaluator bounds",
    "no alpha claim from smoke or synthetic data",
)


def _docs() -> tuple[Path, ...]:
    return (*BOOTSTRAP_FILES, RUNTIME_SKILL, CLAUDE_MIRROR)


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def test_bootstrap_contract_stays_under_word_budget() -> None:
    # Arrange
    text = " ".join(path.read_text(encoding="utf-8") for path in BOOTSTRAP_FILES)

    # Act
    words = re.findall(r"\S+", text)

    # Assert
    assert len(words) <= 1_450


def test_runtime_skill_stays_under_line_budget() -> None:
    # Arrange
    lines = RUNTIME_SKILL.read_text(encoding="utf-8").splitlines()

    # Act
    line_count = len(lines)

    # Assert
    assert line_count < 250


def test_runtime_and_claude_mirror_share_canonical_rule_statements() -> None:
    # Arrange
    runtime = _normalized(RUNTIME_SKILL.read_text(encoding="utf-8"))
    mirror = _normalized(CLAUDE_MIRROR.read_text(encoding="utf-8"))

    # Act
    missing = {
        statement: ("runtime" if statement not in runtime else "mirror")
        for statement in RULE_STATEMENTS
        if statement.casefold() not in runtime or statement.casefold() not in mirror
    }

    # Assert
    assert not missing, f"canonical statements missing from {missing}"


def test_new_contract_contains_no_obsolete_role_or_command_terms() -> None:
    # Arrange
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in _docs())
    forbidden = (
        "auto" + "research-",
        "super" + "visor",
        "final" + "izer",
        "re" + "play",
        "mem" + "palace-writer",
        "deb" + "ate",
        "fallback" + " command",
        "fallback" + " provider",
    )

    # Act
    found = tuple(term for term in forbidden if term in text)

    # Assert
    assert not found


def test_command_examples_are_members_of_frozen_help_appendix() -> None:
    # Arrange
    text = "\n".join(path.read_text(encoding="utf-8") for path in _docs())
    appendix = FROZEN_HELP_TEXT
    examples = re.findall(r"`([^`]*gateway-cli research[^`]*)`", text)

    # Act
    missing = tuple(example for example in examples if example not in appendix)

    # Assert
    assert not missing


def test_runtime_and_claude_mirror_state_the_sixty_session_hedge_boundary() -> None:
    # Arrange
    required = (
        "holding horizon above sixty sessions is refused",
        "declares `long_only: false`",
        "(no leverage)",
    )

    # Act
    problems = []
    for path in (RUNTIME_SKILL, CLAUDE_MIRROR):
        text = _normalized(path.read_text(encoding="utf-8"))
        problems += [f"{path} lacks {item!r}" for item in required if item not in text]
        if "above five sessions" in text:
            problems.append(f"{path} keeps the five-session limit")

    # Assert
    assert not problems
