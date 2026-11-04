'use strict';
// gitr web prototype: one Monaco document holds the whole diff; per-line
// metadata comes from the server alongside the text.

const MONACO = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min';
// Monaco's workers must come from our origin; a data: URL that imports the
// CDN script is the documented workaround for loading Monaco from a CDN.
window.MonacoEnvironment = {
  getWorkerUrl: () => 'data:text/javascript;charset=utf-8,' + encodeURIComponent(
    `self.MonacoEnvironment={baseUrl:'${MONACO}/'};importScripts('${MONACO}/vs/base/worker/workerMain.js');`),
};

const DECO_MARGIN = 3000;      // lines decorated above and below the viewport
const WORD_WINDOW = 4000;      // lines per word-diff request
const POLL_MS = 3000;
// File and hunk headers share one background with Monaco's sticky header,
// so a header looks the same pinned at the top and in place.
const HEADER_BG = '#433d59';
const FILE_MINIMAP = '#bd93f9';
// File bands show through Monaco's 70% label box; hunk rows have no label,
// so they get 30% alpha to come out equally strong.
const HUNK_MINIMAP = '#bd93f94d';
// The label box is 1.5 x the 9px label font tall, at 2px per minimap row.
const MINIMAP_LABEL_ROWS = 6;

const $ = (id) => document.getElementById(id);
const timing = (window.__gitrTiming = {});

let monaco, editor, decos, wordDecos, fileDecos;
let doc = null;                // current diff payload
let fileStarts = [];           // sorted 1-based start line per file
let fileRows = [];             // DOM rows parallel to doc.files
let activeFile = -1;
let pinned = null;             // {file, scrollTop} after a click, see onScroll
let decoWindow = [0, 0];
let wordsByLine = new Map();   // line -> [startCol, endCol, ...]
let wordGen = 0;               // bumps on every load so stale fetches are dropped
let current = null;            // {wt, base, untracked} last requested
let shown = null;              // {wt, base, untracked} of the diff on screen, null while loading
let numWidth = 4;

require.config({ paths: { vs: MONACO + '/vs' } });
require(['vs/editor/editor.main'], (m) => { monaco = m; init(); });

// --setup ----------------------------------------------------------------

