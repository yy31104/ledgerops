"""Keep successful narration readable without hiding pytest failures.

Loaded only by make demo. Double-quiet pytest suppresses the success summary;
this hook suppresses the successful test glyph while retaining failure reporting.
"""


def pytest_report_teststatus(report):
    if report.when == "call" and report.passed:
        return "passed", "", ""
    return None
