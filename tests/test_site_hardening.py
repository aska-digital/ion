"""Issue #72 (first half): the 404 page must work at any nested path.

GitHub Pages serves 404.html at the requested path, so relative links
resolve under that path and break. Local links are root-relative."""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_404_links_are_root_relative():
    html = (ROOT / "site" / "404.html").read_text()
    hrefs = re.findall(r'href="([^"]+)"', html)
    local = [h for h in hrefs if not h.startswith(("https://", "#"))]
    assert local, "expected local links on the 404 page"
    assert all(h.startswith("/ion/") for h in local), local
