"""The recorded approval-integrity experiment must remain executable and green."""

from experiments.approval_integrity.benchmark import run_benchmark


def test_every_approval_integrity_case_passes() -> None:
    result = run_benchmark()

    assert result["overall"] == {"passed": 21, "total": 21, "accuracy": 1.0}
    assert all(not metric["failures"] for metric in result["metrics"].values())
