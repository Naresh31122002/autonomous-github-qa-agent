"""
GitHub Integration Tool
Fetches code from GitHub repositories and analyzes commits
"""
import os
import tempfile
import shutil
import logging
from github import Github, GithubException
from typing import Dict, List, Optional

logger = logging.getLogger("github_integration")


def normalize_repo_input(repo_input: str) -> str:
    """
    Normalize GitHub repo input into owner/repo format.
    Supports full GitHub URLs and owner/repo input.
    """
    raw = (repo_input or "").strip()
    if not raw:
        raise ValueError("Repository input is empty")

    cleaned = raw.rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break

    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Repository must be in owner/repo format")

    owner_repo = f"{parts[0]}/{parts[1]}"
    if owner_repo.count("/") != 1:
        raise ValueError("Repository must be in owner/repo format")

    return owner_repo


def _github_client(github_token: Optional[str] = None) -> Github:
    return Github(github_token) if github_token else Github()


def get_repository_metadata(repo_url: str, github_token: Optional[str] = None) -> Dict:
    """
    Validate the repository exists and return metadata.
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
        logger.debug("Normalized repo: %s", owner_repo)
        repo = _github_client(github_token).get_repo(owner_repo)
        payload = {
            "success": True,
            "normalized_repo": owner_repo,
            "repo_name": repo.full_name,
            "default_branch": repo.default_branch,
            "private": bool(repo.private),
            "description": repo.description or "No description",
        }
        logger.debug("Repo exists: True")
        logger.debug("Repo metadata response: %s", payload)
        logger.debug("Default branch: %s", repo.default_branch)
        return payload
    except Exception as e:
        logger.exception("Repository metadata lookup failed")
        return {
            "success": False,
            "normalized_repo": "",
            "error": f"Error loading repository metadata: {str(e)}",
        }


def fetch_repository(repo_url: str, github_token: Optional[str] = None) -> Dict:
    """
    Fetch repository files from GitHub
    
    Args:
        repo_url: GitHub repository URL (e.g., 'owner/repo' or full URL)
        github_token: Optional GitHub personal access token for private repos
        
    Returns:
        Dictionary with repository info and file contents
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
            
        # Initialize GitHub client
        g = _github_client(github_token)
        
        # Get repository
        repo = g.get_repo(owner_repo)
        
        # Get default branch
        default_branch = repo.default_branch
        
        # Get all files from repository
        contents_list = repo.get_contents("", ref=default_branch)
        if not isinstance(contents_list, list):
            contents_list = [contents_list]
        
        files = {}
        
        while contents_list:
            file_content = contents_list.pop(0)
            if file_content.type == "dir":
                dir_contents = repo.get_contents(file_content.path, ref=default_branch)
                if isinstance(dir_contents, list):
                    contents_list.extend(dir_contents)
                else:
                    contents_list.append(dir_contents)
            else:
                # Only process code files
                if file_content.path.endswith(('.py', '.js', '.ts', '.java', '.cpp', '.c', '.go', '.rb')):
                    try:
                        files[file_content.path] = file_content.decoded_content.decode('utf-8')
                    except:
                        pass  # Skip binary or unreadable files
        
        return {
            'success': True,
            'repo_name': repo.full_name,
            'description': repo.description or 'No description',
            'language': repo.language or 'Unknown',
            'stars': repo.stargazers_count,
            'default_branch': default_branch,
            'files': files,
            'file_count': len(files)
        }
        
    except GithubException as e:
        return {
            'success': False,
            'error': f'GitHub API error: {str(e)}',
            'files': {}
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Error fetching repository: {str(e)}',
            'files': {}
        }


