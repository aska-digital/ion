import pytest
from ion.analyze.coupling import RepoData, xref_directed


@pytest.mark.parametrize("target,text", [
    ("scripts", '{"scripts": {"test": "pytest"}}'),
    ("docs", '[project.optional-dependencies]\ndocs = []'),
    ("requests", "https://github.com/psf/requests.git"),
    ("core", "git@github.com:ours/core-utils.git"),
    ("core", "https://github.com/ours/core.git-extra"),
])
def test_manifest_words_and_other_repos_are_not_edges(target, text):
    assert xref_directed(RepoData("source", manifest_text=text), target, "ours")[0] == 0


@pytest.mark.parametrize("url", ["https://github.com/ours/core.git", "git@github.com:ours/core", "git+https://github.com/ours/core.git#main"])
def test_org_git_url_is_full_signal(url):
    assert xref_directed(RepoData("source", manifest_text=url), "core", "ours")[0] == 1


def test_parsed_dependency_remains_full_signal():
    assert xref_directed(RepoData("source", deps={"core"}), "core", "ours")[0] == 1
