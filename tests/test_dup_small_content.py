from ion.analyze.coupling import RepoData, dup_signal


def repo(name, size, total=None):
    return RepoData(name, files={"settings.cfg": ("same-blob", size)}, eligible_bytes=total or size)


def test_single_small_shared_file_cannot_trigger_merge_alone():
    score, evidence = dup_signal(repo("a", 200), repo("b", 200))
    assert score * 0.6 < 0.6
    assert score == 200 / 4096
    assert evidence["shared_bytes"] == 200


def test_small_subset_does_not_saturate_large_repo():
    score, _ = dup_signal(repo("a", 5000), repo("b", 5000, 50000))
    assert score == 0.1


def test_substantial_duplicate_repos_still_saturate():
    assert dup_signal(repo("a", 10000), repo("b", 10000))[0] == 1
