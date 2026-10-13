# Contributing to Mimar

Thanks for helping. Mimar is a steward bot that can change an organisation's repositories, so the bar for changes is
"explainable, tested, and unable to surprise anyone".

## Ground rules

1. **Licence hygiene.** Mimar is Apache-2.0 (Copyright 2026 ASKA Business Solutions and the Mimar contributors).
   - **Do not copy code from other projects**, whatever its licence. Re-implement ideas in your own words, and credit the
     idea in the "Prior art and credits" table in `README.md` (project, licence, idea taken).
   - Do not paste code generated from an AGPL or GPL project's source. Describe the method and write it fresh.
   - If vendoring is ever truly necessary, the source must be MIT, BSD, Apache-2.0 or ISC. Keep the upstream licence
     header in the file, add the project, upstream URL, **commit SHA**, licence (SPDX), copyright line, the files and what
     you changed to `THIRD_PARTY.md`, and include the licence text there. If the upstream has a NOTICE file, carry its text
     into `NOTICE`.
   - **No GPL, LGPL or AGPL dependency, and never pygit2.** `uv run python scripts/check_licenses.py` fails the build on
     anything outside MIT, BSD, Apache-2.0, ISC, PSF and MPL-2.0.
2. **Writes go through typed requests.** Code outside `mimar/writes.py` must not call a mutating GitHub API. Add a request
   type (schema, default cap) there if you need a new kind of write. A test enforces this.
3. **Structure needs approval.** A new executor or proposal kind must be gated by the approval check, must be idempotent
   through the ledger, and must be covered by the circuit breaker's blast-radius rules.
4. **Quiet by default.** No comments on other repos, no chatty issue traffic; issues are edited in place.
5. **Explainable.** A proposal shows its evidence and the exact commands it will run.

## Setup

```sh
uv sync                                     # Python 3.12+; add --extra graph for networkx
uv run pytest -q                            # fully offline
uv run python scripts/check_licenses.py
```

The split and move tests need `git` with `git subtree` and [git-filter-repo](https://github.com/newren/git-filter-repo).

## Making a change

- One branch and one pull request per change. Say what and why in the description; link the issue with `Fixes #N`.
- Add tests with the change: every feature and fix needs at least one that fails without it. Tests use fixture git
  repos and a fake GitHub that records every write; they never touch the network.
- Keep the docs in step: `SPEC.md` for design, `README.md` for users, and a row in the credits table if an idea came from
  somewhere.
- Commit messages: a short imperative subject, a body that says why.

## Adding an analyzer, an autofixer or a notifier

- **Analyzer**: a pure function over a snapshot dict and an optional file listing, returning findings. Put thresholds in
  `config.py` defaults and the schema.
- **Autofixer**: subclass `mimar.fleet.Autofixer` (`find_repos`, `apply`, commit message, PR title and body) and
  `register` it. It edits a fresh checkout only; it never talks to GitHub. PRs open through the `autofix` executor.
- **Notifier**: subclass `Notifier` in `mimar/notify.py`.

## Conduct and security

Be kind: see `CODE_OF_CONDUCT.md`. Report vulnerabilities privately: see `SECURITY.md`.
