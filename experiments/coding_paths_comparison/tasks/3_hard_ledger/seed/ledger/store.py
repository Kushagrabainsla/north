from ledger.model import Transaction
from ledger.parser import parse_statement


class Ledger:
    def __init__(self) -> None:
        self._transactions: list[Transaction] = []

    def import_statement(self, text: str) -> int:
        """Add the statement's transactions that are not already here; return how many were added."""
        added = 0
        for txn in parse_statement(text):
            if self._already_have(txn):
                continue
            self._transactions.append(txn)
            added += 1
        return added

    def _already_have(self, txn: Transaction) -> bool:
        key = (txn.day, txn.amount_cents, txn.description)
        return any((t.day, t.amount_cents, t.description) == key for t in self._transactions)

    def monthly_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for txn in self._transactions:
            month = f"{txn.day.year:04d}-{txn.day.month:02d}"
            totals[month] = totals.get(month, 0) + txn.amount_cents
        return dict(sorted(totals.items()))
