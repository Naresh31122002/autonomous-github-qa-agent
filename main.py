# ============================================================
# main.py — Streamlit UI for Code Quality Monitor (Phase 4)
# ============================================================
# PHASE 4: GitHub Integration + Beautiful UI
# - GitHub repository input with file selection
# - Original 5-step visualization (step cards)
# - Activity log with color coding
# - Tabbed results view
# - Commit tracking for changed files
# ============================================================

import os
import datetime
import json
import tempfile
import shutil
import logging
import time
import streamlit as st
from dotenv import load_dotenv
from groq import Groq
from agent import loop
from agent.tools import github_integration
from components.repo_selector import render_setup_panel
from components.scope_editor import render_new_files_panel, render_scope_editor
from utils import CommitTracker
from utils.report_persistence import (
    LATEST_ANALYSIS_PATH,
    WEBHOOK_STATUS_PATH,
    build_analysis_payload,
    get_file_signature,
    load_latest_analysis,
    load_ui_state,
    load_webhook_status,
    save_latest_analysis,
    save_ui_state,
)
from utils.repo_scope import (
    REPO_SCOPE_PATH,
    append_files_to_scope,
    expand_monitored_files,
    load_repo_scope_for_repo,
    normalize_path,
    save_repo_scope,
)

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    import streamlit.components.v1 as components

    def st_autorefresh(interval=5000, key="autorefresh"):
        refresh_key = json.dumps(key)
        components.html(
            f"""
            <script>
                const key = {refresh_key};
                const interval = {int(interval)};
                const parentWindow = window.parent;
                if (!parentWindow.__streamlitReloadTimers) {{
                    parentWindow.__streamlitReloadTimers = {{}};
                }}
                if (parentWindow.__streamlitReloadTimers[key]) {{
                    clearTimeout(parentWindow.__streamlitReloadTimers[key]);
                }}
                parentWindow.__streamlitReloadTimers[key] = setTimeout(() => {{
                    parentWindow.location.reload();
                }}, interval);
            </script>
            """,
            height=0,
        )
        return 0

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("main")

def debug_log(msg):
    logger.debug(msg)

def start_manual_analysis():
    debug_log("[DEBUG] Callback: start_manual_analysis")
    st.session_state.manual_analysis_running = True

def start_fetch():
    debug_log("[DEBUG] Callback: start_fetch")
    st.session_state.fetch_in_progress = True

# ── LOAD ENVIRONMENT ─────────────────────────────────────────────
load_dotenv()

# ── PAGE CONFIG ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Code Quality Monitor — Autonomous AI Agent",
    page_icon="🔍",
    layout="wide",
)

# ── INITIALIZE SESSION STATE ────────────────────────────────────
if 'repo_files' not in st.session_state:
    st.session_state.repo_files = []
if 'changed_files' not in st.session_state:
    st.session_state.changed_files = []
if 'selected_files' not in st.session_state:
    st.session_state.selected_files = []
if 'current_commit' not in st.session_state:
    st.session_state.current_commit = None
if 'repo_url' not in st.session_state:
    st.session_state.repo_url = ""
if 'current_page' not in st.session_state:
    st.session_state.current_page = 1
if 'use_github' not in st.session_state:
    st.session_state.use_github = False
if 'temp_dir' not in st.session_state:
    st.session_state.temp_dir = None
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'analysis_results' not in st.session_state:
    st.session_state.analysis_results = None
if 'analysis_file_signature' not in st.session_state:
    st.session_state.analysis_file_signature = None
if 'webhook_status' not in st.session_state:
    st.session_state.webhook_status = None
if 'webhook_status_signature' not in st.session_state:
    st.session_state.webhook_status_signature = None
if 'report_view' not in st.session_state:
    st.session_state.report_view = "Issue Summary"
if 'repo_branches' not in st.session_state:
    st.session_state.repo_branches = []
if 'selected_branch' not in st.session_state:
    st.session_state.selected_branch = ""
if 'repo_default_branch' not in st.session_state:
    st.session_state.repo_default_branch = ""
if 'selected_folders' not in st.session_state:
    st.session_state.selected_folders = []
if 'loaded_branch' not in st.session_state:
    st.session_state.loaded_branch = ""
if 'scope_initialized' not in st.session_state:
    st.session_state.scope_initialized = False
if 'new_scope_candidates' not in st.session_state:
    st.session_state.new_scope_candidates = []

REPORT_VIEWS = [
    "Issue Summary",
    "Generated Tests",
    "Full Report",
    "Agent Reasoning",
]


