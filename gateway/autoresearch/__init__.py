"""Autoresearch loop control-plane package.

The only allowed external runtime dependencies are
``gateway.autoresearch_platform_validation`` (a stdlib-only leaf),
``gateway.autoresearch_readiness`` (a leaf chain via the sanctioned
``gateway.autoresearch_panel_receipts`` stdlib-only leaf), and
``gateway.autoresearch_panel_receipts`` itself. The fourth sanctioned leaf is
``gateway.mempalace_finalizer``. The fifth sanctioned leaf is
``gateway.autoresearch_runs`` (stdlib plus ``gateway.autoresearch.enums`` only);
the sixth sanctioned leaf is ``gateway.autoresearch_decision_receipts`` (stdlib
plus package-native constants, errors, enums, state, and transitions only).
The seventh sanctioned leaf is ``gateway.autoresearch_systemd`` (stdlib
``subprocess`` and ``collections.abc`` only).
The first four are verified never to import the monolithic control-plane
module or this package, while ``gateway.autoresearch_runs`` is verified never
to import the monolithic control-plane module and imports only
``gateway.autoresearch.enums`` from this package. The sixth leaf is verified
not to import the monolithic control-plane module.
"""
