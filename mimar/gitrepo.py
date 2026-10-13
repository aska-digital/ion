"""Read-only access to a bare mirror (or any git dir) at a ref."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

VENDOR_RE = re.compile(r"(^|/)(node_modules|vendor|vendored|third_party|dist|build|\.venv|venv|__pycache__|site-packages|"
                       r"\.next|target|coverage|\.git)(/|$)")
LOCK_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "Gemfile.lock",
              "composer.lock", "go.sum", "Pipfile.lock", "bun.lockb", "bun.lock"}
GENERATED_RE = re.compile(r"(\.min\.(js|css)$|\.map$|\.lock$|_pb2\.py$|\.generated\.|\.snap$|\.svg$|\.png$|\.jpe?g$|"
                          r"\.gif$|\.ico$|\.woff2?$|\.pdf$|\.zip$|\.gz$|\.ttf$|\.mp4$|\.webp$|\.csv$|\.jsonl?$)", re.I)
TEXT_RE = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|swift|c|h|cc|cpp|hpp|sh|bash|zsh|lua|php|cs|scala|"
                     r"md|toml|yaml|yml|txt|cfg|ini|html|css|sql|nix)$", re.I)


def is_eligible(path: str) -> bool:
    """Files that count towards duplication analysis."""
    name = path.rsplit("/", 1)[-1]
    return not (VENDOR_RE.search(path) or name in LOCK_NAMES or GENERATED_RE.search(path)) and bool(TEXT_RE.search(path))


class GitRepo:
    def __init__(self, path: str | Path, ref: str | None = None):
        self.path = str(path)
        self.ref = ref or "HEAD"

    def _git(self, *args: str, input: str | None = None) -> str:
        p = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, input=input, errors="replace")
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip()[:300])
        return p.stdout

    def exists(self) -> bool:
        return Path(self.path).exists()

    def resolve_ref(self) -> str | None:
        try:
            self._git("rev-parse", "--verify", "-q", self.ref + "^{commit}")
            return self.ref
        except RuntimeError:
            return None

    def files(self) -> dict[str, tuple[str, int]]:
        """path -> (blob sha, size)"""
        out = self._git("ls-tree", "-r", "-l", "-z", self.ref)
        res = {}
        for rec in out.split("\0"):
            if not rec:
                continue
            meta, path = rec.split("\t", 1)
            mode, typ, sha, size = meta.split()
            if typ == "blob":
                res[path] = (sha, int(size) if size.isdigit() else 0)
        return res

    def read_blobs(self, shas: list[str], max_bytes: int = 200_000) -> dict[str, str]:
        if not shas:
            return {}
        p = subprocess.run(["git", "-C", self.path, "cat-file", "--batch"], input="\n".join(shas) + "\n",
                           capture_output=True)
        data, out, i = p.stdout, {}, 0
        for sha in shas:
            nl = data.index(b"\n", i)
            header = data[i:nl].split()
            if len(header) < 3:
                i = nl + 1
                continue
            size = int(header[2])
            body = data[nl + 1: nl + 1 + size]
            i = nl + 1 + size + 1
            out[sha] = body[:max_bytes].decode("utf-8", "replace")
        return out

    def read(self, path: str) -> str | None:
        try:
            return self._git("show", f"{self.ref}:{path}")
        except RuntimeError:
            return None

    def log_names(self, limit: int = 400) -> list[dict]:
        """Commits with the top-level paths and full paths they touched."""
        sep = "\x1e"
        out = self._git("log", self.ref, f"-n{limit}", "--no-merges", f"--format={sep}%H%x1f%an%x1f%ae%x1f%aI", "--name-only")
        commits = []
        for chunk in out.split(sep):
            if not chunk.strip():
                continue
            head, _, rest = chunk.partition("\n")
            oid, name, email, date = head.split("\x1f")
            commits.append({"oid": oid, "name": name, "email": email, "date": date,
                            "paths": [p for p in rest.split("\n") if p]})
        return commits

    def count(self, paths: list[str] | None = None) -> int:
        args = ["rev-list", "--count", self.ref]
        if paths:
            args += ["--", *paths]
        return int(self._git(*args).strip())
