"""Notifier plugins: stdout, file, divan_bus, github_issue, webhook. Failures are queued and retried."""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

from . import tracker
from .util import iso, redact

log = logging.getLogger("mimar")


class Notifier:
    type = ""

    def __init__(self, ctx, conf: dict):
        self.ctx, self.conf = ctx, conf

    def send(self, text: str) -> None:
        raise NotImplementedError


class StdoutNotifier(Notifier):
    type = "stdout"

    def send(self, text):
        print(text, file=sys.stdout)


class FileNotifier(Notifier):
    type = "file"

    def send(self, text):
        path = Path(os.path.expanduser(self.conf.get("path") or "~/.mimar/digest.log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"--- {iso()}\n{text}\n")


class DivanBusNotifier(Notifier):
    type = "divan_bus"

    def send(self, text):
        bus = self.ctx.cfg["bus"]
        client = self.conf.get("client") or bus.get("client")
        if not client:
            raise RuntimeError("divan_bus notifier needs bus.client")
        py = os.path.expanduser(self.conf.get("python") or bus.get("python") or sys.executable)
        to = self.conf.get("to") or bus.get("notify")
        if not to:
            raise RuntimeError("divan_bus notifier needs `to` or bus.notify")
        rc, out, err = self.ctx.bus_runner([py, os.path.expanduser(client), "post", "--to", to, text],
                                           {"DIVAN_URL": bus["url"], "DIVAN_SEAT": bus["seat"]}, None)
        if rc != 0:
            raise RuntimeError(f"bus post failed: {redact(err.strip())[:200]}")


class GithubIssueNotifier(Notifier):
    type = "github_issue"

    def send(self, text):
        n = self.conf.get("number")
        if not n:
            raise RuntimeError("github_issue notifier needs `number`")
        self.ctx.writes.tracker("edit_issue_body", number=int(n), body=text)


class WebhookNotifier(Notifier):
    type = "webhook"

    def send(self, text):
        url = self.conf.get("url") or os.environ.get(self.conf.get("url_env", ""), "")
        if not url:
            raise RuntimeError("webhook url not set")
        body = json.dumps({"text": text, "content": text[:1900]}).encode()   # Slack uses text, Discord uses content
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status >= 300:
                raise RuntimeError(f"webhook returned {r.status}")


REGISTRY = {c.type: c for c in (StdoutNotifier, FileNotifier, DivanBusNotifier, GithubIssueNotifier, WebhookNotifier)}


def make(ctx, conf: dict) -> Notifier:
    return REGISTRY[conf["type"]](ctx, conf)


def notify(ctx, text: str) -> list[str]:
    """Send to every configured notifier. Failed deliveries go to the outbox. Returns error strings."""
    errors = flush_outbox(ctx)
    for conf in ctx.cfg["notifiers"]:
        try:
            make(ctx, conf).send(text)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{conf['type']}: {redact(str(e))[:160]}")
            ctx.db.x("insert into outbox(ts,to_seat,body) values(?,?,?)", (iso(), json.dumps(conf), text))
    return errors


def flush_outbox(ctx) -> list[str]:
    errors = []
    for r in ctx.db.q("select * from outbox where sent=0 order by id limit 50"):
        try:
            make(ctx, json.loads(r["to_seat"])).send(r["body"])
            ctx.db.x("update outbox set sent=1 where id=?", (r["id"],))
        except Exception as e:  # noqa: BLE001
            errors.append(f"retry {json.loads(r['to_seat'])['type']}: {redact(str(e))[:120]}")
    return errors
