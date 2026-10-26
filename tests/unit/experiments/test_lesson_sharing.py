"""The lesson world enforces its hidden rules; each arm's board publishes, labels and retracts as described."""

import datetime
import json

from experiments.lesson_sharing.fleet import Board
from experiments.lesson_sharing.world import Item, Ledgerly, Task, customers, grade, make_tasks

_CUSTOMER = customers()[0]
_ITEMS = (Item("Delivery fee", 2, 4550),)
_DUE = datetime.date(2027, 3, 14)


def _world(kind: str = "create_send", due: datetime.date = _DUE) -> Ledgerly:
    return Ledgerly(
        Task(
            "t00",
            kind,
            _CUSTOMER,
            items=_ITEMS if kind != "send_existing" else (),
            due=due,
            existing_invoice="INV-5100",
        )
    )


def _create(world: Ledgerly, due_date: str, items: list[dict]) -> dict:
    return json.loads(
        world.call("create_invoice", {"customer_id": _CUSTOMER.customer_id, "due_date": due_date, "items": items})
    )


def test_the_documented_way_fails_on_every_hidden_rule() -> None:
    world = _world()
    good_item = {"description": "Delivery fee", "qty": 2, "unit_price": 4550}

    assert "E102" in _create(world, "2027-03-14", [good_item])["error"]
    assert (
        "unknown field 'quantity'"
        in _create(world, "14/03/2027", [{"description": "Delivery fee", "quantity": 2, "unit_price": 4550}])["error"]
    )
    assert "E103" in _create(world, "14/03/2027", [{**good_item, "unit_price": 45.5}])["error"]
    invoice = _create(world, "14/03/2027", [good_item])["invoice_id"]
    assert (
        "E301"
        in json.loads(world.call("send_invoice", {"invoice_id": invoice, "to_email": _CUSTOMER.billing_email}))["error"]
    )
    world.call("finalize_invoice", {"invoice_id": invoice})
    assert (
        "E302" in json.loads(world.call("send_invoice", {"invoice_id": invoice, "to_email": _CUSTOMER.email}))["error"]
    )
    world.call("send_invoice", {"invoice_id": invoice, "to_email": _CUSTOMER.billing_email})

    assert grade(world) == (True, [])


def test_dollars_and_a_month_first_date_are_accepted_but_graded_wrong() -> None:
    world = _world(due=datetime.date(2027, 7, 5))
    invoice = _create(world, "07/05/2027", [{"description": "Delivery fee", "qty": 2, "unit_price": 46}])["invoice_id"]
    world.call("finalize_invoice", {"invoice_id": invoice})
    world.call("send_invoice", {"invoice_id": invoice, "to_email": _CUSTOMER.billing_email})

    assert grade(world) == (False, ["dollars_not_cents", "month_first_date", "no_correct_invoice"])


def test_sending_an_existing_draft_needs_finalize_and_the_billing_email() -> None:
    world = _world("send_existing")
    world.call("finalize_invoice", {"invoice_id": "INV-5100"})
    world.call("send_invoice", {"invoice_id": "INV-5100", "to_email": _CUSTOMER.billing_email})

    assert grade(world) == (True, [])


def test_task_sets_are_seeded_and_mixed() -> None:
    first, again = make_tasks(3), make_tasks(3)

    assert [t.prompt() for t in first] == [t.prompt() for t in again]
    assert sorted(t.kind for t in first).count("create_send") == 6


def test_today_publishes_only_a_successful_authors_notes_when_it_ends() -> None:
    board = Board("end_success")
    board.post("rule from a winner", "a", 1)
    board.post("rule from a loser", "b", 1)

    assert board.snapshot(5) == ([], 0)
    board.author_ended("a", True, 4)
    board.author_ended("b", False, 4)

    assert board.snapshot(4)[0] == []
    assert board.snapshot(5)[0] == ["#1 rule from a winner"]


def test_instant_notes_are_visible_next_round_and_never_retracted() -> None:
    board = Board("instant")
    board.post("rule", "a", 2)
    board.author_ended("a", False, 3)
    board.flag(1, "b", 3)

    assert board.snapshot(2)[0] == []
    assert board.snapshot(9)[0] == ["#1 rule"]


def test_gated_notes_are_labelled_then_confirmed_or_retracted_and_readers_are_told() -> None:
    board = Board("instant", labels=True, retract=True)
    board.post("good rule", "a", 1)
    board.post("bad rule", "b", 1)
    _, cursor = board.snapshot(2)
    board.author_ended("a", True, 3)
    board.author_ended("b", False, 3)

    lines, _ = board.updates(cursor, 4, reader="c")
    assert lines == [
        "#1 confirmed: its author's task succeeded.",
        "#2 RETRACTED (its author's task failed). Do not rely on it.",
    ]
    assert board.snapshot(4)[0] == ["#1 [confirmed] good rule"]


def test_one_flag_retracts_an_unconfirmed_note_and_two_retract_a_confirmed_one() -> None:
    board = Board("instant", labels=True, retract=True)
    board.post("unconfirmed", "a", 1)
    board.post("confirmed", "b", 1)
    board.author_ended("b", True, 2)

    assert board.flag(1, "a", 3) == "you wrote that note"
    board.flag(1, "c", 3)
    board.flag(2, "c", 3)
    assert board.snapshot(4)[0] == ["#2 [confirmed] confirmed"]
    board.flag(2, "d", 4)
    assert board.snapshot(5)[0] == []


def test_a_reader_is_not_sent_its_own_note() -> None:
    board = Board("instant")
    board.post("mine", "a", 1)
    board.post("theirs", "b", 1)

    assert board.updates(0, 2, reader="a") == (["#2 theirs"], 2)


def test_wrong_claims_in_agent_lessons_are_counted_by_rule() -> None:
    from experiments.lesson_sharing.summarize import wrong_claims

    assert wrong_claims("ISO YYYY-MM-DD is rejected, so use US MM/DD/YYYY format.") == ["date"]
    assert wrong_claims("Due date accepts DD/MM/YYYY; MM/DD/YYYY was rejected.") == []
    assert wrong_claims("MM/DD/YYYY 01/18/2027 was rejected.") == []
    assert wrong_claims("unit_price accepts decimal prices (do not round).") == ["money"]
    assert wrong_claims("unit_price must be integer minor units (cents).") == []
    assert wrong_claims("send_invoice should use the email that search_customers returns.") == ["email"]


def test_labels_alone_confirm_but_never_retract_and_retraction_alone_shows_no_labels() -> None:
    labels = Board("instant", labels=True)
    labels.post("rule", "a", 1)
    labels.author_ended("a", False, 2)
    labels.flag(1, "b", 2)
    retract = Board("instant", retract=True)
    retract.post("rule", "a", 1)
    retract.post("other", "b", 1)
    retract.author_ended("a", False, 2)
    retract.author_ended("b", True, 2)

    assert labels.snapshot(5)[0] == ["#1 [unconfirmed] rule"]
    assert retract.snapshot(5)[0] == ["#2 other"]


def test_mcnemar_counts_only_discordant_paired_tasks() -> None:
    from experiments.lesson_sharing.summarize import mcnemar

    def run(results: list[bool]) -> dict:
        return {"seed": 0, "tasks": [{"task_id": f"t{i}", "success": ok} for i, ok in enumerate(results)]}

    test = mcnemar([run([True] * 8 + [False, True])], [run([False] * 8 + [False, True])])

    assert (test["only_first_won"], test["only_second_won"]) == (8, 0)
    assert test["p"] == round(2 / 2**8, 4)
