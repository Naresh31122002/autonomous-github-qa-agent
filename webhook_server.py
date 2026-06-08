"""
GitHub Webhook Server
Receives GitHub push events and triggers autonomous code quality analysis.
"""
import hashlib
import hmac
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from groq import Groq
import uvicorn

from agent import loop
from agent.tools import github_integration, jira_integration
from utils import CommitTracker
from utils.report_persistence import (
    build_analysis_payload,
    infer_loops_used,
    save_latest_analysis,
    save_webhook_status,
)
from utils.repo_scope import (
    branch_matches,
    collect_unmonitored_paths,
    extract_branch_name,
    filter_monitored_paths,
    load_repo_scope_for_repo,
)

load_dotenv()

app = FastAPI(title="Code Quality Monitor Webhook Server")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("webhook_server")

analysis_history: List[Dict] = []

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "reports"))

CODE_EXTENSIONS = (".py", ".js", ".ts", ".java", ".cpp", ".c", ".go", ".rb")


def verify_signature(request: Request, payload: bytes) -> bool:
    """Verify GitHub webhook signature when WEBHOOK_SECRET is configured."""
    if not WEBHOOK_SECRET:
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature:
        return False

    expected_signature = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected_signature)


def repo_key(repo_url: str) -> str:
    """Normalize a GitHub URL to owner/repo when possible."""
    if "github.com" in repo_url:
        parts = repo_url.rstrip("/").split("/")
        return f"{parts[-2]}/{parts[-1]}"
    return repo_url.strip()


