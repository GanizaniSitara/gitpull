"""Core functions for GitPull."""

import configparser
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from urllib.error import HTTPError, URLError


def parse_repo_arg(arg):
    """
    Parse a repository argument into owner/repo.

    Supported formats:
    - owner/repo
    - github.com/owner/repo
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    """
    # Strip trailing slashes and .git suffix
    arg = arg.rstrip('/')
    if arg.endswith('.git'):
        arg = arg[:-4]

    # Full URL: https://github.com/owner/repo
    https_pattern = r'^https?://github\.com/([^/]+)/([^/]+)$'
    match = re.match(https_pattern, arg)
    if match:
        return match.group(1), match.group(2)

    # URL without protocol: github.com/owner/repo
    domain_pattern = r'^github\.com/([^/]+)/([^/]+)$'
    match = re.match(domain_pattern, arg)
    if match:
        return match.group(1), match.group(2)

    # Simple format: owner/repo
    simple_pattern = r'^([^/]+)/([^/]+)$'
    match = re.match(simple_pattern, arg)
    if match:
        return match.group(1), match.group(2)

    raise ValueError(f"Could not parse repository: {arg}\n"
                     f"Expected format: owner/repo, github.com/owner/repo, or https://github.com/owner/repo")


GITPULL_FILE = '.gitpull'


def read_gitpull_file(directory='.'):
    """Read the repo URL from a .gitpull file."""
    gitpull_path = os.path.join(directory, GITPULL_FILE)
    if not os.path.exists(gitpull_path):
        return None
    with open(gitpull_path, 'r') as f:
        url = f.read().strip()
    return url if url else None


def write_gitpull_file(url, directory='.'):
    """Write the repo URL to a .gitpull file."""
    gitpull_path = os.path.join(directory, GITPULL_FILE)
    with open(gitpull_path, 'w') as f:
        f.write(url + '\n')


def get_remote_url():
    """Parse .git/config for the origin remote URL."""
    git_config_path = os.path.join('.git', 'config')

    if not os.path.exists(git_config_path):
        raise FileNotFoundError("Not a git repository (no .git/config found)")

    config = configparser.ConfigParser()
    config.read(git_config_path)

    # Look for [remote "origin"] section
    section = 'remote "origin"'
    if section not in config:
        raise ValueError("No 'origin' remote found in .git/config")

    url = config.get(section, 'url', fallback=None)
    if not url:
        raise ValueError("No URL found for 'origin' remote")

    return url


def parse_github_url(url):
    """
    Extract owner/repo from various GitHub URL formats.

    Supported formats:
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo
    - git@github.com:owner/repo.git
    - git@github.com:owner/repo
    """
    # HTTPS format: https://github.com/owner/repo.git or https://github.com/owner/repo
    https_pattern = r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$'
    match = re.match(https_pattern, url)
    if match:
        return match.group(1), match.group(2)

    # SSH format: git@github.com:owner/repo.git or git@github.com:owner/repo
    ssh_pattern = r'git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$'
    match = re.match(ssh_pattern, url)
    if match:
        return match.group(1), match.group(2)

    raise ValueError(f"Could not parse GitHub URL: {url}")


def get_default_branch(owner, repo):
    """Call GitHub API to get the default branch name."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}"

    request = urllib.request.Request(
        api_url,
        headers={'User-Agent': 'gitpull-tool'}
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data['default_branch']
    except HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Repository {owner}/{repo} not found (or is private)")
        raise RuntimeError(f"GitHub API error: {e.code} {e.reason}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def download_zip(owner, repo, branch):
    """Download zip archive to a temp location and return the path."""
    zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"

    request = urllib.request.Request(
        zip_url,
        headers={'User-Agent': 'gitpull-tool'}
    )

    # Create temp file for the zip
    fd, zip_path = tempfile.mkstemp(suffix='.zip')
    os.close(fd)

    try:
        print(f"Downloading {branch} branch...")
        with urllib.request.urlopen(request, timeout=120) as response:
            with open(zip_path, 'wb') as f:
                shutil.copyfileobj(response, f)
        return zip_path
    except (HTTPError, URLError) as e:
        os.unlink(zip_path)
        raise RuntimeError(f"Failed to download zip: {e}")


def extract_zip(zip_path, target_dir):
    """
    Extract zip contents to target directory.

    - Skips .git/ directory
    - Overwrites existing files
    - Handles the root folder in the zip (e.g., repo-branch/)
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        # Find the root folder name (e.g., "repo-branch/")
        names = zf.namelist()
        if not names:
            raise ValueError("Empty zip archive")

        # The root folder is the common prefix
        root_folder = names[0].split('/')[0] + '/'

        extracted_count = 0
        skipped_count = 0

        for member in zf.infolist():
            # Skip the root folder entry itself
            if member.filename == root_folder:
                continue

            # Get the relative path (strip the root folder)
            if not member.filename.startswith(root_folder):
                continue

            relative_path = member.filename[len(root_folder):]
            if not relative_path:
                continue

            # Skip .git directory
            if relative_path.startswith('.git/') or relative_path == '.git':
                skipped_count += 1
                continue

            target_path = os.path.join(target_dir, relative_path)

            # Handle directories
            if member.is_dir():
                os.makedirs(target_path, exist_ok=True)
            else:
                # Ensure parent directory exists
                parent_dir = os.path.dirname(target_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

                # Extract file
                with zf.open(member) as source:
                    with open(target_path, 'wb') as target:
                        shutil.copyfileobj(source, target)
                extracted_count += 1

        print(f"Extracted {extracted_count} files")
        if skipped_count:
            print(f"Skipped {skipped_count} .git entries")
