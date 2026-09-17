"""The recorded system-quality experiment must remain executable."""

from experiments.system_quality.benchmark import run_benchmark


def test_every_system_quality_case_passes() -> None:
    result = run_benchmark()

    assert result["overall"] == {"passed": 30, "total": 30, "accuracy": 1.0}
    assert all(not metric["failures"] for metric in result["metrics"].values())


def test_benchmark_spans_distinct_repository_shapes() -> None:
    result = run_benchmark()

    assert result["repository_types"] == [
        "documentation_only",
        "mixed_infrastructure",
        "small_python_service",
        "typescript_monorepo",
    ]
