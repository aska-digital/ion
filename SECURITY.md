# Security policy

## Reporting a vulnerability

Please report security problems **privately** through GitHub private vulnerability reporting: <https://github.com/aska-digital/ion/security/advisories/new>. Do not open a public issue or pull request for
a vulnerability.

Include what you found, how to reproduce it, and the version or commit. You should get an acknowledgement within a few
days. We will agree a disclosure date with you once a fix is ready, and credit you if you wish.

## What counts

Ion can change repositories and settings in a GitHub organisation, so the following are in scope:

- bypassing the approval gate (running a structural change without a verified, in-date, maintainer approval for the
  current plan hash);
- making a non-maintainer's label, bus message or dashboard edit count as an approval;
- getting Ion to write outside its typed write requests, exceed a per-run cap, push to a default branch, force-push or
  delete anything;
- defeating the circuit breaker or the kill switch;
- leaking a token or private repository data (for example through logs, issue bodies or git command lines);
- prompt injection through repo names, descriptions or file contents that changes what Ion does.

## Notes for operators

- Keep the tracker repository (`tracker_repo`) **private**. Proposal bodies name private repositories.
- Ion's token is whatever `gh auth token` returns. Give it only the scopes it needs, and prefer a dedicated machine
  user or, later, a GitHub App.
- `ion pause` (or `paused: true`) stops all writes.
