"""Autoresearch exception types."""

from __future__ import annotations


class AutoresearchError(ValueError):
    """Base error for deterministic autoresearch control-plane failures."""


class AutoresearchConfigError(AutoresearchError):
    """Raised when autoresearch runtime config deviates from policy."""


class AutoresearchReceiptError(AutoresearchError):
    """Raised when a required source receipt cannot be generated."""


class AutoresearchValidationError(AutoresearchError):
    """Raised when an artifact or state is invalid."""
