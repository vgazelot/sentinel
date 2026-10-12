"""Reading GitHub check results the same way everywhere: rollup state, failing checks, CI families."""

# CANCELLED is left out on purpose: a run superseded by a newer one (workflow `concurrency:`) is not a failure
FAILED = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
ROLLUP = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure", "PENDING": "pending", "EXPECTED": "pending"}


def rollup(state: str | None) -> str | None:
    """statusCheckRollup.state → success / failure / pending / None (no checks)."""
    return ROLLUP.get(state or "")


def outcome(context: dict) -> str:
    """One check run or commit status → success / failure / pending / neutral (cancelled, skipped)."""
    if context["__typename"] == "CheckRun":
        if context.get("status") not in (None, "COMPLETED") or context.get("conclusion") is None:
            return "pending"
        conclusion = context.get("conclusion")
        if conclusion in FAILED:
            return "failure"
        return "neutral" if conclusion in ("CANCELLED", "SKIPPED", "STALE") else "success"
    return {"SUCCESS": "success", "PENDING": "pending", "EXPECTED": "pending"}.get(context.get("state"), "failure")


def name(context: dict) -> str:
    return context.get("name") or context.get("context") or "?"


def failed_names(contexts: list[dict]) -> list[str]:
    return [name(c) for c in contexts if outcome(c) == "failure"]


def family(check: str) -> str:
    """"Molecule Test (EC2) - rsyslog / default" → "Molecule Test (EC2)": one CI cause, many jobs."""
    return check.split(" - ")[0].split(" / ")[0].strip() or check
