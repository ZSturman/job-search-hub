from job_search_hub.safety.claims import detect_unsupported_metrics


def test_unsupported_metric_flagged():
    violations = detect_unsupported_metrics(
        "Reduced costs by 42% in Q3.",
        approved_metrics=["Improved throughput by 10%"],
    )
    assert any(v.code == "unsupported_metric" for v in violations)


def test_supported_metric_passes():
    violations = detect_unsupported_metrics(
        "Improved throughput by 10% across the platform.",
        approved_metrics=["Improved throughput by 10%"],
    )
    assert not violations
