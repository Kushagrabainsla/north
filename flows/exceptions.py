"""Errors raised by the flow registry."""

from __future__ import annotations


class FlowError(Exception):
    """Base class for flow errors."""


class FlowNotFoundError(FlowError):
    """Raised when a requested flow is not registered."""


class FlowParseError(FlowError):
    """Raised when a flow document cannot be parsed."""
