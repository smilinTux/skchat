"""skchat browser QA lane (skwatchdog WD-10).

A scripted, report-only walk of skchat web over a raw CDP Chrome, capturing
a screenshot plus console diagnostics per step, grading the IMAGE through
skgateway, and writing one result artifact that the skos watchdog reads and
folds into the daily digest as ordinary ``WatchdogEvent`` records.

Start at :mod:`skchat.browser_qa.lane`; its module docstring carries the
safety rules (never join a Space, end what you create, do not seize the
human's CDP port) and the severity discipline (only deterministic evidence
earns ``problem``).

    from skchat.browser_qa import run_lane
    result = run_lane()
    print(result.severity, result.summary)

THE ARTIFACT CONTRACT, which is what skos reads:

  Root      ``$SKCHAT_BROWSER_QA_DIR`` or ``~/.skchat/browser-qa``
  Per run   ``<root>/<run_id>/result.json`` plus its evidence files
  Newest    ``<root>/latest.json``, an identical copy of the most recent
            ``result.json``
  run_id    ``<YYYYmmdd>T<HHMMSS>Z-<8 hex>``, so directory names sort
            chronologically

The skos adapter should glob ``<root>/*/result.json``, keep the runs whose
``finished_at`` falls inside the digest window, and refuse any document
whose ``artifact_version`` it does not know rather than guess at the shape.
"""

from __future__ import annotations

from .cdp import DEFAULT_CDP_PORT, BrowserPage, CdpError, ConsoleEntry
from .grade import RUBRIC_REF, BrowserGrade, grade_screenshot
from .lane import (
    ARTIFACT_VERSION,
    DEFAULT_ROOT,
    PROBLEM_CLASSES,
    LaneResult,
    StepResult,
    UnsafeNavigation,
    assert_safe_url,
    compute_severity,
    run_lane,
)
from .screenshot import ScreenshotStats, inspect_png

__all__ = [
    "ARTIFACT_VERSION",
    "BrowserGrade",
    "BrowserPage",
    "CdpError",
    "ConsoleEntry",
    "DEFAULT_CDP_PORT",
    "DEFAULT_ROOT",
    "LaneResult",
    "PROBLEM_CLASSES",
    "RUBRIC_REF",
    "ScreenshotStats",
    "StepResult",
    "UnsafeNavigation",
    "assert_safe_url",
    "compute_severity",
    "grade_screenshot",
    "inspect_png",
    "run_lane",
]
