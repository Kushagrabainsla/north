"""Pure text rendering and slash-input parsing for the terminal UI."""

from __future__ import annotations


def estimated_tokens(text: str) -> int:
    """Approximate the session meter's token count at four characters per token."""
    return max(1, len(text) // 4)


def slash_argument(text: str) -> str | None:
    """Return a slash command's first argument, or ``None`` when given bare."""
    parts = text.split()
    return parts[1] if len(parts) > 1 else None


def requested_context_document(text: str) -> str:
    """Return the document named by ``/context <doc>`` or ``/context show <doc>``."""
    parts = text.split()
    if len(parts) == 2 and parts[1] not in ("show", "edit"):
        return parts[1].removesuffix(".md")
    if len(parts) >= 3 and parts[1] == "show":
        return parts[2].removesuffix(".md")
    return ""


def decision_lines(card: dict) -> list[str]:
    """One decided card as the TUI shows it: who decided, why, and the memory it used.

    Reads the same fields the approvals page does, labels included, so the two
    never word a decision differently.
    """
    from rich.markup import escape

    chosen = (
        f' "{escape(card["chosen_option"])}"' if card.get("type") == "question" and card.get("chosen_option") else ""
    )
    who = (
        f"  [bright_black]by {escape(card['decided_by_label'])}[/bright_black]" if card.get("decided_by_label") else ""
    )
    lines = [f"    [white]{escape(card.get('title', ''))}[/white]  {card.get('status', '')}{chosen}{who}"]
    lines += _why_lines(card)
    prior = card.get("overruled")
    if prior:
        by = f" by {escape(prior['decided_by_label'])}" if prior.get("decided_by_label") else ""
        lines.append(f"      [yellow]you overruled north[/yellow], which had {prior.get('status', '')}{by}")
        lines += ["  " + line for line in _why_lines(prior)]
    return lines


def _why_lines(decision: dict) -> list[str]:
    from rich.markup import escape

    lines = [f"      [bright_black]{escape(decision['reason'])}[/bright_black]"] if decision.get("reason") else []
    for ref in decision.get("memory_used") or []:
        text = f": {ref['text']}" if ref.get("text") else ""
        lines.append(f"      [bright_black]· {escape(ref.get('label', ref.get('kind', '')) + text)}[/bright_black]")
    return lines
