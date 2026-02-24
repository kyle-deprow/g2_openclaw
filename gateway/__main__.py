"""Allow running with ``python -m gateway``.

Subcommands
-----------
- ``python -m gateway``             → start the WebSocket server (default)
- ``python -m gateway init-env``    → generate ``.env`` from system detection
- ``python -m gateway init-env --force`` → overwrite existing ``.env``
"""

import sys


def _run_server() -> None:
    """Start the gateway WebSocket server (hot path — no typer import)."""
    import asyncio

    from gateway.server import main

    asyncio.run(main())


def _run_cli() -> None:
    """Dispatch to the typer CLI app (only imported when needed).

    Strips the subcommand name (e.g. ``init-env``) from ``sys.argv`` so typer
    receives only the flags (``--force``, etc.).
    """
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # drop the subcommand
    from gateway.cli import app

    app()


if __name__ == "__main__" or not sys.argv[0].endswith("__main__.py"):
    pass  # allow import without side-effects

# Route based on first positional arg
_cli_commands = {"init-env"}
if len(sys.argv) > 1 and sys.argv[1] in _cli_commands:
    _run_cli()
else:
    _run_server()
