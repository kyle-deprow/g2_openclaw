"""Allow running with ``python -m gateway``.

Subcommands
-----------
- ``python -m gateway``             → start the WebSocket server (default)
- ``python -m gateway init-env``    → generate ``.env`` from system detection
- ``python -m gateway init-env --force`` → overwrite existing ``.env``
- ``python -m gateway stop``        → stop all G2 OpenClaw processes
"""

import sys


def _run_server() -> None:
    """Start the gateway WebSocket server (hot path — no typer import)."""
    import asyncio
    import os

    # Handle --local-audio flag before importing the server (sets env var for config)
    if "--local-audio" in sys.argv:
        os.environ["G2_LOCAL_AUDIO"] = "true"
        sys.argv.remove("--local-audio")

    # Handle --test-wav PATH flag (inject WAV file for pipeline testing)
    if "--test-wav" in sys.argv:
        idx = sys.argv.index("--test-wav")
        if idx + 1 < len(sys.argv):
            os.environ["G2_TEST_WAV"] = sys.argv[idx + 1]
            del sys.argv[idx : idx + 2]
        else:
            print("Error: --test-wav requires a file path argument", file=sys.stderr)
            sys.exit(1)

    from gateway.server import main

    asyncio.run(main())


def _run_cli() -> None:
    """Dispatch to the typer CLI app (only imported when needed).

    Keeps the subcommand name so typer can route to the correct command.
    """
    from gateway.cli import app

    app()


if __name__ == "__main__" or not sys.argv[0].endswith("__main__.py"):
    pass  # allow import without side-effects

# Route based on first positional arg
_cli_commands = {"init-env", "launch", "push-config", "stop"}
if len(sys.argv) > 1 and sys.argv[1] in _cli_commands:
    _run_cli()
else:
    _run_server()
