#!/usr/bin/env bash
# Assemble the GitHub Pages artifact from site/.
# Used by .github/workflows/pages.yml and for local testing:
#   bash site/build.sh [output-dir]      (default: _site in the repository root)
#   python3 -m http.server -d _site 8000
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/_site}"
case "$OUT" in
  ""|"/"|"$ROOT"|"$HOME") echo "refusing to use '$OUT' as the output directory" >&2; exit 1 ;;
esac

rm -rf "$OUT"
mkdir -p "$OUT/data"

# Everything in site/ except this script and og.html (the page og.png is
# rendered from; see the comment at the top of og.html).
for f in "$ROOT"/site/*; do
  case "$(basename "$f")" in build.sh|og.html) continue ;; esac
  cp -R "$f" "$OUT/"
done

# Build stamp shown in the page footer.
COMMIT="${GITHUB_SHA:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)}"
printf '{"commit":"%s","built":"%s"}\n' "$COMMIT" "$(date -u +%Y-%m-%d)" > "$OUT/data/build.json"

touch "$OUT/.nojekyll"
echo "Site assembled in $OUT"
