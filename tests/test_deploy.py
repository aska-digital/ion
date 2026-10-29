"""deploy/install.sh and the LaunchAgent it renders.

Each test runs the real installer against a temp HOME (a stub `plutil`, and `UV=true` so nothing is synced), then runs
the rendered ProgramArguments the way launchd would: in WorkingDirectory, with only the plist's EnvironmentVariables.
`run` is swapped for `status`, which reads the same config, state dir and kill switch offline.
"""
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ION_BIN = Path(sys.executable).parent / "ion"
TRUE = shutil.which("true")

pytestmark = pytest.mark.skipif(not ION_BIN.exists() or not shutil.which("bash") or not TRUE,
                                reason="needs bash and the ion entry point next to the test interpreter")


def _src(tmp_path: Path) -> Path:
    """The layout install.sh expects (deploy/ and .venv/bin/ion), in a temp dir rather than this checkout."""
    src = tmp_path / "src"
    (src / "deploy").mkdir(parents=True)
    for f in ("install.sh", "ai.aska.ion.plist"):
        shutil.copy2(ROOT / "deploy" / f, src / "deploy" / f)
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "ion").symlink_to(ION_BIN)
    return src


def _install(tmp_path: Path, home: Path, config_arg: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "plutil").write_text('#!/bin/sh\necho "$2: OK"\n')
    (fake / "plutil").chmod(0o755)
    return subprocess.run(["bash", str(_src(tmp_path) / "deploy" / "install.sh"), config_arg], cwd=cwd,
                          env={"HOME": str(home), "PATH": f"{fake}:/usr/bin:/bin", "UV": TRUE, **(env or {})},
                          capture_output=True, text=True)


def _plist(home: Path) -> dict:
    raw = (home / "Library" / "LaunchAgents" / "ai.aska.ion.plist").read_bytes()
    # the template's header comment contains `--apply`, which CoreFoundation accepts and expat does not
    return plistlib.loads(re.sub(rb"<!--.*?-->", b"", raw, flags=re.S))


def _as_launchd(plist: dict) -> subprocess.CompletedProcess:
    argv = list(plist["ProgramArguments"])
    argv[argv.index("run")] = "status"
    return subprocess.run(argv, cwd=plist["WorkingDirectory"], env=dict(plist["EnvironmentVariables"]),
                          capture_output=True, text=True)


def _shell(home: Path, *args, ion_home: Path | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", **({"ION_HOME": str(ion_home)} if ion_home else {})}
    return subprocess.run([str(ION_BIN), *args], env=env, cwd=cwd, capture_output=True, text=True)


def _legacy_state(home: Path) -> Path:
    """An operator from before the rename: ~/.mimar holds the config, a proposal and an engaged kill switch."""
    legacy = home / ".mimar"
    legacy.mkdir(parents=True)
    (legacy / "mimar.yml").write_text("org: acme\n")
    assert _shell(home, "--config", str(legacy / "mimar.yml"), "status").returncode == 0      # creates state.db
    con = sqlite3.connect(legacy / "state.db")
    con.execute("insert into proposals(id,num,kind,key,status,score,title,state) values"
                "('P-0001',1,'archive','archive:old-tool','awaiting-approval',0.9,'archive old-tool','{}')")
    con.commit()
    con.close()
    assert _shell(home, "pause").returncode == 0
    return legacy


def test_relative_config_path_is_the_file_the_operator_named(tmp_path):
    """launchd runs the agent in the source tree, where a dev `ion.yml` may sit (README: `ion --config ion.yml doctor`)."""
    home = tmp_path / "home"
    (home / ".ion").mkdir(parents=True)
    (home / ".ion" / "ion.yml").write_text("org: acme\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ion.yml").write_text("org: dev-sandbox\n")
    p = _install(tmp_path, home, "ion.yml", cwd=home / ".ion")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "org: acme" in _as_launchd(_plist(home)).stdout


def test_operator_ion_home_reaches_the_launch_agent(tmp_path):
    """`ion pause` from a shell with ION_HOME set must stop the scheduled run too."""
    home, state = tmp_path / "home", tmp_path / "ion-state"
    state.mkdir()
    (state / "ion.yml").write_text("org: acme\n")
    p = _install(tmp_path, home, "", cwd=tmp_path, env={"ION_HOME": str(state)})       # config defaults to $ION_HOME
    assert p.returncode == 0, p.stdout + p.stderr
    assert _shell(home, "pause", ion_home=state).returncode == 0
    plist = _plist(home)
    assert plist["StandardOutPath"] == str(state / "ion.log")
    assert "kill switch: ON" in _as_launchd(plist).stdout


def test_install_refuses_to_abandon_unmigrated_mimar_state(tmp_path):
    home = tmp_path / "home"
    legacy = _legacy_state(home)
    p = _install(tmp_path, home, str(legacy / "mimar.yml"), cwd=tmp_path)
    assert p.returncode != 0 and "~/.mimar" in p.stderr
    assert not (home / "Library" / "LaunchAgents" / "ai.aska.ion.plist").exists()
    assert not (home / ".ion").exists()


def test_an_empty_ion_dir_does_not_hide_mimar_state(tmp_path):
    """The old migration note said `mkdir -p ~/.ion && mv <config> ~/.ion/ion.yml`; state.db and PAUSED stayed behind."""
    home = tmp_path / "home"
    legacy = _legacy_state(home)
    (home / ".ion").mkdir()
    (legacy / "mimar.yml").rename(home / ".ion" / "ion.yml")
    out = _shell(home, "status", cwd=tmp_path).stdout
    assert "kill switch: ON" in out and "P-0001" in out


def test_following_the_printed_migration_note_keeps_state(tmp_path):
    home = tmp_path / "home"
    _legacy_state(home)
    note = _shell(home, "status", cwd=tmp_path).stderr
    cmd = re.search(r"migrate: (.*) \(the name", note).group(1)
    assert subprocess.run(["bash", "-c", cmd], env={"HOME": str(home), "PATH": "/usr/bin:/bin"}).returncode == 0
    assert not (home / ".mimar" / "state.db").exists()
    out = _shell(home, "status", cwd=tmp_path)
    assert "kill switch: ON" in out.stdout and "P-0001" in out.stdout and "deprecated" not in out.stderr
