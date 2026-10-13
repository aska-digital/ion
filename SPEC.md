# Mimar — steward bot for the aska-digital org

*Mimar* is Ottoman Turkish for architect (Mimar Sinan built the empire's mosques). It sits beside the divan, the kethuda and the hafiz.

Mimar watches every repo in the aska-digital org and keeps the org in good shape. It suggests and carries out changes to how the org is laid out: merging repos, splitting them, archiving, renaming, moving code. It also keeps policy enforced. **It never makes a structural change without a maintainer's approval.**

## 1. Principles

1. **Read widely, write narrowly.** Analysis can touch everything. Writes go through one executor layer that checks approvals, supports dry runs and logs every action.
2. **Approval before structure.** Merges, splits, archives, renames, transfers and new repos all need an explicit approval from a maintainer. The only auto-fixes are policy fixes on an allowlist in config (branch protection drift, Dependabot, secret scanning), and the whole allowlist can be turned off.
3. **Nothing irreversible.** Mimar never deletes a repo, a branch with unmerged work, or history. It never force-pushes to a default branch. A merge keeps full history (subtree merge). A split keeps history too (filter-repo into a new repo, and the source keeps everything until a PR removes the moved directory). Archiving is used in place of deletion.
4. **Low-noise GitHub.** GitHub is the permanent public record. Mimar writes one tracking issue per proposal in `aska-digital/mimar`, and updates that issue's body in place rather than adding comments. It never comments on other repos. It opens PRs only for real changes. Conversation goes over the Divan Bus.
5. **Gentle on the API.** Metadata comes from batched GraphQL. Contents come from local `git fetch` mirrors in `~/.mimar/cache`, not the REST contents API. Each run has an API budget (default 500 GraphQL points) and stops cleanly when it hits it.
6. **Cheap by default.** Deterministic analysis does most of the work. An LLM only turns candidate findings into readable proposals and judges borderline cases. The default backend is headless Claude Code (`claude -p --model <m>`) on the existing seat, which adds no cost. The Anthropic API is a pluggable alternative. Each run has an LLM call cap.
7. **Explainable.** Every proposal carries its evidence (metrics, file lists, graph edges), a confidence score, a migration plan, a rollback plan and a blast-radius note.

## 2. Architecture

```
collect  ->  analyze  ->  propose  ->  approve  ->  execute  ->  report
(GraphQL     (signals,    (score,     (label or    (dry-run,    (bus digest,
 + git        graphs)      LLM         bus msg)     PRs,         issue body)
 mirrors)                  synth)                   ledger)
```

Python 3.12+, managed with `uv`. Package `mimar/`, CLI `mimar` (Typer or argparse). State lives in SQLite at `~/.mimar/state.db`; set `MIMAR_HOME` to override.

### 2.1 Collect (`mimar/collect/`)
- `github.py`: a batched GraphQL query over every org repo. It fetches name, visibility, fork/archived status, default branch, size, languages, topics, description, pushedAt, open issue and PR counts, licence, the last N commits on the default branch (author, date, changed-file count), branch protection (`branchProtectionRules`), vulnerability alert count, collaborators (if allowed), and the `.github/mimar.yml` override file via `object(expression:)`. Paginate. Record rate-limit cost.
- `mirror.py`: keeps a bare mirror per repo in `~/.mimar/cache/<repo>.git` using `git clone --mirror` the first time and `git fetch --prune` after that. Repos are skipped if `pushedAt` hasn't changed. Auth uses `gh auth token` through a credential helper, and the token is never logged.
- `snapshot.py`: writes one row per repo per run (JSON blob plus key columns) so trends can be tracked.

### 2.2 Analyze (`mimar/analyze/`), all deterministic and unit-tested
- **health**: missing README, LICENSE, CI or description; stale (no push in N days); open PRs older than N days; many vulnerability alerts; empty repo; a default branch other than `main`.
- **policy**: compares actual state against the desired state in `mimar.yml`, covering branch protection, Dependabot alerts, secret scanning (public repos only, because the free plan can't do it on private ones) and required files. Marks plan limits (for example, private-repo protection on the free plan) as `blocked_by_plan` rather than as drift.
- **coupling** (between repos), scored per repo pair:
  - cross-references: one repo names another in imports, `pyproject` or `package.json` dependencies, git URLs, submodules, or README links;
  - duplicated code: files with identical blob SHAs, plus near-duplicates via MinHash over normalised lines, ignoring vendored, lock and generated files;
  - co-change: commits in both repos within the same 24h window by the same author, normalised;
  - shared dependency set (Jaccard over declared deps) and a shared naming prefix (a weak signal).
- **cohesion** (inside one repo): builds a graph of the repo's top-level directories (or packages) using import edges and co-change. It finds clusters (connected components or Louvain if `networkx` is available) with few edges between clusters, different dependency sets or contributors, and a meaningful size. Those become split candidates.
- **lifecycle**: archive candidates (stale, no dependents, superseded by a duplicate) and forks that have fallen far behind upstream.

### 2.3 Propose (`mimar/propose/`)
- Proposal kinds: `merge`, `split`, `archive`, `rename`, `move_code`, `policy_fix`, `health_fix`, `new_repo`.
- `scoring.py`: turns signals into a score from 0 to 1 using thresholds in `mimar.yml`. Merges need strong coupling (for example, a coupling score of at least 0.6 sustained over at least 2 snapshots) and no visibility conflict. Never propose merging public with private without flagging it as a visibility decision. Splits need cohesion clusters above the thresholds.
- **Hysteresis and cooldown**: a candidate has to persist across K runs (default 2) before it becomes a proposal. A rejected proposal is suppressed for `cooldown_days` (default 60) unless its score rises by at least 0.15.
- `synth.py`: for each new candidate, one LLM call takes the structured evidence and returns JSON `{title, summary, rationale, risks, migration_plan[], rollback_plan[], blast_radius, confidence, recommend: propose|hold|drop}`. That JSON is validated against a schema, and validation failures fall back to a templated proposal. The LLM can lower confidence or recommend `hold`/`drop`. It cannot invent a proposal with no deterministic candidate behind it.
- `llm.py` backends: `claude_cli` (default; `claude -p --output-format json --model haiku`, with sonnet for merge/split proposals), `anthropic_api` (env `ANTHROPIC_API_KEY`), and `none` (templated only). Each run has a call cap and a timeout.

### 2.4 Approve (`mimar/approve/`)
- Maintainers are listed in `mimar.yml` (`maintainers: [ahrazzle]`), optionally per repo.
- Each proposal gets one issue in `aska-digital/mimar` titled `[P-0007] merge protean-handoff into protean-ops`. It's labelled `mimar:proposal`, `kind:<k>` and `state:awaiting-approval`. The body holds the evidence, the plan and the exact commands the executor will run (the dry-run output). Updates edit the body. Nobody adds comments.
- Approval works through either of two paths. **(a)** A maintainer adds the label `mimar:approved`, and Mimar checks the label event's actor through the timeline API to confirm it's a maintainer. Rejection uses `mimar:rejected`, and `mimar:hold` pauses a proposal. **(b)** A Divan Bus message from a maintainer seat (`saga-divan` maps to maintainer authority in config) with body `mimar approve P-0007` / `reject` / `hold`, verified by the bus signature. A bus approval is mirrored onto the issue as the label, applied by Mimar, so the record lives on GitHub.
- Approvals expire after `approval_ttl_days` (default 14), or straight away if the evidence fingerprint changes enough (the plan is re-planned and approval asked again).
- Structural kinds can require a quorum (`quorum: 1` by default).

### 2.5 Execute (`mimar/execute/`)
Every executor has the same interface: `plan(proposal) -> Plan` (a list of steps with exact commands or API calls), `apply(plan, dry_run)`, `verify()` and `rollback_notes()`. `apply` refuses to run unless the proposal is approved (or `policy_fix` is allowlisted) and the global kill switch is off. The kill switch is `~/.mimar/PAUSED`, or `paused: true` in config. Each step is written to the `ledger` table before and after it runs, so it's idempotent and can be resumed.
- **merge** A into B: clone B into a work dir; `git subtree add --prefix=<dir> <A mirror> <branch>` with no squash, so history is kept; open a PR in B on branch `mimar/P-xxxx`. Once the PR merges (detected on a later run), open a PR in A that replaces the README with a pointer to B, then archive A. Archiving is a separate approved step if `archive_requires_second_approval: true`, which is the default.
- **split** dir D out of A into a new repo N: `git filter-repo --subdirectory-filter D` on a fresh clone, then create N (the visibility matches A unless the plan says otherwise, and the plan copies A's protections and policy), and push. After that, a PR in A removes D and adds a pointer. Nothing is removed from A until N exists and has been verified (same commit count for D's paths).
- **archive**, **rename** (with a redirect note) and **move_code** (subtree-style, as two PRs).
- **policy_fix**: idempotent REST or GraphQL calls such as branch protection PUT, Dependabot alerts or secret scanning. Auto-applied only when its class is in `auto_apply` in config, and each fix is still logged and reported.
- **health_fix**: opens a PR, for example to add a missing LICENSE or README skeleton or a Dependabot config. Approval required unless allowlisted.
- Work dirs go in `~/.mimar/work/P-xxxx`. Commit identity comes from config (`git_identity`). Commits carry no AI attribution trailers in public repos, unless config says otherwise.

### 2.6 Report (`mimar/report/`)
- `mimar report`: a markdown org health dashboard, written to `~/.mimar/reports/YYYY-MM-DD.md`.
- Divan Bus: after each run it posts a short digest to the `saga-divan` seat: new proposals, approvals waiting, actions taken and errors. It posts only when something changed. The bus client is `~/divan-bus/clients/divan.py`, run as a subprocess (`DIVAN_URL`, `DIVAN_SEAT=mimar`, key path from config). If the bus is down, the digest is queued in the DB and retried.
- Optional: one pinned issue in `aska-digital/mimar` named "Org health", whose body is replaced each week.

## 3. Config (`mimar.yml` at repo root; per-repo overrides in `<repo>/.github/mimar.yml`)
```yaml
org: aska-digital
paused: false
maintainers: [ahrazzle]
bus: {url: "http://127.0.0.1:8787", seat: mimar, notify: saga-divan, maintainer_seats: [saga-divan]}
llm: {backend: claude_cli, model_default: haiku, model_structural: sonnet, max_calls_per_run: 20}
budget: {graphql_points_per_run: 500}
policy:
  default_branch: main
  branch_protection:   # applied to public non-fork repos; private repos are skipped on the free plan
    require_pr: true
    required_approvals: 0
    allow_force_pushes: false
    allow_deletions: false
    enforce_admins: false
  dependabot_alerts: true
  secret_scanning: public_only
  required_files: [README.md, LICENSE]   # LICENSE for public repos only
auto_apply: [branch_protection, dependabot_alerts, secret_scanning]
thresholds:
  merge_coupling: 0.6
  split_cohesion_gap: 0.5
  stale_days: 180
  persist_runs: 2
  cooldown_days: 60
approval_ttl_days: 14
archive_requires_second_approval: true
exclude: [hermes-agent]   # forks we keep in sync with upstream
git_identity: {name: "mimar", email: "mimar@askaconsult.com"}
```

## 4. CLI
`mimar collect | analyze | propose | sync-approvals | execute [--dry-run/--apply] [P-xxxx] | report | run` (`run` does the full pipeline and is what the scheduler calls) `| status | explain P-xxxx | pause | resume | doctor` (checks gh auth, git, filter-repo, claude, the bus and the config).

## 5. Deployment
- Phase 1 (now, at no cost): a LaunchAgent `ai.aska.mimar` on the Mac runs `mimar run` daily at 06:30 local, logging to `~/.mimar/mimar.log`. The source is in `~/Work/mimar` (not ~/Documents, because of TCC). It's pushed to the private repo `aska-digital/mimar`. Auth uses the `gh` token on the Mac.
- Phase 2 (when cost or uptime justify it): a GitHub App identity (separate rate limit, bot attribution) and a GitHub Actions schedule in `aska-digital/mimar`, with `llm.backend: anthropic_api`. The code needs no changes, only config.

## 6. Tests
`pytest`, with no network access. Fixtures build local git repos (two coupled, one cohesive monorepo with two separable halves, one stale). GraphQL responses are recorded as JSON fixtures. The tests cover the analyzers, scoring, hysteresis and cooldown, approval verification (including rejecting labels added by people who aren't maintainers), the executor plans, and the dry-run output for merge and split. Merge and split `apply` run end-to-end against local bare repos, with the GitHub API calls mocked. The LLM is stubbed.

## 7. Out of scope for v1
Webhooks or a live server (polling once a day is enough); cross-org work; auto-merging PRs Mimar opens itself (a maintainer merges them).
