# gitpull

Download GitHub repositories via zip archive when `git pull` is blocked at the proxy level.

## Usage

```bash
# Update an existing repo (run from repo root)
python gitpull.py

# Clone a new repo
python gitpull.py owner/repo
python gitpull.py github.com/owner/repo
python gitpull.py https://github.com/owner/repo
```

## How it works

1. Fetches repository info from GitHub API to get the default branch
2. Downloads the zip archive from `github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip`
3. Extracts files to the target directory, preserving the existing `.git` folder (for updates)

## Requirements

- Python 3.6+
- No external dependencies (uses only standard library)

## Limitations

- Only works with public GitHub repositories
- Does not update `.git` history (files are replaced, but git sees them as local changes)
- Clone mode will not create a `.git` directory (use `git init` + `git remote add` if needed)
