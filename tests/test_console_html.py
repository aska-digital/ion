"""Console output is portable and treats repository content as inert evidence."""
import base64
import hashlib
import json
import re
from html.parser import HTMLParser

from ion.console_html import render_console


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_console_self_contained_and_semantic():
    source = render_console({})
    page = Page(source)
    assert not any(attrs.get('src') for _, attrs in page.tags)
    assert not any(tag == 'link' for tag, _ in page.tags)
    assert any(tag == 'nav' and attrs.get('aria-label') for tag, attrs in page.tags)
    assert any(tag == 'main' and attrs.get('id') == 'main' for tag, attrs in page.tags)
    assert {attrs['data-view'] for _, attrs in page.tags if 'data-view' in attrs} == {'overview', 'repositories', 'proposals', 'runs'}
    assert 'Offline snapshot' in source
    assert 'No matching records' in source
    assert 'No readiness evidence' in source
    assert 'innerHTML' not in source
    assert 'fetch(' not in source
    assert 'XMLHttpRequest' not in source


def test_hostile_content_is_losslessly_encoded_without_script_breakout():
    attack = '</script><img src=x onerror=alert(1)>\u2028\u2029&'
    data = {'org': attack, 'proposals': [{'evidence': {'text': attack}, 'issue_url': 'javascript:alert(1)'}]}
    source = render_console(data)
    assert attack not in source
    assert '<img src=x' not in source
    payload = re.search(r'<script type="application/json" id="ion-data">(.*?)</script>', source, re.S).group(1)
    assert json.loads(payload) == data
    assert '\\u003c/script\\u003e' in payload
    assert '\\u2028' in payload and '\\u2029' in payload
    assert source.count('</script>') == 2
    assert 'textContent' in source


def test_csp_authorizes_only_exact_bundled_script_and_style():
    source = render_console({'demo': True})
    page = Page(source)
    csp = next(attrs['content'] for tag, attrs in page.tags if tag == 'meta' and attrs.get('http-equiv') == 'Content-Security-Policy')
    script = re.search(r'<script>(.*?)</script>', source, re.S).group(1)
    style = re.search(r'<style>(.*?)</style>', source, re.S).group(1)
    for value in (script, style):
        digest = base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
        assert 'sha256-' + digest in csp
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp
    assert 'unsafe-inline' not in csp
    assert 'DEMONSTRATION' in source
    assert 'github\\.com' in script
    assert 'link unavailable; see raw evidence' in script


def test_console_qualifies_saved_authority_and_pipeline_metrics():
    source = render_console({})
    assert 'Last complete successful pipeline:' in source
    assert 'Last successful collection:' not in source
    assert 'Recorded approval evaluation' in source
    assert 'Verified approval' not in source
    assert 'Regenerated current phase plan' in source
    assert 'Plan hash mismatch:' in source
    assert 'Generated fingerprint' in source
    assert 'Saved tracking issue body (plain text)' in source
    assert 'p.recorded_body' in source
    assert 'metricSummary(r.metrics)' in source
    assert 'metricSummary(s.metrics)' in source
    assert 'Executor dry run:' in source
    assert 'may still publish to GitHub' in source
