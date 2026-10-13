# Mimar

Mimar is a steward bot for a GitHub organisation. It watches every repo, spots drift from your policy, and proposes structural changes (merge, split, archive, rename, move code). **It never changes structure without a maintainer's approval.** Apache-2.0.

*Mimar* is Ottoman Turkish for architect.

```
collect -> analyze -> propose -> approve -> execute -> report
```

- **collect**: one batched GraphQL query for metadata, plus bare git mirrors under `~/.mimar/cache` for contents. No REST contents calls.
- **analyze** (deterministic, no LLM): per-repo health, policy drift, pairwise coupling, in-repo cohesion, lifecycle.
- **propose**: scores, hysteresis, cooldown, an optional LLM pass that writes the text, one tracking issue per proposal.
- **approve**: a maintainer applies a label (or sends a bus message). Mimar checks who did it.
- **execute**: the only layer that writes. Dry run by default. Ledgered, resumable.
- **report**: markdown dashboard, short digest to your notifiers.

## Install

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), `git` (with `git subtree`), the GitHub CLI `gh` (logged in), and for splits [git-filter-repo](https://github.com/newren/git-filter-repo).

```sh
git clone <this repo> && cd mimar
uv sync                      # add --extra graph for networkx (Louvain clustering)
uv run mimar --config mimar.yml doctor
```

`doctor` checks gh auth, git and subtree, filter-repo, the LLM backend, the bus (if used) and the config.

## Configure

Copy `mimar.example.yml` to `mimar.yml` and edit. Pass it with `--config`, or set `MIMAR_CONFIG`, or keep it at `./mimar.yml` or `~/.mimar/mimar.yml`. State lives in `~/.mimar/state.db` (`MIMAR_HOME` overrides). Per-repo overrides go in `<repo>/.github/mimar.yml` (keys: `maintainers`, `ignore`, `policy`).

Keys worth knowing:

| key | meaning |
|---|---|
| `org` | the organisation to steward |
| `maintainers` | GitHub logins whose approval counts |
| `tracker_repo` | repo where proposals are filed as issues. **Keep it private.** Proposal bodies name your private repos and quote their metadata. Default `mimar-tracker`. |
| `approval_sources` | `github_label` (default), `divan_bus` |
| `notifiers` | list of `stdout`, `file`, `divan_bus`, `github_issue`, `webhook` |
| `llm.backend` | `none` (default), `claude_cli`, `anthropic_api` |
| `auto_apply` | policy fixes Mimar may apply without approval. Empty by default. |
| `thresholds` | `merge_coupling`, `split_cohesion_gap`, `stale_days`, `persist_runs`, `cooldown_days`, ... |
| `quorum`, `quorum_by_kind` | approvals required per proposal |
| `approval_ttl_days` | approvals expire (default 14) |
| `budget.graphql_points_per_run` | collection stops cleanly when reached (default 500) |
| `git_identity` | author of commits Mimar makes. No AI trailer unless `ai_attribution: true`. |

Mimar's own org config is in `config/aska-digital.yml`.

### Notifiers

Each notifier is an entry in `notifiers`:

- `stdout`: prints the digest.
- `file`: appends to `path`.
- `divan_bus`: posts through a bus client subprocess (`bus.client`, `bus.python`, `bus.url`, `bus.seat`, and `to` or `bus.notify`).
- `github_issue`: replaces the body of issue `number` in the tracker repo.
- `webhook`: POSTs `{"text": ..., "content": ...}` (Slack and Discord both accept it) to `url` or the env var named by `url_env`.

A failed delivery is queued in the database and retried on the next run. A digest is sent only when something changed.

## Approval flow

1. Mimar files an issue `[P-0007] merge a into b` in the tracker repo, labelled `mimar:proposal`, `kind:<k>`, `state:<s>`. The body holds the evidence, the exact commands the executor will run, the rollback and the blast radius. Mimar edits the body in place and never comments.
2. A maintainer adds `mimar:approved`, `mimar:rejected` or `mimar:hold`. Mimar reads the issue **timeline** and takes the actor of the label event. A label from anyone who is not a maintainer (for the repos involved) is recorded as ignored and has no effect. A label removed again is withdrawn.
3. Alternatively, with `divan_bus` as an approval source, a message `mimar approve|reject|hold P-0007` from a seat listed in `bus.maintainer_seats` counts, after its signature is verified through the bus client. Mimar mirrors the decision onto the issue as the label, so the record lives on GitHub. Those mirrored label events are Mimar's own and are not counted twice.
4. Approvals expire after `approval_ttl_days`, and also when the plan changes (different commands, or the score moves by 0.15 or more). Then the proposal goes back to awaiting approval.
5. `quorum` distinct approvers are needed (per kind if set).
6. Rejected proposals are suppressed for `cooldown_days` unless the score rises by 0.15.

A candidate must persist across `persist_runs` collection snapshots before it becomes a proposal.

### How coupling is scored

Per repo pair: cross-references (manifests, imports, git URLs, README links), duplicated code (identical blobs plus MinHash near-duplicates over normalised lines, ignoring vendored, lock and generated files), co-change (same author active in both repos within a day), shared dependency set, shared name prefix. They combine as a noisy-or with weights `0.6, 0.6, 0.6, 0.3, 0.15`. Co-change is half-weighted unless a content signal agrees, because one person working on several repos on the same days proves little. One saturated signal alone reaches exactly 0.6, the merge threshold. Public/private pairs are flagged as a visibility decision.

## Commands

```
mimar collect | analyze | propose [--dry-run] | sync-approvals | execute [--apply] [P-0007]
mimar report | run [--apply] | status | explain P-0007 | pause | resume | doctor
```

`run` is the whole pipeline and what the scheduler calls. Executors dry-run unless `--apply` is given.

## Safety model

- **Approval before structure.** Every executor's `apply` calls one gate first (also for dry runs). It refuses unless the proposal has a verified approval, or is a `policy_fix` (or `health_fix`) whose class is in `auto_apply`. A status flag in the database is not approval: the gate re-derives it from recorded, maintainer-verified approvals, the epoch, quorum and TTL.
- **Kill switch.** `mimar pause` (creates `~/.mimar/PAUSED`) or `paused: true` stops every executor and all tracker writes. Reads still work.
- **Nothing irreversible.** No deletes of repos, branches or history. Archive instead of delete. No force push, ever: a guard rejects `--force`, `-f`, `--force-with-lease`, `+refspec`, `:ref` deletes and branch deletion in any git call the executors make. Executors push only to `mimar/P-xxxx*` branches, except the first push into a brand new empty repo during a split, which is refused if the repo already has refs.
- **History kept.** Merge uses `git subtree add` without `--squash`, and checks that the source tip is an ancestor of the result. Split uses `git filter-repo` on a fresh clone, creates the new repo, pushes, verifies the commit count, and only then opens a PR in the source that removes the directory. Nothing is removed from the source before the new repo is verified.
- **Archive is a separate approval** after a merge (`archive_requires_second_approval`, default true).
- **Ledger.** Every step writes a `before` and an `after` row, so a failed run resumes without repeating finished steps (`mimar explain P-0007` shows it).
- **Low noise.** Issues are edited in place. Mimar never comments on other repos and opens PRs only for real changes. A maintainer merges them; Mimar never merges its own PRs.
- **Gentle on the API.** Metadata is GraphQL with a points budget per run. Contents come from local mirrors.
- **Untrusted text.** Repo names, descriptions and file contents given to an LLM are marked as data. LLM output is schema-validated, may only lower confidence, cannot add repos, and the plan shown to maintainers always comes from the executor, not the model.
- Tokens come from `gh` and are never put on a command line or in logs; git uses a credential helper that calls `gh auth token` at run time.

## Tests

```sh
uv run pytest -q
```

Fully offline: fixture git repos in temp dirs, recorded GraphQL JSON, a stubbed LLM, a fake GitHub that records writes, a fake bus client. Merge and split run end to end against local bare repos.

## Deployment

Phase 1 is a LaunchAgent on a Mac. `deploy/install.sh` renders `deploy/ai.aska.mimar.plist` into `~/Library/LaunchAgents` and **does not load it**:

```sh
deploy/install.sh config/aska-digital.yml
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.aska.mimar.plist
```

It runs `mimar run` daily at 06:30 and logs to `~/.mimar/mimar.log`. The shipped plist runs executors as dry runs; add `--apply` after `run` once you trust the proposals. Keep the source outside `~/Documents` (macOS privacy prompts).

Phase 2 (when cost or uptime justify it): a GitHub App identity and a GitHub Actions schedule with `llm.backend: anthropic_api`. Only config changes.

## Licence

Apache-2.0, see `LICENSE` and `NOTICE`. Dependencies are listed in `THIRD_PARTY.md`.
