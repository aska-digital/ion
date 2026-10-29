# Ion: steward bot for a GitHub org (spec v2)

*Ion* is the org's steward bot (see "Why ion" in the README). It watches every repo in an org and keeps the org in good shape. It suggests and carries out changes to how the org is laid out: merging repos, splitting them, archiving, renaming, moving code. It also keeps policy enforced. **It never makes a structural change without a maintainer's approval.**

v2 keeps the v1 pipeline and hardens it. What is new is marked *(v2)*. Section 9 lists every v2 change against the research survey it came from.

## 1. Principles

1. **Read widely, write narrowly.** Analysis can touch everything. Every write is a *typed request* that the executor layer validates, caps, logs and (for structure) checks for approval. *(v2: the typed request is enforced in code, see 2.5.)*
2. **Approval before structure.** Merges, splits, archives, renames, transfers, new repos and fleet fixes all need an explicit approval from a maintainer. Archiving is never automatic, whatever the config says. Policy fixes can run unattended only for classes whose action level is `fix`, and the whole thing can be turned off.
3. **Approval is for one plan.** An approval is bound to a hash of the exact plan that the tracker issue showed. If the plan changes, the approval stops counting and the label comes off. *(v2)*
4. **Nothing irreversible.** Ion never deletes a repo, a branch with unmerged work, or history. It never force-pushes. A merge keeps full history (subtree merge). A split keeps history (filter-repo into a new repo) and the source keeps everything until a PR removes the moved directory. Archiving is used in place of deletion.
5. **Verify exactly, then proceed.** A move is checked by tree equality and the filter-repo commit map, not by counting commits. Each moved commit carries a provenance trailer. *(v2)*
6. **Stop when the run looks wrong.** A circuit breaker aborts the whole run if it would touch an unreasonable share of the org or lower protection anywhere. *(v2)*
7. **Low-noise GitHub.** GitHub is the permanent public record. One tracking issue per proposal and one dashboard issue, both edited in place. Ion never comments on other repos and opens PRs only for real changes. Conversation goes over the Divan Bus (or any notifier).
8. **Gentle on the API.** Metadata comes from batched GraphQL under a points budget. Contents come from local git mirrors, not the REST contents API. Searching the fleet (`ion grep`) is free.
9. **Cheap by default.** Deterministic analysis does most of the work. An LLM only writes the text of candidate findings and may lower confidence. Default backend: headless Claude Code on an existing seat. Each run has a call cap.
10. **Explainable.** Every proposal carries evidence, a confidence score, the exact commands, a rollback plan, a blast radius and its plan hash.

## 2. Architecture

```
collect  ->  analyze  ->  propose  ->  approve  ->  execute  ->  report
(GraphQL     (signals,    (score,     (label, bus   (typed       (dashboard
 + git        graphs,      hysteresis, or ticked     writes,      issue, digest,
 mirrors)     boundaries)  LLM text)   box, bound    caps,        markdown)
                                       to plan hash) breaker,
                                                     ledger)
```

Python 3.12+, managed with `uv`. Package `ion/`, CLI `ion`. State lives in SQLite at `~/.ion/state.db` (`ION_HOME` overrides).

### 2.1 Collect (`ion/collect/`)
- `github.py`: batched GraphQL over every org repo: visibility, fork and archive flags, default branch, size, languages, topics, description, `pushedAt`, open issue and PR counts, licence, recent commits (author, date, changed-file count), branch protection, vulnerability alerts, and `.github/ion.yml`. Paginated, with the rate-limit cost recorded and a points budget (default 500).
- `mirror.py`: a bare mirror per repo in `~/.ion/cache/<repo>.git`; `git fetch --prune` after the first clone; skipped when `pushedAt` is unchanged. The token comes from a credential helper that calls `gh auth token` and is never logged.
- `snapshot.py`: one row per repo per run so trends can be tracked.

