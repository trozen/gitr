#!/bin/sh
# Rebuild gitr_web/static/monaco/ after changing the monaco-editor version in
# package.json: npm install && npm run build. The output is committed, so
# installing or running gitr-serve needs neither Node nor a network.
set -e
cd "$(dirname "$0")"
out=../gitr_web/static/monaco
rm -rf "$out"
npx esbuild entry.js --bundle --minify --format=iife --outfile="$out/editor.js" \
    --loader:.ttf=file --asset-names='[name]' --legal-comments=none
npx esbuild node_modules/monaco-editor/esm/vs/editor/editor.worker.js --bundle --minify \
    --format=iife --outfile="$out/editor.worker.js" --legal-comments=none
cp node_modules/monaco-editor/LICENSE "$out/LICENSE"
ls -l "$out"
