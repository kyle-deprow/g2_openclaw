"""Allow running with ``python -m gateway``."""

import asyncio

from gateway.server import main

asyncio.run(main())
