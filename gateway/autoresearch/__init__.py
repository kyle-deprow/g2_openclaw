"""Autoresearch loop control plane (decomposition of gateway.autoresearch_runner).

The only allowed external runtime dependencies are
``gateway.autoresearch_platform_validation`` (a stdlib-only leaf) and
``gateway.autoresearch_readiness`` (a leaf chain via
``gateway.autoresearch_panel_receipts``); both are verified never to import
``gateway.autoresearch_runner`` or this package.
"""