### 2.2 Analyze (`ion/analyze/`), deterministic
- **health**: missing README, LICENSE, CI, description; stale; old PRs; vulnerabilities; default branch.
- **policy**: actual state against the desired state in `ion.yml`: branch protection, Dependabot alerts, secret scanning, required files. Plan limits are `blocked_by_plan`, not drift. Each finding carries its **action level** (`log`, `issue` or `fix`) and repos can be governed **opt-out** (default) or **opt-in**. *(v2)*
- **coupling** between repos, per pair: cross-references, duplicated code (identical blobs plus MinHash near-duplicates), co-change, shared dependencies, shared name prefix.
  - *(v2)* Co-change is change coupling in Tornhill's sense: shared revisions divided by the average number of revisions. It ignores commits that touch more than `max_changeset_size` files, bot authors and Ion's own git identity, and needs `min_revs` revisions per repo and `min_shared_revs` shared ones.
  - *(v2)* The shared-name-prefix signal has a small weight and counts only when another signal agrees. In the v1 live run, 265 coupled pairs were mostly `protean-*` repos that share only a naming scheme.
- **cohesion** inside one repo: clusters of top-level units by import edges and co-change (same filters as above); split candidates when clusters are separable.
- **lifecycle** *(v2: staged)*: `active` (pushed within `stale_days`), then `stale`, then `archive-proposed` (another `dormant_days` without a push). Any push resets the stage. A repo listed in `lifecycle.frozen` or carrying the `ion-frozen` topic is exempt. Reaching the last stage only makes a repo eligible for an archive *proposal*.
- **boundaries** *(v2)*: repo tags (GitHub topics such as `scope:app`, `layer:shared`) plus allowed-dependency rules in config. A dependency that breaks a rule is a finding. Merge proposals across scopes are flagged and, by default, not proposed.

### 2.3 Propose (`ion/propose/`)
- Kinds: `merge`, `split`, `archive`, `rename`, `move_code`, `policy_fix`, `health_fix`, `new_repo`, and *(v2)* `autofix` (a fleet change over many repos).
- `scoring.py`: scores, hysteresis (a candidate must persist for K runs, default 2) and cooldown (a rejected proposal is suppressed for `cooldown_days` unless its score rises by `rescore_delta`).
- `synth.py`: one LLM call per new candidate returns JSON that is validated against a schema; failures fall back to a template. The LLM can lower confidence or recommend hold or drop. It cannot invent a proposal. The migration plan shown to maintainers always comes from the executor.
- Every proposal gets a **plan hash** (`plan-sha256`): SHA-256 over its kind, subject and every step with its exact command. *(v2)*

### 2.4 Approve (`ion/approve/`)
- Maintainers are listed in config. A repo's `.github/ion.yml` may narrow the list for that repo, never widen it: its `maintainers` is intersected with the org list (case-insensitive), so a login the org list lacks never counts, and a repo list that names no org maintainer leaves that repo with no approvers. An actor must be a maintainer of every repo the proposal touches.
- Three channels, all verified, all bound to the plan hash *(v2)*:
  1. **Label** on the proposal issue: `ion:approved`, `ion:rejected`, `ion:hold`. Ion reads the issue timeline and takes the actor of the label event. A decision label from a non-maintainer is recorded as ignored **and removed again** (one API call, no comment). *(v2)*
  2. **Divan Bus** message `ion approve|reject|hold P-0007 [plan-sha256:<prefix>]` from a maintainer seat, signature-checked. A named hash must be one the issue actually displayed before the message. The decision is mirrored onto the issue as the label.
  3. **Dashboard checkbox** *(v2, opt-in via `approval_sources: [dashboard]`)*: the "Org health" issue has an approve box and a hold box per proposal. See 2.6 for how the actor is verified.
- **Plan-hash binding** *(v2)*: the issue body shows `plan-sha256: <hash>`; the hash that was displayed just before an approval event is stored with the approval. An approval counts only if it equals the proposal's current hash. A re-plan (different commands, or a score move of `rescore_delta`) changes the hash, takes `ion:approved` and `ion:hold` off the issue, invalidates the recorded approvals and asks again. An approval from before any plan was displayed never counts.
- Approvals expire after `approval_ttl_days` (labels come off then too). `quorum` approvers per proposal (per kind if set).
- Ion's own decision labels (mirrors of bus or dashboard decisions) are recognised by event id and never counted twice. A login is not used to recognise Ion, because in phase 1 Ion runs on a maintainer's token.

