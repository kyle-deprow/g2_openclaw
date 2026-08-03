"""Autoresearch loop control-plane package.

The only allowed external runtime dependencies are
``gateway.autoresearch_platform_validation`` (a stdlib-only leaf),
``gateway.autoresearch_readiness`` (a leaf chain via the sanctioned
``gateway.autoresearch_panel_receipts`` stdlib-only leaf), and
``gateway.autoresearch_panel_receipts`` itself. The fourth sanctioned leaf is
``gateway.mempalace_finalizer``; all are verified never to import the
monolithic control-plane module or this package.
"""
