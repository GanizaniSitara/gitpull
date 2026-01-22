# gitpull

Download GitHub repositories via zip archive when `git pull` is blocked at the proxy level.

## Installation

```bash
pip install git+https://github.com/yourusername/gitpull.git
```

Or install from source:

```bash
git clone https://github.com/yourusername/gitpull.git
cd gitpull
pip install .
```

For development:

```bash
pip install -e .
```

## Usage

```bash
# Update an existing repo (run from repo root)
gitpull

# Clone a new repo
gitpull owner/repo
gitpull github.com/owner/repo
gitpull https://github.com/owner/repo

# Show version
gitpull --version
```

You can also use it as a library:

```python
from gitpull import get_default_branch, download_zip, extract_zip
```

## How it works

1. Fetches repository info from GitHub API to get the default branch
2. Downloads the zip archive from `github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip`
3. Extracts files to the target directory, preserving the existing `.git` folder (for updates)

## Requirements

- Python 3.8+
- No external dependencies (uses only standard library)

## Limitations

- Only works with public GitHub repositories
- Does not update `.git` history (files are replaced, but git sees them as local changes)
- Clone mode will not create a `.git` directory (use `git init` + `git remote add` if needed)
