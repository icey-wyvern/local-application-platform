import json
from pathlib import Path
from typing import Any

from lap_build_agent.errors import BuildError
from lap_build_agent.models import VulnerabilitySummary

SEVERITY_ORDER = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def evaluate_trivy_report(path: Path, threshold: str) -> VulnerabilitySummary:
    if threshold not in SEVERITY_ORDER:
        raise BuildError(
            "policy_configuration_invalid", "unknown vulnerability threshold", stage="scan"
        )
    try:
        report: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("SchemaVersion"), int):
            raise TypeError
        results = report.get("Results", [])
        if not isinstance(results, list):
            raise TypeError
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise BuildError(
            "scanner_output_invalid", "Trivy did not produce a valid report", stage="scan"
        ) from error

    counts = {severity: 0 for severity in SEVERITY_ORDER}
    blocked = 0
    for result in results:
        if not isinstance(result, dict):
            raise BuildError("scanner_output_invalid", "Trivy result data is invalid", stage="scan")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise BuildError(
                "scanner_output_invalid", "Trivy vulnerability data is invalid", stage="scan"
            )
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise BuildError(
                    "scanner_output_invalid", "Trivy vulnerability data is invalid", stage="scan"
                )
            severity = str(vulnerability.get("Severity", "UNKNOWN")).upper()
            if severity not in SEVERITY_ORDER:
                severity = "UNKNOWN"
            counts[severity] += 1
            if SEVERITY_ORDER[severity] >= SEVERITY_ORDER[threshold]:
                blocked += 1
    summary = VulnerabilitySummary(threshold=threshold, counts=counts, blocked=blocked)
    if blocked:
        raise BuildError(
            "vulnerability_policy_rejected",
            f"artifact contains {blocked} vulnerabilities at or above {threshold}",
            stage="scan",
        )
    return summary
