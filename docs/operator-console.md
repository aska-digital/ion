# Ion operator console

Ion helps a maintainer understand a GitHub organisation and review changes to its structure. The console puts saved repository findings, proposals and execution history in one place. It cannot approve or execute a change.

## Start with a tour

```sh
uv run ion console --demo --output ion-tour.html
```

Open that file in a browser. All tour records are synthetic. The command does not read deployment configuration, credentials or the state database.

## Inspect your organisation

```sh
uv run ion --config ~/.ion/ion.yml console --output ion-console-01.html
uv run ion --config ~/.ion/ion.yml console --json > ion-evidence.json
uv run ion runs --json --limit 20
```

`ION_HOME` selects the state directory. HTML and JSON use the same versioned evidence model. The console opens the database in a read transaction and does not migrate it. Missing state produces an empty view without creating a database. These commands make no GitHub, Bus, notifier or LLM calls and do not import autofix plugins.

The browser page has no external dependencies. Navigation, search, filters, proposal details and evidence downloads work from a local file. Repository text is displayed as text, not executed as markup. Links are restricted to GitHub repository, issue and pull request pages.

The file includes private repository names, findings and proposal text when those are present in saved state. Keep it within the same access boundary as the tracker. Known token formats, credential fields and URL credentials are scrubbed, but this is not a general private-data anonymizer.

Named exports are created with owner-only permissions and refuse existing paths, including symlinks. Shell redirection follows the shell's permissions instead.

## Read the evidence

| View | What it shows |
| --- | --- |
| Overview | Coverage, analysis provenance, pipeline freshness, pause state and the last breaker trip |
| Repositories | Lifecycle, visibility, policy findings and local mirror ref checks |
| Proposals | Saved approval evaluation, stored hash, regenerated phase plan, rollback notes, issue body, changesets and ledger |
| Runs | Each pipeline stage, timings, API usage, successful and failed writes, warnings and errors |

The completeness check requires a finished collection. A newer incomplete attempt remains visible alongside the older usable snapshot. If no complete collection exists, the console labels any saved partial data as incomplete.

Analysis must refer to the selected collection generation and the current configuration fingerprint. Older analyses without a fingerprint show unknown provenance. Run `ion analyze` to record a new fingerprint. Repository findings remain visible when evidence is old, with the mismatch shown in Overview.

Mirror checks compare readable loose or packed branch refs with the collected commit. They do not verify every Git object. Missing mirrors, mismatches, symbolic refs and unsupported reftable storage are visible as missing or unknown.

## Review a proposal

The console evaluates saved approval rows against the stored plan hash, configured maintainers, quorum and expiration. It does not refresh GitHub decisions. Proposal status and approval evaluation are separate: an `approved` database status can appear beside an expired recorded approval.

The phase plan is regenerated locally from the saved subject. Its fingerprint is compared with the stored approval hash. A mismatch is shown explicitly. Existing proposals may use placeholder IDs or earlier phases in their hashes. The console does not repair those hashes or imply that a regenerated plan has approval.

Autofix plans are not recomputed because that would load configured plugin code. Their saved issue body, subject and approved diff hashes remain inspectable. Changeset PR states come only from saved observations. An unobserved PR is unknown.

Follow the proposal's GitHub link to review its live state and use the existing verified approval channel. Evidence readiness describes the local data only. It is not an execution gate or permission to change repositories.

## Follow a run

`ion run` records collect, analyze, propose, approvals, execute and report separately from the collection history. Each stage starts before work begins and records its result afterward. A process killed before completion leaves a visibly running record. It is never inferred to have succeeded. Caught interruptions are recorded as interrupted.

A complete success requires every stage to succeed. Partial collection, mirror warnings, disabled mirror synchronization, failed API calls, failed writes and reporting errors prevent success. The record includes the collection generation and a configuration hash without configuration values.

GitHub counters measure CLI invocations. A paginated `gh api` call counts as one invocation because the client cannot observe each network request within it. GraphQL points come from responses. Writes are counted from broker records, including failures. Legacy runs have no invented metrics.

```yaml
report:
  org_health_issue: false
  stale_after_hours: 48
```

The last complete successful pipeline controls freshness. The first run shows `never` until it finishes successfully. A run exactly at the configured age is stale. A fresh prior success does not hide the latest failed or interrupted attempt.

Reports emitted during the pipeline show that the current outcome is pending. Their last-success time refers to a previously completed run because report delivery is itself a stage that can fail. Use `ion runs` or regenerate the offline console after completion for the final outcome.

Staleness is evaluated when a command runs. It cannot alert while the scheduler and every reporting command are stopped. Regenerate the HTML snapshot to refresh the evidence.

## Storage compatibility

Ion's default SQLite rollback-journal database works directly. The reader refuses WAL-mode databases, since SQLite can write shared-memory files even on a read-only connection. Use a consistent SQLite backup in rollback-journal mode for offline inspection of a WAL deployment. Never discard a WAL file or use an immutable reader to hide uncheckpointed changes.

Existing databases without pipeline tables remain readable and show no pipeline history. Normal pipeline commands add the new tables. Corrupt databases return a concise error. Malformed proposal records are flagged without hiding the rest of the proposals.

## Next acceptance work

The console makes the current engine inspectable. Production execution still needs the existing audit work on approval identity, plan reproducibility, crash recovery and protection preservation. The open repository issues track those changes. This interface does not widen the executor's authority or remove those requirements.
