"""Command-line interface for GitPull."""

import argparse
import os
import sys

from .core import (
    parse_repo_arg,
    get_remote_url,
    parse_github_url,
    get_default_branch,
    download_zip,
    extract_zip,
)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download and extract a GitHub repo's latest files.",
        epilog="Examples:\n"
               "  gitpull                             # Update existing repo\n"
               "  gitpull owner/repo                  # Clone into ./repo/\n"
               "  gitpull https://github.com/o/r     # Clone from URL\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        'repo',
        nargs='?',
        help='Repository to clone (owner/repo or GitHub URL). '
             'If omitted, updates the current git repository.'
    )
    parser.add_argument(
        '--version',
        action='store_true',
        help='Show version and exit'
    )
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"gitpull {__version__}")
        return

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
                print("or provide a repo to clone: gitpull owner/repo", file=sys.stderr)
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
