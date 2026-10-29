import base64
import datetime as dt
import json
from types import SimpleNamespace

import pytest
import jsonschema

from ion.appauth import AppGH, _http
from ion.config import SCHEMA
from ion.ghclient import GHError, make_gh
from ion.util import Refused


CONFIG = {"backend": "github_app", "client_id": "Iv1.example", "installation_id": 42, "private_key_path": "/not/read/test.pem"}


class API:
    def __init__(self, clock): self.clock = clock; self.requests = []; self.tokens = 0
    def __call__(self, method, path, token, body):
        self.requests.append((method, path, token, body))
        if path.endswith("/access_tokens"):
            self.tokens += 1
            expiry = dt.datetime.fromtimestamp(self.clock[0] + 3600, dt.timezone.utc).isoformat()
            return {"token": f"secret-{self.tokens}", "expires_at": expiry}, {}
        if path == "/app": return {"slug": "ion-app"}, {}
        if path == "/graphql": return {"data": {"ok": True}}, {}
        return {"ok": True}, {}


def setup_app():
    clock = [1000000]
    api = API(clock)
    signed = []
    app = AppGH(CONFIG, clock=lambda: clock[0], transport=api,
                signer=lambda payload, path: signed.append((payload, path)) or b"signature")
    return app, clock, api, signed


def test_jwt_claims_and_cached_installation_token_refresh():
    app, clock, api, signed = setup_app()
    assert app.rest("GET", "/repos/org/repo")["ok"]
    jwt = api.requests[0][2]
    header, claims, signature = jwt.split(".")
    decode = lambda part: json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    assert decode(header)["alg"] == "RS256"
    assert decode(claims) == {"iat": 999940, "exp": 1000540, "iss": CONFIG["client_id"]}
    assert api.requests[0][:2] == ("POST", "/app/installations/42/access_tokens")
    assert api.requests[1][2] == "secret-1"
    app.graphql("query { ok }")
    assert api.tokens == 1
    clock[0] += 3541
    app.rest("GET", "/repos/org/repo")
    assert api.tokens == 2 and api.requests[-1][2] == "secret-2"
    assert len(signed) == 2


def test_app_identity_and_pagination_boundaries():
    app, clock, api, _ = setup_app()
    assert app.token_login() == "ion-app[bot]"
    assert app.token_login() == "ion-app[bot]"
    assert sum(r[1] == "/app" for r in api.requests) == 1
    original = app._transport
    def pages(method, path, token, body):
        if path.endswith("/access_tokens"): return original(method, path, token, body)
        if path == "/items": return [1], {"Link": '<https://api.github.com/items?page=2>; rel="next"'}
        return [2], {}
    app._transport = pages
    assert app.paginate("/items") == [1, 2]
    app._transport = lambda *args: ([1], {"link": '<https://attacker.invalid/items>; rel="next"'})
    with pytest.raises(GHError, match="outside"): app.paginate("/items")
    with pytest.raises(GHError, match="relative"): _http("GET", "https://attacker.invalid", "secret")


def test_errors_are_sanitized_and_401_invalidates_without_replaying():
    app, clock, api, _ = setup_app()
    app.rest("GET", "/ok")
    calls = []
    def fail(*args): calls.append(args); raise GHError("secret-1 PRIVATE KEY jwt contents", status=401)
    app._transport = fail
    with pytest.raises(GHError) as error: app.rest("POST", "/repos/org/repo/issues", {"title": "x"})
    assert "secret" not in str(error.value) and "PRIVATE" not in str(error.value)
    assert len(calls) == 1 and app._token is None
    app._signer = lambda *args: (_ for _ in ()).throw(ValueError("private key contents"))
    with pytest.raises(GHError, match="signing failed") as error: app._jwt()
    assert "contents" not in str(error.value)


def test_backend_config_requires_app_fields():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"auth": {"backend": "github_app"}}, SCHEMA)
    jsonschema.validate({"auth": CONFIG}, SCHEMA)
    assert make_gh({"auth": CONFIG}).is_app
    assert not getattr(make_gh({}), "is_app", False)


def test_app_git_paths_fail_closed_before_any_git_or_api(ctx, monkeypatch):
    from ion.collect import run_collect
    from ion.execute.base import Executor
    ctx.gh = SimpleNamespace(is_app=True, supports_git=False)
    with pytest.raises(Refused, match="no-mirror"): run_collect(ctx)
    executor = Executor(ctx)
    executor.kind = "merge"
    with pytest.raises(Refused, match="transport"): executor.apply({})
    with pytest.raises(Refused, match="fallback"): executor.g(["push", "origin", "ion/test"])
    with pytest.raises(Refused, match="fallback"): executor.auth("https://github.com/org/repo")


def test_app_actor_cannot_approve_even_if_listed_as_maintainer(ctx):
    from ion.approve.labels import authorised
    ctx.gh = SimpleNamespace(is_app=True, token_login=lambda: "ion-app[bot]")
    assert not authorised(ctx, "ion-app[bot]", {"ion-app[bot]"})[0]
    assert authorised(ctx, "alice", {"alice"})[0]


def test_label_sync_skips_authenticated_app_events_and_keeps_human_decisions(ctx):
    from helpers import new_proposal
    from ion.approve.labels import sync_labels
    p = new_proposal(ctx, "archive", {"repo": "old-tool"})
    ctx.gh.is_app = True
    ctx.gh.token_login = lambda: "ion-app[bot]"
    ctx.cfg["maintainers"] = ["maint", "ion-app[bot]"]
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "ion-app[bot]")
    assert sync_labels(ctx, p)["recorded"] == 0
    assert not ctx.db.q("select * from approvals")
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "maint", ts="2099-01-02T00:00:00Z")
    assert sync_labels(ctx, p)["recorded"] == 1
    assert ctx.db.one("select actor from approvals")["actor"] == "maint"
