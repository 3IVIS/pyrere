import os

# Directories that are never source code and should never be walked.
# Pruned in-place so os.walk doesn't descend into them.
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytype",
    ".pytest_cache",
    ".hypothesis",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    "build",
    "dist",
    ".eggs",
    "buck-out",
    ".direnv",
}


def _should_skip(dirname: str) -> bool:
    """Return True for directories that are definitely not user source code."""
    return dirname in _SKIP_DIRS or dirname.endswith(".egg-info")


def load_python_files(repo_path: str):
    """
    Yield absolute paths of every .py file under repo_path, skipping
    virtual-env, cache, build, and VCS directories.
    """
    for root, dirs, files in os.walk(repo_path):
        # Prune dirs in-place; os.walk respects this and won't descend into them
        dirs[:] = [d for d in dirs if not _should_skip(d)]

        for file in files:
            if file.endswith(".py"):
                yield os.path.join(root, file)
