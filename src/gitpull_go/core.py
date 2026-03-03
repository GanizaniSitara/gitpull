"""Core functions for gitpull-go - Go module downloading and caching."""

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_gopath():
    """Get GOPATH, defaulting to ~/go."""
    result = subprocess.run(["go", "env", "GOPATH"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return os.path.join(os.path.expanduser("~"), "go")


def get_gomodcache():
    """Get GOMODCACHE location."""
    result = subprocess.run(["go", "env", "GOMODCACHE"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return os.path.join(get_gopath(), "pkg", "mod")


def get_cache_download_dir():
    """Get the module download cache directory."""
    return os.path.join(get_gomodcache(), "cache", "download")


def github_api_get(url):
    """Make a GitHub API request."""
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "gitpull-go/0.1"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"

    req = Request(url, headers=headers)
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        if e.code == 403:
            print(f"  [!] Rate limited by GitHub API. Set GITHUB_TOKEN to increase limits.")
        raise


def download_url(url):
    """Download a URL and return bytes."""
    headers = {"User-Agent": "gitpull-go/0.1"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"

    req = Request(url, headers=headers)
    with urlopen(req) as resp:
        return resp.read()


def parse_module_spec(spec):
    """
    Parse a module spec like:
        github.com/mark3labs/mcp-go@v0.17.0
        github.com/mark3labs/mcp-go

    Returns (module_path, version_or_none).
    """
    if "@" in spec:
        path, version = spec.rsplit("@", 1)
        return path, version
    return spec, None


def extract_github_owner_repo(module_path):
    """
    Extract GitHub owner/repo from a module path.

    e.g. github.com/mark3labs/mcp-go -> (mark3labs, mcp-go)
    Also handles subpaths: github.com/mark3labs/mcp-go/subpkg -> (mark3labs, mcp-go)
    """
    parts = module_path.split("/")
    if len(parts) < 3 or parts[0] != "github.com":
        return None, None
    return parts[1], parts[2]


def get_module_subdir(module_path):
    """
    Get the subdirectory within the repo for this module.

    e.g. github.com/owner/repo/sub/pkg -> sub/pkg
    """
    parts = module_path.split("/")
    if len(parts) > 3:
        return "/".join(parts[3:])
    return ""


def get_latest_version(owner, repo):
    """Get the latest semver tag from a GitHub repo."""
    print(f"  Fetching tags for {owner}/{repo}...")
    tags_url = f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=100"
    tags = github_api_get(tags_url)

    semver_pattern = re.compile(r"^v\d+\.\d+\.\d+")
    semver_tags = [t["name"] for t in tags if semver_pattern.match(t["name"])]

    if not semver_tags:
        print(f"  [!] No semver tags found. Available tags: {[t['name'] for t in tags[:10]]}")
        if tags:
            return tags[0]["name"]
        raise RuntimeError(f"No tags found for {owner}/{repo}")

    def semver_key(v):
        m = re.match(r"v(\d+)\.(\d+)\.(\d+)", v)
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (0, 0, 0)

    semver_tags.sort(key=semver_key, reverse=True)
    return semver_tags[0]


def get_tag_info(owner, repo, tag):
    """Get commit info for a tag (needed for .info file)."""
    try:
        ref_url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{tag}"
        ref = github_api_get(ref_url)
        sha = ref["object"]["sha"]
        obj_type = ref["object"]["type"]

        # If it's an annotated tag, dereference it
        if obj_type == "tag":
            tag_url = f"https://api.github.com/repos/{owner}/{repo}/git/tags/{sha}"
            tag_obj = github_api_get(tag_url)
            sha = tag_obj["object"]["sha"]

        # Get the commit to find the timestamp
        commit_url = f"https://api.github.com/repos/{owner}/{repo}/git/commits/{sha}"
        commit = github_api_get(commit_url)
        timestamp = commit["committer"]["date"]  # ISO 8601
        return sha, timestamp

    except HTTPError:
        # Fallback: use commits API
        commit_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{tag}"
        commit = github_api_get(commit_url)
        sha = commit["sha"]
        timestamp = commit["commit"]["committer"]["date"]
        return sha, timestamp


def escape_module_path(path):
    """
    Escape uppercase letters in module paths for filesystem storage.

    Go uses '!' + lowercase for uppercase letters in cache paths.
    e.g. GitHub -> !github
    """
    result = []
    for c in path:
        if c.isupper():
            result.append("!" + c.lower())
        else:
            result.append(c)
    return "".join(result)


# Directories that Go's module zip creator always excludes
_VCS_DIRS = {".git", ".hg", ".svn", ".bzr"}


def _should_exclude(rel_path, nested_modules):
    """
    Check whether a file should be excluded from the Go module zip.

    Matches Go's module zip exclusion rules from golang.org/x/mod/zip.
    """
    parts = rel_path.split("/")

    # Exclude VCS directories (.git, .hg, .svn, .bzr)
    for part in parts:
        if part in _VCS_DIRS:
            return True

    # Exclude vendor/ at module root
    if parts[0] == "vendor":
        return True

    # Exclude files inside nested submodules (dirs with their own go.mod)
    for mod_dir in nested_modules:
        if rel_path.startswith(mod_dir + "/"):
            return True

    # Exclude .hg_archival.txt at root
    if rel_path == ".hg_archival.txt":
        return True

    return False


def _find_nested_modules(src_zip, github_prefix):
    """Find subdirectories that contain their own go.mod (nested modules)."""
    nested = set()
    for name in src_zip.namelist():
        if not name.startswith(github_prefix):
            continue
        rel = name[len(github_prefix):]
        if "/" in rel and rel.endswith("/go.mod"):
            # e.g. "sub/pkg/go.mod" -> nested module dir is "sub/pkg"
            mod_dir = rel.rsplit("/", 1)[0]
            nested.add(mod_dir)
    return nested


def build_module_zip(github_zip_bytes, module_path, version, subdir=""):
    """
    Repackage a GitHub zip archive into Go module zip format.

    GitHub zips have: repo-branch/file.go
    Go module zips need: module@version/file.go

    Applies Go's file exclusion rules: VCS dirs, vendor/, nested modules.
    """
    prefix = f"{module_path}@{version}/"

    src_zip = zipfile.ZipFile(io.BytesIO(github_zip_bytes))

    # Find the top-level directory in the GitHub zip
    top_dirs = set()
    for name in src_zip.namelist():
        parts = name.split("/")
        if parts[0]:
            top_dirs.add(parts[0])

    if len(top_dirs) != 1:
        raise RuntimeError(f"Expected one top-level dir in zip, got: {top_dirs}")

    github_prefix = top_dirs.pop() + "/"
    if subdir:
        github_prefix += subdir.strip("/") + "/"

    # Find nested modules so we can exclude their files
    nested_modules = _find_nested_modules(src_zip, github_prefix)

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
        for info in src_zip.infolist():
            if info.is_dir():
                continue
            if not info.filename.startswith(github_prefix):
                continue

            rel_path = info.filename[len(github_prefix):]
            if not rel_path:
                continue

            if _should_exclude(rel_path, nested_modules):
                continue

            new_name = prefix + rel_path
            data = src_zip.read(info.filename)
            out_zip.writestr(new_name, data)

    return out_buf.getvalue()


def extract_go_mod(github_zip_bytes, subdir=""):
    """Extract go.mod content from the GitHub zip."""
    src_zip = zipfile.ZipFile(io.BytesIO(github_zip_bytes))

    top_dirs = set()
    for name in src_zip.namelist():
        parts = name.split("/")
        if parts[0]:
            top_dirs.add(parts[0])

    github_prefix = top_dirs.pop() + "/"
    if subdir:
        github_prefix += subdir.strip("/") + "/"

    go_mod_path = github_prefix + "go.mod"
    try:
        return src_zip.read(go_mod_path).decode("utf-8")
    except KeyError:
        return None


def compute_zip_hash(zip_bytes):
    """
    Compute the h1: hash Go uses for ziphash files.

    This implements Go's dirhash.Hash1 algorithm:
    1. Sort all file names in the zip
    2. For each file: format a line as "<hex_sha256>  <filename>\n"
    3. SHA-256 the concatenated summary
    4. Return "h1:" + base64(hash)
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = sorted(zf.namelist())

    summary = io.BytesIO()
    for name in names:
        # Skip directories
        info = zf.getinfo(name)
        if info.is_dir():
            continue
        file_hash = hashlib.sha256(zf.read(name)).hexdigest()
        line = f"{file_hash}  {name}\n"
        summary.write(line.encode("utf-8"))

    h = hashlib.sha256(summary.getvalue()).digest()
    return "h1:" + base64.b64encode(h).decode()


def place_in_cache(module_path, version, info_json, go_mod_content, mod_zip_bytes):
    """
    Place all required files in the Go module download cache.

    Files created:
        cache/download/{escaped_path}/@v/{version}.info
        cache/download/{escaped_path}/@v/{version}.mod
        cache/download/{escaped_path}/@v/{version}.zip
        cache/download/{escaped_path}/@v/{version}.ziphash
    """
    cache_dir = get_cache_download_dir()
    escaped = escape_module_path(module_path)
    version_dir = os.path.join(cache_dir, escaped.replace("/", os.sep), "@v")
    os.makedirs(version_dir, exist_ok=True)

    # .info file
    info_path = os.path.join(version_dir, f"{version}.info")
    with open(info_path, "w") as f:
        json.dump(info_json, f)
    print(f"  Written: {info_path}")

    # .mod file
    mod_path = os.path.join(version_dir, f"{version}.mod")
    with open(mod_path, "w") as f:
        f.write(go_mod_content)
    print(f"  Written: {mod_path}")

    # .zip file
    zip_path = os.path.join(version_dir, f"{version}.zip")
    with open(zip_path, "wb") as f:
        f.write(mod_zip_bytes)
    print(f"  Written: {zip_path}")

    # .ziphash file
    ziphash_path = os.path.join(version_dir, f"{version}.ziphash")
    with open(ziphash_path, "w") as f:
        f.write(compute_zip_hash(mod_zip_bytes))
    print(f"  Written: {ziphash_path}")

    return version_dir


def download_module(module_path, version=None):
    """Download a single Go module from GitHub and place it in the cache."""
    print(f"\n{'='*60}")
    print(f"Module: {module_path}")

    owner, repo = extract_github_owner_repo(module_path)
    if not owner:
        print(f"  [!] Not a github.com module, skipping: {module_path}")
        return False

    subdir = get_module_subdir(module_path)

    # Resolve version
    if not version:
        version = get_latest_version(owner, repo)
    print(f"  Version: {version}")

    # Get commit info for .info file
    print(f"  Fetching commit info for {version}...")
    sha, timestamp = get_tag_info(owner, repo, version)

    # Download the zip from GitHub
    zip_url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{version}.zip"
    print(f"  Downloading: {zip_url}")
    try:
        github_zip = download_url(zip_url)
    except HTTPError as e:
        print(f"  [!] Failed to download zip: {e}")
        # Try without refs/tags/ in case it's a branch
        zip_url = f"https://github.com/{owner}/{repo}/archive/{version}.zip"
        print(f"  Retrying: {zip_url}")
        github_zip = download_url(zip_url)

    print(f"  Downloaded {len(github_zip)} bytes")

    # Extract go.mod
    go_mod = extract_go_mod(github_zip, subdir)
    if go_mod is None:
        print(f"  [!] No go.mod found, synthesizing one")
        go_mod = f"module {module_path}\n"

    # Build the module zip in Go's format
    print(f"  Repackaging zip for Go module cache...")
    mod_zip = build_module_zip(github_zip, module_path, version, subdir)

    # Build .info JSON
    info = {
        "Version": version,
        "Time": timestamp,
    }

    # Place everything in the cache
    print(f"  Placing in cache...")
    place_in_cache(module_path, version, info, go_mod, mod_zip)

    print(f"  Done: {module_path}@{version}")
    return True


def parse_go_mod(go_mod_path):
    """
    Parse go.mod file and return list of (module_path, version) tuples
    for all required dependencies.
    """
    deps = []
    in_require_block = False

    with open(go_mod_path, "r") as f:
        for line in f:
            line = line.strip()

            # Skip comments
            if line.startswith("//"):
                continue

            # Detect require block
            if line.startswith("require ("):
                in_require_block = True
                continue

            if in_require_block and line == ")":
                in_require_block = False
                continue

            # Single-line require
            if line.startswith("require ") and "(" not in line:
                parts = line.split()
                if len(parts) >= 3:
                    deps.append((parts[1], parts[2]))
                continue

            # Inside require block
            if in_require_block:
                # Remove inline comments
                if "//" in line:
                    line = line[:line.index("//")].strip()
                parts = line.split()
                if len(parts) >= 2:
                    deps.append((parts[0], parts[1]))

    return deps


def download_all_from_gomod():
    """Read go.mod in current directory and download all dependencies."""
    go_mod_path = os.path.join(os.getcwd(), "go.mod")
    if not os.path.exists(go_mod_path):
        print("[!] No go.mod found in current directory")
        sys.exit(1)

    print(f"Reading {go_mod_path}...")
    deps = parse_go_mod(go_mod_path)

    if not deps:
        print("No dependencies found in go.mod")
        return

    print(f"Found {len(deps)} dependencies:")
    for path, ver in deps:
        print(f"  {path}@{ver}")

    # Filter to github.com modules only
    github_deps = [(p, v) for p, v in deps if p.startswith("github.com/")]
    non_github = [(p, v) for p, v in deps if not p.startswith("github.com/")]

    if non_github:
        print(f"\n[!] Skipping {len(non_github)} non-GitHub dependencies:")
        for p, v in non_github:
            print(f"  {p}@{v}")
        print("  (gitpull-go currently only supports github.com modules)")

    success = 0
    failed = 0

    for path, ver in github_deps:
        try:
            if download_module(path, ver):
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  [!] Error: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {success} succeeded, {failed} failed")

    print_cache_instructions()


def print_cache_instructions():
    """Print instructions for using the cached modules."""
    cache_dir = get_cache_download_dir().replace("\\", "/")
    print(f"\nModule cache: {get_cache_download_dir()}")
    print(f"\nTo build with the cached modules, run:")
    print(f"  set GONOSUMCHECK=*")
    print(f"  set GONOSUMDB=*")
    print(f"  set GOPROXY=file:///{cache_dir},direct")
    print(f"  go build")
