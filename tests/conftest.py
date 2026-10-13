"""Offline fixtures: local git repos, recorded GraphQL, a fake GitHub, a fake bus."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mimar.config import load_config
from mimar.context import Context
from mimar.db import DB

FIX = Path(__file__).parent / "fixtures"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Dev One", "GIT_AUTHOR_EMAIL": "dev1@example.com",
       "GIT_COMMITTER_NAME": "Dev One", "GIT_COMMITTER_EMAIL": "dev1@example.com", "GIT_TERMINAL_PROMPT": "0"}


def sh(*argv, cwd=None, env=None):
    p = subprocess.run(argv, cwd=cwd, env=env or ENV, capture_output=True, text=True)
    assert p.returncode == 0, f"{argv}: {p.stderr}"
    return p.stdout


def code(seed: str, n: int = 24) -> str:
    return "\n".join(f"value_{seed}_{i} = compute_{seed}({i}) + helper_{seed}_{i % 3}()" for i in range(n)) + "\n"


def build_repo(remotes: Path, name: str, commits: list[tuple[str, str, dict]]) -> Path:
    """commits: (message, ISO date, {path: content}). Returns the bare 'remote' path."""
    work = remotes / "_work" / name
    work.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", cwd=work)
    for msg, date, files in commits:
        for path, content in files.items():
            f = work / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        sh("git", "add", "-A", cwd=work)
        env = {**ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        sh("git", "commit", "-q", "-m", msg, cwd=work, env=env)
    bare = remotes / f"{name}.git"
    sh("git", "clone", "-q", "--bare", str(work), str(bare))
    return bare


DAYS = ["2026-09-01", "2026-09-03", "2026-09-05", "2026-09-08", "2026-09-10"]


def build_world(remotes: Path) -> None:
    remotes.mkdir(parents=True, exist_ok=True)
    pyproject = lambda name, deps: f'[project]\nname = "{name}"\nversion = "0.1"\ndependencies = {json.dumps(deps)}\n'
    shared = {"utils.py": code("util", 30), "parse.py": code("parse", 30)}
    near = code("io", 30)
    core_commits, cli_commits = [], []
    for i, d in enumerate(DAYS):
        core = {f"alpha_core/mod{i}.py": code(f"core{i}", 12)}
        cli = {f"alpha_cli/cmd{i}.py": code(f"cmd{i}", 12)}
        if i == 0:
            core.update({"pyproject.toml": pyproject("alpha-core", ["requests", "pyyaml", "click"]), "README.md": "# alpha-core\n",
                         **{f"alpha_core/{k}": v for k, v in shared.items()}, "alpha_core/io.py": near})
            cli.update({"pyproject.toml": pyproject("alpha-cli", ["alpha-core", "requests", "pyyaml", "click"]), "README.md": "# alpha-cli\n",
                        "alpha_cli/main.py": "from alpha_core import utils\nimport alpha_core.parse\n" + code("main", 10),
                        **{f"alpha_cli/{k}": v for k, v in shared.items()},
                        "alpha_cli/io.py": near.replace("value_io_3 =", "value_io_three =")})
        core_commits.append((f"core {i}", f"{d}T09:00:00+00:00", core))
        cli_commits.append((f"cli {i}", f"{d}T15:00:00+00:00", cli))
    build_repo(remotes, "alpha-core", core_commits)
    build_repo(remotes, "alpha-cli", cli_commits)

    mono = []
    for i, d in enumerate(DAYS):
        files = {}
        for half in ("engine", "site"):
            if i == 0:
                for j in range(4):
                    files[f"{half}/m{j}.py"] = f"from {half}_utils import tools\n" + code(f"{half}{j}", 10)
                for j in range(3):
                    files[f"{half}_utils/t{j}.py"] = code(f"{half}u{j}", 10)
            else:
                files[f"{half}/m{i % 4}.py"] = f"from {half}_utils import tools\n" + code(f"{half}{i}x", 10)
                files[f"{half}_utils/t{i % 3}.py"] = code(f"{half}u{i}x", 10)
            mono.append((f"{half} {i}", f"{d}T1{0 if half == 'engine' else 2}:00:00+00:00", dict(files)))
            files = {}
    build_repo(remotes, "mono", mono)
    build_repo(remotes, "old-tool", [("init", "2024-01-01T00:00:00+00:00", {"README.md": "# old\n", "x.py": code("old", 12)})])
    build_repo(remotes, "secret-sauce", [("init", "2026-10-01T00:00:00+00:00", {"README.md": "# s\n", "s.py": code("sauce", 12)})])


class FakeGH:
    """In-memory GitHub. Records every write; never touches the network."""

    def __init__(self, remotes: Path, pages: list[dict] | None = None, org="acme", tracker="mimar-tracker"):
        self.remotes, self.org, self.tracker = remotes, org, tracker
        self.pages = pages if pages is not None else [json.loads((FIX / "graphql_org_page1.json").read_text()),
                                                       json.loads((FIX / "graphql_org_page2.json").read_text())]
        self.writes: list[tuple[str, str, dict | None]] = []
        self.graphql_calls = 0
        self.issues: dict[int, dict] = {}
        self.timeline: dict[int, list[dict]] = {}
        self.merged: set[tuple[str, int]] = set()
        self.prs: dict[tuple[str, int], dict] = {}
        self.archived: set[str] = set()
        self.next_pr = 1
        self.event_id = 100
        self.bot_login = "mimar-bot"

    # --- graphql
    def graphql(self, query, variables=None):
        self.graphql_calls += 1
        i = 0 if not (variables or {}).get("after") else 1
        return self.pages[min(i, len(self.pages) - 1)]

    # --- helpers for tests
    def add_label_event(self, issue: int, label: str, actor: str, ts: str = "2099-01-01T00:00:00Z", event="labeled"):
        self.event_id += 1
        self.timeline.setdefault(issue, []).append({"id": self.event_id, "event": event, "label": {"name": label},
                                                    "actor": {"login": actor}, "created_at": ts})
        return self.event_id

    def writes_to(self, method, frag):
        return [w for w in self.writes if w[0] == method and frag in w[1]]

    def paginate(self, path):
        n = int(path.split("/issues/")[1].split("/")[0])
        return list(self.timeline.get(n, []))

    def rest(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, body))
        parts = path.strip("/").split("/")
        if method == "GET":
            if len(parts) == 3 and parts[0] == "repos":
                if parts[2] in {p.name[:-4] for p in self.remotes.glob("*.git")} or parts[2] in self.archived:
                    return {"name": parts[2], "archived": parts[2] in self.archived,
                            "security_and_analysis": {"secret_scanning": {"status": "enabled"}}}
                raise RuntimeError("404 Not Found")
            if "pulls" in parts:
                return {"merged": (parts[2], int(parts[4])) in self.merged}
            if path == "/user":
                return {"login": self.bot_login}
            return {}
        if method == "POST" and parts[-1] == "pulls":
            n = self.next_pr
            self.next_pr += 1
            self.prs[(parts[2], n)] = body
            return {"number": n}
        if method == "POST" and parts[0] == "orgs" and parts[-1] == "repos":
            sh("git", "init", "-q", "--bare", "-b", "main", str(self.remotes / f"{body['name']}.git"))
            return {"name": body["name"]}
        if method == "POST" and parts[-1] == "issues" and parts[2] == self.tracker:
            n = len(self.issues) + 1
            self.issues[n] = {"body": body["body"], "title": body["title"], "labels": list(body.get("labels", []))}
            return {"number": n}
        if method == "PATCH" and "issues" in parts:
            self.issues[int(parts[4])].update({k: v for k, v in body.items()})
            return {}
        if method == "POST" and parts[-1] == "labels" and "issues" in parts:
            n = int(parts[4])
            for l in body["labels"]:
                self.issues[n]["labels"].append(l)
                self.add_label_event(n, l, self.bot_login, ts="2099-01-01T00:00:01Z")
            return {}
        if method == "DELETE" and "labels" in parts and "issues" in parts:
            import urllib.parse
            n = int(parts[4])
            label = urllib.parse.unquote(parts[6])
            if label in self.issues.get(n, {}).get("labels", []):
                self.issues[n]["labels"].remove(label)
            self.add_label_event(n, label, self.bot_login, ts="2100-01-01T00:00:00Z", event="unlabeled")
            return {}
        if method == "PATCH" and len(parts) == 3 and body.get("archived"):
            self.archived.add(parts[2])
        return {}


class FakeBus:
    """Stands in for ~/divan-bus/clients/divan.py. Records posts; serves a scripted inbox."""

    def __init__(self):
        self.inbox: list[dict] = []
        self.posts: list[list[str]] = []
        self.verified: set[str] | None = None   # None = all verified
        self.down = False

    def __call__(self, argv, env=None, input=None):
        if self.down:
            return 1, "", "connection refused"
        if "inbox" in argv:
            return 0, "\n".join(json.dumps(e) for e in self.inbox) + "\n", ""
        if "-c" in argv:
            envs = json.loads(input)
            ok = [e["id"] for e in envs if self.verified is None or e["id"] in self.verified]
            return 0, json.dumps(ok) + "\n", ""
        if "post" in argv:
            self.posts.append(argv)
            return 0, "{}", ""
        if "head" in argv:
            return 0, "{}", ""
        return 1, "", "unknown"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("MIMAR_HOME", str(h))
    return h


@pytest.fixture(scope="session")
def _world_template(tmp_path_factory):
    t = tmp_path_factory.mktemp("world_template") / "remotes"
    build_world(t)
    return t


@pytest.fixture
def world(tmp_path, _world_template):
    import shutil
    remotes = tmp_path / "remotes"
    shutil.copytree(_world_template, remotes, ignore=shutil.ignore_patterns("_work"))
    return remotes


def make_ctx(tmp_path, remotes, overrides=None, gh=None, bus=None):
    base = {"org": "acme", "maintainers": ["maint"], "exclude": ["hermes-agent"], "tracker_repo": "mimar-tracker",
            "thresholds": {"min_cluster_files": 5, "min_cluster_share": 0.2, "dup_min_bytes": 100},
            "git_identity": {"name": "mimar", "email": "mimar@example.com"}}
    from mimar.config import deep_merge
    cfg = load_config(None, deep_merge(base, overrides or {}))
    ctx = Context(cfg=cfg, db=DB(tmp_path / "state.db"), gh=gh or FakeGH(remotes),
                  bus_runner=bus or FakeBus(),
                  remote_url_fn=lambda r: str(remotes / f"{r}.git"),
                  mirror_path_fn=lambda r: tmp_path / "cache" / f"{r}.git",
                  work_root=tmp_path / "work")
    return ctx


@pytest.fixture
def ctx(tmp_path, home, world):
    return make_ctx(tmp_path, world)


@pytest.fixture
def collected(ctx):
    from mimar.collect import run_collect
    run_collect(ctx)
    return ctx
