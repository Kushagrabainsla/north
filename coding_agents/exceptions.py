"""Errors raised by the coding-agent module. See docs/CODING_STYLE.md Section 13."""

from __future__ import annotations

from exceptions import NorthError


class CodingAgentError(NorthError):
    """Base class for every coding-agent failure."""


class BackendUnavailableError(CodingAgentError):
    """No installed coding agent can take the run: not installed, logged out, or too old."""
