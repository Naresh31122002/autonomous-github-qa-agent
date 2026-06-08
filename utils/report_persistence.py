"""
Helpers for persisting the latest analysis, webhook status, and UI state.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "reports"))
LATEST_ANALYSIS_PATH = REPORTS_DIR / "latest_analysis.json"
WEBHOOK_STATUS_PATH = REPORTS_DIR / "webhook_status.json"
LATEST_UI_STATE_PATH = REPORTS_DIR / "latest_ui_state.json"


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_reports_dir()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def get_file_signature(path: Path) -> Optional[str]:
    """Return a lightweight signature so callers can detect file changes."""
    if not path.exists():
        return None

    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def parse_json_output(raw_text: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Safely parse tool JSON output."""
    if default is None:
        default = {}
    if not raw_text:
        return default

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return default


def split_generated_tests(test_output: str) -> List[str]:
    """Store generated tests as a list while preserving full code blocks."""
    cleaned = (test_output or "").strip()
    return [cleaned] if cleaned else []


def infer_loops_used(logs: List[str]) -> Optional[int]:
    """Infer the last loop number from agent logs."""
    last_loop = 0
    for message in logs:
        if "Loop " not in message:
            continue
        try:
            fragment = message.split("Loop ", 1)[1]
            loop_num = int(fragment.split(" ", 1)[0])
            last_loop = max(last_loop, loop_num)
        except (IndexError, ValueError):
            continue
    return last_loop or None


def build_analysis_payload(
    repo_url: str,
    results: Dict[str, Any],
    *,
    timestamp: Optional[str] = None,
    agent_reasoning: str = "",
    commit_sha: Optional[str] = None,
    analyzed_files: Optional[List[str]] = None,
    loops_used: Optional[int] = None,
    source: str = "manual",
) -> Dict[str, Any]:
    """Convert raw tool results into the persisted UI payload."""
    scanner_output = results.get("code_scanner", "")
    priority_output = results.get("issue_prioritizer", "")
    tests_output = results.get("test_case_generator", "")
    report_output = results.get("report_writer", "")

    scanner_data = parse_json_output(scanner_output, {"issues": [], "summary": {}})
    priority_data = parse_json_output(
        priority_output,
        {
            "critical": [],
            "high": [],
            "medium": [],
            "low": [],
            "recommendations": [],
        },
    )

    issues = scanner_data.get("issues", [])
    summary = scanner_data.get("summary", {}) or {}
    recommendations = priority_data.get("recommendations", []) or []

    payload: Dict[str, Any] = {
        "repo_url": repo_url,
        "timestamp": timestamp or datetime.now().isoformat(),
        "issues": issues,
        "recommendations": recommendations,
        "generated_tests": split_generated_tests(tests_output),
        "full_report": report_output or "# Code Quality Analysis Report\n\nNo report generated.",
        "agent_reasoning": agent_reasoning.strip(),
        "summary": {
            "total_issues": summary.get("total_issues", len(issues)),
            "critical": summary.get("critical", 0),
            "high": summary.get("high", 0),
            "medium": summary.get("medium", 0),
            "low": summary.get("low", 0),
            "files_analyzed": summary.get("files_analyzed", len(analyzed_files or [])),
        },
        "priority_groups": {
            "critical": priority_data.get("critical", []) or [],
            "high": priority_data.get("high", []) or [],
            "medium": priority_data.get("medium", []) or [],
            "low": priority_data.get("low", []) or [],
        },
        "raw_outputs": {
            "code_scanner": scanner_output,
            "test_case_generator": tests_output,
            "issue_prioritizer": priority_output,
            "report_writer": report_output,
        },
        "source": source,
    }

    if commit_sha:
        payload["commit_sha"] = commit_sha
    if analyzed_files is not None:
        payload["analyzed_files"] = analyzed_files
    if loops_used is not None:
        payload["loops_used"] = loops_used

    return payload


def save_latest_analysis(payload: Dict[str, Any]) -> str:
    """Persist the latest completed analysis for the Streamlit UI."""
    _atomic_write_json(LATEST_ANALYSIS_PATH, payload)
    return str(LATEST_ANALYSIS_PATH)


def _rebuild_latest_analysis_from_history() -> Optional[Dict[str, Any]]:
    """Backfill latest_analysis.json from the newest saved report if needed."""
    candidates = sorted(
        [
            path for path in REPORTS_DIR.glob("*.json")
            if path.name not in {LATEST_ANALYSIS_PATH.name, WEBHOOK_STATUS_PATH.name, LATEST_UI_STATE_PATH.name}
        ],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    for path in candidates:
        report_data = _load_json(path, None)
        if not report_data:
            continue

        results = report_data.get("results")
        repo_url = report_data.get("repo_url")
        if not results or not repo_url:
            continue

        payload = build_analysis_payload(
            repo_url,
            results,
            timestamp=report_data.get("timestamp"),
            commit_sha=report_data.get("commit_sha"),
            analyzed_files=report_data.get("analyzed_files"),
            source="webhook",
        )
        save_latest_analysis(payload)
        return payload

    return None


def load_latest_analysis() -> Optional[Dict[str, Any]]:
    """Load the most recent persisted analysis payload."""
    payload = _load_json(LATEST_ANALYSIS_PATH, None)
    if payload:
        return payload
    return _rebuild_latest_analysis_from_history()


def save_webhook_status(
    status: str,
    *,
    repo_url: str = "",
    commit_sha: Optional[str] = None,
    message: str = "",
    updated_at: Optional[str] = None,
) -> str:
    """Persist webhook lifecycle state for UI badges."""
    payload: Dict[str, Any] = {
        "status": status,
        "repo_url": repo_url,
        "message": message,
        "updated_at": updated_at or datetime.now().isoformat(),
    }
    if commit_sha:
        payload["commit_sha"] = commit_sha

    _atomic_write_json(WEBHOOK_STATUS_PATH, payload)
    return str(WEBHOOK_STATUS_PATH)


def load_webhook_status() -> Optional[Dict[str, Any]]:
    """Load the latest webhook lifecycle state."""
    return _load_json(WEBHOOK_STATUS_PATH, None)


def save_ui_state(report_view: str) -> str:
    """Persist the last selected report view across hard refreshes."""
    payload = {
        "report_view": report_view,
        "updated_at": datetime.now().isoformat(),
    }
    _atomic_write_json(LATEST_UI_STATE_PATH, payload)
    return str(LATEST_UI_STATE_PATH)


def load_ui_state() -> Dict[str, Any]:
    """Load persisted UI state."""
    return _load_json(LATEST_UI_STATE_PATH, {})
