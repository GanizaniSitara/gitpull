"""Command-line interface for gitpull-go."""

import argparse
import sys

from . import __version__
from .core import (
    configure_go_env,
    diagnose,
    download_all_from_gomod,
    download_module,
    get_cache_download_dir,
    parse_module_spec,
    set_verbose,
)


def main():
    """CLI entry point for gitpull-go."""
    parser = argparse.ArgumentParser(
        description="Download Go modules from GitHub into the local module cache.",
        epilog="Examples:\n"
               "  gitpull-go                                     # Download all deps from go.mod\n"
               "  gitpull-go github.com/user/repo@v1.2.3         # Download specific module+version\n"
               "  gitpull-go github.com/user/repo                # Download latest version\n"
               "  gitpull-go --setup                             # Configure Go env (run once)\n"
               "  gitpull-go --diagnose                          # Check state for issues\n"
               "  gitpull-go -v                                  # Verbose output\n"
               "\n"
               "gitpull-go automatically configures Go to skip checksum verification\n"
               "after downloading. Just run 'go build' when it's done.\n"
               "\n"
               "Environment variables:\n"
               "  GITHUB_TOKEN / GH_TOKEN    GitHub API token (for rate limits / private repos)\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'module',
        nargs='?',
        help='Module to download (e.g. github.com/user/repo@v1.2.3). '
             'If omitted, reads go.mod in the current directory.'
    )
    parser.add_argument(
        '--version',
        action='store_true',
        help='Show version and exit'
    )
    parser.add_argument(
        '--cache-dir',
        action='store_true',
        help='Show module cache directory and exit'
    )
    parser.add_argument(
        '--setup',
        action='store_true',
        help='Configure Go env to use local cache and skip sum verification '
             '(runs go env -w, deletes go.sum, clears sumdb cache)'
    )
    parser.add_argument(
        '--diagnose',
        action='store_true',
        help='Inspect go.mod, go.sum, cache, and Go env for consistency issues'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose/debug output'
    )
    args = parser.parse_args()

    if args.verbose:
        set_verbose(True)

    if args.version:
        print(f"gitpull-go {__version__}")
        return

    if args.cache_dir:
        print(get_cache_download_dir())
        return

    if args.diagnose:
        diagnose()
        return

    if args.setup:
        configure_go_env()
        return

    if args.module:
        # Download a specific module (and all its transitive deps)
        module_path, version = parse_module_spec(args.module)
        try:
            visited = set()
            download_module(module_path, version, _visited=visited)
            # Collect all downloaded modules (including transitive deps)
            all_downloaded = set()
            for key in visited:
                mod_path = key.rsplit("@", 1)[0] if "@" in key else key
                if mod_path.startswith("github.com/"):
                    all_downloaded.add(mod_path)
            configure_go_env(downloaded_modules=list(all_downloaded))
        except Exception as e:
            print(f"[!] Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Download all from go.mod (configure_go_env called automatically)
        download_all_from_gomod()


if __name__ == '__main__':
    main()
