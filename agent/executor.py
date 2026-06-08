# ============================================================
# executor.py — Routes plan steps to tool functions
# ============================================================
# PURPOSE:
#   The executor takes the JSON plan from planner.py and runs each
#   step by calling the correct tool function. It is the "hands"
#   of the agent — it does the actual work.
#
# INFORMATION FLOW (4-tool pipeline):
#   problem_definer    → only needs the idea (first in chain)
#   solution_architect → needs idea + problem analysis
#   idea_challenger    → needs idea + problem + solution (challenges both)
#   submission_writer  → needs EVERYTHING including challenger output
#
# RESILIENCE:
#   Each tool call is wrapped in retry logic with exponential backoff.
#
# HOW TO CUSTOMISE (vibe coding prompt):
#   "Add a new elif branch in executor.py for a tool called
#    market_validator. Import it from agent.tools.market_validator
#    and pass it the idea and problem_definer results."
# ============================================================

import time
import os

from agent.tools import code_scanner, test_case_generator, issue_prioritizer, report_writer

# ── RETRY CONFIGURATION ─────────────────────────────────────────
MAX_RETRIES = 2
RETRY_DELAY = 2


def _collect_python_files(repo_path):
    """Collect Python files for optional RAG indexing."""
    python_files = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', 'node_modules', '.venv', 'venv', '.rag_vector_db']]
        for filename in files:
            if filename.endswith('.py') and filename != '__init__.py':
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        python_files.append({
                            "filename": os.path.relpath(filepath, repo_path),
                            "content": f.read(),
                        })
                except Exception:
                    continue
    return python_files


def _build_rag_context(repo_path, on_log):
    """Build repository context with RAG when optional dependencies are available."""
    try:
        from rag import RAGRetriever

        python_files = _collect_python_files(repo_path)
        if not python_files:
            return ""

        store_path = os.path.join(repo_path, ".rag_vector_db")
        rag = RAGRetriever(store_path=store_path)
        rag.clear_index()
        rag.index_repository(python_files)

        queries = [
            "security vulnerabilities hardcoded credentials sql injection",
            "missing error handling risky code paths",
            "test coverage gaps edge cases",
        ]
        seen = set()
        context_parts = ["## Retrieved Repository Context"]
        for query in queries:
            for result in rag.retrieve_context(query, k=2):
                key = (result.get("filename"), result.get("chunk_id"))
                if key in seen:
                    continue
                seen.add(key)
                context_parts.append(
                    f"\n### {result.get('filename')} lines {result.get('start_line')}-{result.get('end_line')}\n"
                    f"```python\n{result.get('text', '')[:1000]}\n```"
                )

        if len(context_parts) == 1:
            return ""
        on_log(f"RAG context built from {len(python_files)} Python file(s)")
        return "\n".join(context_parts)
    except Exception as e:
        on_log(f"RAG context unavailable: {e}")
        return ""


def _run_with_retry(fn, tool_name, on_log):
    """Run a tool function with retry logic for transient API errors."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                wait = RETRY_DELAY * (2 ** (attempt - 1))
                on_log(f"Tool {tool_name} error (attempt {attempt}): {e} — retrying in {wait}s...")
                time.sleep(wait)
            else:
                on_log(f"Tool {tool_name} failed after {attempt} attempts: {e}")
                raise last_error


def run_plan(plan, repo_path, client, model, on_log):
    """Execute each step in the plan by routing to the correct tool."""

    results = {}
    rag_context = _build_rag_context(repo_path, on_log)
    
    on_log(f"[DEBUG] Repository path: {repo_path}")
    on_log(f"[DEBUG] Checking if path exists: {os.path.exists(repo_path)}")
    if os.path.exists(repo_path):
        import glob
        py_files = glob.glob(os.path.join(repo_path, "**/*.py"), recursive=True)
        on_log(f"[DEBUG] Python files found: {len(py_files)}")

    for step in plan:
        tool = step["tool"]
        on_log(f"Running tool: {tool} — {step['reason']}")

        # ── ROUTE TO THE CORRECT TOOL ────────────────────────────────
        if tool == "code_scanner":
            results["code_scanner"] = _run_with_retry(
                lambda: code_scanner.run(repo_path, client, model, rag_context=rag_context),
                tool, on_log,
            )

        elif tool == "test_case_generator":
            scanner_output = results.get("code_scanner", "")
            results["test_case_generator"] = _run_with_retry(
                lambda: test_case_generator.run(repo_path, scanner_output, client, model, rag_context=rag_context),
                tool, on_log,
            )

        elif tool == "issue_prioritizer":
            scanner_output = results.get("code_scanner", "")
            test_output = results.get("test_case_generator", "")
            results["issue_prioritizer"] = _run_with_retry(
                lambda: issue_prioritizer.run(scanner_output, test_output, client, model),
                tool, on_log,
            )

        elif tool == "report_writer":
            scanner_output = results.get("code_scanner", "")
            test_output = results.get("test_case_generator", "")
            priority_output = results.get("issue_prioritizer", "")
            results["report_writer"] = _run_with_retry(
                lambda: report_writer.run(scanner_output, test_output, priority_output, client, model, rag_context=rag_context),
                tool, on_log,
            )

        else:
            on_log(f"Unknown tool: {tool} — skipping")
            continue

        on_log(f"Tool {tool} complete ✓")

    return results