### 2.5 Execute (`ion/execute/`, `ion/writes.py`)
Every executor has the same interface: `plan(proposal) -> Plan`, `apply(plan, dry_run)`, `verify()`, `rollback_notes()`. `apply` refuses to run unless the approval gate passes (an action-level `fix` policy class is the only alternative to approval) and the kill switch is off. Each step is written to the `ledger` table before and after it runs, so a run resumes where it stopped.

**Typed write requests and caps** *(v2)*: nothing outside `ion/writes.py` calls a mutating GitHub API. A `WriteRequest` has a type (`open_issue`, `edit_issue_body`, `create_label`, `add_label`, `remove_label`, `open_pr`, `push_branch`, `put_branch_protection`, `enable_dependabot_alerts`, `enable_secret_scanning`, `archive_repo`, `create_repo`, `rename_repo`), a repo and a payload that is validated against a JSON schema. The broker then checks the kill switch, applies the **per-type cap for the run** (defaults: 5 PRs, 1 archive, 2 repo creations, 15 pushes, ... overridable with `writes.caps`), asks the breaker, performs the call and appends a row to `write_log`. A request over its cap is *deferred* (the executor step waits and resumes next run), not failed. A `push_branch` request must target a `ion/*` branch and never the default branch (the one exception: the first push into a brand new empty repo).

**Circuit breaker** *(v2)*: config `guards`. Before an `--apply` run writes anything, a preflight totals everything that run would do. During the run, every write is checked again. The run is aborted (and the digest says so, which reaches the bus) if:
- the run would touch more than `max_repo_fraction` of the org's repos (default 25%, applied once at least `min_repos` repos are involved);
- more than `max_archives` repos would be archived (default 2);
- a branch-protection change would be weaker than what the repo has now (`forbid_protection_lowering`);
- more than `max_writes` writes happen in one run.
A trip is recorded in `last_breaker_trip` and shown by `ion status` and on the dashboard.

