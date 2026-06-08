"""
Monitoring scope editor and webhook new-file notification helpers.
"""
from typing import Dict, List, Sequence

import streamlit as st

from components.repo_selector import render_setup_panel


def render_scope_editor(
    *,
    branches: Sequence[str],
    selected_branch: str,
    repo_files: Sequence[Dict],
    selected_files: Sequence[str],
    selected_folders: Sequence[str],
    default_branch: str,
) -> Dict[str, List[str]]:
    with st.expander("Edit Monitoring Scope", expanded=False):
        return render_setup_panel(
            branches=branches,
            selected_branch=selected_branch,
            repo_files=repo_files,
            selected_files=selected_files,
            selected_folders=selected_folders,
            default_branch=default_branch,
            key_prefix="scope_editor",
        )


def render_new_files_panel(new_files: Sequence[str]) -> Dict[str, bool]:
    if not new_files:
        return {"add": False, "ignore": False}

    st.warning("New changed files detected outside monitoring scope")
    for path in new_files:
        st.checkbox(f"`{path}`", value=False, disabled=True, key=f"new_scope_file_{path}")

    col1, col2 = st.columns(2)
    with col1:
        add = st.button("Add to Monitoring", key="add_new_scope_files", use_container_width=True)
    with col2:
        ignore = st.button("Ignore", key="ignore_new_scope_files", use_container_width=True)

    return {"add": add, "ignore": ignore}