function init() {
  const lineState = { clone() { return this; }, equals() { return true; } };
  monaco.languages.register({ id: 'gitrdiff' });
  // Stateless and one token per line: Monaco can tokenize any line in
  // isolation, so jumping deep into a big document needs no warm-up.
  monaco.languages.setTokensProvider('gitrdiff', {
    getInitialState: () => lineState,
    tokenize(line) {
      const c = line[0];
      const scope = c === '+' ? 'inserted' : c === '-' ? 'deleted' : c === ' ' ? 'context'
        : c === '@' ? 'hunk' : c === '\\' ? 'meta'
        : /^[AMDRCT] /.test(line) ? 'fileheader' : 'meta';
      return { tokens: [{ startIndex: 0, scopes: scope }], endState: lineState };
    },
  });
  monaco.languages.registerFoldingRangeProvider('gitrdiff', { provideFoldingRanges: foldingRanges });
  monaco.editor.defineTheme('gitr', {
    base: 'vs-dark', inherit: true,
    rules: [
      { token: 'inserted', foreground: '50fa7b' },
      { token: 'deleted', foreground: 'ff5555' },
      { token: 'hunk', foreground: 'ffb86c' },
      { token: 'fileheader', foreground: 'bd93f9', fontStyle: 'bold' },
      { token: 'context', foreground: 'f8f8f2' },
      { token: 'meta', foreground: '6272a4' },
    ],
    colors: {
      'editor.background': '#282a36', 'editor.foreground': '#f8f8f2',
      'editorLineNumber.foreground': '#6272a4', 'editorLineNumber.activeForeground': '#f8f8f2',
      'editor.lineHighlightBackground': '#ffffff08',
      'editor.selectionBackground': '#5a5f8a', 'editor.inactiveSelectionBackground': '#4d5175',
      'minimapSlider.background': '#6272a450', 'minimapSlider.hoverBackground': '#6272a470',
      'minimapSlider.activeBackground': '#6272a490',
      'minimap.background': '#21222c', 'editorStickyScroll.background': HEADER_BG,
      'editorStickyScrollHover.background': HEADER_BG,
    },
  });
  editor = monaco.editor.create($('editor'), {
    value: '', language: 'gitrdiff', theme: 'gitr', readOnly: true, domReadOnly: true,
    // Big agent diffs are exactly the case here; without this Monaco turns
    // tokenization (our colours) off above 20 MB / 300k lines.
    largeFileOptimizations: false,
    automaticLayout: true, fontSize: 13, lineHeight: 19, scrollBeyondLastLine: true,
    minimap: { enabled: true, renderCharacters: true, size: 'proportional', scale: 1, maxColumn: 120,
               showSlider: 'always' },
    lineNumbers: lineNumber, lineNumbersMinChars: 9, lineDecorationsWidth: 6,
    glyphMargin: false, folding: true, showFoldingControls: 'mouseover',
    foldingMaximumRegions: 200000,
    stickyScroll: { enabled: true, maxLineCount: 2, defaultModel: 'foldingProviderModel' },
    renderLineHighlight: 'line', occurrencesHighlight: 'off', selectionHighlight: false,
    renderWhitespace: 'none', guides: { indentation: false },
    wordWrap: loadPref('wrap', true) ? 'on' : 'off', wrappingIndent: 'none',
    wrappingStrategy: 'simple',  // monospace arithmetic, no DOM measuring
    smoothScrolling: false, mouseWheelScrollSensitivity: 1, contextmenu: false,
    unicodeHighlight: { ambiguousCharacters: false, invisibleCharacters: false },
  });
  decos = editor.createDecorationsCollection();
  wordDecos = editor.createDecorationsCollection();
  fileDecos = editor.createDecorationsCollection();
  editor.onDidScrollChange(onScroll);

  $('base').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && current) load(current.wt, $('base').value.trim() || current.defaultBase);
  });
  $('reload').addEventListener('click', () => current && load(current.wt, current.base));
  $('untracked').addEventListener('change', () => current && load(current.wt, current.base));
  $('wrap').checked = loadPref('wrap', true);
  $('wrap').addEventListener('change', () => {
    savePref('wrap', $('wrap').checked);
    editor.updateOptions({ wordWrap: $('wrap').checked ? 'on' : 'off' });
  });
  initSash();
  loadProjects();
  setInterval(pollChanges, POLL_MS);
}

function initSash() {
  const sash = $('sash'), side = $('sidebar');
  sash.addEventListener('mousedown', (e) => {
    e.preventDefault();
    sash.classList.add('drag');
    const move = (ev) => { side.style.width = Math.max(180, ev.clientX) + 'px'; };
    const up = () => { sash.classList.remove('drag');
      removeEventListener('mousemove', move); removeEventListener('mouseup', up); };
    addEventListener('mousemove', move);
    addEventListener('mouseup', up);
  });
}

