#!/usr/bin/env bash
# Assemble north's landing page into _site/ (or the folder given): the page from site/, with
# the icons and font taken from where north keeps them, so neither is copied by hand.
#
#     bash scripts/build_site.sh            # then open _site/index.html
#
# .github/workflows/pages.yml runs this and publishes the result to GitHub Pages.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/_site}"

rm -rf "$out"
mkdir -p "$out/assets"
cp -R "$root/site/." "$out/"
cp "$root/web/public/favicon.svg" "$root/web/public/favicon.ico" "$root/web/public/apple-touch-icon.png" \
   "$root/web/public/icon-512.png" "$out/"
cp "$root/web/src/fonts/jetbrains-mono-normal.woff2" "$out/assets/"
touch "$out/.nojekyll"  # serve the files as they are
echo "built $out"
