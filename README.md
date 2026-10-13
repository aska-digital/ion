# Ion

Ion is a steward bot for a GitHub organisation. It watches every repo, spots drift from your policy, and proposes structural changes (merge, split, archive, rename, move code). **It never changes structure without a maintainer's approval.** Apache-2.0.

**Website:** [aska-digital.github.io/ion](https://aska-digital.github.io/ion/), an infographic abstract with a working model of the approval gate, plus a guide to running ion. The site source lives in `site/` and deploys with `.github/workflows/pages.yml`.

See [Why ion](#why-ion) for the name.

```
collect -> analyze -> propose -> approve -> execute -> report
```

- **collect**: one batched GraphQL query for metadata, plus bare git mirrors under `~/.ion/cache` for contents. No REST contents calls.
- **analyze** (deterministic, no LLM): per-repo health, policy drift, pairwise coupling, in-repo cohesion, staged lifecycle, tag boundaries.
- **propose**: scores, hysteresis, cooldown, an optional LLM pass that writes the text, one tracking issue per proposal, each with a plan hash.
- **approve**: a maintainer applies a label, sends a bus message or ticks a dashboard box. Ion checks who did it and which plan they saw.
- **execute**: the only layer that writes, and only through typed, capped, logged write requests. Dry run by default. Ledgered, resumable, behind a circuit breaker.
- **report**: the "Org health" dashboard issue, a markdown report, a short digest to your notifiers.

## Features

- **Typed write requests with per-run caps.** Every GitHub write is a validated request of a known type; each type has a cap per run (5 PRs, 1 archive, ...). Over the cap, work waits for the next run.
- **Circuit breaker.** A run that would touch more than a quarter of the org, archive more than 2 repos, lower branch protection anywhere, or make too many writes is aborted before it writes.
- **Approval bound to a plan hash.** The issue shows `plan-sha256: ...`; an approval counts only for that hash; any re-plan takes the label off and asks again.
- **Action level per policy** (`log`, `issue`, `fix`) and **opt-in or opt-out** per repo.
- **Co-change hygiene.** Huge commits, bots and Ion's own identity are ignored; minimum revision counts; the shared-name-prefix signal is down-weighted and never counts alone.
- **Exact verification of moves.** Tree equality plus the filter-repo commit map, and a `Ion-Origin: org/repo@sha` trailer on every moved commit.
- **Org health dashboard issue** with approve and hold checkboxes, where the actor is still verified.
- **Staged lifecycle** (active, stale, archive-proposed) with a frozen exemption. Archiving is never automatic.
- **`ion plan-config`** previews what a config change would do before you use it.
- **Ordered changesets**: multi-repo proposals list their PRs in order, with dependencies and live states.
- **Boundary rules from repo tags** (GitHub topics): violations are findings, cross-scope merges are blocked or flagged.
- **`ion grep`** over every repo's mirror, and **`ion autofix`** for fleet changes (preview diffs, then a proposal; PRs only after approval).

## Install

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), `git` (with `git subtree`), the GitHub CLI `gh` (logged in), and for splits [git-filter-repo](https://github.com/newren/git-filter-repo).

```sh
git clone <this repo> && cd ion
uv sync                      # add --extra graph for networkx (Louvain clustering)
uv run ion --config ion.yml doctor
```

`doctor` checks gh auth, git and subtree, filter-repo, the LLM backend, the bus (if used) and the config.

## Architecture

```
collect -> analyze -> propose -> approve -> execute -> report
              |                    ^           |
        boundaries,            label, bus,   typed write requests
        lifecycle,             dashboard     -> caps -> circuit breaker
        coupling                 box            -> GitHub / git push
                              (bound to the plan hash)
```

Source layout: `ion/collect` (GraphQL, mirrors, snapshots), `ion/analyze` (health, policy, coupling, cohesion, lifecycle, boundaries), `ion/propose` (scoring, LLM text, engine), `ion/approve` (label, bus, state machine), `ion/execute` (one executor per proposal kind, `provenance.py` for verification), `ion/writes.py` (typed requests, caps, breaker), `ion/changesets.py`, `ion/report` (markdown, digest, `orghealth.py` for the dashboard issue), `ion/fleet.py` (grep, autofix), `ion/planconfig.py`. The full design is in `SPEC.md`.

## Configure

Copy `ion.example.yml` to `ion.yml` and edit. Pass it with `--config`, or set `ION_CONFIG`, or keep it at `./ion.yml` or `~/.ion/ion.yml`. State lives in `~/.ion/state.db` (`ION_HOME` overrides). Per-repo overrides go in `<repo>/.github/ion.yml` (keys: `maintainers`, `ignore`, `enabled`, `policy`, `policy_actions`).

Keys worth knowing:

| key | meaning |
|---|---|
| `org` | the organisation to steward |
| `maintainers` | GitHub logins whose approval counts |
| `tracker_repo` | repo where proposals are filed as issues. **Keep it private.** Proposal bodies name your private repos and quote their metadata. Default `ion-tracker`. |
| `approval_sources` | `github_label` (default), `divan_bus`, `dashboard` (checkboxes on the Org health issue) |
| `notifiers` | list of `stdout`, `file`, `divan_bus`, `github_issue`, `webhook` |
| `llm.backend` | `none` (default), `claude_cli`, `anthropic_api` |
| `policy_actions` | per policy class (`branch_protection`, `dependabot_alerts`, `secret_scanning`, `required_files`): `log` (record only), `issue` (propose, needs approval; the default) or `fix` (apply unattended). `auto_apply` is the old spelling of `fix`. |
| `policy_strategy` | `opt-out` (default: every repo is governed unless it sets `enabled: false`) or `opt-in` |
| `guards` | circuit breaker: `max_repo_fraction`, `min_repos`, `max_archives`, `max_writes`, `forbid_protection_lowering` |
| `writes.caps` | per-run cap by write type, for example `{open_pr: 5, archive_repo: 1}` |
| `coupling` | co-change filters: `max_changeset_size`, `min_revs`, `min_shared_revs`, `prefix_weight`, `bot_authors` |
| `lifecycle` | `dormant_days`, `frozen` (repos), `frozen_topic` |
| `boundaries` | tag rules `[{from, allow}]`, `scope_prefix`, `block_cross_scope_merge` |
| `report.org_health_issue` | publish the Org health dashboard issue |
| `thresholds` | `merge_coupling`, `split_cohesion_gap`, `stale_days`, `persist_runs`, `cooldown_days`, ... |
| `quorum`, `quorum_by_kind` | approvals required per proposal |
| `approval_ttl_days` | approvals expire (default 14) |
| `budget.graphql_points_per_run` | collection stops cleanly when reached (default 500) |
| `git_identity` | author of commits Ion makes. No AI trailer unless `ai_attribution: true`. |

Deployment configs (with your org's maintainers, bus addresses and paths) belong outside this repo, for example in `~/.ion/ion.yml` or in your private tracker repo.

### Notifiers

Each notifier is an entry in `notifiers`:

- `stdout`: prints the digest.
- `file`: appends to `path`.
- `divan_bus`: posts through a bus client subprocess (`bus.client`, `bus.python`, `bus.url`, `bus.seat`, and `to` or `bus.notify`).
- `github_issue`: replaces the body of issue `number` in the tracker repo.
- `webhook`: POSTs `{"text": ..., "content": ...}` (Slack and Discord both accept it) to `url` or the env var named by `url_env`.

A failed delivery is queued in the database and retried on the next run. A digest is sent only when something changed.

## Approval flow

1. Ion files an issue `[P-0007] merge a into b` in the tracker repo, labelled `ion:proposal`, `kind:<k>`, `state:<s>` and, while it waits, the org-wide `needs decision` label. That label comes off when the proposal leaves `awaiting-approval`, so a dashboard can treat it as the live decision queue. The body holds the evidence, the changesets in order, the exact commands the executor will run, the rollback, the blast radius and **`plan-sha256: <hash>`**. Ion edits the body in place and never comments. Pull requests ion opens also carry `needs decision`, because they wait for a maintainer to merge them.
2. A maintainer decides in one of three ways:
   - adds `ion:approved`, `ion:rejected` or `ion:hold`. Ion reads the issue **timeline** and takes the actor of the label event. A decision label from anyone who is not a maintainer (for the repos involved) is recorded as ignored and **taken off again** (one API call, no comment);
   - sends a Divan Bus message `ion approve|reject|hold P-0007` (optionally `plan-sha256:<prefix>`) from a seat in `bus.maintainer_seats`, after its signature is verified. Ion mirrors the decision onto the issue as the label, so the record lives on GitHub;
   - ticks the approve or hold box on the **Org health** issue (`approval_sources: [dashboard]`). Ion reads that issue's edit history and accepts the tick only if every edit since its own last write was made by a maintainer; an edit it cannot attribute fails closed.
3. **An approval counts only for the plan the approver saw.** Ion records which plan hash was on display just before each decision. If the plan changes (different commands, or the score moves by 0.15 or more) the hash changes, the recorded approvals stop counting, `ion:approved` and `ion:hold` come off the issue, and approval is asked again. A decision made before any plan was displayed never counts.
4. Approvals expire after `approval_ttl_days` (the label comes off too). `quorum` distinct approvers are needed (per kind if set).
5. Rejected proposals are suppressed for `cooldown_days` unless the score rises by 0.15.
6. Archiving is never automatic: it always needs its own approval, whatever the policy actions say.

A candidate must persist across `persist_runs` collection snapshots before it becomes a proposal.

### How coupling is scored

Per repo pair: cross-references (manifests, imports, git URLs, README links), duplicated code (identical blobs plus MinHash near-duplicates over normalised lines, ignoring vendored, lock and generated files), co-change, shared dependency set, shared name prefix. They combine as a noisy-or with weights `0.6, 0.6, 0.6, 0.3, 0.05`. One saturated signal alone reaches exactly 0.6, the merge threshold. Public/private pairs are flagged as a visibility decision.

- **Co-change** is shared revisions divided by the average number of revisions of the two repos. A revision is a day with a kept commit. Commits touching more than `max_changeset_size` files, commits by bots and commits by Ion's own git identity are left out; a repo needs `min_revs` revisions and a pair `min_shared_revs` shared ones. Co-change is half-weighted unless a content signal agrees, because one person working on several repos on the same days proves little.
- **Name prefix** counts only when another signal is at least 0.15, with a small weight. Repos named `protean-*` share a prefix because of a naming scheme.

## Commands

```
ion collect | analyze | propose [--dry-run] | sync-approvals | execute [--apply] [P-0007]
ion report | run [--apply] | status | explain P-0007 | pause | resume | doctor
ion grep PATTERN [--path GLOB] [--repo GLOB] [-i] [-F]
ion autofix [NAME] [--repo GLOB] [--propose]
ion plan-config NEW.yml            (or --set key=value ...)
ion dashboard [--print]
```

`run` is the whole pipeline and what the scheduler calls. Executors dry-run unless `--apply` is given.

- `ion grep` searches the default branch of every mirrored repo with `git grep`. No API calls.
- `ion autofix` lists the autofixers; `ion autofix NAME` prints the diffs it would make (nothing is written anywhere); `--propose` files one proposal for them. PRs are opened only by the executor, after a maintainer approves exactly those diffs. Write your own by subclassing `ion.fleet.Autofixer`, calling `register`, and naming the module under `autofix.plugins` in the config.
- `ion plan-config` compares the current config with a new one over the same snapshot: candidates gained and lost, policy drift, repos that would opt out, lifecycle changes, boundary violations, open proposals that would be re-planned. It writes nothing.

## Safety model

- **Approval before structure.** Every executor's `apply` calls one gate first (also for dry runs). It refuses unless the proposal has a verified approval *for its current plan hash*, or is a `policy_fix` or `health_fix` whose class has action level `fix` in a governed repo. A status flag in the database is not approval: the gate re-derives it from recorded, maintainer-verified approvals, the epoch, the plan hash, quorum and TTL. Archive, merge, split and the rest can never be unattended.
- **Typed writes with caps.** Nothing but `ion/writes.py` calls a mutating GitHub API (a test enforces this). Each request is schema-validated, counted against a per-type cap for the run, checked by the breaker and logged in `write_log`. A request over its cap is deferred to the next run, not failed. Pushes must target `ion/*` branches, never the default branch.
- **Circuit breaker.** Before an `--apply` run writes anything, a preflight adds up its blast radius; every write is checked again in flight. More than a quarter of the repos, more than 2 archives, any branch-protection change that is weaker than the current rule, or more than 150 writes: the run stops, says so in the digest, and records the trip (`ion status`).
- **Kill switch.** `ion pause` (creates `~/.ion/PAUSED`) or `paused: true` stops every executor and all tracker writes. Reads still work.
- **Nothing irreversible.** No deletes of repos, branches or history. Archive instead of delete. No force push, ever: a guard rejects `--force`, `-f`, `--force-with-lease`, `+refspec`, `:ref` deletes and branch deletion in any git call the executors make.
- **History kept and verified exactly.** Merge uses `git subtree add` without `--squash` and checks that the target's `HEAD:<prefix>` tree equals the source tip's tree and that the tip is an ancestor of the result. Split and move use `git filter-repo` on a fresh clone and check the commit map (every source commit touching the moved paths has a counterpart) and tree equality before anything is removed from the source. Counting commits is not a check. Rewritten commits carry `Ion-Origin: <org>/<repo>@<sha>`; the merge commit carries `Ion-Proposal` and `Ion-Origin`.
- **Ordered changesets.** A dependent step (the pointer PR, the removal PR, the archive) refuses to run until the PR or step before it has landed.
- **Archive is a separate approval** after a merge (`archive_requires_second_approval`, default true).
- **Ledger.** Every step writes a `before` and an `after` row, so a failed run resumes without repeating finished steps (`ion explain P-0007` shows it).
- **Low noise.** Issues are edited in place. Ion never comments on other repos and opens PRs only for real changes. A maintainer merges them; Ion never merges its own PRs.
- **Gentle on the API.** Metadata is GraphQL with a points budget per run. Contents come from local mirrors.
- **Untrusted text.** Repo names, descriptions and file contents given to an LLM are marked as data. LLM output is schema-validated, may only lower confidence, cannot add repos, and the plan shown to maintainers always comes from the executor, not the model.
- Tokens come from `gh` and are never put on a command line or in logs; git uses a credential helper that calls `gh auth token` at run time.
- **Licences.** `scripts/check_licenses.py` fails if any dependency is outside MIT, BSD, Apache-2.0, ISC, PSF or MPL-2.0. No GPL dependency, ever (pygit2 is refused by name).

## Tests

```sh
uv run pytest -q
```

Fully offline: fixture git repos in temp dirs, recorded GraphQL JSON, a stubbed LLM, a fake GitHub that records writes, a fake bus client. Merge, split, move and fleet autofix run end to end against local bare repos.

```sh
uv run python scripts/check_licenses.py     # the licence allowlist; also run by CI
```

`.github/workflows/ci.yml` runs both on Ubuntu with Python 3.12.

## Deployment

Phase 1 is a LaunchAgent on a Mac. `deploy/install.sh` renders `deploy/ai.aska.ion.plist` into `~/Library/LaunchAgents` and **does not load it**:

```sh
deploy/install.sh ~/.ion/ion.yml
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.aska.ion.plist
```

It runs `ion run` daily at 06:30 and logs to `~/.ion/ion.log`. The shipped plist runs executors as dry runs; add `--apply` after `run` once you trust the proposals. Keep the source outside `~/Documents` (macOS privacy prompts).

Phase 2 (when cost or uptime justify it): a GitHub App identity and a GitHub Actions schedule with `llm.backend: anthropic_api`. Only config changes.

## Prior art and credits

Ion's designs stand on published work by other projects. **No code was copied from any of them**; the ideas were re-implemented from their documentation and described behaviour. Nothing is vendored (see `THIRD_PARTY.md`). Names and marks belong to their owners, and none is used as a Ion feature name.

| Project | Licence | Idea taken |
|---|---|---|
| gh-aw (GitHub Agentic Workflows) | MIT | typed "safe outputs": the agent only proposes writes, a separate step validates them and enforces per-type limits |
| actions/stale | MIT | a hard cap on API writes per run (`operations-per-run`) |
| Peribolos (Prow) | Apache-2.0 | abort when a run would change too much (`--maximum-removal-delta`, minimum admins) |
| palantir/policy-bot | Apache-2.0 | approvals invalidated when the thing approved changes; edited approvals are not trusted |
| Prow (approve, OWNERS, lifecycle, branchprotector) | Apache-2.0 | a notifier edited in place; staged stale-then-rotten lifecycle with a frozen exemption; branch protection from central config |
| ossf/allstar | Apache-2.0 | per-policy action level (log, issue, fix) and opt-in or opt-out strategy |
| ossf/scorecard | Apache-2.0 | separating atomic findings from aggregate scores |
| todogroup/repolinter | Apache-2.0 | rules with levels and optional fixes |
| github/safe-settings, probot/settings | ISC | dry-run preview of a settings change; per-repo overrides of org settings |
| Renovate | AGPL-3.0 (design only) | the dashboard issue layout: one issue regenerated each run, with sections and checkboxes |
| Sourcegraph batch changes, Skyscanner/turbolift | Apache-2.0 (src-cli); Apache-2.0 | preview before apply; a campaign tracks its changesets |
| lindell/multi-gitter | Apache-2.0 | run a change per repo with a dry-run that shows diffs |
| asottile/all-repos | MIT | grep over local clones; the find-repos plus apply autofixer shape |
| github-community-projects/evergreen | MIT | detecting ecosystems to generate a Dependabot configuration |
| Zuul | Apache-2.0 | cross-repo `Depends-On` ordering of changes |
| Copybara | Apache-2.0 | a provenance label on every migrated commit |
| josh, splitsh/lite | MIT | a split is a deterministic function of history, so it can be re-verified |
| newren/git-filter-repo | MIT | the commit map (`.git/filter-repo/commit-map`) used for exact verification; the tool itself is run as a separate program |
| bors-ng | Apache-2.0 | verify the combined result before declaring success |
| code-maat and Adam Tornhill's change-coupling method | GPL-3.0 (method only) | coupling as shared revisions over average revisions, with minimum counts and a maximum changeset size |
| CodeScene | proprietary (ideas only) | temporal coupling and knowledge-loss as signals worth building towards |
| Nx, Turborepo, Bazel | MIT, MIT, Apache-2.0 | tag-based allowed-dependency rules (module boundaries, visibility) |
| Mergify | open engine formerly Apache-2.0; now closed | a config simulator to test a rule before committing it |
| PR-Agent, SWE-agent | MIT | self-check of generated text; a record of model calls (planned, see `SPEC.md` section 8) |

Licences were checked on 2026-10-13 against each project's LICENSE file. If you think a credit is missing or wrong, open an issue.

## Contributing

Contributions are welcome. Read `CONTRIBUTING.md` first; the short version:

- Ion is Apache-2.0. **Do not copy code from other projects.** Re-implement ideas in your own words and credit them in the table above. If you ever vendor code, it must be MIT, BSD, Apache-2.0 or ISC, recorded in `THIRD_PARTY.md` with the upstream commit and the licence text.
- Never add a GPL, LGPL or AGPL dependency (`scripts/check_licenses.py` fails the build).
- Every change needs tests (`uv run pytest -q`), and anything that writes to GitHub must go through a typed write request.
- Report security problems privately, see `SECURITY.md`. Be kind, see `CODE_OF_CONDUCT.md`.

## Licence

Copyright 2026 ASKA Business Solutions and the Ion contributors. Apache-2.0, see `LICENSE` and `NOTICE`. Dependencies are listed in `THIRD_PARTY.md`.

## Why ion

- **The word.** Michael Faraday coined "ion" in 1834 from the Greek ἰόν, "going", for the charged particles that move through a solution toward an electrode. An ion is something that goes. ion moves through every repository in the org and carries a signal from one place to another.
- **Lattices.** In a crystal, ions sit on the points of a lattice, and the pull between them holds the structure together. ion's job is the same: keep the org's structure sound. It watches how repos relate, proposes merges and splits, and holds policy steady. It also fits Exo's lattice theme: the incentre lattice, the exosphere, x0.
- **Charge, applied with care.** An ion carries charge, and charge only does work along a path. ion acts only through approved paths: the `ion:approved` label or a signed bus message. It makes no silent changes.
- **The amp team.** Current is ions in motion, measured in amperes. ion is reviewed, and likely powered, by the amp team, whose specialty is reviewing.
