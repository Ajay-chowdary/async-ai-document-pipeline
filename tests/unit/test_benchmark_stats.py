"""Pure helpers used by the benchmark script."""

from scripts.benchmark import percentile


def test_percentile_endpoints() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 50) == 30.0
    assert percentile(values, 100) == 50.0


def test_percentile_single_value() -> None:
    assert percentile([7.0], 95) == 7.0
