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


def compute_gomod_hash(go_mod_bytes):
    """
    Compute the h1: hash for a go.mod file (for go.sum entries).

    Uses the same dirhash.Hash1 algorithm as compute_zip_hash but
    for a single file named "go.mod". Accepts bytes to ensure the
    hash matches exactly what Go reads from disk.
    """
    if isinstance(go_mod_bytes, str):
        go_mod_bytes = go_mod_bytes.encode("utf-8")
    file_hash = hashlib.sha256(go_mod_bytes).hexdigest()
    line = f"{file_hash}  go.mod\n"
    h = hashlib.sha256(line.encode("utf-8")).digest()
    return "h1:" + base64.b64encode(h).decode()


def update_go_sum(module_path, version, zip_hash, gomod_hash):
    """
    Update go.sum with correct hashes for a cached module.

    Replaces any existing entries for this module@version with hashes
    computed from our cached zips, so go build can verify them without
    needing network access.
    """
    go_sum_path = os.path.join(os.getcwd(), "go.sum")

    existing_lines = []
    if os.path.exists(go_sum_path):
        with open(go_sum_path, "r") as f:
            existing_lines = f.readlines()

    # Filter out old entries for this module@version
    prefix_zip = f"{module_path} {version} "
    prefix_mod = f"{module_path} {version}/go.mod "
    new_lines = [l for l in existing_lines
                 if not l.startswith(prefix_zip) and not l.startswith(prefix_mod)]

    # Add entries with hashes from our cached zips
    new_lines.append(f"{module_path} {version} {zip_hash}\n")
    new_lines.append(f"{module_path} {version}/go.mod {gomod_hash}\n")

    # go.sum is conventionally sorted
    new_lines.sort()
    with open(go_sum_path, "w") as f:
        f.writelines(new_lines)


