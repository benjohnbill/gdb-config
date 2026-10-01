#!/usr/bin/env bash
# Rebuild ../NAME.png from NAME.svg at the SVG's own width x height (1:1).
# Zed's Markdown preview draws PNG but not SVG, so the PNG is what README.md links.
set -eu
cd "$(dirname "$0")"
for svg in *.svg; do
  w=$(grep -o '<svg[^>]*' "$svg" | grep -o ' width="[0-9]*"' | grep -o '[0-9]*')
  h=$(grep -o '<svg[^>]*' "$svg" | grep -o ' height="[0-9]*"' | grep -o '[0-9]*')
  google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=1 --default-background-color=00000000 \
    --window-size="$w,$h" --screenshot="$PWD/../${svg%.svg}.png" "file://$PWD/$svg" >/dev/null 2>&1
  echo "${svg} -> ../${svg%.svg}.png (${w}x${h})"
done