def fetch_commit_changes(repo_url: str, commit_sha: str, github_token: Optional[str] = None) -> Dict:
    """
    Fetch files changed in a specific commit
    
    Args:
        repo_url: GitHub repository URL
        commit_sha: Commit SHA to analyze
        github_token: Optional GitHub token
        
    Returns:
        Dictionary with changed files and their contents
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
            
        # Initialize GitHub client
        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)
        
        # Get commit
        commit = repo.get_commit(commit_sha)
        
        # Get changed files
        changed_files = {}
        for file in commit.files:
            if file.filename.endswith(('.py', '.js', '.ts', '.java', '.cpp', '.c', '.go', '.rb')):
                try:
                    # Get file content at this commit
                    content_file = repo.get_contents(file.filename, ref=commit_sha)
                    if not isinstance(content_file, list):
                        changed_files[file.filename] = {
                            'content': content_file.decoded_content.decode('utf-8'),
                            'status': file.status,  # added, modified, removed
                            'additions': file.additions,
                            'deletions': file.deletions,
                            'changes': file.changes
                        }
                except:
                    pass
        
        return {
            'success': True,
            'commit_sha': commit_sha,
            'commit_message': commit.commit.message,
            'author': commit.commit.author.name,
            'date': commit.commit.author.date.isoformat(),
            'changed_files': changed_files,
            'file_count': len(changed_files)
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': f'Error fetching commit: {str(e)}',
            'changed_files': {}
        }


def clone_to_temp(repo_url: str, github_token: Optional[str] = None) -> str:
    """
    Clone repository to temporary directory
    
    Args:
        repo_url: GitHub repository URL
        github_token: Optional GitHub token
        
    Returns:
        Path to temporary directory with cloned repo
    """
    import subprocess
    
    temp_dir = tempfile.mkdtemp(prefix='github_agent_')
    
    try:
        # Build clone URL with token if provided
        if github_token and 'github.com' in repo_url:
            clone_url = repo_url.replace('https://', f'https://{github_token}@')
        else:
            clone_url = repo_url
            
        # Clone repository
        subprocess.run(
            ['git', 'clone', '--depth', '1', clone_url, temp_dir],
            check=True,
            capture_output=True
        )
        
        return temp_dir
        
    except subprocess.CalledProcessError as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise Exception(f'Failed to clone repository: {e.stderr.decode()}')


def cleanup_temp(temp_dir: str):
    """Remove temporary directory"""
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


def post_review_comment(repo_url: str, commit_sha: str, comment: str, github_token: str) -> bool:
    """
    Post a review comment on a commit
    
    Args:
        repo_url: GitHub repository URL
        commit_sha: Commit SHA to comment on
        comment: Comment text
        github_token: GitHub token (required for posting)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
            
        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)
        commit = repo.get_commit(commit_sha)
        
        # Create commit comment
        commit.create_comment(comment)
        
        return True
        
    except Exception as e:
        print(f"Error posting comment: {e}")
        return False


def list_branches(repo_url: str, github_token: Optional[str] = None) -> Dict:
    """
    List branches for a repository.
    """
    try:
        owner_repo = normalize_repo_input(repo_url)

        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)
        branches = [branch.name for branch in repo.get_branches()]
        logger.debug("Loaded branches: %s", branches)
        return {
            'success': True,
            'normalized_repo': owner_repo,
            'default_branch': repo.default_branch,
            'branches': branches,
        }
    except Exception as e:
        logger.exception("Branch listing failed")
        return {
            'success': False,
            'error': f'Error listing branches: {str(e)}',
            'branches': [],
        }


def list_all_code_files(
    repo_url: str,
    github_token: Optional[str] = None,
    file_extensions: Optional[List[str]] = None,
    branch: Optional[str] = None,
) -> Dict:
    """
    List ALL code files in repository with metadata.

    Uses the Git Tree API (recursive) for a single API call instead of
    walking directories one-by-one with get_contents().
    """
    if file_extensions is None:
        file_extensions = ['.py', '.js', '.ts', '.java', '.md', '.cpp', '.c', '.go', '.rb', '.php', '.swift', '.kt']

    ext_set = set(file_extensions)

    try:
        owner_repo = normalize_repo_input(repo_url)

        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)

        default_branch = repo.default_branch
        ref_branch = branch or default_branch

        # Single API call: fetch entire tree recursively
        branch_obj = repo.get_branch(ref_branch)
        tree_sha = branch_obj.commit.sha
        git_tree = repo.get_git_tree(tree_sha, recursive=True)

        files = []
        for element in git_tree.tree:
            if element.type != "blob":
                continue
            ext = os.path.splitext(element.path)[1]
            if ext in ext_set:
                files.append({
                    'path': element.path,
                    'size': element.size or 0,
                    'type': 'file',
                    'extension': ext,
                    'sha': element.sha,
                })

        payload = {
            'success': True,
            'normalized_repo': owner_repo,
            'repo_name': repo.full_name,
            'default_branch': default_branch,
            'branch': ref_branch,
            'files': files,
            'file_count': len(files),
        }
        logger.debug('File tree loaded successfully via Git Tree API (%d files)', len(files))
        return payload

    except Exception as e:
        logger.exception("File tree load failed")
        return {
            'success': False,
            'error': f'Error listing files: {str(e)}',
            'files': [],
        }