def safe_write_temp_file(temp_dir: str, relative_path: str, content: str) -> Optional[str]:
    """Write a fetched repository file while preventing path traversal."""
    normalized = os.path.normpath(relative_path)
    if normalized.startswith("..") or os.path.isabs(normalized):
        logger.warning("Skipping unsafe path from GitHub: %s", relative_path)
        return None

    full_path = os.path.join(temp_dir, normalized)
    parent = os.path.dirname(full_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return normalized


def record_history(record: Dict) -> None:
    """Store a bounded in-memory analysis history for dashboard polling."""
    analysis_history.append(record)
    if len(analysis_history) > 50:
        analysis_history.pop(0)


def parse_scanner_issues(results: Dict) -> Tuple[List[Dict], Dict]:
    """Extract scanner issues and counts from loop results."""
    try:
        scanner_data = json.loads(results.get("code_scanner", "{}"))
    except json.JSONDecodeError:
        return [], {}

    return scanner_data.get("issues", []), scanner_data.get("summary", {})


def high_severity_issues(results: Dict) -> List[Dict]:
    """Return critical/high issues from scanner output."""
    issues, _ = parse_scanner_issues(results)
    return [
        issue for issue in issues
        if str(issue.get("severity", "")).lower() in {"critical", "high"}
    ]


def jira_config_from_env() -> Dict[str, str]:
    """Load Jira configuration from environment variables."""
    return {
        "JIRA_BASE_URL": os.getenv("JIRA_BASE_URL", ""),
        "JIRA_EMAIL": os.getenv("JIRA_EMAIL", ""),
        "JIRA_API_TOKEN": os.getenv("JIRA_API_TOKEN", ""),
        "JIRA_PROJECT_KEY": os.getenv("JIRA_PROJECT_KEY", ""),
    }


def create_jira_tickets(results: Dict) -> List[Dict]:
    """Create Jira tickets for critical and high severity issues."""
    config = jira_config_from_env()
    if not all(config.values()):
        return [{"success": False, "message": "Jira not configured"}]

    created = []
    for issue in high_severity_issues(results):
        try:
            created.append(jira_integration.create_jira_ticket(issue, config))
        except Exception as e:
            logger.exception("Jira ticket creation failed")
            created.append({"success": False, "error": str(e), "issue": issue})
    return created


def save_reports(
    repo_url: str,
    commit_sha: Optional[str],
    results: Dict,
    analyzed_files: List[str],
    logs: List[str],
    branch_name: str = "",
    unmonitored_changed_files: Optional[List[str]] = None,
) -> Dict:
    """Persist markdown and structured JSON reports to reports/."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    repo_name = repo_key(repo_url).replace("/", "__")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_sha = (commit_sha or "manual")[:12]
    base_name = f"{repo_name}_{short_sha}_{timestamp}"

    issues, summary = parse_scanner_issues(results)
    markdown = results.get("report_writer") or "# Code Quality Analysis Report\n\nNo report generated."

    md_path = REPORTS_DIR / f"{base_name}.md"
    json_path = REPORTS_DIR / f"{base_name}.json"

    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "repo_name": repo_key(repo_url),
                "repo_url": repo_url,
                "timestamp": datetime.now().isoformat(),
                "commit_sha": commit_sha,
                "analyzed_files": analyzed_files,
                "issue_counts": {
                    "total": summary.get("total_issues", len(issues)),
                    "critical": summary.get("critical", 0),
                    "high": summary.get("high", 0),
                    "medium": summary.get("medium", 0),
                    "low": summary.get("low", 0),
                },
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    latest_payload = build_analysis_payload(
        repo_url,
        results,
        timestamp=datetime.now().isoformat(),
        agent_reasoning="\n".join(logs),
        commit_sha=commit_sha,
        analyzed_files=analyzed_files,
        loops_used=infer_loops_used(logs),
        source="webhook",
    )
    latest_payload["branch"] = branch_name
    latest_payload["unmonitored_changed_files"] = unmonitored_changed_files or []
    latest_analysis_path = save_latest_analysis(latest_payload)

    return {
        "markdown": str(md_path),
        "json": str(json_path),
        "latest_analysis": latest_analysis_path,
    }


def build_github_comment(results: Dict, report_paths: Dict, jira_results: List[Dict]) -> str:
    """Build a concise commit comment with issues, tests, and recommendations."""
    issues, summary = parse_scanner_issues(results)
    critical_high = high_severity_issues(results)
    tests = results.get("test_case_generator", "")
    report = results.get("report_writer", "")

    issue_lines = []
    for issue in critical_high[:10]:
        issue_lines.append(
            f"- [{str(issue.get('severity', 'unknown')).upper()}] "
            f"{issue.get('file', 'unknown')}: {issue.get('description', issue.get('issue', 'No description'))}"
        )
    if not issue_lines:
        issue_lines.append("- No critical or high severity issues detected.")

    created_count = sum(1 for item in jira_results if item.get("success"))
    tests_excerpt = tests[:1200] if tests else "No tests generated."
    report_excerpt = report[:1200] if report else "No report generated."

    return f"""## Automated Code Quality Analysis

**Total issues:** {summary.get('total_issues', len(issues))}
**Critical:** {summary.get('critical', 0)}
**High:** {summary.get('high', 0)}
**Medium:** {summary.get('medium', 0)}
**Low:** {summary.get('low', 0)}

### Critical and High Issues
{chr(10).join(issue_lines)}

### Generated Tests
```python
{tests_excerpt}
```

### Recommendations
{report_excerpt}

### Automation
- Markdown report: `{report_paths.get('markdown', 'not saved')}`
- JSON report: `{report_paths.get('json', 'not saved')}`
- Jira tickets created: {created_count}

---
Powered by AI Code Review & QA Intelligence Platform.
"""


def get_files_for_analysis(
    repo_url: str,
    commit_sha: str,
    previous_sha: Optional[str],
    scope: Optional[Dict] = None,
) -> Tuple[str, List[str], str, List[str]]:
    """Fetch only added/modified files needed for this commit analysis."""
    temp_dir = tempfile.mkdtemp(prefix="webhook_analysis_")
    analyzed_files: List[str] = []
    all_changed_files: List[str] = []

    try:
        if previous_sha:
            changes = github_integration.get_changed_files_between_commits(
                repo_url, previous_sha, commit_sha, GITHUB_TOKEN
            )
            if changes.get("success"):
                changed = changes.get("modified", []) + changes.get("added", [])
                for item in changed:
                    path = item.get("path", "")
                    all_changed_files.append(path)
                    if not path.endswith(CODE_EXTENSIONS):
                        continue
                    if not filter_monitored_paths([path], scope):
                        continue
                    content = github_integration.fetch_file_content(
                        repo_url, path, commit_sha, GITHUB_TOKEN
                    )
                    if content.get("success"):
                        written = safe_write_temp_file(temp_dir, path, content["content"])
                        if written:
                            analyzed_files.append(written)
                return (
                    temp_dir,
                    analyzed_files,
                    f"Diff {previous_sha[:8]}..{commit_sha[:8]}",
                    collect_unmonitored_paths(all_changed_files, scope),
                )
            logger.warning("Commit comparison failed, falling back to commit files: %s", changes.get("error"))

        commit_data = github_integration.fetch_commit_changes(repo_url, commit_sha, GITHUB_TOKEN)
        if not commit_data.get("success"):
            raise RuntimeError(commit_data.get("error", "Unable to fetch commit changes"))

        for path, file_data in commit_data.get("changed_files", {}).items():
            all_changed_files.append(path)
            if not path.endswith(CODE_EXTENSIONS):
                continue
            if not filter_monitored_paths([path], scope):
                continue
            written = safe_write_temp_file(temp_dir, path, file_data.get("content", ""))
            if written:
                analyzed_files.append(written)

        return (
            temp_dir,
            analyzed_files,
            f"Commit {commit_sha[:8]}",
            collect_unmonitored_paths(all_changed_files, scope),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def analyze_repository(
    repo_url: str,
    commit_sha: Optional[str] = None,
    before_sha: Optional[str] = None,
    force: bool = False,
    branch_name: str = "",
) -> Dict:
    """Run the autonomous code quality analysis pipeline for a repo commit."""
    started_at = datetime.now().isoformat()
    tracker = CommitTracker()
    temp_dir = None
    logs: List[str] = []
    scope = load_repo_scope_for_repo(repo_url)

    def on_log(message: str) -> None:
        logs.append(str(message))
        logger.info(message)

    try:
        save_webhook_status(
            "processing",
            repo_url=repo_url,
            commit_sha=commit_sha,
            message="Webhook analysis is running.",
            updated_at=started_at,
        )

        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not configured")
        if not GITHUB_TOKEN:
            raise RuntimeError("GITHUB_TOKEN is not configured")
        if not repo_url:
            raise RuntimeError("repo_url is required")

        if branch_name and not branch_matches(scope, branch_name):
            record = {
                "timestamp": started_at,
                "repo_url": repo_url,
                "commit_sha": commit_sha,
                "status": "skipped",
                "message": f"Ignored push from branch: {branch_name}",
                "branch": branch_name,
            }
            save_webhook_status(
                "skipped",
                repo_url=repo_url,
                commit_sha=commit_sha,
                message=record["message"],
            )
            record_history(record)
            return record

        if not commit_sha:
            latest = github_integration.get_latest_commit_sha(repo_url, GITHUB_TOKEN, branch=scope.get("branch") if scope else None)
            if not latest.get("success"):
                raise RuntimeError(latest.get("error", "Unable to get latest commit"))
            commit_sha = latest["sha"]

        if commit_sha.startswith("000000"):
            record = {
                "timestamp": started_at,
                "repo_url": repo_url,
                "commit_sha": commit_sha,
                "status": "skipped",
                "message": "Branch deletion event ignored",
            }
            save_webhook_status(
                "skipped",
                repo_url=repo_url,
                commit_sha=commit_sha,
                message=record["message"],
            )
            record_history(record)
            return record

        last_commit = tracker.get_last_commit(repo_url)
        previous_sha = before_sha if before_sha and not before_sha.startswith("000000") else last_commit

        if not force and last_commit == commit_sha:
            record = {
                "timestamp": started_at,
                "repo_url": repo_url,
                "commit_sha": commit_sha,
                "status": "skipped",
                "message": "Commit already analyzed",
            }
            save_webhook_status(
                "skipped",
                repo_url=repo_url,
                commit_sha=commit_sha,
                message=record["message"],
            )
            record_history(record)
            return record

        temp_dir, analyzed_files, analysis_type, unmonitored_changed_files = get_files_for_analysis(
            repo_url,
            commit_sha,
            previous_sha,
            scope,
        )
        if not analyzed_files:
            tracker.save_commit(repo_url, commit_sha, [])
            record = {
                "timestamp": started_at,
                "repo_url": repo_url,
                "commit_sha": commit_sha,
                "status": "skipped",
                "message": "No monitored code files changed for analysis",
                "analyzed_files": [],
                "unmonitored_changed_files": unmonitored_changed_files,
            }
            save_webhook_status(
                "skipped",
                repo_url=repo_url,
                commit_sha=commit_sha,
                message=record["message"],
            )
            record_history(record)
            return record

        client = Groq(api_key=GROQ_API_KEY)
        results = loop.run(
            repo_path=temp_dir,
            client=client,
            model=DEFAULT_MODEL,
            on_log=on_log,
            max_loops=3,
        )

        success = bool(results.get("report_writer"))
        report_paths = save_reports(
            repo_url,
            commit_sha,
            results,
            analyzed_files,
            logs,
            branch_name=branch_name,
            unmonitored_changed_files=unmonitored_changed_files,
        )
        jira_results = create_jira_tickets(results)

        if GITHUB_TOKEN:
            comment = build_github_comment(results, report_paths, jira_results)
            github_integration.post_review_comment(repo_url, commit_sha, comment, GITHUB_TOKEN)

        if success:
            tracker.save_commit(repo_url, commit_sha, analyzed_files)

        record = {
            "timestamp": started_at,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "analysis_type": analysis_type,
            "status": "completed" if success else "failed",
            "success": success,
            "analyzed_files": analyzed_files,
            "unmonitored_changed_files": unmonitored_changed_files,
            "branch": branch_name,
            "reports": report_paths,
            "jira": jira_results,
            "logs": logs[-20:],
        }
        save_webhook_status(
            "completed" if success else "failed",
            repo_url=repo_url,
            commit_sha=commit_sha,
            message="Latest analysis written to reports/latest_analysis.json" if success else "Analysis completed without a report.",
        )
        record_history(record)
        return record

    except Exception as e:
        logger.exception("Analysis failed")
        record = {
            "timestamp": started_at,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "status": "failed",
            "success": False,
            "error": str(e),
            "logs": logs[-20:],
        }
        save_webhook_status(
            "failed",
            repo_url=repo_url,
            commit_sha=commit_sha,
            message=str(e),
        )
        record_history(record)
        return record
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "running",
        "service": "Code Quality Monitor Webhook Server",
        "analyses_completed": len(analysis_history),
    }


@app.get("/history")
async def get_history():
    """Get recent analysis history."""
    return {
        "total": len(analysis_history),
        "analyses": analysis_history[-10:],
    }


@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive GitHub push webhooks and queue background analysis."""
    payload = await request.body()
    if not verify_signature(request, payload):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        return {"message": "Webhook configured successfully"}
    if event_type != "push":
        return {"status": "ignored", "message": f"Event type '{event_type}' not handled"}

    repo_url = data.get("repository", {}).get("html_url")
    commit_sha = data.get("after")
    before_sha = data.get("before")
    branch_name = extract_branch_name(data.get("ref", ""))

    if not repo_url or not commit_sha:
        raise HTTPException(status_code=400, detail="Push payload missing repository or commit")

    save_webhook_status(
        "queued",
        repo_url=repo_url,
        commit_sha=commit_sha,
        message="Webhook received. Analysis queued.",
    )
    background_tasks.add_task(analyze_repository, repo_url, commit_sha, before_sha, False, branch_name)
    return {
        "status": "accepted",
        "message": "Analysis queued",
        "repo": repo_url,
        "commit": commit_sha[:8],
    }


@app.post("/analyze")
async def manual_analysis(
    repo_url: str,
    background_tasks: BackgroundTasks,
    commit_sha: Optional[str] = None,
    force: bool = False,
):
    """Trigger analysis manually via API."""
    save_webhook_status(
        "queued",
        repo_url=repo_url,
        commit_sha=commit_sha,
        message="Manual analysis queued.",
    )
    background_tasks.add_task(analyze_repository, repo_url, commit_sha, None, force, "")
    return {
        "status": "accepted",
        "message": "Analysis queued",
        "repo": repo_url,
        "commit": commit_sha[:8] if commit_sha else "latest",
    }


if __name__ == "__main__":
    logger.info("Starting Code Quality Monitor Webhook Server")
    logger.info("Webhook URL: http://localhost:8000/webhook/github")
    logger.info("History URL: http://localhost:8000/history")
    logger.info("Manual trigger: POST http://localhost:8000/analyze")
    uvicorn.run(app, host="0.0.0.0", port=8000)