Executors:
- **merge** A into B: `git subtree add` (no squash) into a PR on `ion/P-xxxx`; then a pointer PR in A; then a *separate* archive proposal. Verified by **tree equality**: B's `HEAD:<prefix>` tree equals A's tip tree, and A's tip is an ancestor of the result. The merge commit carries `Ion-Proposal` and `Ion-Origin: <org>/<A>@<sha>` trailers (the imported commits keep their own SHAs). *(v2)*
- **split** dir D out of A into N: `git filter-repo` on a fresh clone, with a commit callback that appends `Ion-Origin: <org>/<A>@<original sha>` to every rewritten commit. Verified by the **commit map** (`.git/filter-repo/commit-map`: every source commit touching D maps to a commit reachable in the result) and **tree equality** (the result's root tree equals A's `HEAD:D` tree; several dirs: each tree equal and nothing else in the root). Only then is the removal PR opened. *(v2)*
- **move_code** with the same two checks (filtered tree, then the target's `HEAD:<dest>` tree).
- **archive**, **rename**, **new_repo**, **policy_fix**, **health_fix** (unchanged).
- **autofix** *(v2)*: fleet changes over the mirrors (2.7). One PR per repo, opened only after approval, each recomputed on a fresh clone and compared with the approved diff.

**Ordered changesets** *(v2)*: a structural proposal owns an ordered list of changesets with `depends_on` (merge: import PR in B, then pointer PR in A, then archive A; split: create N, then removal PR in A; move_code: add in T, then remove in S). A step cannot run before its dependencies have merged or finished; the issue shows the list with live PR states; dependent PR bodies carry `Depends-On: org/repo#N`. Resuming after a crash is trivial because states come from what exists.

The kill switch is `~/.ion/PAUSED`, or `paused: true` in config. Work dirs go in `~/.ion/work/P-xxxx`. Commit identity comes from config. No AI attribution trailers in public repos unless config says so.

### 2.6 Report (`ion/report/`)
- `ion report`: a markdown dashboard in `~/.ion/reports/` and a short digest to the notifiers (stdout, file, Divan Bus, webhook, GitHub issue). It posts only when something changed. Failed deliveries are queued.
- **Org health dashboard issue** *(v2, `report.org_health_issue: true`)*: one issue in the tracker repo, regenerated in place each run, with sections *Awaiting approval*, *Approved (queued)*, *On hold*, *Blocked by plan*, *Rate-limited or stopped early*, *Errors*, *Lifecycle*, *Recently done*, each proposal linking to its own issue.
- **Checkbox approvals** *(opt-in)*: each awaiting proposal has an approve box and a hold box. A box carries the plan hash it was rendered for. Ion remembers the hash and time of its own last write to the issue. At sync, if the body changed, it reads the issue's edit history (`userContentEdits`); every edit after Ion's last write must be by a maintainer of the repos involved, otherwise the ticks are recorded as ignored. A change with no attributable edit fails closed. A tick counts only if the plan hash on the box is still the proposal's current hash. Ion's next write replaces the body, so ticks never linger, and a tick is applied to a proposal at most once.

### 2.7 Fleet tools (`ion/fleet.py`) *(v2)*
- `ion grep PATTERN`: `git grep` over every mirror's default branch (flags: `--path`, `--repo`, `-i`, `-F`, `--max`). No API calls, no writes.
- `ion autofix [NAME]`: an autofixer is a class with `find_repos(ctx, repos)`, `apply(workdir, repo, snap)`, a commit message and a PR title and body. Built in: `readme-skeleton`, `dependabot-config` (ecosystems detected from the file tree). Plugins register with `fleet.register` from modules named in `autofix.plugins`. Without flags it prints diffs and writes nothing. With `--propose` it files one `autofix` proposal whose subject holds a digest of each diff, so the approval covers exactly those changes. PRs are opened only by the executor, through typed `push_branch` and `open_pr` requests, after approval, subject to caps and the breaker.

### 2.8 Config preview (`ion/planconfig.py`) *(v2)*
`ion plan-config NEW.yml` (or `--set key=value`) runs the analysis and candidate gathering under the current and the new config over the same snapshot and mirrors, and reports: changed keys, candidates added, removed, rescored or newly blocked, policy drift and action-level changes, repos that would opt out, lifecycle stage changes, boundary violations, and which open proposals would be re-planned or dropped. It writes nothing.

### 2.9 Offline operator console and pipeline journal

`ion console` exports a self-contained HTML view or JSON from a consistent local read transaction. `--demo` uses synthetic records without opening local state. The console makes no service calls, imports no autofix plugins and cannot approve or execute. It shows collection completeness, matching analysis generation and configuration, mirror ref coverage, saved proposal decisions, current phase plans, ledger evidence and run history. Unknown and mismatched evidence remains explicit. See [the operator guide](docs/operator-console.md).

`pipeline_runs` and `pipeline_steps` record the full pipeline separately from collection `runs`: start, finish, outcome, duration, configuration hash, collection generation, measured API usage, broker writes and errors. Interrupted or partial work cannot become a successful freshness record. `report.stale_after_hours` defaults to 48. Reports generated before their own delivery completes explicitly mark the current outcome as pending.

## 3. Config (`ion.yml`; per-repo overrides in `<repo>/.github/ion.yml`)
See `ion.example.yml`. New keys in v2:

```yaml
policy_actions: {branch_protection: fix, dependabot_alerts: fix, secret_scanning: fix, required_files: issue}  # log | issue | fix
policy_strategy: opt-out          # or opt-in (then repos opt in with `enabled: true`, or are listed in policy_repos)
guards: {enabled: true, max_repo_fraction: 0.25, min_repos: 4, max_archives: 2, max_writes: 150, forbid_protection_lowering: true}
writes: {caps: {open_pr: 5, archive_repo: 1}}
coupling: {max_changeset_size: 40, min_revs: 5, min_shared_revs: 3, prefix_weight: 0.05, bot_authors: [dependabot, renovate, "[bot]"]}
lifecycle: {dormant_days: 90, frozen: [], frozen_topic: ion-frozen}
boundaries: {rules: [{from: "scope:app", allow: ["scope:app", "layer:shared"]}], scope_prefix: "scope:", block_cross_scope_merge: true}
approval_sources: [github_label, divan_bus, dashboard]
report: {org_health_issue: true}
```
`auto_apply` still works as the old spelling of `policy_actions: {<class>: fix}`. A repo's `.github/ion.yml` may set `enabled`, `ignore`, `maintainers`, `policy`, `policy_actions`. Anyone who can push to the repo can edit that file, so `maintainers` and `policy_actions` can only narrow the org config: `maintainers` is intersected with the org list, and a `policy_actions` level above the org level (`log` < `issue` < `fix`) is lowered to it. What was ignored shows as the `override_widens` health finding. The tracker repo (`tracker_repo`) must be private: proposal bodies name private repos.

## 4. CLI
`ion collect | analyze | propose | sync-approvals | execute [--apply] [P-xxxx] | report | run [--apply] | status | explain P-xxxx | pause | resume | doctor`
and, new in v2: `ion grep | autofix | plan-config | dashboard`.

## 5. Deployment
- Phase 1: a LaunchAgent on a Mac runs `ion run` daily (executors dry-run until `--apply` is added). Source outside `~/Documents`.
- Phase 2: a GitHub App identity and a scheduled Action with `llm.backend: anthropic_api`. Only config changes.

## 6. Tests
`pytest`, no network. Fixtures build local git repos; GraphQL is recorded JSON; a fake GitHub records every write; a fake bus; a stubbed LLM. Every feature and fix in this spec has tests, including: plan-hash approval invalidated on a re-plan; the circuit breaker tripping on volume, on repo share, on archives and on a protection downgrade; per-type caps deferring rather than failing; tree equality rejecting a result with the right commit count and the wrong content; non-maintainer label removal; dashboard actor verification; no mutating GitHub call outside `writes.py` (a static test). `scripts/check_licenses.py` is the CI-free licence guard.

## 7. Out of scope
Webhooks or a live server (polling once a day is enough); cross-org work; auto-merging PRs Ion opens itself (a maintainer merges them); team and membership management; an agent that writes code; AI review of other people's PRs.

## 8. Not yet built (stubs and later work)
- **Split proposals that prefer cuts along tag boundaries.** Tags are repo-level topics; there are no per-directory tags to cut along. Merge-side boundary checks are done.
- **Multi-branch or tag import on merge** (default branch only).
- **Dashboard pinning** (GitHub's pin needs a GraphQL mutation Ion does not make; pin the issue by hand once).
- **Features 13 to 20 of the research survey** except the Dependabot generator, which exists as the `dependabot-config` autofixer: inherited config with globs, co-change by shared reference, LLM self-check and trajectory log, conditional rules via facts, Scorecard input, path owners.

## 9. v2 against the research survey (ranked features)
| # | Feature | Where |
|---|---|---|
| 1 | Typed write requests with per-type caps | `writes.py`, all executors, tracker, labels |
| 2 | Run-wide circuit breaker | `writes.py` (in flight), `execute/runner.py` (preflight) |
| 3 | Approval bound to a plan hash | `execute/base.py` (hash), `proposals.py`, `approve/` |
| 4 | Action level per policy, opt-in or opt-out | `analyze/policy.py`, `execute/base.py` (gate) |
| 5 | Co-change hygiene filters | `analyze/coupling.py`, `analyze/cohesion.py` |
| 6 | Verification by commit map and tree equality, provenance trailers | `execute/provenance.py`, merge, split, move_code |
| 7 | Dashboard issue with checkbox decisions | `report/orghealth.py` |
| 8 | Staged repo lifecycle | `analyze/lifecycle.py` |
| 9 | Config preview | `planconfig.py` |
| 10 | Ordered changesets | `changesets.py`, merge, split, move_code, autofix |
| 11 | Boundary rules from tags | `analyze/boundaries.py` |
| 12 | Org-wide grep and autofixers over mirrors | `fleet.py`, `execute/autofix.py` |

The ideas come from published designs of other projects; no code was copied. Credits are in the README.
