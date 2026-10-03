from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    price_cents: int
    qty: int = 1


class Cart:
    """A shopping cart. All money is in whole cents."""

    def __init__(self) -> None:
        self.items: list[Item] = []

    def add(self, sku: str, price_cents: int, qty: int = 1) -> None:
        if qty < 1 or price_cents < 0:
            raise ValueError("quantity must be at least 1 and the price cannot be negative")
        for item in self.items:
            if item.sku == sku and item.price_cents == price_cents:
                item.qty += qty
                return
        self.items.append(Item(sku, price_cents, qty))

    def subtotal_cents(self) -> int:
        return sum(item.price_cents * item.qty for item in self.items)

    def total_cents(self) -> int:
        return self.subtotal_cents()
