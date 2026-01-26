"""Command-line interface for GitPull."""

import argparse
import os
import sys

from .core import (
    GITPULL_FILE,
    VERSION_FILE,
    parse_repo_arg,
    read_gitpull_file,
    write_gitpull_file,
    read_version_file,
    write_version_file,
    get_remote_url,
    parse_github_url,
    get_default_branch,
    get_latest_commit_sha,
    download_zip,
    extract_zip,
    download_via_api,
)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download and extract a GitHub repo's latest files.",
        epilog="Examples:\n"
               "  gitpull                             # Update existing repo\n"
               "  gitpull owner/repo                  # Clone into ./repo/\n"
               "  gitpull https://github.com/o/r     # Clone from URL\n"
               "  gitpull --init owner/repo          # Set repo URL for current dir\n"
               "\n"
               "For directories without .git, gitpull stores the repo URL in a\n"
               ".gitpull file. If neither exists, you'll be prompted to enter one.\n",
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
    parser.add_argument(
        '--init',
        metavar='URL',
        help='Initialize .gitpull with a GitHub repo URL for future updates'
    )
    parser.add_argument(
        '--fallback',
        action='store_true',
        help='Use API fallback to download files individually (use if zip download is blocked)'
    )
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"gitpull {__version__}")
        return

    try:
        if args.init:
            # Init mode: save repo URL to .gitpull
            owner, repo = parse_repo_arg(args.init)
            url = f"https://github.com/{owner}/{repo}"
            write_gitpull_file(url)
            print(f"Initialized {GITPULL_FILE} with: {url}")
            return

        if args.repo:
            # Clone mode: download to a directory (new or existing)
            owner, repo = parse_repo_arg(args.repo)
            print(f"Repository: {owner}/{repo}")

            target_dir = repo
            dir_exists = os.path.exists(target_dir)

            # Get default branch and latest commit
            print("Fetching repository info...")
            branch = get_default_branch(owner, repo)
            print(f"Default branch: {branch}")

            new_sha = get_latest_commit_sha(owner, repo, branch)
            short_new_sha = new_sha[:7]

            # Check for existing version
            previous_sha = None
            if dir_exists:
                previous_sha = read_version_file(target_dir)

            if previous_sha:
                short_prev_sha = previous_sha[:7]
                if previous_sha == new_sha:
                    print(f"Warning: Already at commit {short_new_sha}")
                    print("No new commits to pull.")
                    return
                print(f"Upgrading: {short_prev_sha} -> {short_new_sha}")
            else:
                print(f"Commit: {short_new_sha}")

            # Create target directory if it doesn't exist
            if not dir_exists:
                os.makedirs(target_dir)
                print(f"Downloading to {target_dir}/...")
            else:
                print(f"Updating existing directory {target_dir}/...")

            if args.fallback:
                # Use API fallback method
                download_via_api(owner, repo, branch, target_dir)
            else:
                # Try zip download
                try:
                    zip_path = download_zip(owner, repo, branch)
                except RuntimeError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    print("\nTip: If zip download is blocked, try running with --fallback", file=sys.stderr)
                    print("     This will download files individually via GitHub API.", file=sys.stderr)
                    sys.exit(1)

                try:
                    extract_zip(zip_path, target_dir)
                finally:
                    if os.path.exists(zip_path):
                        os.unlink(zip_path)

            # Write .gitpull file and version for future updates
            url = f"https://github.com/{owner}/{repo}"
            write_gitpull_file(url, target_dir)
            write_version_file(new_sha, target_dir)
            print(f"Created {GITPULL_FILE} for future updates")
            print(f"Version saved: {short_new_sha}")
            print("Done!")

        else:
            # Update mode: refresh existing repo
            remote_url = None

            # Check for .gitpull file first
            gitpull_url = read_gitpull_file()
            if gitpull_url:
                print(f"Found {GITPULL_FILE}: {gitpull_url}")
                remote_url = gitpull_url
            elif os.path.isdir('.git'):
                # Fall back to .git/config
                print("Reading git config...")
                remote_url = get_remote_url()
                print(f"Remote URL: {remote_url}")
            else:
                # Neither exists - prompt interactively
                print("No .git directory or .gitpull file found.")
                print("Enter the GitHub repository URL to set up for future updates.")
                print("(e.g., https://github.com/owner/repo or owner/repo)")
                print()
                try:
                    user_input = input("GitHub repo: ").strip()
                except EOFError:
                    print("\nAborted.", file=sys.stderr)
                    sys.exit(1)

                if not user_input:
                    print("Error: No URL provided", file=sys.stderr)
                    sys.exit(1)

                owner, repo = parse_repo_arg(user_input)
                remote_url = f"https://github.com/{owner}/{repo}"
                write_gitpull_file(remote_url)
                print(f"Saved to {GITPULL_FILE}")

            # Parse GitHub owner/repo
            owner, repo = parse_github_url(remote_url)
            print(f"Repository: {owner}/{repo}")

            # Get default branch and latest commit
            print("Fetching repository info...")
            branch = get_default_branch(owner, repo)
            print(f"Default branch: {branch}")

            new_sha = get_latest_commit_sha(owner, repo, branch)
            short_new_sha = new_sha[:7]

            # Check for existing version
            previous_sha = read_version_file()
            if previous_sha:
                short_prev_sha = previous_sha[:7]
                if previous_sha == new_sha:
                    print(f"Warning: Already at commit {short_new_sha}")
                    print("No new commits to pull.")
                    return
                print(f"Upgrading: {short_prev_sha} -> {short_new_sha}")
            else:
                print(f"Commit: {short_new_sha}")

            if args.fallback:
                # Use API fallback method
                download_via_api(owner, repo, branch, '.')
            else:
                # Try zip download
                try:
                    zip_path = download_zip(owner, repo, branch)
                except RuntimeError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    print("\nTip: If zip download is blocked, try running with --fallback", file=sys.stderr)
                    print("     This will download files individually via GitHub API.", file=sys.stderr)
                    sys.exit(1)

                try:
                    # Extract files
                    print("Extracting files...")
                    extract_zip(zip_path, '.')
                finally:
                    # Clean up temp file
                    if os.path.exists(zip_path):
                        os.unlink(zip_path)

            # Save new version
            write_version_file(new_sha)
            print(f"Version saved: {short_new_sha}")
            print("Done!")

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
