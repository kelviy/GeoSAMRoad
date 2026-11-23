"""Build the docs site strictly, then fail if it holds anything private.

    .venv/bin/python tools/build_docs.py

Zensical's strict build fails on missing pages and anchors. The repo and its
site are public, so the scan then fails on local paths, Google Drive or Docs
links and credentials in any built page or the search index. CI runs this same
script.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

PATTERNS = {
    "absolute local path": re.compile(
        r"/mnt/[\w.-]+|/home/[a-z_][\w-]*/|/Users/[\w.-]+/|[A-Z]:\\Users\\"
    ),
    "Google Drive or Docs link": re.compile(r"(?:drive|docs)\.google\.com"),
    "Linear link (workspace slug holds a real name)": re.compile(r"linear\.app"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_\w{20,}"),
    "API key": re.compile(
        r"\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[\w-]{10,}"
    ),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "40-hex secret (W&B key)": re.compile(r"\b[0-9a-f]{40}\b"),
}


def scan(site_dir: Path) -> list[str]:
    files = [*site_dir.rglob("*.html"), *site_dir.glob("*.json")]
    hits = []
    for path in sorted(files):
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                hits.append(
                    f"{path.relative_to(site_dir)}: {label}: {match.group(0)!r}"
                )
    return hits


def main() -> int:
    zensical = Path(sys.executable).parent / "zensical"
    build = subprocess.run(
        [zensical, "build", "--strict", "--clean"], check=False, cwd=ROOT
    )
    if build.returncode:
        print("Docs build failed: fix the warnings above.", file=sys.stderr)
        return build.returncode
    hits = scan(SITE)
    if hits:
        print(
            "Sensitive content in the built site. Replace each with a placeholder "
            "such as <repo>, <data-dir> or 'Ask kelviy for the link':",
            *hits,
            sep="\n  ",
            file=sys.stderr,
        )
        return 1
    print("Docs built, no sensitive content found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
