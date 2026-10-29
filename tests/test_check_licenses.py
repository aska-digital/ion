"""scripts/check_licenses.py: the allowlist guard that keeps GPL out of Ion's dependencies."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("check_licenses", ROOT / "scripts" / "check_licenses.py")
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)


@pytest.mark.parametrize("expr", ["MIT", "BSD-3-Clause", "BSD-2-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0",
                                  "Apache-2.0 OR BSD-2-Clause", "MIT AND BSD-3-Clause", "(MIT OR GPL-3.0-only) AND ISC",
                                  "MIT OR GPL-2.0-or-later", "apache-2.0"])
def test_allowed_expressions(expr):
    assert cl.expression_ok(expr)[0]


@pytest.mark.parametrize("expr", ["GPL-3.0-only", "LGPL-2.1-or-later", "AGPL-3.0", "MIT AND GPL-2.0", "GPL-2.0 WITH Classpath-exception-2.0",
                                  "BUSL-1.1", "SSPL-1.0", "Proprietary", "", "MIT AND (GPL-3.0 OR AGPL-3.0)"])
def test_rejected_expressions(expr):
    ok, bad = cl.expression_ok(expr)
    assert not ok and bad


def test_bad_ids_are_reported():
    assert cl.expression_ok("MIT AND GPL-2.0") == (False, ["GPL-2.0"])


def test_leftover_tokens_are_rejected():
    # "MIT GPL-3.0" is not an expression: the trailing id used to be ignored.
    assert cl.expression_ok("MIT GPL-3.0") == (False, ["GPL-3.0"])
    ok, bad = cl.expression_ok("MIT BSD-3-Clause")
    assert not ok and bad


def test_dangling_operator_is_rejected_not_raised():
    ok, bad = cl.expression_ok("MIT AND")
    assert not ok and bad


def test_declared_but_not_installed_fails(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nname="t"\nversion="1"\ndependencies=["definitely-not-installed-pkg-xyz>=1"]\n')
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_licenses.py"), "--pyproject", str(f)],
                       capture_output=True, text=True)
    assert p.returncode == 1 and "declared but not installed" in p.stdout


def test_requirement_names_skip_extras_and_normalise():
    assert cl.requirement_name("Jsonschema-Specifications>=2023") == "jsonschema-specifications"
    assert cl.requirement_name("foo[bar]>=1; python_version < '3.11'") == "foo"
    assert cl.requirement_name("foo; extra == 'test'") is None


class FakeMeta(dict):
    def get_all(self, k):
        return self.get("_" + k)


class FakeDist:
    def __init__(self, name, **kw):
        self.metadata = FakeMeta({"Name": name, "Version": "1", **kw})
        self.requires = []


def test_licence_sources_in_order():
    assert cl.licence_of(FakeDist("a", **{"License-Expression": "MIT"}))[0] == "MIT"
    d = FakeDist("b", **{"_Classifier": ["License :: OSI Approved :: BSD License"]})
    assert cl.licence_of(d) == ("BSD", "classifiers")
    d = FakeDist("c", **{"_Classifier": ["License :: OSI Approved :: MIT License", "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]})
    assert cl.licence_of(d)[0].startswith("MIT OR ")                                  # dual licence: one allowed option is enough
    assert cl.licence_of(FakeDist("d", License="MIT"))[0] == "MIT"
    assert cl.licence_of(FakeDist("e", License="Apache License\nVersion 2.0, January 2004\n" * 3))[0] == "Apache-2.0"
    assert cl.licence_of(FakeDist("f"))[0] is None


def test_unknown_licence_and_denied_package_fail():
    rows = cl.check({"x": FakeDist("x"), "pygit2": FakeDist("pygit2", **{"License-Expression": "GPL-2.0-only WITH GCC-exception-2.0"}),
                     "y": FakeDist("y", **{"License-Expression": "ISC"})})
    ok = {r["name"]: r["ok"] for r in rows}
    assert ok == {"x": False, "pygit2": False, "y": True}


def test_pygit2_in_pyproject_is_refused(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nname="t"\nversion="1"\ndependencies=["pygit2>=1"]\n')
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_licenses.py"), "--pyproject", str(f)], capture_output=True, text=True)
    assert p.returncode == 1 and "denied in pyproject: pygit2" in p.stdout


def test_the_repo_itself_passes():
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_licenses.py")], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "0 not allowed" in p.stdout


def test_no_gpl_dependency_declared():
    text = (ROOT / "pyproject.toml").read_text().lower()
    for banned in ("pygit2", "gpl"):
        assert banned not in text.replace("license-files", "")
