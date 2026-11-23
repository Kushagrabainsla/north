#!/usr/bin/env bash
# Assemble north's site into _site/ (or the folder given): the landing page from site/, the
# guides from guides/ (an Astro Starlight project), and the icons and font taken from where
# north keeps them, so neither is copied by hand.
#
#     bash scripts/build_site.sh            # then open _site/index.html
#
# The guides need Node 22.23.4 or later and npm.
# .github/workflows/pages.yml runs this and publishes the result to GitHub Pages.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/_site}"
export ASTRO_TELEMETRY_DISABLED=1  # send nothing to Astro

npm --prefix "$root/guides" ci --no-audit --no-fund
npm --prefix "$root/guides" run build

rm -rf "$out"
mkdir -p "$out/assets"
cp -R "$root/site/." "$out/"
# The guides live under guides/; their build also adds the search index, the sitemap and
# 404.html at the root. None of these files has the same name as a landing page file.
cp -R "$root/guides/dist/." "$out/"
cp "$root/web/public/favicon.svg" "$root/web/public/favicon.ico" "$root/web/public/apple-touch-icon.png" \
   "$root/web/public/icon-512.png" "$out/"
cp "$root/web/src/fonts/jetbrains-mono-normal.woff2" "$out/assets/"
touch "$out/.nojekyll"  # serve the files as they are, _astro/ too
echo "built $out"
