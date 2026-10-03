from ledger.store import Ledger

STATEMENT = """id,timestamp,description,amount_cents
a1,2024-03-10T12:00:00Z,rent,-90000
a2,2024-03-11T09:30:00Z,coffee,-450
a3,2024-04-02T08:00:00Z,salary,250000
"""


def test_monthly_totals():
    ledger = Ledger()
    ledger.import_statement(STATEMENT)
    assert ledger.monthly_totals() == {"2024-03": -90450, "2024-04": 250000}


def test_importing_the_same_statement_twice_changes_nothing():
    ledger = Ledger()
    assert ledger.import_statement(STATEMENT) == 3
    assert ledger.import_statement(STATEMENT) == 0
    assert ledger.monthly_totals() == {"2024-03": -90450, "2024-04": 250000}
