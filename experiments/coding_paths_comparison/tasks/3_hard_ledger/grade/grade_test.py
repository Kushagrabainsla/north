from ledger.store import Ledger

HEAD = "id,timestamp,description,amount_cents\n"


def ledger_with(*rows):
    ledger = Ledger()
    ledger.import_statement(HEAD + "\n".join(rows))
    return ledger


def test_a_late_evening_purchase_in_the_americas_belongs_to_the_next_utc_month():
    ledger = ledger_with("x1,2024-03-31T23:30:00-05:00,dinner,-3000")
    assert ledger.monthly_totals() == {"2024-04": -3000}


def test_an_early_morning_purchase_east_of_utc_belongs_to_the_previous_utc_month():
    ledger = ledger_with("x1,2024-04-01T01:00:00+05:00,breakfast,-800")
    assert ledger.monthly_totals() == {"2024-03": -800}


def test_z_and_naive_timestamps_are_utc():
    ledger = ledger_with("x1,2024-03-31T23:30:00Z,a,-1", "x2,2024-04-01T00:10:00,b,-2")
    assert ledger.monthly_totals() == {"2024-03": -1, "2024-04": -2}


def test_half_hour_offsets():
    ledger = ledger_with("x1,2024-05-01T04:00:00+05:30,tea,-5")  # 22:30 UTC on 30 April
    assert ledger.monthly_totals() == {"2024-04": -5}


def test_two_identical_coffees_with_different_ids_both_count():
    ledger = ledger_with(
        "c1,2024-03-11T09:30:00Z,coffee,-450",
        "c2,2024-03-11T14:00:00Z,coffee,-450",
    )
    assert ledger.monthly_totals() == {"2024-03": -900}


def test_the_return_value_counts_only_new_transactions():
    ledger = Ledger()
    first = HEAD + "c1,2024-03-11T09:30:00Z,coffee,-450\nc2,2024-03-11T14:00:00Z,coffee,-450"
    assert ledger.import_statement(first) == 2
    assert ledger.import_statement(first) == 0
    assert ledger.monthly_totals() == {"2024-03": -900}


def test_overlapping_statements_add_only_the_new_rows():
    ledger = Ledger()
    ledger.import_statement(HEAD + "c1,2024-03-11T09:30:00Z,coffee,-450\nc2,2024-03-11T14:00:00Z,coffee,-450")
    added = ledger.import_statement(HEAD + "c2,2024-03-11T14:00:00Z,coffee,-450\nc3,2024-03-12T08:00:00Z,coffee,-450")
    assert added == 1
    assert ledger.monthly_totals() == {"2024-03": -1350}


def test_the_same_id_with_a_corrected_day_is_still_the_same_transaction():
    ledger = ledger_with("c1,2024-03-11T09:30:00Z,coffee,-450")
    ledger.import_statement(HEAD + "c1,2024-03-12T09:30:00Z,coffee,-450")
    assert ledger.monthly_totals() == {"2024-03": -450}
