# Third-party dependencies

Mimar's own code is Apache-2.0. It depends on the packages below; none are vendored.
The `git`, `gh` and `git-filter-repo` tools are invoked as external programs, not linked.

| Package | Role | Required | Licence |
|---|---|---|---|
| PyYAML | read config files | yes | MIT |
| jsonschema | validate config and LLM output | yes | MIT |
| networkx | Louvain clustering for split analysis | optional (`mimar[graph]`) | BSD-3-Clause |
| pytest | tests | dev only | MIT |
| hatchling | build backend | build only | MIT |
| attrs, referencing, rpds-py, jsonschema-specifications | transitive via jsonschema | yes | MIT |

External programs (not bundled): git (GPL-2.0), GitHub CLI `gh` (MIT), git-filter-repo (MIT/GPL-2.0 dual), Claude Code CLI (optional LLM backend, Anthropic terms).