async function api(path, params) {
  const r = await fetch(path + '?' + new URLSearchParams(params));
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

// --projects -------------------------------------------------------------

async function loadProjects() {
  const projects = await api('/api/projects', {});
  const box = $('projects');
  box.textContent = '';
  const hash = new URLSearchParams(location.hash.slice(1));
  let initial = null;
  for (const p of projects) {
    box.append(el('div', 'row project', [el('span', 'grow', p.name)]));
    if (p.error) box.append(el('div', 'err', p.error));
    for (const w of p.worktrees) {
      const label = w.branch || (w.head ? w.head + ' (detached)' : '?');
      const rel = w.path === p.path ? '' : w.path.split('/').pop();
      const row = el('div', 'row wt', [el('span', 'grow branch', label), el('span', 'dim', rel)]);
      row.title = w.path;
      row.dataset.wt = w.path;
      row.addEventListener('click', () => load(w.path, p.defaultBase, p.defaultBase));
      box.append(row);
      if (hash.get('wt') === w.path) initial = [w.path, hash.get('base') || p.defaultBase, p.defaultBase];
    }
  }
  $('untracked').checked = hash.get('untracked') === '1';
  if (initial) load(...initial);
}

// --loading a diff -------------------------------------------------------

async function load(wt, base, defaultBase) {
  const gen = ++wordGen;
  const untracked = $('untracked').checked ? '1' : '0';
  current = { wt, base, untracked, defaultBase: defaultBase || current?.defaultBase || base };
  location.hash = new URLSearchParams({ wt, base, untracked }).toString();
  document.querySelectorAll('.wt').forEach((r) => r.classList.toggle('active', r.dataset.wt === wt));
  $('base').value = base;
  $('crumb').textContent = wt;
  $('changed').hidden = true;
  shown = null;
  showProgress(`loading diff against ${base} ...`);
  const t0 = performance.now();
  let d;
  try {
    d = await api('/api/diff', { wt, base, untracked });
  } catch (e) {
    if (gen === wordGen) showProgress('error: ' + e.message);
    return;
  }
  if (gen !== wordGen) return;
  const t1 = performance.now();
  showProgress(`rendering ${d.files.length} files, ${d.kinds.length} lines ...`);
  await nextFrame();
  doc = d;
  shown = { wt, base, untracked };
  fileStarts = d.files.map((f) => f.start);
  let maxNo = 1;
  for (let i = 0; i < d.oldNo.length; i++) maxNo = Math.max(maxNo, d.oldNo[i], d.newNo[i]);
  numWidth = String(maxNo).length;
  wordsByLine = new Map();
  decoWindow = [0, 0];
  activeFile = -1;
  pinned = null;
  editor.updateOptions({ lineNumbersMinChars: numWidth * 2 + 2 });
  editor.getModel().setValue(d.text);
  editor.setScrollTop(0);
  renderFiles();
  markHeadersInMinimap();
  updateDecorations(true);
  if (d.files.length) hideProgress();
  else showProgress(`no changes against ${base} (merge-base ${d.mergeBase})`);
  await nextFrame();
  const t2 = performance.now();
  Object.assign(timing, { fetchMs: Math.round(t1 - t0), renderMs: Math.round(t2 - t1),
                          serverGit: d.timing.git_ms, serverDoc: d.timing.doc_ms,
                          lines: d.kinds.length, files: d.files.length,
                          bytes: d.text.length });
  $('mergebase').textContent = 'merge-base ' + d.mergeBase;
  const add = d.files.reduce((s, f) => s + f.add, 0), del = d.files.reduce((s, f) => s + f.del, 0);
  $('stats').textContent = `${d.files.length} files  +${add} -${del}  ${d.kinds.length} lines`;
  $('timing').textContent = `git ${d.timing.git_ms} ms, parse ${d.timing.doc_ms} ms, ` +
    `fetch ${timing.fetchMs} ms, render ${timing.renderMs} ms`;
  onScroll();
  fetchWords(gen);
}

function showProgress(msg) { const p = $('progress'); p.textContent = msg; p.hidden = false; }
function hideProgress() { $('progress').hidden = true; }
const nextFrame = () => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));

// --gutter and folding ---------------------------------------------------

function lineNumber(n) {
  if (!doc) return '';
  const k = doc.kinds[n - 1];
  const o = doc.oldNo[n - 1], w = doc.newNo[n - 1];
  if (k === 'c') return String(o).padStart(numWidth) + ' ' + String(w).padStart(numWidth);
  if (k === 'r') return String(o).padStart(numWidth) + ' '.repeat(numWidth + 1);
  if (k === 'a') return ' '.repeat(numWidth + 1) + String(w).padStart(numWidth);
  return '';
}

// Files and hunks as folding regions: Monaco's sticky scroll then pins the
// current file header and hunk header at the top, and files fold for free.
function foldingRanges() {
  if (!doc) return [];
  const out = [];
  for (const f of doc.files) {
    if (f.end > f.start) out.push({ start: f.start, end: f.end });
    let hunk = 0;
    for (let ln = f.start + 1; ln <= f.end; ln++) {
      if (doc.kinds[ln - 1] !== 'h') continue;
      if (hunk && ln - 1 > hunk) out.push({ start: hunk, end: ln - 1 });
      hunk = ln;
    }
    if (hunk && f.end > hunk) out.push({ start: hunk, end: f.end });
  }
  return out;
}

// --decorations ----------------------------------------------------------

const LINE_CLASS = { a: 'l-add', r: 'l-rem', f: 'l-file', h: 'l-head' };
const MARGIN_CLASS = { f: 'l-file', h: 'l-head' };

