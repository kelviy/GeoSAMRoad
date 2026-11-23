"""MkDocs hook: fail the build if the built site holds anything private.

The repo and its site are public. This scans every built HTML page and the
search index for local paths, Google Drive or Docs links and credentials.
"""

import re
from pathlib import Path

from mkdocs.exceptions import PluginError

PATTERNS = {
    "absolute local path": re.compile(
        r"/mnt/[\w.-]+|/home/[a-z_][\w-]*/|/Users/[\w.-]+/|[A-Z]:\\Users\\"
    ),
    "Google Drive or Docs link": re.compile(r"(?:drive|docs)\.google\.com"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_\w{20,}"),
    "API key": re.compile(
        r"\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[\w-]{10,}"
    ),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "40-hex secret (W&B key)": re.compile(r"\b[0-9a-f]{40}\b"),
}


def scan(site_dir: Path) -> list[str]:
    files = [*site_dir.rglob("*.html"), *site_dir.glob("search/*.json")]
    hits = []
    for path in sorted(files):
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                hits.append(
                    f"{path.relative_to(site_dir)}: {label}: {match.group(0)!r}"
                )
    return hits


def on_post_build(config, **_):
    hits = scan(Path(config["site_dir"]))
    if hits:
        raise PluginError(
            "Sensitive content in the built site. Replace each with a placeholder "
            "such as <repo>, <data-dir> or 'Ask kelviy for the link':\n  "
            + "\n  ".join(hits)
        )
