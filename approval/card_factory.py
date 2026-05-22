"""CardFactory — builds the correct Card type from an AgentResult.

See docs/CODING_STYLE.md Sections 3 (Factory pattern), 6.1, 9.7.
"""

from __future__ import annotations

from agents.models import AgentResult
from approval.models import Card, CardType
from utils.ids import generate_id


class CardFactory:
    """Determines and constructs the appropriate Card for a completed AgentResult.

    Decision rules (in priority order):
    1. Agent has a question  → QUESTION card
    2. Agent requires approval → APPROVAL card
    3. Otherwise             → INFORMATION card
    """

    @staticmethod
    def from_agent_result(
        task_id: str,
        agent_name: str,
        result: AgentResult,
    ) -> Card:
        """Build a Card from an agent's completed result.

        Args:
            task_id:    The task this result belongs to.
            agent_name: The name of the producing agent.
            result:     The AgentResult to convert.

        Returns:
            A Card ready to be sent via a Notifier.
        """
        if result.has_question:
            return Card(
                id=generate_id(),
                type=CardType.QUESTION,
                task_id=task_id,
                agent=agent_name,
                title=f"{agent_name.capitalize()} — Question",
                message=result.question or result.output,
                options=result.question_options,
            )

        if result.requires_approval:
            return Card(
                id=generate_id(),
                type=CardType.APPROVAL,
                task_id=task_id,
                agent=agent_name,
                title=f"{agent_name.capitalize()} — Approval Required",
                message=result.output,
                options=["Approve", "Reject"],
            )

        return Card(
            id=generate_id(),
            type=CardType.INFORMATION,
            task_id=task_id,
            agent=agent_name,
            title=f"{agent_name.capitalize()} — Completed",
            message=result.summary,
        )

    @staticmethod
    def north_star_conflict_card(task_id: str, tension: str) -> Card:
        """Build an APPROVAL card for a North Star goal conflict.

        Args:
            task_id: The task that triggered the conflict.
            tension: The human-readable conflict explanation.

        Returns:
            A Card presenting the tension and asking user to proceed or cancel.
        """
        return Card(
            id=generate_id(),
            type=CardType.APPROVAL,
            task_id=task_id,
            agent="orchestrator",
            title="North Star Conflict Detected",
            message=tension,
            options=["Proceed anyway", "Cancel"],
        )