def place_in_cache(module_path, version, info_json, go_mod_content, mod_zip_bytes):
    """
    Place all required files in the Go module download cache.

    Files created:
        cache/download/{escaped_path}/@v/{version}.info
        cache/download/{escaped_path}/@v/{version}.mod
        cache/download/{escaped_path}/@v/{version}.zip
        cache/download/{escaped_path}/@v/{version}.ziphash

    Returns the zip h1: hash (reused for go.sum updates).
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

    # .mod file (binary mode to prevent Windows \r\n conversion)
    go_mod_bytes = go_mod_content.encode("utf-8")
    mod_path = os.path.join(version_dir, f"{version}.mod")
    with open(mod_path, "wb") as f:
        f.write(go_mod_bytes)
    print(f"  Written: {mod_path}")

    # .zip file
    zip_path = os.path.join(version_dir, f"{version}.zip")
    with open(zip_path, "wb") as f:
        f.write(mod_zip_bytes)
    print(f"  Written: {zip_path}")

    # .ziphash file
    zip_hash = compute_zip_hash(mod_zip_bytes)
    ziphash_path = os.path.join(version_dir, f"{version}.ziphash")
    with open(ziphash_path, "w") as f:
        f.write(zip_hash)
    print(f"  Written: {ziphash_path}")

    return zip_hash


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

    # For major version suffixes like /v2, /v3 etc., check whether the
    # code actually lives in a subdirectory or at the repo root.
    # Many modules use "major version branch" (code at root, go.mod says
    # module .../v3) rather than "major version subdirectory" (code in v3/).
    if subdir and re.match(r'^v\d+$', subdir):
        src_zip = zipfile.ZipFile(io.BytesIO(github_zip))
        top_dir = next(iter(set(
            n.split("/")[0] for n in src_zip.namelist() if n.split("/")[0]
        )))
        subdir_prefix = f"{top_dir}/{subdir}/"
        has_subdir = any(n.startswith(subdir_prefix) for n in src_zip.namelist())
        if not has_subdir:
            print(f"  Major version module ({subdir}): code is at repo root")
            subdir = ""

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
    zip_hash = place_in_cache(module_path, version, info, go_mod, mod_zip)

    # Update go.sum with correct hashes so go build works without network
    go_mod_path = os.path.join(os.getcwd(), "go.mod")
    if os.path.exists(go_mod_path):
        gomod_hash = compute_gomod_hash(go_mod)
        update_go_sum(module_path, version, zip_hash, gomod_hash)
        print(f"  Updated go.sum")

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
    downloaded = []

    for path, ver in github_deps:
        try:
            if download_module(path, ver):
                success += 1
                downloaded.append(path)
            else:
                failed += 1
        except Exception as e:
            print(f"  [!] Error: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {success} succeeded, {failed} failed")

    # Configure env with knowledge of which modules we cached
    configure_go_env(downloaded_modules=downloaded)


def clean_sum_state():
    """
    Clear the local sumdb cache to prevent Go from repopulating go.sum
    with official hashes that won't match our cached zips.

    Note: go.sum is no longer deleted here. Instead, download_module()
    surgically updates go.sum entries with hashes computed from our
    cached zips, so go build can verify them offline.
    """
    import shutil

    # Clear the local sumdb cache
    sumdb_dir = os.path.join(get_gomodcache(), "cache", "download", "sumdb")
    if os.path.isdir(sumdb_dir):
        shutil.rmtree(sumdb_dir)
        print(f"  Cleared sumdb cache: {sumdb_dir}")


def _get_go_env(key):
    """Read a single Go env variable."""
    result = subprocess.run(["go", "env", key], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _set_go_env(key, value):
    """Set a Go env variable via go env -w. Returns True on success."""
    result = subprocess.run(
        ["go", "env", "-w", f"{key}={value}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  go env -w {key}={value}")
        return True
    else:
        stderr = result.stderr.strip()
        if "unknown" in stderr.lower():
            print(f"  [!] {key} not supported by this Go version")
        else:
            print(f"  [!] Failed to set {key}: {stderr}")
        return False


def _append_go_env(key, new_patterns):
    """Append comma-separated patterns to an existing Go env variable."""
    existing = _get_go_env(key)
    existing_set = set(existing.split(",")) if existing else set()
    to_add = [p for p in new_patterns if p not in existing_set]
    if not to_add:
        print(f"  {key} already includes cached module patterns")
        return True
    updated = f"{existing},{','.join(to_add)}" if existing else ",".join(to_add)
    return _set_go_env(key, updated)


def configure_go_env(downloaded_modules=None):
    """
    Configure Go environment to use the local module cache.

    Non-destructive: prepends local cache to existing GOPROXY and
    appends cached module patterns to GONOSUMDB/GONOSUMCHECK,
    preserving any existing corporate proxy or sum DB settings.
    """
    cache_dir = get_cache_download_dir().replace("\\", "/")
    local_proxy = f"file:///{cache_dir}"

    print(f"\nConfiguring Go environment:")

    # Prepend local cache to GOPROXY (preserve existing proxies)
    existing_goproxy = _get_go_env("GOPROXY")
    if local_proxy not in existing_goproxy:
        goproxy = f"{local_proxy},{existing_goproxy}" if existing_goproxy else f"{local_proxy},direct"
        _set_go_env("GOPROXY", goproxy)
    else:
        print(f"  GOPROXY already includes local cache")

    # Build patterns for modules we cached (e.g. github.com/owner/repo)
    if downloaded_modules:
        patterns = set()
        for mod in downloaded_modules:
            owner, repo = extract_github_owner_repo(mod)
            if owner:
                patterns.add(f"github.com/{owner}/{repo}")
        patterns = sorted(patterns)

        # Append to GONOSUMDB so Go doesn't check sum DB for our cached modules
        _append_go_env("GONOSUMDB", patterns)

        # Try GONOSUMCHECK too (not all Go versions support it)
        _append_go_env("GONOSUMCHECK", patterns)

    clean_sum_state()
    print(f"\nReady. Run: go build")


def print_cache_instructions():
    """Print instructions for using the cached modules."""
    cache_dir = get_cache_download_dir().replace("\\", "/")
    print(f"\nModule cache: {get_cache_download_dir()}")
    print(f"\nTo build, run: gitpull-go --setup && go build")
    print(f"  (--setup configures Go env and clears stale checksums)")
