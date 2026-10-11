"""Deterministic factual-evidence checks for approved answers and qualifications."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass
class ClaimViolation:
    code: str
    message: str
    excerpt: str = ""


def _detect_metrics(text: str) -> list[str]:
    """Return numeric expressions that probably represent claimed metrics."""
    pattern = re.compile(
        r"""
        (?:                       # one of:
            \$\d[\d,]*\b          # dollar amount
          | \b\d+(?:\.\d+)?\s?%   # percent
          | \b\d{2,}\+?\s+
            (?:users|customers|clients|projects|hours|requests|records|rows|tests|features)
          | \b\d+x\b              # multipliers like 3x
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    return [m.group(0) for m in pattern.finditer(text)]


def detect_unsupported_metrics(
    text: str,
    *,
    approved_metrics: Iterable[str],
) -> list[ClaimViolation]:
    """Flag numeric claims that don't appear in any approved Profile Fact text.

    `approved_metrics` should be the set of metric strings from Profile Facts
    where `Metrics Verified == True`. This is text-level containment only;
    the LLM grounding check is the higher-rigor pass.
    """
    approved = {m.strip() for m in approved_metrics if m and m.strip()}
    violations: list[ClaimViolation] = []
    for metric in _detect_metrics(text):
        if not any(metric.lower() in a.lower() for a in approved):
            violations.append(
                ClaimViolation(
                    code="unsupported_metric",
                    message=f"metric {metric!r} not found in any approved Profile Fact",
                    excerpt=metric,
                )
            )
    return violations
