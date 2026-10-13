# Third-party software

Mimar's own code is Apache-2.0 (Copyright 2026 ASKA Business Solutions and the Mimar contributors).

## A. Vendored code

**None.** No source code from any other project is included in Mimar. Design ideas from other projects are credited in the
"Prior art and credits" section of `README.md`; they are re-implementations in Mimar's own words and create no licence
obligation.

If code is ever vendored it must come from an MIT, BSD, Apache-2.0 or ISC project and be recorded here, one entry each:

```
<project> (<upstream URL>) @ <upstream commit SHA>
License: <SPDX>. <copyright line>
Files: <paths in this repository>
Changes: <what was modified>
<full licence text>
```

The upstream licence header stays at the top of each vendored file, with a line `Modified by Mimar contributors, 2026`
if it was changed. If the upstream project ships a NOTICE file, its text is added to `NOTICE`. Never vendor from AGPL,
GPL, source-available or proprietary projects.

## B. Python dependencies

Checked by `scripts/check_licenses.py` (allowlist: MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0; it also refuses `pygit2`).
Run it for the current, authoritative list; this table is a snapshot of the installed set.

| Package | Role | Required | Licence |
|---|---|---|---|
| PyYAML | read config files | yes | MIT |
| jsonschema | validate config, typed write requests and LLM output | yes | MIT |
| attrs, referencing, rpds-py, jsonschema-specifications | transitive via jsonschema | yes | MIT |
| networkx | Louvain clustering for split analysis | optional (`mimar[graph]`) | BSD-3-Clause |
| pytest | tests | dev only | MIT |
| pluggy, iniconfig | transitive via pytest | dev only | MIT |
| packaging | transitive via pytest | dev only | Apache-2.0 OR BSD-2-Clause |
| Pygments | transitive via pytest | dev only | BSD-2-Clause |
| hatchling | build backend | build only | MIT |

## C. External programs (called as separate processes, not linked or bundled)

| Program | Licence | Used for |
|---|---|---|
| git (with `git subtree`) | GPL-2.0 | mirrors, merges, pushes |
| GitHub CLI `gh` | MIT | API access and the token |
| git-filter-repo | MIT (dual-licensed) | splits and moves; its commit map is read for verification |
| Claude Code CLI | Anthropic terms | optional LLM backend |

Running a separate program does not bring its licence into Mimar.