// Unlike the line backgrounds these cover the whole document (one per
// file), since the minimap shows all of it.
function markHeadersInMinimap() {
  const list = [];
  for (const f of doc.files) {
    // Monaco draws a section label on a box of 70% minimap background and
    // offers no option to change it; colouring the rows underneath tints
    // that box, which keeps the label and still marks the file.
    // One decoration per row: Monaco 0.52 paints the full-width row of a
    // multi-line minimap decoration only for its first line.
    const last = Math.min(f.start + MINIMAP_LABEL_ROWS - 1, f.end);
    for (let ln = f.start; ln <= last; ln++)
      list.push({ range: new monaco.Range(ln, 1, ln, 1),
                  options: { minimap: { color: FILE_MINIMAP, position: monaco.editor.MinimapPosition.Inline } } });
    list.push({ range: new monaco.Range(f.start, 1, f.start, 1),
                options: { minimap: { position: monaco.editor.MinimapPosition.Inline,
                                      sectionHeaderStyle: monaco.editor.MinimapSectionHeaderStyle.Normal,
                                      sectionHeaderText: f.path.slice(f.path.lastIndexOf('/') + 1) } } });
  }
  // Hunk headers right below a file header already sit inside its band.
  for (let ln = 1; ln <= doc.kinds.length; ln++) {
    if (doc.kinds[ln - 1] !== 'h' || doc.kinds[ln - 2] === 'f') continue;
    list.push({ range: new monaco.Range(ln, 1, ln, 1),
                options: { minimap: { color: HUNK_MINIMAP, position: monaco.editor.MinimapPosition.Inline } } });
  }
  fileDecos.set(list);
}

// Only the region around the viewport carries decorations, with runs of
// same-kind lines merged into one whole-line decoration, so the cost does
// not grow with the size of the diff.
function updateDecorations(force) {
  if (!doc) return;
  const vr = editor.getVisibleRanges();
  if (!vr.length && !force) return;
  const top = vr.length ? vr[0].startLineNumber : 1;
  const bottom = vr.length ? vr[vr.length - 1].endLineNumber : 1;
  const [a, b] = decoWindow;
  const n = doc.kinds.length;
  // A window clamped at the start or end of the document has no margin to
  // grow on that side, so it counts as enough.
  if (!force && (a === 1 || top - a > DECO_MARGIN / 3) && (b === n || b - bottom > DECO_MARGIN / 3)) return;
  const from = Math.max(1, top - DECO_MARGIN), to = Math.min(n, bottom + DECO_MARGIN);
  decoWindow = [from, to];
  const list = [];
  let runStart = from, runKind = doc.kinds[from - 1];
  const flush = (end) => {
    const cls = LINE_CLASS[runKind];
    if (cls) list.push({ range: new monaco.Range(runStart, 1, end, 1),
                         options: { isWholeLine: true, className: cls,
                                    marginClassName: MARGIN_CLASS[runKind] } });
  };
  for (let ln = from + 1; ln <= to; ln++) {
    const k = doc.kinds[ln - 1];
    // Each file header is its own run: its top border separates files, and
    // an empty file's header is directly followed by the next one.
    if (k !== runKind || k === 'f') { flush(ln - 1); runStart = ln; runKind = k; }
  }
  flush(to);
  decos.set(list);
  updateWordDecorations();
}

function updateWordDecorations() {
  const [from, to] = decoWindow;
  const list = [];
  for (let ln = from; ln <= to; ln++) {
    const r = wordsByLine.get(ln);
    if (!r) continue;
    const cls = doc.kinds[ln - 1] === 'a' ? 'w-add' : 'w-rem';
    for (let i = 0; i < r.length; i += 2)
      list.push({ range: new monaco.Range(ln, r[i], ln, r[i + 1]), options: { inlineClassName: cls } });
  }
  wordDecos.set(list);
}

