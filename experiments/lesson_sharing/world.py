"""A fake invoicing API ("Ledgerly") with five rules its docs do not state.

Every task runs against its own fresh copy, so agents share nothing but the
lessons they write. The rules are the same in every copy, which is what makes a
lesson from one agent useful to another. Errors are terse but solvable; two of
the wrong ways in are silent (accepted, but the invoice is wrong), so a wrong
lesson can do harm that no error reveals.

Deterministic: no model, no network, no clock.
"""

from __future__ import annotations

import datetime
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

# The rules, and the correct lesson an oracle hands out for each.
RULES: dict[str, str] = {
    "date": "due_date must be written DD/MM/YYYY, day first (14 March 2027 -> 14/03/2027).",
    "cents": "unit_price is a whole number of cents ($45.50 -> 4550).",
    "qty": "Line items use the key 'qty', not 'quantity': {description, qty, unit_price}.",
    "finalize": "Call finalize_invoice before send_invoice; a draft cannot be sent.",
    "billing": "send_invoice must go to the billing_email from get_customer, not the search email.",
}

# Plausible but wrong. (author_fails, post_round, outcome_round): when its author
# posts it and when the author's task ends. Only the third one's author succeeds.
WRONG_LESSONS: list[tuple[str, bool, int, int]] = [
    ("due_date must be written MM/DD/YYYY, month first (US format).", True, 1, 6),
    ("send_invoice should use the email that search_customers returns.", True, 1, 6),
    ("unit_price is in whole dollars; round to the nearest dollar.", False, 2, 5),
]

_NAMES = [
    "Harbor Lane Bakery",
    "Northwind Tools",
    "Copper Kettle Cafe",
    "Bluebird Dental",
    "Granite Peak Gym",
    "Saffron Street Foods",
    "Maple Row Books",
    "Tidewater Marine",
    "Juniper Design Co",
    "Orchard Hill Farms",
    "Lumen Optics",
    "Redwood Legal",
    "Silverline Couriers",
    "Pinecrest Clinic",
    "Atlas Print Shop",
    "Willow Creek Vets",
]
_BILLING_BOXES = ["ap", "invoices", "accounts", "finance"]
_ITEMS = [
    "Consulting hour",
    "Delivery fee",
    "Annual license",
    "Repair kit",
    "Workshop seat",
    "Paper stock",
    "Cleaning service",
    "Design review",
    "Server rack rental",
    "Training manual",
]
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    billing_email: str


def customers() -> list[Customer]:
    out = []
    for index, name in enumerate(_NAMES):
        domain = re.sub(r"[^a-z]", "", name.lower())[:14] + ".example"
        out.append(
            Customer(
                customer_id=f"C-{1001 + index}",
                name=name,
                email=f"hello@{domain}",
                billing_email=f"{_BILLING_BOXES[index % len(_BILLING_BOXES)]}@{domain}",
            )
        )
    return out


@dataclass(frozen=True)
class Item:
    description: str
    qty: int
    cents: int


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str  # "create_send" | "create_draft" | "send_existing"
    customer: Customer
    items: tuple[Item, ...] = ()
    due: datetime.date | None = None
    existing_invoice: str = ""

    def prompt(self) -> str:
        lines = ", ".join(f"{item.qty} x {item.description} at ${item.cents / 100:.2f} each" for item in self.items)
        due = self.due.strftime("%B %-d, %Y") if self.due else ""
        if self.kind == "create_send":
            return f"Create an invoice for {self.customer.name} with: {lines}. It is due {due}. Then send it to them."
        if self.kind == "create_draft":
            return f"Create a draft invoice for {self.customer.name} with: {lines}. It is due {due}. Do not send it."
        return f"Invoice {self.existing_invoice} for {self.customer.name} is a draft. Send it to the customer."


