"""
GitPull - Download and extract a GitHub repo's latest files.

Works around git pull being blocked at proxy level by downloading
the zip archive directly from GitHub.
"""

__version__ = "1.0.0"

from .core import (
    parse_repo_arg,
    get_remote_url,
    parse_github_url,
    get_default_branch,
    get_branches,
    download_zip,
    extract_zip,
)

__all__ = [
    "__version__",
    "parse_repo_arg",
    "get_remote_url",
    "parse_github_url",
    "get_default_branch",
    "get_branches",
    "download_zip",
    "extract_zip",
]
