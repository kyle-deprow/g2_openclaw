"""Autoresearch loop control plane (decomposition of gateway.autoresearch_runner).

The only allowed external runtime dependencies are
``gateway.autoresearch_platform_validation`` (a stdlib-only leaf),
``gateway.autoresearch_readiness`` (a leaf chain via the sanctioned
``gateway.autoresearch_panel_receipts`` stdlib-only leaf), and
``gateway.autoresearch_panel_receipts`` itself; all are verified never to
import ``gateway.autoresearch_runner`` or this package.
"""
