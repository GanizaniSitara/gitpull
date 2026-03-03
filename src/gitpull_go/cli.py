"""Command-line interface for gitpull-go."""

import argparse
import sys

from . import __version__
from .core import (
    configure_go_env,
    download_all_from_gomod,
    download_module,
    get_cache_download_dir,
    parse_module_spec,
    print_cache_instructions,
)


def main():
    """CLI entry point for gitpull-go."""
    parser = argparse.ArgumentParser(
        description="Download Go modules from GitHub into the local module cache.",
        epilog="Examples:\n"
               "  gitpull-go                                     # Download all deps from go.mod\n"
               "  gitpull-go github.com/user/repo@v1.2.3         # Download specific module+version\n"
               "  gitpull-go github.com/user/repo                # Download latest version\n"
               "  gitpull-go --cache-dir                         # Show module cache directory\n"
               "\n"
               "After running gitpull-go, delete go.sum and set env vars before building:\n"
               "  del go.sum              (or rm -f go.sum on Linux/Mac)\n"
               "  go env -w GONOSUMCHECK=*\n"
               "  go env -w GONOSUMDB=*\n"
               "  go env -w GOPROXY=file:///%%GOPATH%%/pkg/mod/cache/download,direct\n"
               "  go build\n"
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
             '(runs go env -w, deletes go.sum)'
    )
    args = parser.parse_args()

    if args.version:
        print(f"gitpull-go {__version__}")
        return

    if args.cache_dir:
        print(get_cache_download_dir())
        return

    if args.setup:
        configure_go_env()
        return

    if args.module:
        # Download a specific module
        module_path, version = parse_module_spec(args.module)
        try:
            download_module(module_path, version)
            print_cache_instructions()
        except Exception as e:
            print(f"[!] Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Download all from go.mod
        download_all_from_gomod()


if __name__ == '__main__':
    main()