def get_changed_files_between_commits(repo_url: str, base_sha: str, head_sha: str, github_token: Optional[str] = None) -> Dict:
    """
    Get files changed between two commits
    
    Args:
        repo_url: GitHub repository URL
        base_sha: Base commit SHA (older)
        head_sha: Head commit SHA (newer)
        github_token: GitHub token (required)
        
    Returns:
        Dictionary with changed files categorized by status
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
            
        # Initialize GitHub client
        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)
        
        # Compare commits
        comparison = repo.compare(base_sha, head_sha)
        
        # Categorize files by status
        modified = []
        added = []
        deleted = []
        
        for file in comparison.files:
            file_info = {
                'path': file.filename,
                'additions': file.additions,
                'deletions': file.deletions,
                'changes': file.changes,
                'status': file.status
            }
            
            if file.status == 'modified':
                modified.append(file_info)
            elif file.status == 'added':
                added.append(file_info)
            elif file.status in ['removed', 'deleted']:
                deleted.append(file_info)
        
        return {
            'success': True,
            'base_sha': base_sha,
            'head_sha': head_sha,
            'modified': modified,
            'added': added,
            'deleted': deleted,
            'total_changes': len(modified) + len(added) + len(deleted)
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': f'Error comparing commits: {str(e)}',
            'modified': [],
            'added': [],
            'deleted': []
        }


def get_latest_commit_sha(repo_url: str, github_token: Optional[str] = None, branch: Optional[str] = None) -> Dict:
    """
    Get latest commit SHA from default branch
    
    Args:
        repo_url: GitHub repository URL
        github_token: GitHub token (required)
        
    Returns:
        Dictionary with commit information
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
            
        # Initialize GitHub client
        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)
        
        # Get latest commit from selected branch
        branch_name = branch or repo.default_branch
        branch_obj = repo.get_branch(branch_name)
        commit = branch_obj.commit
        
        return {
            'success': True,
            'sha': commit.sha,
            'branch': branch_name,
            'message': commit.commit.message,
            'author': commit.commit.author.name,
            'date': commit.commit.author.date.isoformat(),
            'url': commit.html_url
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': f'Error getting latest commit: {str(e)}'
        }


def fetch_file_content(repo_url: str, file_path: str, commit_sha: str, github_token: Optional[str] = None) -> Dict:
    """
    Fetch specific file content at specific commit
    
    Args:
        repo_url: GitHub repository URL
        file_path: Path to file in repository
        commit_sha: Commit SHA to fetch from
        github_token: GitHub token (required)
        
    Returns:
        Dictionary with file content
    """
    try:
        owner_repo = normalize_repo_input(repo_url)
            
        # Initialize GitHub client
        g = _github_client(github_token)
        repo = g.get_repo(owner_repo)
        
        # Get file content at specific commit
        content_file = repo.get_contents(file_path, ref=commit_sha)
        
        if isinstance(content_file, list):
            return {
                'success': False,
                'error': 'Path is a directory, not a file'
            }
        
        return {
            'success': True,
            'path': file_path,
            'content': content_file.decoded_content.decode('utf-8'),
            'size': content_file.size,
            'sha': content_file.sha
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': f'Error fetching file: {str(e)}'
        }


# Tool metadata for the agent
TOOL_METADATA = {
    "name": "github_integration",
    "description": "Fetches code from GitHub repositories for analysis",
    "functions": {
        "fetch_repository": "Fetch all code files from a GitHub repository",
        "fetch_commit_changes": "Fetch files changed in a specific commit",
        "get_repository_metadata": "Validate repo and fetch metadata",
        "list_all_code_files": "List all code files with metadata",
        "list_branches": "List repository branches",
        "get_changed_files_between_commits": "Get files changed between two commits",
        "get_latest_commit_sha": "Get latest commit SHA and info",
        "fetch_file_content": "Fetch specific file content at commit",
        "clone_to_temp": "Clone repository to temporary directory for analysis",
        "post_review_comment": "Post analysis results as a commit comment"
    }
}
