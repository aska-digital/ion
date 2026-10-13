"""GitHub access through the `gh` CLI (token handled by gh, never logged).

Tests substitute a fake with the same interface.
"""
from __future__ import annotations

import json
import subprocess

from .util import redact


class GHError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class GH:
    def __init__(self, runner=None):
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(argv: list[str], stdin: str | None):
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """Return the full response body: {data, errors?}. Partial errors are returned, not raised."""
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        for k, v in (variables or {}).items():
            if v is None:
                continue
            argv += ["-F" if isinstance(v, (int, bool)) else "-f", f"{k}={v}"]
        rc, out, err = self._runner(argv, None)
        try:
            body = json.loads(out) if out.strip() else None
        except ValueError:
            body = None
        if body is None or ("data" not in body):
            raise GHError(redact(err.strip() or out.strip())[:500])
        return body

    def rest(self, method: str, path: str, body: dict | None = None):
        argv = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        stdin = None
        if body is not None:
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        rc, out, err = self._runner(argv, stdin)
        if rc != 0:
            status = None
            for tok in (err + out).split():
                if tok.strip("():").isdigit() and len(tok.strip("():")) == 3:
                    status = int(tok.strip("():"))
                    break
            raise GHError(redact(err.strip() or out.strip())[:500], status)
        return json.loads(out) if out.strip() else None

    def paginate(self, path: str):
        argv = ["gh", "api", "--paginate", "--slurp", path, "-H", "Accept: application/vnd.github+json"]
        rc, out, err = self._runner(argv, None)
        if rc != 0:
            raise GHError(redact(err.strip() or out.strip())[:500])
        pages = json.loads(out)
        flat = []
        for p in pages:
            flat.extend(p if isinstance(p, list) else [p])
        return flat

    def token_login(self) -> str | None:
        try:
            return self.rest("GET", "/user").get("login")
        except GHError:
            return None