def make_tasks(seed: int, count: int = 12) -> list[Task]:
    """Half create-and-send, a quarter draft-only, a quarter send-an-existing-draft, in a seeded order."""
    rng = random.Random(seed)
    people = customers()
    kinds = ["create_send"] * (count // 2) + ["create_draft"] * (count // 4)
    kinds += ["send_existing"] * (count - len(kinds))
    rng.shuffle(kinds)
    tasks = []
    for index, kind in enumerate(kinds):
        customer = rng.choice(people)
        if kind == "send_existing":
            tasks.append(
                Task(f"t{index:02d}", kind, customer, existing_invoice=f"INV-{5000 + rng.randrange(100, 999)}")
            )
            continue
        items = tuple(
            Item(rng.choice(_ITEMS), rng.randint(1, 5), rng.randint(5, 400) * 100 + rng.choice([25, 50, 75, 99]))
            for _ in range(rng.randint(1, 3))
        )
        # Mostly days above 12, so a month-first date fails loudly; a few at 12 or below fail silently.
        day = rng.randint(13, 28) if rng.random() < 0.7 else rng.randint(1, 12)
        due = datetime.date(2027, rng.randint(1, 12), day)
        tasks.append(Task(f"t{index:02d}", kind, customer, items=items, due=due))
    return tasks


@dataclass
class Invoice:
    invoice_id: str
    customer_id: str
    due: datetime.date | None
    items: list[Item]
    status: str = "draft"  # draft -> open -> sent
    sent_to: str = ""


class ApiError(Exception):
    pass


@dataclass
class Ledgerly:
    """One task's private copy of the API."""

    task: Task
    invoices: dict[str, Invoice] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    _next_id: int = 7001

    def __post_init__(self) -> None:
        self._customers = {customer.customer_id: customer for customer in customers()}
        if self.task.kind == "send_existing":
            self.invoices[self.task.existing_invoice] = Invoice(
                self.task.existing_invoice,
                self.task.customer.customer_id,
                datetime.date(2027, 6, 30),
                [Item("Consulting hour", 2, 15000)],
            )

    def call(self, name: str, params: dict[str, Any]) -> str:
        """Run one tool call; the result is what the agent sees."""
        self.attempts.append(f"{name} {json.dumps(params)[:160]}")
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            return json.dumps(handler(**params))
        except ApiError as exc:
            self.errors.append(str(exc))
            return json.dumps({"error": str(exc)})
        except TypeError as exc:
            self.errors.append("bad arguments")
            return json.dumps({"error": f"bad arguments: {exc}"})

    # ---- tools ----

    def _tool_search_customers(self, query: str) -> dict:
        needle = str(query).lower().strip()
        found = [c for c in self._customers.values() if needle and needle in c.name.lower()]
        return {"results": [{"id": c.customer_id, "name": c.name, "email": c.email} for c in found[:5]]}

    def _tool_get_customer(self, customer_id: str) -> dict:
        customer = self._customer(customer_id)
        return {
            "id": customer.customer_id,
            "name": customer.name,
            "email": customer.email,
            "billing_email": customer.billing_email,
        }

    def _tool_create_invoice(self, customer_id: str, due_date: Any, items: Any) -> dict:
        customer = self._customer(customer_id)
        due = _parse_due(due_date)
        if not isinstance(items, list) or not items:
            raise ApiError("E104: line item malformed")
        parsed = [_parse_item(item) for item in items]
        invoice = Invoice(f"INV-{self._next_id}", customer.customer_id, due, parsed)
        self._next_id += 1
        self.invoices[invoice.invoice_id] = invoice
        return {
            "invoice_id": invoice.invoice_id,
            "status": invoice.status,
            "total": sum(i.qty * i.cents for i in parsed),
        }

    def _tool_get_invoice(self, invoice_id: str) -> dict:
        invoice = self._invoice(invoice_id)
        return {
            "invoice_id": invoice.invoice_id,
            "customer_id": invoice.customer_id,
            "status": invoice.status,
            "due_date": invoice.due.strftime("%d/%m/%Y") if invoice.due else None,
            "items": [{"description": i.description, "qty": i.qty, "unit_price": i.cents} for i in invoice.items],
        }

    def _tool_finalize_invoice(self, invoice_id: str) -> dict:
        invoice = self._invoice(invoice_id)
        if invoice.status == "draft":
            invoice.status = "open"
        return {"invoice_id": invoice.invoice_id, "status": invoice.status}

    def _tool_send_invoice(self, invoice_id: str, to_email: str) -> dict:
        invoice = self._invoice(invoice_id)
        if invoice.status == "draft":
            raise ApiError("E301: invoice state invalid for sending")
        if str(to_email).strip().lower() != self._customers[invoice.customer_id].billing_email:
            raise ApiError("E302: recipient not permitted for this customer")
        invoice.status, invoice.sent_to = "sent", str(to_email).strip().lower()
        return {"invoice_id": invoice.invoice_id, "status": invoice.status}

    # ---- helpers ----

    def _customer(self, customer_id: Any) -> Customer:
        customer = self._customers.get(str(customer_id).strip())
        if customer is None:
            raise ApiError("E201: customer_id not found")
        return customer

    def _invoice(self, invoice_id: Any) -> Invoice:
        invoice = self.invoices.get(str(invoice_id).strip())
        if invoice is None:
            raise ApiError("E404: invoice not found")
        return invoice


def _parse_due(value: Any) -> datetime.date:
    match = _DATE_RE.match(str(value).strip())
    if not match:
        raise ApiError("E102: due_date rejected (use slashes)")
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        raise ApiError("E102: due_date rejected (use slashes)") from None


def _parse_item(item: Any) -> Item:
    if not isinstance(item, dict) or not isinstance(item.get("description"), str):
        raise ApiError("E104: line item malformed")
    if "qty" not in item:
        unknown = next((key for key in item if key not in {"description", "unit_price"}), None)
        raise ApiError(
            f"E104: line item malformed (unknown field '{unknown}')" if unknown else "E104: line item malformed"
        )
    qty, price = item["qty"], item.get("unit_price")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        raise ApiError("E104: line item malformed")
    if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        raise ApiError("E103: unit_price rejected (integer minor units)")
    return Item(item["description"], qty, price)


def grade(world: Ledgerly) -> tuple[bool, list[str]]:
    """Whether the task was done exactly, and why not (silent mistakes named)."""
    task = world.task
    reasons: list[str] = []
    if task.kind == "send_existing":
        invoice = world.invoices[task.existing_invoice]
        ok = invoice.status == "sent" and invoice.sent_to == task.customer.billing_email
        return ok, [] if ok else ["not_sent"]
    wanted = sorted((i.description, i.qty, i.cents) for i in task.items)
    mine = [inv for inv in world.invoices.values() if inv.customer_id == task.customer.customer_id]
    right = [
        inv
        for inv in mine
        if inv.due == task.due and sorted((i.description, i.qty, i.cents) for i in inv.items) == wanted
    ]
    for invoice in mine:
        if invoice in right:
            continue
        if task.due and invoice.due == _swapped(task.due):
            reasons.append("month_first_date")
        if any(i.cents == round(t.cents / 100) for i in invoice.items for t in task.items):
            reasons.append("dollars_not_cents")
    if task.kind == "create_draft":
        ok = bool(right) and all(inv.status != "sent" for inv in mine)
    else:
        wrong_sent = any(inv.status == "sent" for inv in mine if inv not in right)
        ok = any(inv.status == "sent" for inv in right) and not wrong_sent
        if not ok and right and not wrong_sent:
            reasons.append("not_sent")
    if not right:
        reasons.append("no_correct_invoice")
    return ok, sorted(set(reasons))


def _swapped(day: datetime.date) -> datetime.date | None:
    try:
        return datetime.date(day.year, day.day, day.month)
    except ValueError:
        return None


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_customers",
            "description": "Find customers by name.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer",
            "description": "Get one customer's record.",
            "parameters": {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_invoice",
            "description": "Create a draft invoice. items: line items (description, quantity, unit price).",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "due_date": {"type": "string", "description": "The due date."},
                    "items": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["customer_id", "due_date", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_invoice",
            "description": "Get one invoice.",
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_invoice",
            "description": "Lock an invoice so it can no longer be edited.",
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_invoice",
            "description": "Email an invoice to the customer.",
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}, "to_email": {"type": "string"}},
                "required": ["invoice_id", "to_email"],
            },
        },
    },
]
