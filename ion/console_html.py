"""Self-contained, read-only operator console. Rendering never contacts a service."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path


def render_console(data: dict) -> str:
    assets = Path(__file__).with_name('assets')
    css = (assets / 'console.css').read_text()
    js = (assets / 'console.js').read_text()
    payload = json.dumps(data, ensure_ascii=False, default=str).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    def digest(value):
        return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
    policy = f"default-src 'none'; script-src 'sha256-{digest(js)}'; style-src 'sha256-{digest(css)}'; img-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{policy}"><title>Ion · Organisation stewardship</title><style>{css}</style></head>
<body><a class="skip" href="#main">Skip to content</a><aside class="rail"><div class="brand"><span class="mark">i</span> ion<span class="version">01 / STEWARD</span></div><p class="rail-caption">A clearer view.<br>A human decision.</p><nav aria-label="Console sections"><button data-view="overview" aria-current="page">01 <span>Overview</span></button><button data-view="repositories">02 <span>Repositories</span></button><button data-view="proposals">03 <span>Proposals</span></button><button data-view="runs">04 <span>Runs</span></button></nav><div class="rail-bottom"><span class="signal"></span> LOCAL SNAPSHOT<p>No live connection.<br>Existing approval channels only.</p></div></aside>
<main id="main" tabindex="-1"><header><div><p class="eyebrow">ION / ORGANISATION STEWARDSHIP</p><h1 id="org">Organisation</h1><p id="generated" class="muted"></p></div><button id="download" class="download">Download evidence ↓</button></header><div id="demo" class="demo" hidden>DEMONSTRATION · Synthetic example data. No organisation has been changed.</div><div class="snapshot"><strong>Offline snapshot</strong><span>This page reads saved evidence. It cannot approve or execute changes.</span></div><section id="view" aria-live="polite"></section><footer>ION / KEEP THE EVIDENCE. KEEP THE DECISION.<span>Generated locally · No external resources</span></footer></main>
<script type="application/json" id="ion-data">{payload}</script><script>{js}</script></body></html>'''
