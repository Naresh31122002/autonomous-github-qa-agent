"""
+Helpers for persisting and applying repository monitoring scope.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "reports"))
REPO_SCOPE_PATH = REPORTS_DIR / "repo_scope.json"


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_repo_url(repo_url: str) -> str:
    if not repo_url:
        return ""
    return repo_url.rstrip("/")


def normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_reports_dir()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_repo_scope() -> Dict[str, Any]:
    payload = _load_json(REPO_SCOPE_PATH, {})
    if not isinstance(payload, dict):
        return {}
    payload.setdefault("selected_files", [])
    payload.setdefault("selected_folders", [])
    payload.setdefault("monitoring_enabled", False)
    return payload


def save_repo_scope(payload: Dict[str, Any]) -> str:
    existing = load_repo_scope()
    merged = {
        "repo_url": _normalize_repo_url(payload.get("repo_url", existing.get("repo_url", ""))),
        "branch": payload.get("branch", existing.get("branch", "")),
        "selected_files": sorted({normalize_path(item) for item in payload.get("selected_files", []) if item}),
        "selected_folders": sorted({normalize_path(item) for item in payload.get("selected_folders", []) if item}),
        "monitoring_enabled": bool(payload.get("monitoring_enabled", True)),
        "created_at": existing.get("created_at") or payload.get("created_at") or datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    _write_json(REPO_SCOPE_PATH, merged)
    return str(REPO_SCOPE_PATH)


def repo_scope_matches(scope: Dict[str, Any], repo_url: str) -> bool:
    return _normalize_repo_url(scope.get("repo_url", "")) == _normalize_repo_url(repo_url)


def load_repo_scope_for_repo(repo_url: str) -> Optional[Dict[str, Any]]:
    scope = load_repo_scope()
    if not scope or not repo_scope_matches(scope, repo_url) or not scope.get("monitoring_enabled"):
        return None
    return scope


def extract_branch_name(ref: str) -> str:
    if not ref:
        return ""
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def branch_matches(scope: Optional[Dict[str, Any]], branch_name: str) -> bool:
    if not scope or not scope.get("monitoring_enabled"):
        return True
    selected_branch = str(scope.get("branch", "")).strip()
    if not selected_branch:
        return True
    return selected_branch == branch_name


def path_is_monitored(path: str, scope: Optional[Dict[str, Any]]) -> bool:
    if not scope or not scope.get("monitoring_enabled"):
        return True

    normalized = normalize_path(path)
    selected_files = {normalize_path(item) for item in scope.get("selected_files", [])}
    selected_folders = {normalize_path(item) for item in scope.get("selected_folders", [])}

    if normalized in selected_files:
        return True

    for folder in selected_folders:
        if normalized == folder or normalized.startswith(f"{folder}/"):
            return True

    return False


def filter_monitored_paths(paths: Sequence[str], scope: Optional[Dict[str, Any]]) -> List[str]:
    return [normalize_path(path) for path in paths if path_is_monitored(path, scope)]


def collect_unmonitored_paths(paths: Sequence[str], scope: Optional[Dict[str, Any]]) -> List[str]:
    if not scope or not scope.get("monitoring_enabled"):
        return []
    return sorted({normalize_path(path) for path in paths if not path_is_monitored(path, scope)})


def expand_monitored_files(
    repo_files: Sequence[Dict[str, Any]],
    selected_files: Sequence[str],
    selected_folders: Sequence[str],
) -> List[str]:
    selected_file_set = {normalize_path(item) for item in selected_files if item}
    selected_folder_set = {normalize_path(item) for item in selected_folders if item}
    expanded: List[str] = []

    for file_info in repo_files:
        path = normalize_path(file_info.get("path", ""))
        if not path:
            continue
        if path in selected_file_set:
            expanded.append(path)
            continue
        for folder in selected_folder_set:
            if path.startswith(f"{folder}/"):
                expanded.append(path)
                break

    return sorted(set(expanded))


def append_files_to_scope(repo_url: str, file_paths: Iterable[str]) -> Optional[Dict[str, Any]]:
    scope = load_repo_scope_for_repo(repo_url)
    if not scope:
        return None

    files = {normalize_path(item) for item in scope.get("selected_files", [])}
    files.update(normalize_path(item) for item in file_paths if item)
    scope["selected_files"] = sorted(files)
    save_repo_scope(scope)
    return load_repo_scope_for_repo(repo_url)


def build_file_tree(file_paths: Sequence[str]) -> Dict[str, Any]:
    tree: Dict[str, Any] = {}
    for raw_path in file_paths:
        path = normalize_path(raw_path)
        if not path:
            continue
        parts = path.split("/")
        cursor = tree
        for index, part in enumerate(parts):
            is_file = index == len(parts) - 1
            node = cursor.setdefault(part, {"type": "file" if is_file else "folder", "children": {}})
            if not is_file:
                node["type"] = "folder"
                cursor = node["children"]
    return tree


def gather_folder_descendants(folder_path: str, repo_files: Sequence[Dict[str, Any]]) -> Set[str]:
    normalized = normalize_path(folder_path)
    descendants: Set[str] = set()
    for item in repo_files:
        path = normalize_path(item.get("path", ""))
        if path.startswith(f"{normalized}/"):
            descendants.add(path)
    return descendants
