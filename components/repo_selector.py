"""
Repository setup UI helpers for branch and file/folder monitoring selection.
"""
from typing import Any, Dict, List, Sequence, Set, Tuple

import streamlit as st

from utils.repo_scope import build_file_tree, gather_folder_descendants, normalize_path


def _render_tree_nodes(
    tree: Dict[str, Any],
    repo_files: Sequence[Dict[str, Any]],
    selected_files: Set[str],
    selected_folders: Set[str],
    key_prefix: str,
    prefix: str = "",
) -> Tuple[Set[str], Set[str]]:
    updated_files = set(selected_files)
    updated_folders = set(selected_folders)

    for name in sorted(tree.keys()):
        node = tree[name]
        path = normalize_path(f"{prefix}/{name}" if prefix else name)
        if node["type"] == "folder":
            descendants = gather_folder_descendants(path, repo_files)
            fully_selected = bool(descendants) and descendants.issubset(updated_files)
            folder_checked = path in updated_folders or fully_selected
            checked = st.checkbox(f"📁 `{path}/`", value=folder_checked, key=f"{key_prefix}_folder_{path}")
            if checked:
                updated_folders.add(path)
                updated_files.update(descendants)
            else:
                updated_folders.discard(path)
                updated_files.difference_update(descendants)
            if node["children"]:
                with st.expander(f"Browse `{path}/`", expanded=False):
                    updated_files, updated_folders = _render_tree_nodes(
                        node["children"],
                        repo_files,
                        updated_files,
                        updated_folders,
                        key_prefix,
                        path,
                    )
        else:
            file_checked = path in updated_files
            checked = st.checkbox(f"`{path}`", value=file_checked, key=f"{key_prefix}_file_{path}")
            if checked:
                updated_files.add(path)
            else:
                updated_files.discard(path)

    return updated_files, updated_folders


def render_scope_tree(
    repo_files: Sequence[Dict[str, Any]],
    selected_files: Sequence[str],
    selected_folders: Sequence[str],
    key_prefix: str = "scope",
) -> Tuple[List[str], List[str]]:
    tree = build_file_tree([item.get("path", "") for item in repo_files])
    updated_files, updated_folders = _render_tree_nodes(
        tree,
        repo_files,
        {normalize_path(item) for item in selected_files},
        {normalize_path(item) for item in selected_folders},
        key_prefix,
    )
    return sorted(updated_files), sorted(updated_folders)


def render_setup_panel(
    *,
    branches: Sequence[str],
    selected_branch: str,
    repo_files: Sequence[Dict[str, Any]],
    selected_files: Sequence[str],
    selected_folders: Sequence[str],
    default_branch: str,
    key_prefix: str,
) -> Dict[str, Any]:
    st.markdown("### 🛠️ Repository Monitoring Setup")
    branch = st.selectbox(
        "Branch",
        options=list(branches),
        index=list(branches).index(selected_branch) if selected_branch in branches else 0,
        key=f"{key_prefix}_branch",
        help=f"Default branch: {default_branch}",
    )
    st.markdown("### 📁 Repository Files")

    selected_files_out, selected_folders_out = render_scope_tree(
        repo_files,
        selected_files,
        selected_folders,
        key_prefix=key_prefix,
    )

    return {
        "branch": branch,
        "selected_files": selected_files_out,
        "selected_folders": selected_folders_out,
    }

