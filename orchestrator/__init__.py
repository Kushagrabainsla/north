"""Orchestrator package — public interface.

See docs/CODING_STYLE.md Section 7.2.
"""

from __future__ import annotations

from orchestrator.classifier import IntentClassifier
from orchestrator.exceptions import (
    ClassifierError,
    NorthStarConflictError,
    OrchestratorError,
    RoutingError,
)
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import ExecutionPlan, IntentClassification, TaskRequest, TaskResponse
from orchestrator.north_star import NorthStarChecker
from orchestrator.orchestrator import Orchestrator
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.task_context import TaskContextStore

__all__ = [
    "ClassifierError",
    "ExecutionPlan",
    "ExecutionPlanner",
    "EventStreamManager",
    "FailureHandler",
    "IntentClassification",
    "IntentClassifier",
    "NorthStarChecker",
    "NorthStarConflictError",
    "Orchestrator",
    "OrchestratorError",
    "RoutingError",
    "TaskContextStore",
    "TaskRequest",
    "TaskResponse",
]