// Word-diff ranges arrive in windows, starting at the viewport and growing
// outward, the same order gitr applies them in.
async function fetchWords(gen) {
  const n = doc.kinds.length;
  const top = editor.getVisibleRanges()[0]?.startLineNumber || 1;
  const first = Math.max(1, top - (top - 1) % WORD_WINDOW);
  const windows = [];
  for (let up = first - WORD_WINDOW, down = first; up >= 1 || down <= n; up -= WORD_WINDOW, down += WORD_WINDOW) {
    if (down <= n) windows.push(down);
    if (up >= 1) windows.push(up);
  }
  const t0 = performance.now();
  for (const w of windows) {
    let flat;
    try {
      flat = await api('/api/words', { wt: current.wt, id: doc.id, from: w, to: w + WORD_WINDOW - 1 });
    } catch { return; }
    if (gen !== wordGen) return;
    for (let i = 0; i < flat.length; i += 3) {
      let r = wordsByLine.get(flat[i]);
      if (!r) wordsByLine.set(flat[i], (r = []));
      r.push(flat[i + 1], flat[i + 2]);
    }
    if (w <= decoWindow[1] && w + WORD_WINDOW > decoWindow[0]) updateWordDecorations();
  }
  timing.wordsMs = Math.round(performance.now() - t0);
}

// --file list ------------------------------------------------------------

function renderFiles() {
  const box = $('files');
  box.textContent = '';
  const frag = document.createDocumentFragment();
  fileRows = doc.files.map((f, i) => {
    const slash = f.path.lastIndexOf('/');
    const row = el('div', 'row file', [
      el('span', 'st st-' + f.status, f.status),
      el('span', 'grow', [el('span', '', f.path.slice(slash + 1)),
                          el('span', 'dir', slash >= 0 ? '  ' + f.path.slice(0, slash) : '')]),
      el('span', 'n n-add', f.add ? '+' + f.add : ''),
      el('span', 'n n-rem', f.del ? '-' + f.del : ''),
    ]);
    row.title = f.oldPath && f.status === 'R' ? `${f.oldPath} -> ${f.path}` : f.path;
    row.addEventListener('click', () => jumpToFile(i));
    frag.append(row);
    return row;
  });
  box.append(frag);
  $('files-count').textContent = doc.files.length ? `(${doc.files.length})` : '';
}

function jumpToFile(i) {
  editor.setScrollTop(editor.getTopForLineNumber(fileStarts[i]));
  // The last files may be unable to reach the top of the view, which would
  // make the scroll position report an earlier file; keep the clicked one
  // until the user scrolls away.
  pinned = { file: i, scrollTop: editor.getScrollTop() };
  setActive(i);
}

function onScroll() {
  if (!doc) return;
  updateDecorations(false);
  if (pinned) {
    if (Math.abs(editor.getScrollTop() - pinned.scrollTop) < 1) return;
    pinned = null;
  }
  const top = editor.getVisibleRanges()[0]?.startLineNumber;
  if (!top) return;
  setActive(upperBound(fileStarts, top) - 1);
}

function setActive(i) {
  if (i === activeFile) return;
  fileRows[activeFile]?.classList.remove('active');
  activeFile = i;
  const row = fileRows[i];
  if (row) { row.classList.add('active'); row.scrollIntoView({ block: 'nearest' }); }
}

function upperBound(arr, x) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) lo = mid + 1; else hi = mid; }
  return lo;
}

// --change detection -----------------------------------------------------

async function pollChanges() {
  if (!shown || document.hidden || !$('changed').hidden) return;
  // A load started while the request was out replaces the doc, so its
  // answer no longer applies.
  const gen = wordGen, fp = doc.fingerprint;
  try {
    const r = await api('/api/fingerprint', shown);
    if (gen === wordGen && r.fingerprint !== fp) $('changed').hidden = false;
  } catch { /* transient; next poll retries */ }
}

// --preferences ----------------------------------------------------------

// Per-browser view settings; storage can be unavailable (private windows),
// in which case the defaults apply.
function loadPref(key, dflt) {
  try {
    const v = localStorage.getItem('gitr.' + key);
    return v === null ? dflt : JSON.parse(v);
  } catch { return dflt; }
}

function savePref(key, value) {
  try { localStorage.setItem('gitr.' + key, JSON.stringify(value)); } catch { /* not persisted */ }
}

// --dom helper -----------------------------------------------------------

function el(tag, cls, content) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (Array.isArray(content)) e.append(...content);
  else if (content != null) e.textContent = content;
  return e;
}
