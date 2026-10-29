# Actions startup recovery

Collect evidence without changing repository settings or starting another run:

```sh
python3 scripts/diagnose_actions.py aska-digital/ion RUN_ID
```

The output binds the result to a commit and attempt, paginates every job, and rejects an inventory that changes while it is read. `blocked_before_jobs` means no test or deploy job ran. It is not a passing CI result and does not identify the cause.

For issue #73, CI run 40374214129 at `32f28584783b91e3a6da251f2731c46f74e68fa0` completed with `startup_failure` and zero jobs on the 2026-10-29 inspection. The check suite also had zero check runs. Local tests cannot demonstrate recovery of GitHub Actions or Pages.

A repository or organisation owner must inspect the run page and Actions policy/billing settings for the actual refusal. Record that evidence before changing a setting. After remediation, dispatch or re-run CI on the intended commit and verify that the test job and licence check ran successfully. Verify Pages separately: successful build and deploy jobs plus the served build metadata matching the deployed commit. Leave #73 open until both are verified.

## Observed policy blocker

The repository permissions endpoint returned `enabled: true`, `allowed_actions: local_only`, `sha_pinning_required: false` on 2026-10-29. Both committed workflows use actions outside aska-digital, including `actions/checkout`, and CI also uses `astral-sh/setup-uv`. GitHub documents that the organisation-only setting blocks even GitHub-authored actions. This is a concrete policy/workflow incompatibility; it does not rule out other causes for historical failed runs.

Organisation policy could not be inspected with this session's scope (HTTP 403, requires `admin:org` or the Actions policy permission). Do not expand token scopes or relax an intentional organisation restriction as a diagnostic step.

Recommended owner decision: if external CI dependencies are intended, allow only the specific actions required by the committed workflows (checkout, setup-uv, configure-pages, upload-pages-artifact, deploy-pages), then verify CI and Pages. Prefer reviewed commit SHA pins alongside the allowlist. Otherwise keep the policy and replace the workflows with an approved internal implementation. This PR changes no live policy.

Reference: [GitHub repository Actions settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).