def get_query_param(name, default=None):
    """Read query params across Streamlit versions."""
    try:
        value = st.query_params.get(name, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value
    except Exception:
        params = st.experimental_get_query_params()
        values = params.get(name, [default])
        return values[0] if values else default


def set_query_param(name, value):
    """Write a single query param across Streamlit versions."""
    try:
        st.query_params[name] = value
    except Exception:
        st.experimental_set_query_params(**{name: value})


def format_relative_time(timestamp):
    """Render a human-friendly relative timestamp."""
    if not timestamp:
        return "unknown"

    try:
        updated_at = datetime.datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp

    delta = datetime.datetime.now(updated_at.tzinfo) - updated_at
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hr ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def sync_persisted_analysis():
    """Load persisted webhook analysis and status into session state.

    On cold start (no loaded_branch yet), populate all UI fields from the
    persisted report so the user sees the last analysis.  Once the user has
    an active session (loaded_branch is set), only sync the report payload
    itself — never overwrite live UI fields like current_commit,
    selected_files, selected_branch, or selected_folders.
    """
    is_cold_start = not st.session_state.loaded_branch

    analysis_signature = get_file_signature(LATEST_ANALYSIS_PATH)
    if (
        st.session_state.analysis_result is None
        or analysis_signature != st.session_state.analysis_file_signature
    ):
        latest_analysis = load_latest_analysis()
        if latest_analysis:
            analysis_signature = get_file_signature(LATEST_ANALYSIS_PATH)
            st.session_state.analysis_result = latest_analysis
            st.session_state.analysis_results = None
            st.session_state.analysis_file_signature = analysis_signature
            st.session_state.new_scope_candidates = latest_analysis.get("unmonitored_changed_files", [])

            # Only populate UI navigation fields on cold start so we
            # don't clobber the user's live branch/file selections.
            if is_cold_start:
                logger.debug("Cold start: populating UI state from persisted analysis")
                st.session_state.repo_url = latest_analysis.get("repo_url", st.session_state.repo_url)
                st.session_state.current_commit = latest_analysis.get("commit_sha", st.session_state.current_commit)
                st.session_state.selected_files = latest_analysis.get("analyzed_files", st.session_state.selected_files)
                st.session_state.use_github = "github.com" in latest_analysis.get("repo_url", "")
            else:
                logger.debug("Active session: synced report payload, skipped UI field overwrite")

    status_signature = get_file_signature(WEBHOOK_STATUS_PATH)
    if status_signature != st.session_state.webhook_status_signature:
        st.session_state.webhook_status = load_webhook_status()
        st.session_state.webhook_status_signature = status_signature

    # Only restore scope on cold start — don't overwrite live selections.
    if is_cold_start:
        scope = load_repo_scope_for_repo(st.session_state.repo_url)
        if scope:
            st.session_state.selected_branch = scope.get("branch", st.session_state.selected_branch)
            st.session_state.selected_folders = scope.get("selected_folders", st.session_state.selected_folders)


def bootstrap_report_view():
    """Restore the last selected report view on hard refresh."""
    query_view = get_query_param("view")
    persisted_view = load_ui_state().get("report_view")
    candidate = query_view or persisted_view or st.session_state.report_view
    if candidate not in REPORT_VIEWS:
        candidate = REPORT_VIEWS[0]
    st.session_state.report_view = candidate
    set_query_param("view", candidate)


def persist_report_view(view_name):
    """Persist report view in both URL and disk state."""
    if view_name not in REPORT_VIEWS:
        return
    if st.session_state.report_view != view_name:
        st.session_state.report_view = view_name
    set_query_param("view", view_name)
    save_ui_state(view_name)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_list_branches(repo_url, github_token):
    """Cached branch listing — avoids repeated API calls within TTL."""
    return github_integration.list_branches(repo_url, github_token)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_list_files(repo_url, github_token, branch_name):
    """Cached file tree — avoids repeated API calls within TTL."""
    return github_integration.list_all_code_files(repo_url, github_token, branch=branch_name)


def load_branch_metadata(repo_url, branch_name, github_token):
    """Fetch repository files and latest commit for a branch."""
    logger.debug("Loading branch metadata for repo=%s branch=%s", repo_url, branch_name)

    t0 = time.time()
    files_result = _cached_list_files(repo_url, github_token, branch_name)
    logger.debug("[Fetch] Repo tree loaded in %.2f sec (%d files)", time.time() - t0, len(files_result.get('files', [])))
    if not files_result['success']:
        logger.debug("File tree load failed: %s", files_result.get("error"))
        raise RuntimeError(files_result['error'])

    t1 = time.time()
    commit_result = github_integration.get_latest_commit_sha(repo_url, github_token, branch=branch_name)
    logger.debug("[Fetch] Commit SHA loaded in %.2f sec", time.time() - t1)
    if not commit_result['success']:
        logger.debug("Commit load failed: %s", commit_result.get("error"))
        raise RuntimeError(commit_result['error'])

    st.session_state.repo_files = files_result['files']
    st.session_state.current_commit = commit_result['sha']
    st.session_state.loaded_branch = branch_name
    st.session_state.current_page = 1


def initialize_scope_selection(repo_url):
    """Apply saved scope or select all files by default."""
    scope = load_repo_scope_for_repo(repo_url)
    if scope:
        st.session_state.selected_branch = scope.get("branch", st.session_state.selected_branch)
        st.session_state.selected_files = scope.get("selected_files", [])
        st.session_state.selected_folders = scope.get("selected_folders", [])
    else:
        st.session_state.selected_files = [normalize_path(item['path']) for item in st.session_state.repo_files]
        st.session_state.selected_folders = []
    st.session_state.scope_initialized = True


def persist_monitoring_scope(repo_url):
    """Save branch/file/folder monitoring settings."""
    save_repo_scope(
        {
            "repo_url": repo_url,
            "branch": st.session_state.selected_branch,
            "selected_files": st.session_state.selected_files,
            "selected_folders": st.session_state.selected_folders,
            "monitoring_enabled": True,
        }
    )


if 'manual_analysis_running' not in st.session_state:
    st.session_state.manual_analysis_running = False
if 'fetch_in_progress' not in st.session_state:
    st.session_state.fetch_in_progress = False

disable_autorefresh = st.session_state.manual_analysis_running or st.session_state.fetch_in_progress
if not disable_autorefresh:
    st_autorefresh(interval=5000, key="webhook_refresh")
else:
    debug_log("[DEBUG] Autorefresh disabled because action is in progress")

sync_persisted_analysis()
bootstrap_report_view()
debug_log(f"--- RERUN START --- selected_branch={st.session_state.get('selected_branch')}, loaded_branch={st.session_state.get('loaded_branch')}, repo_url={st.session_state.get('repo_url')}, files_count={len(st.session_state.get('repo_files', [])) if st.session_state.get('repo_files') else 0}, selected_files_count={len(st.session_state.get('selected_files', [])) if st.session_state.get('selected_files') else 0}")

# ── CUSTOM CSS ───────────────────────────────────────────────────
st.markdown("""
<style>
    /* Cyberpunk Violet Theme - Unique Design */
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@300;500;700&display=swap');
    
    .stApp {
        background: linear-gradient(135deg, #0a0015 0%, #1a0033 50%, #0f001a 100%);
        background-attachment: fixed;
    }
    
    /* Animated background grid */
    .stApp::before {
        content: '';
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background-image: 
            linear-gradient(rgba(138, 43, 226, 0.1) 1px, transparent 1px),
            linear-gradient(90deg, rgba(138, 43, 226, 0.1) 1px, transparent 1px);
        background-size: 50px 50px;
        pointer-events: none;
        z-index: 0;
    }
    
    /* Step cards - Cyberpunk style with violet glow */
    .step-card {
        border-radius: 0 15px 0 15px;
        padding: 24px 16px;
        text-align: left;
        margin: 8px;
        font-size: 14px;
        min-height: 130px;
        transition: all 0.4s cubic-bezier(0.68, -0.55, 0.265, 1.55);
        position: relative;
        overflow: hidden;
        font-family: 'Rajdhani', sans-serif;
        backdrop-filter: blur(10px);
    }
    .step-card::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        width: 4px;
        height: 100%;
        background: linear-gradient(180deg, #8b5cf6, #a78bfa);
    }
    .step-pending {
        background: rgba(30, 20, 50, 0.6);
        border: 2px solid rgba(139, 92, 246, 0.3);
        color: #9ca3af;
        box-shadow: inset 0 0 20px rgba(139, 92, 246, 0.1);
    }
    .step-active {
        background: rgba(76, 29, 149, 0.4);
        border: 2px solid #a78bfa;
        color: #e9d5ff;
        animation: glowPulse 2s ease-in-out infinite;
        box-shadow: 0 0 30px rgba(167, 139, 250, 0.6), inset 0 0 30px rgba(167, 139, 250, 0.2);
        transform: scale(1.05);
    }
    @keyframes glowPulse {
        0%, 100% { 
            box-shadow: 0 0 30px rgba(167, 139, 250, 0.6), inset 0 0 30px rgba(167, 139, 250, 0.2);
            border-color: #a78bfa;
        }
        50% { 
            box-shadow: 0 0 50px rgba(167, 139, 250, 0.9), inset 0 0 40px rgba(167, 139, 250, 0.3);
            border-color: #c4b5fd;
        }
    }
    .step-done {
        background: rgba(5, 150, 105, 0.3);
        border: 2px solid #10b981;
        color: #d1fae5;
        box-shadow: 0 0 20px rgba(16, 185, 129, 0.4);
    }
    .step-failed {
        background: rgba(153, 27, 27, 0.3);
        border: 2px solid #ef4444;
        color: #fecaca;
        box-shadow: 0 0 20px rgba(239, 68, 68, 0.4);
    }
    .step-skipped {
        background: rgba(76, 29, 149, 0.2);
        border: 2px solid #6b21a8;
        color: #c4b5fd;
        box-shadow: 0 0 15px rgba(107, 33, 168, 0.3);
    }

    /* Activity log - Futuristic terminal */
    .log-box {
        background: rgba(10, 0, 30, 0.9);
        color: #c4b5fd;
        font-family: 'Courier New', monospace;
        font-size: 13px;
        padding: 20px;
        border-radius: 0 20px 0 20px;
        max-height: 280px;
        overflow-y: auto;
        white-space: pre-wrap;
        word-wrap: break-word;
        border: 2px solid rgba(167, 139, 250, 0.4);
        box-shadow: 0 0 30px rgba(167, 139, 250, 0.3), inset 0 0 30px rgba(10, 0, 30, 0.8);
        position: relative;
    }
    .log-box::before {
        content: '> SYSTEM LOG';
        position: absolute;
        top: -12px;
        left: 20px;
        background: #1a0033;
        padding: 2px 12px;
        color: #a78bfa;
        font-size: 11px;
        font-weight: bold;
        letter-spacing: 2px;
    }
    .log-time { color: #6b7280; }
    .log-tool { color: #a78bfa; font-weight: 700; text-shadow: 0 0 10px rgba(167, 139, 250, 0.5); }
    .log-pass { color: #34d399; font-weight: 700; text-shadow: 0 0 10px rgba(52, 211, 153, 0.5); }
    .log-fail { color: #f87171; font-weight: 700; text-shadow: 0 0 10px rgba(248, 113, 113, 0.5); }
    .log-loop { color: #c4b5fd; font-weight: bold; font-size: 15px; text-shadow: 0 0 15px rgba(196, 181, 253, 0.8); }

    /* Branding - Cyberpunk violet gradient */
    .brand-title {
        text-align: center;
        font-size: 3.5em;
        font-weight: 900;
        font-family: 'Orbitron', sans-serif;
        background: linear-gradient(135deg, #8b5cf6 0%, #c4b5fd 50%, #ffffff 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 12px;
        letter-spacing: 3px;
        text-transform: uppercase;
        filter: drop-shadow(0 0 20px rgba(139, 92, 246, 0.6));
        animation: titleGlow 3s ease-in-out infinite;
    }
    @keyframes titleGlow {
        0%, 100% { filter: drop-shadow(0 0 20px rgba(139, 92, 246, 0.6)); }
        50% { filter: drop-shadow(0 0 35px rgba(139, 92, 246, 0.9)); }
    }
    .brand-sub {
        text-align: center;
        font-size: 1.2em;
        color: #c4b5fd;
        margin-top: 8px;
        margin-bottom: 12px;
        font-weight: 300;
        font-family: 'Rajdhani', sans-serif;
        letter-spacing: 2px;
        text-transform: uppercase;
    }
    .brand-badge {
        text-align: center;
        margin-bottom: 32px;
    }
    .brand-badge span {
        background: linear-gradient(135deg, #6b21a8 0%, #8b5cf6 100%);
        color: white;
        padding: 8px 24px;
        border-radius: 0 15px 0 15px;
        font-size: 0.75em;
        font-weight: 700;
        letter-spacing: 2px;
        text-transform: uppercase;
        font-family: 'Orbitron', sans-serif;
        box-shadow: 0 0 25px rgba(139, 92, 246, 0.6);
        border: 2px solid rgba(167, 139, 250, 0.5);
    }

    /* Loop counter - Futuristic */
    .loop-counter {
        text-align: center;
        background: rgba(76, 29, 149, 0.4);
        color: #e9d5ff;
        padding: 16px 28px;
        border-radius: 0 20px 0 20px;
        font-family: 'Orbitron', sans-serif;
        font-weight: bold;
        font-size: 16px;
        margin: 16px 0;
        border: 2px solid #a78bfa;
        box-shadow: 0 0 30px rgba(167, 139, 250, 0.5), inset 0 0 20px rgba(76, 29, 149, 0.3);
        letter-spacing: 3px;
    }

    /* Tool pipeline - Asymmetric cards */
    .pipeline-card {
        background: rgba(30, 20, 50, 0.6);
        border: 2px solid rgba(139, 92, 246, 0.5);
        border-radius: 0 15px 0 15px;
        padding: 20px;
        text-align: center;
        font-size: 13px;
        color: #e9d5ff;
        transition: all 0.4s ease;
        font-family: 'Rajdhani', sans-serif;
        position: relative;
        overflow: hidden;
    }
    .pipeline-card::after {
        content: '';
        position: absolute;
        top: -50%;
        left: -50%;
        width: 200%;
        height: 200%;
        background: linear-gradient(45deg, transparent, rgba(167, 139, 250, 0.1), transparent);
        transform: rotate(45deg);
        transition: all 0.6s ease;
    }
    .pipeline-card:hover {
        border-color: #c4b5fd;
        box-shadow: 0 0 30px rgba(196, 181, 253, 0.6);
        transform: translateY(-5px) scale(1.02);
    }
    .pipeline-card:hover::after {
        left: 100%;
    }
    
    /* Issue badges - Neon style */
    .issue-badge {
        display: inline-block;
        padding: 8px 16px;
        border-radius: 0 12px 0 12px;
        font-size: 11px;
        font-weight: 700;
        margin: 4px;
        text-transform: uppercase;
        letter-spacing: 1px;
        font-family: 'Orbitron', sans-serif;
        position: relative;
    }
    .badge-critical {
        background: rgba(153, 27, 27, 0.4);
        color: #fecaca;
        border: 2px solid #ef4444;
        box-shadow: 0 0 20px rgba(239, 68, 68, 0.6);
        animation: criticalBlink 1.5s ease-in-out infinite;
    }
    @keyframes criticalBlink {
        0%, 100% { box-shadow: 0 0 20px rgba(239, 68, 68, 0.6); }
        50% { box-shadow: 0 0 35px rgba(239, 68, 68, 0.9); }
    }
    .badge-high {
        background: rgba(146, 64, 14, 0.4);
        color: #fed7aa;
        border: 2px solid #f59e0b;
        box-shadow: 0 0 15px rgba(245, 158, 11, 0.5);
    }
    .badge-medium {
        background: rgba(113, 63, 18, 0.4);
        color: #fef3c7;
        border: 2px solid #fbbf24;
        box-shadow: 0 0 15px rgba(251, 191, 36, 0.5);
    }
    .badge-low {
        background: rgba(30, 58, 138, 0.4);
        color: #dbeafe;
        border: 2px solid #3b82f6;
        box-shadow: 0 0 15px rgba(59, 130, 246, 0.5);
    }
    
    /* Sidebar - Violet theme with better text visibility */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0a0015 0%, #1a0033 100%);
        border-right: 2px solid rgba(139, 92, 246, 0.3);
        box-shadow: 5px 0 30px rgba(139, 92, 246, 0.2);
    }
    section[data-testid="stSidebar"] * {
        color: #e9d5ff !important;
    }
    section[data-testid="stSidebar"] h1, 
    section[data-testid="stSidebar"] h2, 
    section[data-testid="stSidebar"] h3 {
        color: #ffffff !important;
    }
    section[data-testid="stSidebar"] label {
        color: #c4b5fd !important;
        font-weight: 600 !important;
    }
    section[data-testid="stSidebar"] .stMarkdown {
        color: #e9d5ff !important;
    }
    /* Fix code blocks in sidebar - violet background with white text */
    section[data-testid="stSidebar"] code {
        background: linear-gradient(135deg, #6b21a8 0%, #8b5cf6 100%) !important;
        color: #ffffff !important;
        padding: 4px 10px !important;
        border-radius: 0 8px 0 8px !important;
        border: 2px solid rgba(167, 139, 250, 0.5) !important;
        font-weight: 700 !important;
        box-shadow: 0 0 15px rgba(139, 92, 246, 0.4) !important;
    }
    section[data-testid="stSidebar"] pre {
        background: rgba(10, 0, 30, 0.9) !important;
        border: 2px solid rgba(139, 92, 246, 0.4) !important;
        border-radius: 8px !important;
        padding: 12px !important;
    }
    section[data-testid="stSidebar"] pre code {
        background: transparent !important;
        color: #c4b5fd !important;
        border: none !important;
        padding: 0 !important;
    }
    
    /* Buttons - Neon violet */
    .stButton>button {
        border-radius: 0 12px 0 12px;
        font-weight: 700;
        font-family: 'Rajdhani', sans-serif;
        letter-spacing: 1px;
        text-transform: uppercase;
        transition: all 0.3s ease;
        border: 2px solid transparent;
    }
    .stButton>button[kind="primary"] {
        background: linear-gradient(135deg, #6b21a8 0%, #8b5cf6 100%);
        border: 2px solid #a78bfa;
        color: white;
        box-shadow: 0 0 25px rgba(139, 92, 246, 0.5);
    }
    .stButton>button[kind="primary"]:hover {
        box-shadow: 0 0 40px rgba(139, 92, 246, 0.8);
        transform: translateY(-3px);
        border-color: #c4b5fd;
    }
    .stButton>button:not([kind="primary"]) {
        background: rgba(30, 20, 50, 0.6);
        border: 2px solid rgba(139, 92, 246, 0.4);
        color: #c4b5fd;
    }
    .stButton>button:not([kind="primary"]):hover {
        border-color: #a78bfa;
        box-shadow: 0 0 20px rgba(139, 92, 246, 0.4);
    }
    
    /* Input fields - Cyberpunk style with better visibility */
    .stTextInput>div>div>input, .stNumberInput>div>div>input {
        background: rgba(10, 0, 30, 0.9) !important;
        border: 2px solid rgba(139, 92, 246, 0.5) !important;
        border-radius: 0 10px 0 10px !important;
        color: #ffffff !important;
        font-family: 'Rajdhani', sans-serif !important;
        font-weight: 500 !important;
    }
    .stTextInput>div>div>input::placeholder {
        color: #a78bfa !important;
        opacity: 0.7 !important;
    }
    .stTextInput>div>div>input:focus, .stNumberInput>div>div>input:focus {
        border-color: #a78bfa !important;
        box-shadow: 0 0 20px rgba(167, 139, 250, 0.4) !important;
        color: #ffffff !important;
    }
    
    /* Radio buttons - Better visibility */
    .stRadio label {
        color: #e9d5ff !important;
        font-weight: 600 !important;
    }
    
    /* Checkboxes - Better visibility */
    .stCheckbox label {
        color: #e9d5ff !important;
    }
    
    /* Select boxes - Better visibility */
    .stSelectbox label {
        color: #c4b5fd !important;
        font-weight: 600 !important;
    }
    .stSelectbox>div>div {
        background: rgba(10, 0, 30, 0.9) !important;
        border: 2px solid rgba(139, 92, 246, 0.5) !important;
        color: #ffffff !important;
    }
    
    /* Multiselect - Better visibility */
    .stMultiSelect label {
        color: #c4b5fd !important;
        font-weight: 600 !important;
    }
    .stMultiSelect>div>div {
        background: rgba(10, 0, 30, 0.9) !important;
        border: 2px solid rgba(139, 92, 246, 0.5) !important;
        color: #ffffff !important;
    }
    
    /* Text areas - Better visibility */
    .stTextArea label {
        color: #c4b5fd !important;
        font-weight: 600 !important;
    }
    .stTextArea textarea {
        background: rgba(10, 0, 30, 0.9) !important;
        border: 2px solid rgba(139, 92, 246, 0.5) !important;
        color: #ffffff !important;
    }
    
    /* General text - Better visibility */
    p, span, div {
        color: #e9d5ff;
    }
    
    /* Headers - Bright white */
    h1, h2, h3, h4, h5, h6 {
        color: #ffffff !important;
    }
    
    /* File name badges in main content - Violet theme */
    .stCheckbox code, code {
        background: linear-gradient(135deg, #6b21a8 0%, #8b5cf6 100%) !important;
        color: #ffffff !important;
        padding: 4px 10px !important;
        border-radius: 0 8px 0 8px !important;
        border: 2px solid rgba(167, 139, 250, 0.5) !important;
        font-weight: 700 !important;
        box-shadow: 0 0 15px rgba(139, 92, 246, 0.4) !important;
    }
    
    /* Links - Violet with better visibility */
    a {
        color: #c4b5fd !important;
        text-decoration: none !important;
    }
    a:hover {
        color: #e9d5ff !important;
        text-shadow: 0 0 10px rgba(196, 181, 253, 0.6);
    }
    
    /* Tabs - Violet theme */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background: rgba(10, 0, 30, 0.6);
        padding: 8px;
        border-radius: 0 15px 0 15px;
        border: 2px solid rgba(139, 92, 246, 0.3);
    }
    .stTabs [data-baseweb="tab"] {
        background: rgba(30, 20, 50, 0.6);
        border-radius: 0 10px 0 10px;
        color: #c4b5fd;
        border: 2px solid transparent;
        font-family: 'Rajdhani', sans-serif;
        font-weight: 600;
    }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #6b21a8 0%, #8b5cf6 100%);
        border-color: #a78bfa;
        box-shadow: 0 0 20px rgba(139, 92, 246, 0.5);
    }
    
    /* Metrics - Futuristic cards */
    [data-testid="stMetricValue"] {
        font-family: 'Orbitron', sans-serif;
        font-size: 2em;
        color: #c4b5fd;
        text-shadow: 0 0 15px rgba(196, 181, 253, 0.6);
    }
    
    /* Scrollbar - Violet theme */
    ::-webkit-scrollbar {
        width: 10px;
        height: 10px;
    }
    ::-webkit-scrollbar-track {
        background: rgba(10, 0, 30, 0.8);
    }
    ::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, #6b21a8, #8b5cf6);
        border-radius: 5px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: linear-gradient(180deg, #8b5cf6, #a78bfa);
    }
</style>
""", unsafe_allow_html=True)

# ── HEADER & BRANDING ───────────────────────────────────────────
st.markdown('<div class="brand-title">Code Quality Monitor</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="brand-sub">Autonomous AI Agent for Code Analysis</div>',
    unsafe_allow_html=True,
)

webhook_status = st.session_state.webhook_status or {}
latest_analysis = st.session_state.analysis_result or {}

status_message = "🔴 Waiting for webhook trigger"
if webhook_status.get("status") in {"queued", "processing"}:
    status_message = (
        f"🟡 Webhook analysis in progress"
        f" ({format_relative_time(webhook_status.get('updated_at'))})"
    )
elif latest_analysis.get("timestamp"):
    status_message = f"🟢 Last webhook update: {format_relative_time(latest_analysis.get('timestamp'))}"

st.caption(status_message)

# ── AUTONOMY EXPLAINER ──────────────────────────────────────────
with st.expander("🤖 What makes this AUTONOMOUS (not a chatbot)?", expanded=False):
    st.markdown("""
**A chatbot** takes your input → calls an LLM once → returns whatever it gets. One shot. Done. No quality check.

**An autonomous agent** takes your *goal* and then:

| Step | What the Agent Does | Why It Matters |
|------|-------------------|---------------|
| **THINK** | Parses the repository path + any feedback from previous attempts | Context awareness — understands what needs improvement |
| **PLAN** | Asks the LLM to create a JSON action plan | Self-directed — it decides *what* to do, not you |
| **EXECUTE** | Runs 4 tools in sequence (scan → test → prioritize → report) | Tool use — each tool does one job, outputs chain together |
| **REVIEW** | Scores its own output 1-10 against quality criteria | **Self-evaluation — THIS is what makes it autonomous** |
| **UPDATE** | If score < 7, carries feedback into the next loop | Self-correction — it learns from its own mistakes |

The loop runs up to **3 times**. Each retry uses specific feedback to improve. This is the exact same architecture used in production AI agents — the domain changes, the loop doesn't.

**4-Tool Pipeline:**
1. **Code Scanner** → Detects bugs, complexity, security issues, code smells
2. **Test Case Generator** → Creates pytest test cases for uncovered functions
3. **Issue Prioritizer** → Ranks issues by severity (Critical/High/Medium/Low)
4. **Report Writer** → Compiles comprehensive markdown report with all findings

> **The one rule: THE LOOP NEVER CHANGES. THE TOOLS DO.**
    """)

# ── SIDEBAR ──────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ Configuration")

    api_key = st.text_input(
        "Groq API Key",
        type="password",
        value=os.getenv("GROQ_API_KEY", ""),
        help="Get your free key at console.groq.com",
    )
    
    github_token = st.text_input(
        "GitHub Token (for GitHub repos)",
        type="password",
        value=os.getenv("GITHUB_TOKEN", ""),
        help="Required only for GitHub repositories",
    )
    
    st.markdown("[🔑 Get Groq API key](https://console.groq.com)")
    st.markdown("[🔑 Get GitHub token](https://github.com/settings/tokens)")

    st.divider()

    st.markdown("### 🏗️ How Autonomy Works Here")
    st.markdown("""
    **The 5-step loop** (`agent/loop.py`):
    ```
    THINK → PLAN → EXECUTE → REVIEW → UPDATE
      ↑                                  |
      └──────── (if score < 7) ──────────┘
    ```

    **What each file does:**
    - `loop.py` — Runs the 5-step cycle (NEVER changes)
    - `planner.py` — LLM → JSON action plan
    - `executor.py` — Routes plan → tool functions
    - `reviewer.py` — Scores report quality
    - `tools/` — The domain-specific work (THESE change)

    **The 4 tools in this agent:**
    1. `code_scanner` — Finds bugs and issues
    2. `test_case_generator` — Creates test cases
    3. `issue_prioritizer` — Ranks by severity
    4. `report_writer` — Compiles final report

    **Why this is autonomous:**
    The **reviewer** scores the report quality. If the score < 7, it tells the agent *what* to improve — and the agent tries again.
    """)

    st.divider()

    model = st.selectbox(
        "Model",
        ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
        help="llama-3.1-8b-instant is fast, llama-3.3-70b-versatile is more capable",
    )
    
    st.divider()
    
    if st.session_state.repo_files:
        st.markdown("### 📊 Repository Stats")
        st.metric("Total Files", len(st.session_state.repo_files))
        st.metric("Changed Files", len(st.session_state.changed_files))
        st.metric("Selected", len(st.session_state.selected_files))

    st.divider()
    st.caption("Built for [StudAI Foundry](https://studai.one) — the national autonomous AI hackathon")

# ── MAIN AREA — GITHUB OR LOCAL ─────────────────────────────────
source_options = ["📦 GitHub Repository", "📁 Local Directory"]
default_source = source_options[0] if st.session_state.use_github or not st.session_state.repo_url else source_options[1]
source_type = st.radio(
    "Choose source:",
    source_options,
    horizontal=True,
    index=source_options.index(default_source),
)

if source_type == source_options[0]:
    st.session_state.use_github = True

    repo_url = st.text_input(
        "GitHub Repository URL",
        value=st.session_state.repo_url,
        placeholder="https://github.com/owner/repo",
        help="Enter the full GitHub repository URL",
    )

    col1, col2 = st.columns([3, 1])
    with col1:
        fetch_button = st.button("🔗 Fetch Repository", use_container_width=True, type="primary", on_click=start_fetch)
    with col2:
        if st.button("💡 Use Example", use_container_width=True):
            st.session_state.repo_url = "https://github.com/Kaushik-SPACEZ/github-agent"
            st.rerun()

    if fetch_button:
        if not repo_url.strip():
            st.session_state.fetch_in_progress = False
            st.error("⚠️ Please enter a GitHub repository URL")
            st.stop()

        fetch_start = time.time()
        logger.debug("[Fetch] Fetch repo clicked")

        try:
            # Stage 1: Validate repository
            with st.spinner("🔍 Validating repository..."):
                t0 = time.time()
                metadata_result = github_integration.get_repository_metadata(repo_url, github_token or None)
                logger.debug("[Fetch] Repo validated in %.2f sec", time.time() - t0)
                normalized_repo = metadata_result.get("normalized_repo", "")
                if not metadata_result["success"]:
                    st.error(f"❌ Repository not found: `{repo_url}`\n\nMake sure the path is correct and the repository exists.")
                    st.stop()

            st.session_state.repo_url = normalized_repo
            st.session_state.repo_default_branch = metadata_result["default_branch"]

            # Stage 2: Fetch branches
            with st.spinner("🌿 Fetching branches..."):
                t0 = time.time()
                branch_result = _cached_list_branches(normalized_repo, github_token or None)
                logger.debug("[Fetch] Branches loaded in %.2f sec", time.time() - t0)
                if branch_result["success"] and branch_result["branches"]:
                    st.session_state.repo_branches = branch_result["branches"]
                    st.session_state.repo_default_branch = branch_result["default_branch"]
                else:
                    fallback_branch = metadata_result["default_branch"]
                    st.session_state.repo_branches = [fallback_branch]
                    st.warning(
                        f"⚠️ Branch list could not be loaded. Falling back to default branch `{fallback_branch}`."
                    )

            scope = load_repo_scope_for_repo(normalized_repo)
            st.session_state.selected_branch = (
                scope.get("branch")
                if scope and scope.get("branch") in st.session_state.repo_branches
                else st.session_state.repo_default_branch
            )

            # Stage 3: Load repository files
            with st.spinner("📂 Loading repository files..."):
                load_branch_metadata(normalized_repo, st.session_state.selected_branch, github_token or None)

            # Stage 4: Prepare scope
            with st.spinner("⚙️ Preparing scope..."):
                st.session_state.changed_files = []
                st.session_state.scope_initialized = False
                initialize_scope_selection(normalized_repo)

            total_time = time.time() - fetch_start
            logger.debug("[Fetch] Total fetch completed in %.2f sec", total_time)
            logger.debug("[Fetch] UI rendered")

            st.session_state.fetch_in_progress = False
            st.success(
                f"✅ Repository loaded in {total_time:.1f}s. "
                f"{len(st.session_state.repo_branches)} branches, "
                f"{len(st.session_state.repo_files)} files ready."
            )
            st.rerun()
        except Exception as e:
            st.session_state.fetch_in_progress = False
            logger.exception("Repository fetch failed")
            st.error(f"❌ Error fetching repository: {str(e)}")
            st.stop()

    debug_log(f"Branch mismatch check: repo_url={bool(st.session_state.repo_url)}, branches={bool(st.session_state.repo_branches)}, selected_branch={st.session_state.selected_branch}, loaded_branch={st.session_state.loaded_branch}")
    if (
        st.session_state.repo_url
        and st.session_state.repo_branches
        and st.session_state.selected_branch
        and st.session_state.selected_branch != st.session_state.loaded_branch
    ):
        try:
            load_branch_metadata(st.session_state.repo_url, st.session_state.selected_branch, github_token or None)
            initialize_scope_selection(st.session_state.repo_url)
        except Exception as e:
            logger.exception("Branch reload failed")
            st.error(f"❌ Error loading branch: {str(e)}")
            st.stop()

    if st.session_state.repo_files and st.session_state.selected_branch:
        st.divider()
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("✅ Select All", use_container_width=True):
                st.session_state.selected_files = [normalize_path(f['path']) for f in st.session_state.repo_files]
                st.session_state.selected_folders = []
                st.rerun()
        with col2:
            if st.button("🗑️ Clear Selection", use_container_width=True):
                st.session_state.selected_files = []
                st.session_state.selected_folders = []
                st.rerun()
        with col3:
            st.caption(f"Scope file: `{REPO_SCOPE_PATH}`")

        setup_state = render_setup_panel(
            branches=st.session_state.repo_branches,
            selected_branch=st.session_state.selected_branch,
            repo_files=st.session_state.repo_files,
            selected_files=st.session_state.selected_files,
            selected_folders=st.session_state.selected_folders,
            default_branch=st.session_state.repo_default_branch,
            key_prefix="repo_setup",
        )
        st.session_state.selected_branch = setup_state['branch']
        st.session_state.selected_files = setup_state['selected_files']
        st.session_state.selected_folders = setup_state['selected_folders']

        effective_files = expand_monitored_files(
            st.session_state.repo_files,
            st.session_state.selected_files,
            st.session_state.selected_folders,
        )

        if effective_files:
            run_button = st.button(
                f"🔍 Analyze {len(effective_files)} Selected Files",
                use_container_width=True,
                type="primary",
                key="run_analysis",
                on_click=start_manual_analysis,
            )
            if run_button:
                debug_log("[DEBUG] Analyze button clicked")
                debug_log(f"[DEBUG] Selected branch: {st.session_state.selected_branch}")
                debug_log(f"[DEBUG] Selected files: {st.session_state.selected_files}")
                debug_log(f"[DEBUG] Repo URL: {st.session_state.repo_url}")
                debug_log(f"[DEBUG] Effective files count: {len(effective_files)}")
        else:
            st.warning("⚠️ No files selected. Select at least one file or folder to analyze.")
            run_button = False
    else:
        run_button = False

else:
    # Local directory mode
    st.session_state.use_github = False
    repo_path = st.text_input(
        "Repository Path",
        value="./mock_repo",
        placeholder="./mock_repo or path to your repository",
        help="Path to the code repository to analyze",
    )
    
    col_run, col_example = st.columns([3, 1])
    with col_run:
        run_button = st.button("🔍 Analyze Code Quality", use_container_width=True, type="primary", key="run_analysis_local", on_click=start_manual_analysis)
    with col_example:
        if st.button("💡 Use Example", use_container_width=True):
            st.session_state["example_repo"] = "./mock_repo"
            st.rerun()
    
    if "example_repo" in st.session_state:
        repo_path = st.session_state.pop("example_repo")

st.divider()

# ── STEP DISPLAY CONTAINERS ─────────────────────────────────────
step_container = st.container()
loop_label = st.empty()
log_header = st.empty()
log_container = st.empty()

# ── STEP ICONS AND LABELS ───────────────────────────────────────
STEP_ICONS = {
    "pending": "○",
    "active": "⚙",
    "done": "✔",
    "failed": "✖",
    "skipped": "↩",
}

STEP_LABELS = ["THINK", "PLAN", "EXECUTE", "REVIEW", "UPDATE"]
STEP_KEYS = ["think", "plan", "execute", "review", "update"]

STEP_DESCRIPTIONS = {
    "think": "Parse repo",
    "plan": "Create plan",
    "execute": "Run tools",
    "review": "Score report",
    "update": "Self-correct",
}


def render_steps(statuses, container):
    """Render the 5 step cards with current statuses."""
    with container:
        container.empty()
        cols = st.columns(5)
        for i, col in enumerate(cols):
            key = STEP_KEYS[i]
            status = statuses.get(key, "pending")
            icon = STEP_ICONS.get(status, "○")
            label = STEP_LABELS[i]
            desc = STEP_DESCRIPTIONS.get(key, "")
            col.markdown(
                f"""<div class="step-card step-{status}">
                    <div style="font-size: 24px;">{icon}</div>
                    <div style="font-weight: bold; margin-top: 4px;">{label}</div>
                    <div style="font-size: 11px; margin-top: 2px; opacity: 0.8;">{desc}</div>
                    <div style="font-size: 12px; margin-top: 4px;">{status.upper()}</div>
                </div>""",
                unsafe_allow_html=True,
            )


def format_log(message):
    """Add timestamp and color-code log lines for readability."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    time_span = f'<span class="log-time">[{ts}]</span>'

    if message.startswith("═══"):
        return f'{time_span} <span class="log-loop">{message}</span>'
    elif "Running tool:" in message:
        return f'{time_span} <span class="log-tool">{message}</span>'
    elif "PASSED" in message or (
        "passed" in message.lower() and "success" in message.lower()
    ):
        return f'{time_span} <span class="log-pass">{message}</span>'
    elif "FAILED" in message or "failed" in message.lower():
        return f'{time_span} <span class="log-fail">{message}</span>'
    else:
        return f"{time_span} {message}"


# ── RUN THE AGENT ────────────────────────────────────────────────
debug_log(f"[DEBUG] Before RUN THE AGENT check, run_button={run_button}")
if run_button:
    debug_log("[DEBUG] === RUN_BUTTON HANDLER REACHED ===")
    debug_log(f"[DEBUG] use_github={st.session_state.use_github}, repo_url={st.session_state.repo_url}")
    
    # ── VALIDATION ───────────────────────────────────────────────
    debug_log(f"[DEBUG] Validating API key: {api_key[:10] if api_key else None}...")
    if not api_key or "your_groq" in api_key:
        debug_log("[DEBUG] Validation failed: Groq API key missing.")
        st.error(
            "⚠️ **Groq API key missing.** Paste your key in the sidebar.\n\n"
            "Get one free (no credit card) → [console.groq.com](https://console.groq.com)"
        )
        st.stop()

    # Prepare repository path
    if st.session_state.use_github:
        debug_log("[DEBUG] Preparing repository path for GitHub mode")
        effective_selected_files = expand_monitored_files(
            st.session_state.repo_files,
            st.session_state.selected_files,
            st.session_state.selected_folders,
        )
        debug_log(f"[DEBUG] effective_selected_files length={len(effective_selected_files) if effective_selected_files else 0}")
        persist_monitoring_scope(st.session_state.repo_url)
        debug_log("[DEBUG] Persisted monitoring scope")

        debug_log(f"[DEBUG] UI selected branch: {st.session_state.selected_branch}")
        debug_log(f"[DEBUG] Selected files count: {len(st.session_state.selected_files)}")
        debug_log(f"[DEBUG] Effective analysis files: {effective_selected_files}")

        # Always fetch the HEAD commit for the selected branch so we
        # analyze the latest code, not a stale persisted commit SHA.
        with st.spinner("🔍 Fetching latest commit for selected branch..."):
            debug_log(f"[DEBUG] Fetching latest commit for URL={st.session_state.repo_url}, branch={st.session_state.selected_branch}")
            fresh_commit = github_integration.get_latest_commit_sha(
                st.session_state.repo_url,
                github_token,
                branch=st.session_state.selected_branch,
            )
            debug_log(f"[DEBUG] Fresh commit result success={fresh_commit.get('success')}")
            if fresh_commit.get("success"):
                st.session_state.current_commit = fresh_commit["sha"]
                debug_log(f"[DEBUG] Fresh commit SHA: {fresh_commit['sha']}")
            else:
                debug_log(f"[DEBUG] Failed to fetch fresh commit: {fresh_commit.get('error')}")
                logger.warning("[Manual Analyze] Could not fetch fresh commit, using existing: %s",
                               st.session_state.current_commit)

        debug_log(f"[DEBUG] Fetching repository files content for branch={st.session_state.selected_branch}")
        # Fetch files from GitHub
        with st.spinner("📥 Fetching file contents from GitHub..."):
            temp_dir = tempfile.mkdtemp(prefix='github_analysis_')
            debug_log(f"[DEBUG] Created temp directory: {temp_dir}")
            
            try:
                for file_path in effective_selected_files:
                    debug_log(f"[DEBUG] Fetching content for file: {file_path}")
                    content_result = github_integration.fetch_file_content(
                        st.session_state.repo_url,
                        file_path,
                        st.session_state.current_commit,
                        github_token
                    )
                    debug_log(f"[DEBUG] Fetch content for {file_path} success={content_result.get('success')}")
                    
                    if content_result['success']:
                        full_path = os.path.join(temp_dir, file_path)
                        os.makedirs(os.path.dirname(full_path), exist_ok=True)
                        with open(full_path, 'w', encoding='utf-8') as f:
                            f.write(content_result['content'])
                    else:
                        debug_log(f"[DEBUG] Failed to fetch content for {file_path}: {content_result.get('error')}")
                        logger.warning("[Manual Analyze] Failed to fetch %s: %s",
                                       file_path, content_result.get('error', 'unknown'))
                
                repo_path = temp_dir
                st.session_state.temp_dir = temp_dir
                debug_log(f"[DEBUG] Finished fetching all files. Temp dir path: {repo_path}")
                
            except Exception as e:
                debug_log(f"[DEBUG] Error occurred during file fetching: {str(e)}")
                st.error(f"❌ Error fetching files: {str(e)}")
                shutil.rmtree(temp_dir, ignore_errors=True)
                st.stop()
    else:
        # Local directory
        debug_log(f"[DEBUG] Preparing local repository: {repo_path}")
        if not repo_path.strip():
            debug_log("[DEBUG] Error: local repo_path is empty")
            st.error(
                "⚠️ **No repository path entered.** Enter a path above, or click **💡 Use Example** for a demo."
            )
            st.stop()

        if not os.path.exists(repo_path):
            debug_log(f"[DEBUG] Error: local repo_path does not exist: {repo_path}")
            st.error(
                f"⚠️ **Repository not found:** `{repo_path}`\n\n"
                "Make sure the path is correct and the repository exists."
            )
            st.stop()

    # ── INITIALISE STATE ─────────────────────────────────────────
    client = Groq(api_key=api_key)
    statuses = {key: "pending" for key in STEP_KEYS}
    log_lines = []
    current_loop = [1]

    render_steps(statuses, step_container)
    log_header.markdown("### 📋 Activity Log")

    # ── CALLBACKS ────────────────────────────────────────────────
    def on_step(step, status):
        """Update a step card status and re-render all cards."""
        statuses[step] = status
        render_steps(statuses, step_container)

    def on_log(message):
        """Append a timestamped log line and re-render the log box."""
        if message.startswith("═══ Loop"):
            try:
                loop_num = int(message.split("Loop ")[1].split(" ")[0])
                current_loop[0] = loop_num
                max_loops = int(message.split("of ")[1].split(" ")[0])
                loop_label.markdown(
                    f'<div class="loop-counter">AUTONOMOUS LOOP: {loop_num} of {max_loops}</div>',
                    unsafe_allow_html=True,
                )
            except (ValueError, IndexError):
                pass

        log_lines.append(format_log(message))
        visible = log_lines[-12:]
        log_html = "\n".join(visible)
        log_container.markdown(
            f'<div class="log-box">{log_html}</div>',
            unsafe_allow_html=True,
        )

    # ── RUN THE LOOP ─────────────────────────────────────────────
    debug_log("[DEBUG] Starting manual analysis")
    with st.spinner("🤖 Agent is analyzing code quality autonomously — watch the steps light up..."):
        try:
            results = loop.run(repo_path, client, model, on_step, on_log)
            debug_log("[DEBUG] Analysis completed — building payload")

            analysis_repo = st.session_state.repo_url if st.session_state.use_github else repo_path
            analysis_payload = build_analysis_payload(
                analysis_repo,
                results,
                agent_reasoning="\n".join(log_lines),
                commit_sha=st.session_state.current_commit if st.session_state.use_github else None,
                analyzed_files=effective_selected_files if st.session_state.use_github else None,
                loops_used=current_loop[0],
                source="manual",
            )
            if st.session_state.use_github:
                analysis_payload["branch"] = st.session_state.selected_branch
                analysis_payload["selected_folders"] = st.session_state.selected_folders
                analysis_payload["unmonitored_changed_files"] = []

            debug_log("[DEBUG] Saving latest_analysis.json")
            save_latest_analysis(analysis_payload)
            debug_log("[DEBUG] Save completed")

            # Store results in session state so they persist across reruns
            st.session_state.analysis_result = analysis_payload
            st.session_state.analysis_results = None
            st.session_state.analysis_file_signature = get_file_signature(LATEST_ANALYSIS_PATH)
            st.session_state.loops_used = current_loop[0]
            debug_log("[DEBUG] Session report updated")
            debug_log(f"[DEBUG] Result commit: {analysis_payload.get('commit_sha')}")
            debug_log(f"[DEBUG] Result timestamp: {analysis_payload.get('timestamp')}")

            # Save commit SHA if GitHub
            if st.session_state.use_github:
                tracker = CommitTracker()
                tracker.save_commit(
                    st.session_state.repo_url,
                    st.session_state.current_commit,
                    effective_selected_files
                )

        except Exception as e:
            st.session_state.manual_analysis_running = False
            error_msg = str(e)
            logger.error("[DEBUG] Manual analysis FAILED: %s", error_msg)
            if "api_key" in error_msg.lower() or "auth" in error_msg.lower():
                st.error(
                    "🔑 **API key error.** Your Groq key may be invalid or expired.\n\n"
                    "Get a new one → [console.groq.com](https://console.groq.com)"
                )
            elif "rate_limit" in error_msg.lower() or "429" in error_msg:
                st.error(
                    "⏳ **Rate limited by Groq.** You've hit the free tier limit.\n\n"
                    "Wait 60 seconds and try again, or switch to a smaller model in the sidebar."
                )
            elif "model" in error_msg.lower():
                st.error(
                    f"🧠 **Model error.** The selected model may be unavailable.\n\n"
                    f"Try a different model from the sidebar.\n\nDetails: `{error_msg}`"
                )
            else:
                st.error(
                    f"❌ **Agent error:** `{error_msg}`\n\nCheck your API key and internet connection."
                )
            st.stop()
        finally:
            st.session_state.manual_analysis_running = False
            # Cleanup temp directory
            if st.session_state.temp_dir and os.path.exists(st.session_state.temp_dir):
                shutil.rmtree(st.session_state.temp_dir, ignore_errors=True)
                st.session_state.temp_dir = None

    # Force UI to re-render with the fresh report
    debug_log("[DEBUG] Manual analysis finished — triggering rerun")
    st.rerun()

# ── DISPLAY RESULTS (from session state or fresh run) ──────────────
if False and 'analysis_results' in st.session_state and st.session_state.analysis_results:
    results = st.session_state.analysis_results
    
    st.divider()
    st.markdown("## 📊 Code Quality Analysis Report")

    if results.get("report_writer"):
        report = results["report_writer"]
        scanner_output = results.get("code_scanner", "")
        test_output = results.get("test_case_generator", "")
        priority_output = results.get("issue_prioritizer", "")

        # Parse scanner summary
        try:
            scanner_data = json.loads(scanner_output)
            summary = scanner_data.get("summary", {})
        except:
            summary = {}

        # ── TABBED VIEW ──────────────────────────────────────────
        tab_summary, tab_tests, tab_report, tab_reasoning = st.tabs(
            ["📊 Issue Summary", "🧪 Generated Tests", "📄 Full Report", "🧠 Agent Reasoning"]
        )

        # ── TAB 1: ISSUE SUMMARY ──────────────────────────────────
        with tab_summary:
            st.markdown("### Issues Found")
            
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Critical", summary.get("critical", 0), delta=None, delta_color="inverse")
            with col2:
                st.metric("High", summary.get("high", 0), delta=None, delta_color="inverse")
            with col3:
                st.metric("Medium", summary.get("medium", 0))
            with col4:
                st.metric("Low", summary.get("low", 0))

            st.markdown(f"**Total Issues:** {summary.get('total_issues', 0)} across {summary.get('files_analyzed', 0)} files")

            st.divider()

            # Parse all issues for Jira integration
            try:
                scanner_data = json.loads(scanner_output)
                all_issues = scanner_data.get("issues", [])
            except:
                all_issues = []

            # Initialize session state for Jira
            if 'selected_issues_for_jira' not in st.session_state:
                st.session_state.selected_issues_for_jira = {}
            if 'issue_assignees' not in st.session_state:
                st.session_state.issue_assignees = {}

            # Jira Integration Section
            if all_issues:
                st.markdown("### 🎫 Jira Integration")
                
                # Test Jira connection
                jira_config = {
                    'JIRA_BASE_URL': os.getenv('JIRA_BASE_URL', ''),
                    'JIRA_EMAIL': os.getenv('JIRA_EMAIL', ''),
                    'JIRA_API_TOKEN': os.getenv('JIRA_API_TOKEN', ''),
                    'JIRA_PROJECT_KEY': os.getenv('JIRA_PROJECT_KEY', '')
                }
                
                jira_enabled = all(jira_config.values())
                
                if jira_enabled:
                    from agent.tools import jira_integration
                    
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.success("✅ Jira configured")
                    with col2:
                        if st.button("🔗 Test Connection"):
                            with st.spinner("Testing Jira connection..."):
                                test_result = jira_integration.test_jira_connection(jira_config)
                                if test_result['success']:
                                    st.success(test_result['message'])
                                else:
                                    st.error(test_result['message'])
                else:
                    st.warning("⚠️ Jira not configured. Add credentials to .env file.")
                
                st.divider()

            # Show prioritized issues with Jira checkboxes
            try:
                priority_data = json.loads(priority_output)
                
                if priority_data.get("critical"):
                    st.markdown("### ⚠️ Critical Issues")
                    for idx, issue in enumerate(priority_data["critical"]):
                        issue_key = f"critical_{idx}"
                        
                        col1, col2 = st.columns([4, 1])
                        with col1:
                            # Use on_change callback to update session state
                            def update_selection(key=issue_key):
                                st.session_state.selected_issues_for_jira[key] = st.session_state[f"jira_check_{key}"]
                            
                            selected = st.checkbox(
                                f"🔴 {issue.get('file', 'Unknown')} - {issue.get('issue', 'No description')}",
                                key=f"jira_check_{issue_key}",
                                value=st.session_state.selected_issues_for_jira.get(issue_key, False),
                                on_change=update_selection,
                                args=(issue_key,)
                            )
                        
                        with col2:
                            assignee = st.text_input(
                                "Assignee",
                                key=f"assignee_{issue_key}",
                                placeholder="email/id",
                                label_visibility="collapsed",
                                value=st.session_state.issue_assignees.get(issue_key, "")
                            )
                            if assignee != st.session_state.issue_assignees.get(issue_key, ""):
                                st.session_state.issue_assignees[issue_key] = assignee
                        
                        if selected:
                            with st.expander("View Details"):
                                st.markdown(f"**Impact:** {issue.get('impact', 'N/A')}")
                                st.markdown(f"**Effort:** {issue.get('effort', 'N/A')}")
                                st.markdown(f"**Description:** {issue.get('issue', 'N/A')}")

                if priority_data.get("high"):
                    st.markdown("### 🔶 High Priority Issues")
                    for idx, issue in enumerate(priority_data["high"]):
                        issue_key = f"high_{idx}"
                        
                        col1, col2 = st.columns([4, 1])
                        with col1:
                            # Use on_change callback to update session state
                            def update_selection(key=issue_key):
                                st.session_state.selected_issues_for_jira[key] = st.session_state[f"jira_check_{key}"]
                            
                            selected = st.checkbox(
                                f"🟠 {issue.get('file', 'Unknown')} - {issue.get('issue', 'No description')}",
                                key=f"jira_check_{issue_key}",
                                value=st.session_state.selected_issues_for_jira.get(issue_key, False),
                                on_change=update_selection,
                                args=(issue_key,)
                            )
                        
                        with col2:
                            assignee = st.text_input(
                                "Assignee",
                                key=f"assignee_{issue_key}",
                                placeholder="email/id",
                                label_visibility="collapsed",
                                value=st.session_state.issue_assignees.get(issue_key, "")
                            )
                            if assignee != st.session_state.issue_assignees.get(issue_key, ""):
                                st.session_state.issue_assignees[issue_key] = assignee
                        
                        if selected:
                            with st.expander("View Details"):
                                st.markdown(f"**Impact:** {issue.get('impact', 'N/A')}")
                                st.markdown(f"**Effort:** {issue.get('effort', 'N/A')}")
                                st.markdown(f"**Description:** {issue.get('issue', 'N/A')}")

                # Push to Jira button
                selected_count = sum(1 for v in st.session_state.selected_issues_for_jira.values() if v)
                
                if selected_count > 0 and jira_enabled:
                    st.divider()
                    if st.button(f"🎫 Push {selected_count} Issue(s) to Jira", type="primary", use_container_width=True):
                        from agent.tools import jira_integration
                        
                        with st.spinner(f"Creating {selected_count} Jira ticket(s)..."):
                            # Collect selected issues
                            issues_to_create = []
                            assignees_map = {}
                            
                            for issue_key, is_selected in st.session_state.selected_issues_for_jira.items():
                                if is_selected:
                                    severity, idx = issue_key.split('_')
                                    idx = int(idx)
                                    
                                    if severity == 'critical':
                                        issue_data = priority_data["critical"][idx]
                                    else:
                                        issue_data = priority_data["high"][idx]
                                    
                                    # Find matching issue from scanner output
                                    for scanner_issue in all_issues:
                                        if (scanner_issue.get('file') == issue_data.get('file') and 
                                            scanner_issue.get('description', '')[:50] in issue_data.get('issue', '')):
                                            issues_to_create.append(scanner_issue)
                                            
                                            assignee = st.session_state.issue_assignees.get(issue_key, "").strip()
                                            if assignee:
                                                assignees_map[len(issues_to_create) - 1] = assignee
                                            break
                            
                            # Create tickets
                            results = jira_integration.create_bulk_tickets(
                                issues_to_create,
                                jira_config,
                                assignees_map
                            )
                            
                            # Display results
                            success_count = sum(1 for r in results if r['result']['success'])
                            
                            if success_count > 0:
                                st.success(f"✅ Successfully created {success_count} Jira ticket(s)!")
                                
                                for r in results:
                                    if r['result']['success']:
                                        ticket_key = r['result']['ticket_key']
                                        ticket_url = r['result']['ticket_url']
                                        st.markdown(f"- [{ticket_key}]({ticket_url}): {r['issue'].get('description', 'No description')[:80]}...")
                            
                            if success_count < len(results):
                                st.error(f"❌ Failed to create {len(results) - success_count} ticket(s)")
                                for r in results:
                                    if not r['result']['success']:
                                        st.error(f"Error: {r['result'].get('error', 'Unknown error')}")

                if priority_data.get("recommendations"):
                    st.markdown("### 💡 Recommendations")
                    for rec in priority_data["recommendations"]:
                        st.info(rec)
            except Exception as e:
                st.warning(f"Could not parse prioritized issues: {str(e)}")

        # ── TAB 2: GENERATED TESTS ────────────────────────────────
        with tab_tests:
            st.markdown("### Generated Test Cases")
            st.markdown("Copy-paste these pytest test cases into your test files:")
            st.code(test_output, language="python")
            
            st.download_button(
                label="📥 Download Test Cases",
                data=test_output,
                file_name="generated_tests.py",
                mime="text/x-python",
                use_container_width=True,
            )

        # ── TAB 3: FULL REPORT ────────────────────────────────────
        with tab_report:
            st.markdown(report)
            st.divider()
            st.download_button(
                label="📥 Download Full Report",
                data=report,
                file_name="code_quality_report.md",
                mime="text/markdown",
                use_container_width=True,
            )

        # ── TAB 4: AGENT REASONING ────────────────────────────────
        with tab_reasoning:
            st.markdown("### 🧠 How the Agent Analyzed Your Code")

            # Tool pipeline visualization
            st.markdown("**4-Tool Pipeline:**")
            p_cols = st.columns(4)
            pipeline_tools = [
                ("1️⃣", "Code\nScanner", "Find Issues"),
                ("2️⃣", "Test\nGenerator", "Create Tests"),
                ("3️⃣", "Issue\nPrioritizer", "Rank Severity"),
                ("4️⃣", "Report\nWriter", "Compile Report"),
            ]
            for i, col in enumerate(p_cols):
                icon, name, output = pipeline_tools[i]
                col.markdown(
                    f"""<div class="pipeline-card">
                        <div style="font-size: 24px;">{icon}</div>
                        <div style="font-weight: bold; margin-top: 4px;">{name}</div>
                        <div style="font-size: 11px; color: #666; margin-top: 4px;">{output}</div>
                    </div>""",
                    unsafe_allow_html=True,
                )

            st.markdown("---")

            # Loops used - get from session state if available
            if 'loops_used' in st.session_state:
                loops_used = st.session_state.loops_used
                st.markdown(f"**Autonomous loops used:** {loops_used} of 3")
                if loops_used == 1:
                    st.success("Report passed quality review on the first attempt!")
                else:
                    st.info(
                        f"The agent self-corrected {loops_used - 1} time(s) based on reviewer feedback."
                    )

            st.markdown("---")

            st.markdown("### 📝 Raw Tool Outputs")
            with st.expander("Code Scanner Output"):
                st.json(scanner_output)
            with st.expander("Test Generator Output"):
                st.code(test_output, language="python")
            with st.expander("Issue Prioritizer Output"):
                st.json(priority_output)

    else:
        st.warning("No report was generated. Check the activity log above for errors.")

    # ── POST-RUN EDUCATION ───────────────────────────────────────
    with st.expander("🎓 What just happened? (The autonomy explained)"):
        loops_used = st.session_state.get('loops_used', 1)
        st.markdown(f"""
**The agent ran {loops_used} loop{'s' if loops_used > 1 else ''}** to analyze your code. Here's what happened:

1. **THINK** — The agent parsed the repository path. On retries, it incorporated reviewer feedback about report quality.

2. **PLAN** — The agent created a JSON plan: run code_scanner → test_case_generator → issue_prioritizer → report_writer. It decided this order itself.

3. **EXECUTE** — The agent ran **4 tools** in sequence. Each builds on the previous:
   - **Code Scanner** → Found bugs, complexity issues, security vulnerabilities
   - **Test Case Generator** → Created pytest test cases for uncovered functions
   - **Issue Prioritizer** → Ranked issues by severity (Critical/High/Medium/Low)
   - **Report Writer** → Compiled comprehensive markdown report

4. **REVIEW** — The agent scored the report quality on 5 aspects: issue detection, test coverage, prioritization, report quality, and completeness.

5. **UPDATE** — If overall score < 7, the agent carried specific feedback into the next loop.

**This is the same 5-step loop used in production AI agents. The domain changes (code quality vs. customer support vs. invoice processing), but the loop stays the same.**
        """)

# Persisted report view for webhook/manual analyses.
analysis_result = st.session_state.analysis_result
if analysis_result:
    st.divider()
    st.markdown("## Latest Persisted Analysis")

    report = analysis_result.get("full_report", "")
    summary = analysis_result.get("summary", {})
    all_issues = analysis_result.get("issues", [])
    recommendations = analysis_result.get("recommendations", [])
    priority_groups = analysis_result.get("priority_groups", {})
    raw_outputs = analysis_result.get("raw_outputs", {})
    agent_reasoning = analysis_result.get("agent_reasoning", "")
    test_output = "\n\n".join(analysis_result.get("generated_tests", [])).strip()

    meta_cols = st.columns([3, 2, 2])
    with meta_cols[0]:
        st.caption(f"Repository: {analysis_result.get('repo_url', 'Unknown')}")
    with meta_cols[1]:
        commit_sha = analysis_result.get("commit_sha")
        if commit_sha:
            st.caption(f"Commit: {commit_sha[:12]}")
    with meta_cols[2]:
        st.caption(f"Updated: {format_relative_time(analysis_result.get('timestamp'))}")

    if st.session_state.use_github and st.session_state.repo_files and st.session_state.repo_branches:
        editor_state = render_scope_editor(
            branches=st.session_state.repo_branches,
            selected_branch=st.session_state.selected_branch or st.session_state.repo_default_branch,
            repo_files=st.session_state.repo_files,
            selected_files=st.session_state.selected_files,
            selected_folders=st.session_state.selected_folders,
            default_branch=st.session_state.repo_default_branch,
        )
        if editor_state and st.button("Save Scope Changes", key="save_scope_changes", use_container_width=True):
            st.session_state.selected_branch = editor_state["branch"]
            st.session_state.selected_files = editor_state["selected_files"]
            st.session_state.selected_folders = editor_state["selected_folders"]
            persist_monitoring_scope(st.session_state.repo_url)
            st.success("✅ Monitoring scope updated.")
            st.rerun()

    panel_actions = render_new_files_panel(st.session_state.new_scope_candidates)
    if panel_actions["add"]:
        append_files_to_scope(st.session_state.repo_url, st.session_state.new_scope_candidates)
        st.session_state.selected_files = sorted(
            set(st.session_state.selected_files).union(st.session_state.new_scope_candidates)
        )
        st.session_state.new_scope_candidates = []
        st.success("✅ Added new files to monitoring scope.")
        st.rerun()
    if panel_actions["ignore"]:
        st.session_state.new_scope_candidates = []
        st.rerun()

    selected_view = st.radio(
        "Persisted report view",
        REPORT_VIEWS,
        index=REPORT_VIEWS.index(st.session_state.report_view),
        horizontal=True,
        label_visibility="collapsed",
        key="persisted_report_view_selector",
    )
    if selected_view != st.session_state.report_view:
        persist_report_view(selected_view)

    if not report:
        st.warning("No report was generated. Check the latest webhook/manual run for errors.")
    elif selected_view == "Issue Summary":
        st.markdown("### Issues Found")

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Critical", summary.get("critical", 0), delta=None, delta_color="inverse")
        with col2:
            st.metric("High", summary.get("high", 0), delta=None, delta_color="inverse")
        with col3:
            st.metric("Medium", summary.get("medium", 0))
        with col4:
            st.metric("Low", summary.get("low", 0))

        st.markdown(
            f"**Total Issues:** {summary.get('total_issues', 0)} across {summary.get('files_analyzed', 0)} files"
        )
        st.divider()

        if 'selected_issues_for_jira' not in st.session_state:
            st.session_state.selected_issues_for_jira = {}
        if 'issue_assignees' not in st.session_state:
            st.session_state.issue_assignees = {}

        jira_config = {
            'JIRA_BASE_URL': os.getenv('JIRA_BASE_URL', ''),
            'JIRA_EMAIL': os.getenv('JIRA_EMAIL', ''),
            'JIRA_API_TOKEN': os.getenv('JIRA_API_TOKEN', ''),
            'JIRA_PROJECT_KEY': os.getenv('JIRA_PROJECT_KEY', '')
        }
        jira_enabled = all(jira_config.values())

        if all_issues:
            st.markdown("### 🎫 Jira Integration")

            if jira_enabled:
                from agent.tools import jira_integration

                col1, col2 = st.columns([3, 1])
                with col1:
                    st.success("✅ Jira configured")
                with col2:
                    if st.button("🔗 Test Connection", key="persisted_jira_test_connection"):
                        with st.spinner("Testing Jira connection..."):
                            test_result = jira_integration.test_jira_connection(jira_config)
                            if test_result['success']:
                                st.success(test_result['message'])
                            else:
                                st.error(test_result['message'])
            else:
                st.warning("⚠️ Jira not configured. Add credentials to .env file.")

            st.divider()

        for severity_label, title, icon in [
            ("critical", "### ⚠️ Critical Issues", "🔴"),
            ("high", "### 🔶 High Priority Issues", "🟠"),
        ]:
            issues = priority_groups.get(severity_label, [])
            if not issues:
                continue

            st.markdown(title)
            for idx, issue in enumerate(issues):
                issue_key = f"persisted_{severity_label}_{idx}"

                col1, col2 = st.columns([4, 1])
                with col1:
                    def update_persisted_selection(key=issue_key):
                        st.session_state.selected_issues_for_jira[key] = st.session_state[f"jira_check_{key}"]

                    selected = st.checkbox(
                        f"{icon} {issue.get('file', 'Unknown')} - {issue.get('issue', 'No description')}",
                        key=f"jira_check_{issue_key}",
                        value=st.session_state.selected_issues_for_jira.get(issue_key, False),
                        on_change=update_persisted_selection,
                        args=(issue_key,)
                    )

                with col2:
                    assignee = st.text_input(
                        "Assignee",
                        key=f"assignee_{issue_key}",
                        placeholder="email/id",
                        label_visibility="collapsed",
                        value=st.session_state.issue_assignees.get(issue_key, "")
                    )
                    if assignee != st.session_state.issue_assignees.get(issue_key, ""):
                        st.session_state.issue_assignees[issue_key] = assignee

                if selected:
                    with st.expander("View Details"):
                        st.markdown(f"**Impact:** {issue.get('impact', 'N/A')}")
                        st.markdown(f"**Effort:** {issue.get('effort', 'N/A')}")
                        st.markdown(f"**Description:** {issue.get('issue', 'N/A')}")

        selected_count = sum(
            1 for key, value in st.session_state.selected_issues_for_jira.items()
            if key.startswith("persisted_") and value
        )
        if selected_count > 0 and jira_enabled:
            st.divider()
            if st.button(
                f"🎫 Push {selected_count} Issue(s) to Jira",
                type="primary",
                use_container_width=True,
                key="persisted_push_to_jira",
            ):
                from agent.tools import jira_integration

                with st.spinner(f"Creating {selected_count} Jira ticket(s)..."):
                    issues_to_create = []
                    assignees_map = {}

                    for issue_key, is_selected in st.session_state.selected_issues_for_jira.items():
                        if not issue_key.startswith("persisted_") or not is_selected:
                            continue

                        _, severity, idx = issue_key.split('_')
                        issue_data = priority_groups.get(severity, [])[int(idx)]

                        for scanner_issue in all_issues:
                            if (
                                scanner_issue.get('file') == issue_data.get('file')
                                and scanner_issue.get('description', '')[:50] in issue_data.get('issue', '')
                            ):
                                issues_to_create.append(scanner_issue)
                                assignee = st.session_state.issue_assignees.get(issue_key, "").strip()
                                if assignee:
                                    assignees_map[len(issues_to_create) - 1] = assignee
                                break

                    jira_results = jira_integration.create_bulk_tickets(
                        issues_to_create,
                        jira_config,
                        assignees_map
                    )

                    success_count = sum(1 for item in jira_results if item['result']['success'])
                    if success_count > 0:
                        st.success(f"✅ Successfully created {success_count} Jira ticket(s)!")
                        for item in jira_results:
                            if item['result']['success']:
                                ticket_key = item['result']['ticket_key']
                                ticket_url = item['result']['ticket_url']
                                st.markdown(
                                    f"- [{ticket_key}]({ticket_url}): {item['issue'].get('description', 'No description')[:80]}..."
                                )

                    if success_count < len(jira_results):
                        st.error(f"Œ Failed to create {len(jira_results) - success_count} ticket(s)")
                        for item in jira_results:
                            if not item['result']['success']:
                                st.error(f"Error: {item['result'].get('error', 'Unknown error')}")

        if recommendations:
            st.markdown("### 💡 Recommendations")
            for rec in recommendations:
                st.info(rec)

    elif selected_view == "Generated Tests":
        st.markdown("### Generated Test Cases")
        st.markdown("Copy-paste these pytest test cases into your test files:")
        st.code(test_output or "# No generated tests were persisted for this run.", language="python")

        st.download_button(
            label="📥 Download Test Cases",
            data=test_output,
            file_name="generated_tests.py",
            mime="text/x-python",
            use_container_width=True,
            key="persisted_download_tests",
        )

    elif selected_view == "Full Report":
        st.markdown(report)
        st.divider()
        st.download_button(
            label="📥 Download Full Report",
            data=report,
            file_name="code_quality_report.md",
            mime="text/markdown",
            use_container_width=True,
            key="persisted_download_report",
        )

    elif selected_view == "Agent Reasoning":
        st.markdown("### 🧠 How the Agent Analyzed Your Code")

        st.markdown("**4-Tool Pipeline:**")
        p_cols = st.columns(4)
        pipeline_tools = [
            ("1️⃣", "Code\nScanner", "Find Issues"),
            ("2️⃣", "Test\nGenerator", "Create Tests"),
            ("3️⃣", "Issue\nPrioritizer", "Rank Severity"),
            ("4️⃣", "Report\nWriter", "Compile Report"),
        ]
        for i, col in enumerate(p_cols):
            icon, name, output = pipeline_tools[i]
            col.markdown(
                f"""<div class="pipeline-card">
                    <div style="font-size: 24px;">{icon}</div>
                    <div style="font-weight: bold; margin-top: 4px;">{name}</div>
                    <div style="font-size: 11px; color: #666; margin-top: 4px;">{output}</div>
                </div>""",
                unsafe_allow_html=True,
            )

        st.markdown("---")

        loops_used = analysis_result.get("loops_used", st.session_state.get("loops_used", 1))
        st.markdown(f"**Autonomous loops used:** {loops_used} of 3")
        if loops_used == 1:
            st.success("Report passed quality review on the first attempt!")
        else:
            st.info(
                f"The agent self-corrected {loops_used - 1} time(s) based on reviewer feedback."
            )

        if agent_reasoning:
            st.markdown("### Activity Trace")
            st.code(agent_reasoning, language="text")

        st.markdown("---")
        st.markdown("### 📝 Raw Tool Outputs")
        with st.expander("Code Scanner Output"):
            st.json(raw_outputs.get("code_scanner", "{}"))
        with st.expander("Test Generator Output"):
            st.code(test_output, language="python")
        with st.expander("Issue Prioritizer Output"):
            st.json(raw_outputs.get("issue_prioritizer", "{}"))
