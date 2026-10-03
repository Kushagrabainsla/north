from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Transaction:
    id: str
    day: date
    description: str
    amount_cents: int
