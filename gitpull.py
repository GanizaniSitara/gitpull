#!/usr/bin/env python3
"""
GitPull - Download and extract a GitHub repo's latest files.

Works around git pull being blocked at proxy level by downloading
the zip archive directly from GitHub.

Usage:
    gitpull.py                    # Update existing repo (reads from .git/config)
    gitpull.py owner/repo         # Clone a new repo into ./repo/
    gitpull.py github.com/o/r     # Clone from URL path
    gitpull.py https://github.com/owner/repo  # Clone from full URL
"""

import argparse
import configparser
import json
import os
import re
import shutil
import sys
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


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download and extract a GitHub repo's latest files.",
        epilog="Examples:\n"
               "  gitpull.py                          # Update existing repo\n"
               "  gitpull.py owner/repo               # Clone into ./repo/\n"
               "  gitpull.py https://github.com/o/r   # Clone from URL\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        'repo',
        nargs='?',
        help='Repository to clone (owner/repo or GitHub URL). '
             'If omitted, updates the current git repository.'
    )
    args = parser.parse_args()

    try:
        if args.repo:
            # Clone mode: download to a new directory
            owner, repo = parse_repo_arg(args.repo)
            print(f"Repository: {owner}/{repo}")

            # Create target directory
            target_dir = repo
            if os.path.exists(target_dir):
                print(f"Error: Directory '{target_dir}' already exists", file=sys.stderr)
                sys.exit(1)

            # Get default branch
            print("Fetching repository info...")
            branch = get_default_branch(owner, repo)
            print(f"Default branch: {branch}")

            # Download zip
            zip_path = download_zip(owner, repo, branch)

            try:
                # Create target directory and extract
                os.makedirs(target_dir)
                print(f"Extracting to {target_dir}/...")
                extract_zip(zip_path, target_dir)
                print("Done!")
            finally:
                if os.path.exists(zip_path):
                    os.unlink(zip_path)

        else:
            # Update mode: refresh existing repo
            if not os.path.isdir('.git'):
                print("Error: Not a git repository (no .git directory found)", file=sys.stderr)
                print("Run this command from the root of a git repository,", file=sys.stderr)
                print("or provide a repo to clone: gitpull.py owner/repo", file=sys.stderr)
                sys.exit(1)

            # Get remote URL
            print("Reading git config...")
            remote_url = get_remote_url()
            print(f"Remote URL: {remote_url}")

            # Parse GitHub owner/repo
            owner, repo = parse_github_url(remote_url)
            print(f"Repository: {owner}/{repo}")

            # Get default branch
            print("Fetching repository info...")
            branch = get_default_branch(owner, repo)
            print(f"Default branch: {branch}")

            # Download zip
            zip_path = download_zip(owner, repo, branch)

            try:
                # Extract files
                print("Extracting files...")
                extract_zip(zip_path, '.')
                print("Done!")
            finally:
                # Clean up temp file
                if os.path.exists(zip_path):
                    os.unlink(zip_path)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


if __name__ == '__main__':
    main()
