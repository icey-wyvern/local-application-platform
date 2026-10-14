import json
from pathlib import Path

import pytest
from lap_build_agent.errors import BuildError
from lap_build_agent.policy import evaluate_trivy_report


def write_report(path: Path, severities: list[str]) -> None:
    path.write_text(
        json.dumps({
            "SchemaVersion": 2,
            "Results": [{"Vulnerabilities": [{"Severity": severity} for severity in severities]}],
        })
    )


def test_policy_counts_fixed_and_unfixed_without_silently_ignoring_them(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    write_report(report, ["LOW", "HIGH"])
    result = evaluate_trivy_report(report, "CRITICAL")
    assert result.counts["HIGH"] == 1
    assert result.blocked == 0


def test_policy_rejects_at_threshold(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    write_report(report, ["CRITICAL"])
    with pytest.raises(BuildError) as raised:
        evaluate_trivy_report(report, "CRITICAL")
    assert raised.value.code == "vulnerability_policy_rejected"


def test_scanner_error_is_not_a_clean_scan(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("not json")
    with pytest.raises(BuildError) as raised:
        evaluate_trivy_report(report, "CRITICAL")
    assert raised.value.code == "scanner_output_invalid"


def test_valid_scratch_report_without_results_is_clean(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"SchemaVersion":2,"ArtifactType":"container_image"}')
    result = evaluate_trivy_report(report, "CRITICAL")
    assert result.blocked == 0
