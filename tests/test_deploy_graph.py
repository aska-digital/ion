from pathlib import Path
import os
import subprocess


def test_installer_requests_graph_without_changing_live_install(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Stop immediately after dependency resolution, before any LaunchAgent writes.
    fake = tmp_path / "uv"
    log = tmp_path / "args"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_LOG"\nexit 19\n')
    fake.chmod(0o755)
    cfg = tmp_path / "ion.yml"
    cfg.write_text("org: test\n")
    result = subprocess.run(["bash", str(root / "deploy/install.sh"), str(cfg)],
                            env={**os.environ, "UV": str(fake), "ARGS_LOG": str(log)}, capture_output=True)
    assert result.returncode == 19
    assert log.read_text().splitlines() == ["sync", "--no-dev", "--extra", "graph"]
