"""One-off generator for the recorded GraphQL response fixtures (run manually; output is committed)."""
import json
from pathlib import Path


def commit(i, login="dev1", day=None):
    return {"oid": f"{i:040x}", "committedDate": day or f"2026-09-{(i % 27) + 1:02d}T10:00:00Z", "changedFilesIfAvailable": 3,
            "author": {"name": "Dev One", "email": "dev1@example.com", "user": {"login": login}}}


def repo(name, private=False, pushed="2026-10-10T10:00:00Z", fork=False, archived=False, rules=None, vuln=True, mimar_yml=None,
         desc="a repo"):
    return {
        "name": name, "url": f"https://github.com/acme/{name}", "description": desc, "isPrivate": private, "isFork": fork,
        "isArchived": archived, "isEmpty": False, "diskUsage": 100, "pushedAt": pushed, "createdAt": "2025-01-01T00:00:00Z",
        "hasVulnerabilityAlertsEnabled": vuln, "licenseInfo": {"spdxId": "MIT"},
        "defaultBranchRef": {"name": "main", "target": {"oid": "a" * 40, "history": {"nodes": [commit(i) for i in range(1, 6)]}}},
        "languages": {"edges": [{"size": 1000, "node": {"name": "Python"}}]},
        "repositoryTopics": {"nodes": []}, "issues": {"totalCount": 2}, "pullRequests": {"totalCount": 1},
        "oldestPR": {"nodes": [{"number": 3, "createdAt": "2026-09-01T00:00:00Z"}]},
        "vulnerabilityAlerts": {"totalCount": 0},
        "branchProtectionRules": {"nodes": rules or []},
        "collaborators": {"nodes": [{"login": "ahrazzle"}]},
        "mimarYml": {"text": mimar_yml} if mimar_yml else None, "parent": None,
    }


GOOD_RULE = {"pattern": "main", "requiresApprovingReviews": True, "requiredApprovingReviewCount": 0,
             "allowsForcePushes": False, "allowsDeletions": False, "isAdminEnforced": False}


def page(nodes, cost=1, has_next=False, cursor=None):
    return {"data": {"rateLimit": {"cost": cost, "remaining": 4990, "resetAt": "2026-10-13T12:00:00Z"},
                     "organization": {"repositories": {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}}}}


if __name__ == "__main__":
    out = Path(__file__).parent
    p1 = page([repo("alpha-core"), repo("alpha-cli", rules=[GOOD_RULE]), repo("mono", rules=[GOOD_RULE])], has_next=True, cursor="c1")
    p2 = page([repo("old-tool", pushed="2024-01-01T00:00:00Z", rules=[GOOD_RULE], vuln=False),
               repo("secret-sauce", private=True),
               repo("hermes-agent", fork=True)])
    (out / "graphql_org_page1.json").write_text(json.dumps(p1, indent=1))
    (out / "graphql_org_page2.json").write_text(json.dumps(p2, indent=1))
