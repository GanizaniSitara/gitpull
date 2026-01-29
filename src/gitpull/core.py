"""Core functions for GitPull."""

import base64
import configparser
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from urllib.error import HTTPError, URLError


def get_package_version():
    """Get the current package version from __init__.py."""
    init_path = os.path.join(os.path.dirname(__file__), '__init__.py')
    with open(init_path, 'r') as f:
        content = f.read()
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if match:
        return match.group(1)
    return "0.0.0"


def bump_version(bump_type='patch'):
    """
    Bump the package version in __init__.py.

    Args:
        bump_type: 'major', 'minor', or 'patch'

    Returns:
        tuple: (old_version, new_version)
    """
    init_path = os.path.join(os.path.dirname(__file__), '__init__.py')

    with open(init_path, 'r') as f:
        content = f.read()

    match = re.search(r'^(__version__\s*=\s*["\'])([^"\']+)(["\'])', content, re.MULTILINE)
    if not match:
        raise ValueError("Could not find __version__ in __init__.py")

    old_version = match.group(2)
    parts = old_version.split('.')

    # Ensure we have at least 3 parts
    while len(parts) < 3:
        parts.append('0')

    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if bump_type == 'major':
        major += 1
        minor = 0
        patch = 0
    elif bump_type == 'minor':
        minor += 1
        patch = 0
    else:  # patch
        patch += 1

    new_version = f"{major}.{minor}.{patch}"

    # Replace in content
    new_content = content[:match.start(2)] + new_version + content[match.end(2):]

    with open(init_path, 'w') as f:
        f.write(new_content)

    return old_version, new_version


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
VERSION_FILE = '.gitpull.version'


def read_version_file(directory='.'):
    """Read the stored commit hash from the version file."""
    version_path = os.path.join(directory, VERSION_FILE)
    if not os.path.exists(version_path):
        return None
    with open(version_path, 'r') as f:
        sha = f.read().strip()
    return sha if sha else None


def write_version_file(sha, directory='.'):
    """Write the commit hash to the version file."""
    version_path = os.path.join(directory, VERSION_FILE)
    with open(version_path, 'w') as f:
        f.write(sha + '\n')


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


def get_branches(owner, repo):
    """Get list of branches from GitHub API (handles pagination)."""
    branches = []
    page = 1
    per_page = 100  # Maximum allowed by GitHub

    while True:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page={per_page}&page={page}"

        request = urllib.request.Request(
            api_url,
            headers={'User-Agent': 'gitpull-tool'}
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
                if not data:
                    break
                branches.extend(branch['name'] for branch in data)
                if len(data) < per_page:
                    break
                page += 1
        except HTTPError as e:
            if e.code == 404:
                raise ValueError(f"Repository {owner}/{repo} not found (or is private)")
            raise RuntimeError(f"GitHub API error: {e.code} {e.reason}")
        except URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")

    return branches


def get_latest_commit_sha(owner, repo, branch):
    """Get the latest commit SHA for a branch."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"

    request = urllib.request.Request(
        api_url,
        headers={'User-Agent': 'gitpull-tool'}
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data['sha']
    except HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Branch {branch} not found in {owner}/{repo}")
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


def get_repo_tree(owner, repo, branch):
    """Get the full file tree from GitHub API."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"

    request = urllib.request.Request(
        api_url,
        headers={'User-Agent': 'gitpull-tool'}
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data.get('tree', [])
    except HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Repository or branch not found: {owner}/{repo}@{branch}")
        raise RuntimeError(f"GitHub API error: {e.code} {e.reason}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def get_blob_content(owner, repo, sha):
    """Get blob content from GitHub API (returns base64 decoded bytes)."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/blobs/{sha}"

    request = urllib.request.Request(
        api_url,
        headers={'User-Agent': 'gitpull-tool'}
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode('utf-8'))
            content = data.get('content', '')
            encoding = data.get('encoding', 'base64')

            if encoding == 'base64':
                return base64.b64decode(content)
            else:
                return content.encode('utf-8')
    except HTTPError as e:
        raise RuntimeError(f"Failed to fetch blob {sha}: {e.code} {e.reason}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def download_via_api(owner, repo, branch, target_dir):
    """
    Download repository files using GitHub API (fallback method).

    This avoids the zip download and raw.githubusercontent.com by using
    the Git Trees and Blobs APIs instead.
    """
    print(f"Fetching file tree for {branch} branch...")
    tree = get_repo_tree(owner, repo, branch)

    # Filter to only blobs (files), exclude .git
    files = [
        item for item in tree
        if item['type'] == 'blob'
        and not item['path'].startswith('.git/')
        and item['path'] != '.git'
    ]

    print(f"Found {len(files)} files to download")

    downloaded_count = 0
    for i, item in enumerate(files, 1):
        path = item['path']
        sha = item['sha']

        target_path = os.path.join(target_dir, path)

        # Create parent directories
        parent_dir = os.path.dirname(target_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # Download and write file
        print(f"[{i}/{len(files)}] {path}")
        content = get_blob_content(owner, repo, sha)

        with open(target_path, 'wb') as f:
            f.write(content)

        downloaded_count += 1

    print(f"Downloaded {downloaded_count} files")
