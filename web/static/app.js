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
const PROJECTS_MS = 10000;
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
let fileDirs = [];             // per file, its folder rows from the top: {path, row, kids}
const collapsedDirs = new Map(); // worktree -> Set of collapsed folder paths
let activeFile = -1;
let pinned = null;             // {file, scrollTop} after a click, see onScroll
let decoWindow = [0, 0];
let wordsByLine = new Map();   // line -> [startCol, endCol, ...]
let wordGen = 0;               // bumps on every load so stale fetches are dropped
let current = null;            // {wt, base, untracked} last requested
let shown = null;              // {wt, base, untracked} of the diff on screen, null while loading
let numWidth = 4;
const worktrees = new Map();   // worktree path -> {p, w} from /api/projects
let commentDecos, hoverDecos;
let comments = [];             // resolved comments of the doc on screen, from the server
let commentStamp = null;
let totalComments = 0;         // including comments on files not in this diff       // review.json mtime they were read at
let editing = null;            // {line, key, file, kind, text} of the open comment editor
const zones = new Map();       // zone key -> {id, sig, zone}

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
        : c === '@' ? 'hunk' : c === '\\' ? 'meta' : c === '~' ? 'reindent'
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
      { token: 'reindent', foreground: '6272a4' },  // gitr's colour for it
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
    lineNumbers: lineNumber, lineNumbersMinChars: 9, lineDecorationsWidth: 16,
    glyphMargin: false, folding: true, showFoldingControls: 'mouseover',
    foldingMaximumRegions: 200000,
    stickyScroll: { enabled: true, maxLineCount: 2, defaultModel: 'foldingProviderModel' },
    renderLineHighlight: 'line', occurrencesHighlight: 'off', selectionHighlight: false,
    renderWhitespace: 'none', guides: { indentation: false },
    wordWrap: 'on', wrappingIndent: 'none',
    wrappingStrategy: 'simple',  // monospace arithmetic, no DOM measuring
    // contextmenu off: the comment menu replaces Monaco's, whose entries
    // (copy aside) do nothing in a read-only diff.
    smoothScrolling: false, mouseWheelScrollSensitivity: 1, contextmenu: false,
    unicodeHighlight: { ambiguousCharacters: false, invisibleCharacters: false },
  });
  decos = editor.createDecorationsCollection();
  wordDecos = editor.createDecorationsCollection();
  fileDecos = editor.createDecorationsCollection();
  commentDecos = editor.createDecorationsCollection();
  hoverDecos = editor.createDecorationsCollection();
  editor.onDidScrollChange(onScroll);
  initComments();

  $('base').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && current) load(current.wt, $('base').value.trim() || current.defaultBase, null, viewPosition());
  });
  $('reload').addEventListener('click', () => current && load(current.wt, current.base, null, viewPosition()));
  $('untracked').addEventListener('change', () => current && load(current.wt, current.base, null, viewPosition()));
  $('reindent').addEventListener('change', () => current && load(current.wt, current.base, null, viewPosition()));
  $('wrap').checked = true;
  $('wrap').addEventListener('change', () => {
    editor.updateOptions({ wordWrap: $('wrap').checked ? 'on' : 'off' });
    if (shown) saveSettings(shown.wt, shown.base);
  });
  initSash();
  loadProjects();
  setInterval(pollChanges, POLL_MS);
  setInterval(refreshProjects, PROJECTS_MS);
  addEventListener('focus', refreshProjects);
  document.addEventListener('visibilitychange', refreshProjects);
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
  renderProjects(await api('/api/projects', {}));
  const hash = new URLSearchParams(location.hash.slice(1));
  const initial = worktrees.has(hash.get('wt')) ? hash.get('wt') : null;
  if (initial) {
    // A link names its base and untracked option; the rest comes from what
    // was saved for the worktree.
    const st = settingsFor(initial);
    if (hash.has('untracked')) st.untracked = hash.get('untracked') === '1';
    applyOptions(st);
    const pos = hash.get('file') ? { file: hash.get('file'), at: +hash.get('at') || 0, off: +hash.get('off') || 0 }
                                 : worktrees.get(initial).w.position;
    load(initial, hash.get('base') || st.base, worktrees.get(initial).p.defaultBase, pos);
  }
}

let projectsSig = '';

function renderProjects(projects) {
  // Settings and positions changed in this page may not have reached the
  // server yet; the copies here are the newer ones.
  for (const p of projects) {
    for (const w of p.worktrees) {
      const known = worktrees.get(w.path);
      if (!known) continue;
      if (known.p.settings) p.settings = known.p.settings;
      if (known.w.settings) w.settings = known.w.settings;
      if ('position' in known.w) w.position = known.w.position;
    }
  }
  projectsSig = listSignature(projects);
  worktrees.clear();
  const box = $('projects');
  box.textContent = '';
  for (const p of projects) {
    box.append(el('div', 'row project', [el('span', 'grow', p.name)]));
    if (p.error) box.append(el('div', 'err', p.error));
    for (const w of p.worktrees) {
      const label = w.branch || (w.head ? w.head + ' (detached)' : '?');
      const rel = w.path === p.path ? '' : w.path.split('/').pop();
      const row = el('div', 'row wt', [el('span', 'grow branch', label), el('span', 'dim', rel)]);
      row.title = w.path;
      row.dataset.wt = w.path;
      row.classList.toggle('active', w.path === current?.wt);
      worktrees.set(w.path, { p, w });
      row.addEventListener('click', () => {
        const st = settingsFor(w.path);
        applyOptions(st);
        load(w.path, st.base, p.defaultBase, w.position);
      });
      box.append(row);
    }
  }
  updateRemoved();
}

// What the list shows; settings and positions are not part of it.
function listSignature(projects) {
  return JSON.stringify(projects.map((p) => [p.name, p.path, p.error,
    p.worktrees.map((w) => [w.path, w.branch, w.head, w.detached])]));
}

// Agents add and remove worktrees and switch branches; the list follows,
// every PROJECTS_MS while the page is visible and when it comes back.
let refreshing = false;
async function refreshProjects() {
  if (document.hidden || refreshing) return;
  refreshing = true;
  try {
    const projects = await api('/api/projects', {});
    if (listSignature(projects) !== projectsSig) renderProjects(projects);
  } catch { /* transient; the next refresh retries */ }
  refreshing = false;
}

function updateRemoved() {
  const gone = !!shown && !worktrees.has(shown.wt);
  $('removed').hidden = !gone;
  if (gone) $('changed').hidden = true;
}

// Saved for the worktree, else the last used in its project (a new agent
// worktree usually wants the same base), else the defaults.
function settingsFor(wt) {
  const { p, w } = worktrees.get(wt);
  // gitr collapses re-indented lines by default too
  return { base: p.defaultBase, untracked: false, wrap: true, reindent: true,
           ...(p.settings || {}), ...(w.settings || {}) };
}

function applyOptions(st) {
  $('untracked').checked = st.untracked;
  $('reindent').checked = st.reindent;
  $('wrap').checked = st.wrap;
  editor.updateOptions({ wordWrap: st.wrap ? 'on' : 'off' });
}

async function saveSettings(wt, base) {
  const st = { base, untracked: $('untracked').checked, wrap: $('wrap').checked,
               reindent: $('reindent').checked };
  const entry = worktrees.get(wt);
  if (entry) { entry.w.settings = st; entry.p.settings = st; }
  try { await post('/api/settings', { wt, ...st }); } catch (e) { flash('settings not saved: ' + e.message); }
}

// --loading a diff -------------------------------------------------------

async function load(wt, base, defaultBase, restore) {
  if (shown && shown.wt !== wt) flushPosition();
  const gen = ++wordGen;
  const untracked = $('untracked').checked ? '1' : '0';
  current = { wt, base, untracked, defaultBase: defaultBase || current?.defaultBase || base };
  setHash({ wt, base, untracked, ...(restore || {}) });
  document.querySelectorAll('.wt').forEach((r) => r.classList.toggle('active', r.dataset.wt === wt));
  $('base').value = base;
  $('crumb').textContent = wt;
  $('changed').hidden = true;
  $('removed').hidden = true;
  shown = null;
  showProgress(`loading diff against ${base} ...`);
  const t0 = performance.now();
  let d;
  try {
    d = await api('/api/diff', { wt, base, untracked, reindent: $('reindent').checked ? '1' : '0' });
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
  saveSettings(wt, base);  // only a base that loaded is worth remembering
  fileStarts = d.files.map((f) => f.start);
  let maxNo = 1;
  for (let i = 0; i < d.oldNo.length; i++) maxNo = Math.max(maxNo, d.oldNo[i], d.newNo[i]);
  numWidth = String(maxNo).length;
  wordsByLine = new Map();
  editing = null;
  applyComments({ comments: [], elsewhere: 0, stamp: null });
  decoWindow = [0, 0];
  activeFile = -1;
  pinned = null;
  editor.updateOptions({ lineNumbersMinChars: numWidth * 2 + 2 });
  editor.getModel().setValue(d.text);
  editor.setScrollTop(0);
  if (restore) scrollToPosition(restore);
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
  loadComments(gen);
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
  if (k === 'c' || k === 'i') return String(o).padStart(numWidth) + ' ' + String(w).padStart(numWidth);
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

// A tree like gitr's (and VS Code's compact folders): a directory holding
// only one subdirectory shares its row ("web/static"), and siblings are in
// diff order, so the list reads in the order the diff scrolls.
function buildTree(files) {
  const node = () => ({ dirs: new Map(), files: [], first: Infinity });
  const root = node();
  files.forEach((f, i) => {
    const parts = f.path.split('/');
    let n = root;
    for (const p of parts.slice(0, -1)) {
      if (!n.dirs.has(p)) n.dirs.set(p, node());
      n = n.dirs.get(p);
      n.first = Math.min(n.first, i);
    }
    n.files.push({ name: parts[parts.length - 1], i });
  });
  return root;
}

function renderFiles() {
  const box = $('files');
  box.textContent = '';
  fileRows = new Array(doc.files.length);
  fileDirs = new Array(doc.files.length);
  const collapsed = collapsedDirs.get(shown.wt) || new Set();
  collapsedDirs.set(shown.wt, collapsed);
  const guides = (depth) => Array.from({ length: depth }, () => el('span', 'guide'));

  const walk = (n, depth, prefix, into, ancestors) => {
    const children = [
      ...n.files.map((f) => ({ first: f.i, file: f })),
      ...[...n.dirs].map(([name, d]) => ({ first: d.first, name, dir: d })),
    ].sort((a, b) => a.first - b.first);
    for (const c of children) {
      if (c.file) {
        const i = c.file.i, f = doc.files[i];
        const row = el('div', 'row file', [
          ...guides(depth), el('span', 'chev-space'),
          // The comment count follows the name, where the eye already is.
          el('span', 'grow namecell', [el('span', 'name st-' + f.status, c.file.name),
                                       el('span', 'n n-cmt', '')]),
          el('span', 'n n-add', f.add ? '+' + f.add : ''),
          el('span', 'n n-rem', f.del ? '-' + f.del : ''),
          el('span', 'st st-' + f.status, f.status),
        ]);
        row.title = f.oldPath && f.status === 'R' ? `${f.oldPath} -> ${f.path}` : f.path;
        row.addEventListener('click', () => jumpToFile(i));
        into.append(row);
        fileRows[i] = row;
        fileDirs[i] = ancestors;
        continue;
      }
      let name = c.name, d = c.dir;
      while (!d.files.length && d.dirs.size === 1) {
        const [sub, subNode] = d.dirs.entries().next().value;
        name += '/' + sub;
        d = subNode;
      }
      const path = prefix + name;
      const row = el('div', 'row dir', [...guides(depth), el('span', 'chev'), el('span', 'grow', name)]);
      row.title = path;
      const kids = el('div', 'kids');
      const entry = { path, row, kids };
      row.addEventListener('click', () => {
        if (collapsed.has(path)) collapsed.delete(path); else collapsed.add(path);
        showCollapsed(entry, collapsed.has(path));
        setActive(activeFile, true);
      });
      showCollapsed(entry, collapsed.has(path));
      into.append(row, kids);
      walk(d, depth + 1, path + '/', kids, [...ancestors, entry]);
    }
  };
  const frag = document.createDocumentFragment();
  walk(buildTree(doc.files), 0, '', frag, []);
  box.append(frag);
  $('files-count').textContent = doc.files.length ? `(${doc.files.length})` : '';
}

function showCollapsed(entry, isCollapsed) {
  entry.row.classList.toggle('collapsed', isCollapsed);
  entry.kids.hidden = isCollapsed;
}

function jumpToFile(i) {
  editor.setScrollTop(editor.getTopForLineNumber(fileStarts[i]));
  // The last files may be unable to reach the top of the view, which would
  // make the scroll position report an earlier file; keep the clicked one
  // until the user scrolls away.
  pinned = { file: i, scrollTop: editor.getScrollTop() };
  setActive(i);
}

// --view position ---------------------------------------------------------
//
// Kept in the page address so a browser reload comes back to it. A file and
// its new-side line number rather than a line of the combined document: a
// reload after the worktree changed keeps the place even when files before
// it changed. Lines without a new-side number (a deleted file, headers) use
// the offset from the file's start.

function viewPosition() {
  const top = editor.getVisibleRanges()[0]?.startLineNumber;
  if (!doc || !top || !doc.files.length) return null;
  const f = doc.files[Math.max(0, upperBound(fileStarts, top) - 1)];
  for (let ln = top; ln <= f.end && ln < top + 50; ln++)
    if ('aci'.includes(doc.kinds[ln - 1])) return { file: f.path, at: doc.newNo[ln - 1] - (ln - top), off: 0 };
  return { file: f.path, at: 0, off: top - f.start };
}

function scrollToPosition(pos) {
  const f = doc.files.find((x) => x.path === pos.file);
  if (!f) return;
  let target = Math.min(f.start + (pos.off || 0), f.end);
  if (pos.at) {
    for (let ln = f.start; ln <= f.end; ln++) {
      if ('aci'.includes(doc.kinds[ln - 1]) && doc.newNo[ln - 1] >= pos.at) {
        target = ln - (doc.newNo[ln - 1] - pos.at);
        break;
      }
    }
    target = Math.max(f.start, Math.min(target, f.end));
  }
  editor.setScrollTop(editor.getTopForLineNumber(target));
}

// The view is about to show another worktree: its debounced save would
// otherwise record the new one's position.
function flushPosition() {
  clearTimeout(hashTimer);
  clearTimeout(posTimer);
  const pos = viewPosition();
  const entry = worktrees.get(shown.wt);
  if (entry) entry.w.position = pos;
  post('/api/position', { wt: shown.wt, pos }).catch(() => {});
}

function setHash(params) {
  // replaceState: scrolling must not fill the back-button history
  history.replaceState(null, '', '#' + new URLSearchParams(params).toString());
}

let hashTimer = 0, posTimer = 0;
function saveViewPosition() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    if (!shown) return;
    const pos = viewPosition();
    setHash({ ...shown, ...(pos || {}) });
    // Kept per worktree for switching back, here at once; on the server
    // (for the next session) only once scrolling has settled.
    const entry = worktrees.get(shown.wt);
    if (entry) entry.w.position = pos;
    const wt = shown.wt;
    clearTimeout(posTimer);
    posTimer = setTimeout(() => post('/api/position', { wt, pos }).catch(() => {}), 1000);
  }, 300);
}

function onScroll() {
  if (!doc) return;
  saveViewPosition();
  updateDecorations(false);
  if (pinned) {
    if (Math.abs(editor.getScrollTop() - pinned.scrollTop) < 1) return;
    pinned = null;
  }
  const top = editor.getVisibleRanges()[0]?.startLineNumber;
  if (!top) return;
  setActive(upperBound(fileStarts, top) - 1);
}

// In a collapsed folder the current file is not visible; its outermost
// collapsed folder is highlighted instead.
function setActive(i, force) {
  if (i === activeFile && !force) return;
  $('files').querySelector('.row.active')?.classList.remove('active');
  activeFile = i;
  if (!fileRows[i]) return;
  const hidden = fileDirs[i].find((d) => d.kids.hidden);
  const row = hidden ? hidden.row : fileRows[i];
  row.classList.add('active');
  row.scrollIntoView({ block: 'nearest' });
}

function upperBound(arr, x) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) lo = mid + 1; else hi = mid; }
  return lo;
}

// --comments -------------------------------------------------------------
//
// Stored by the server in the worktree's .gitr/review.json, gitr's own
// format. Each comment takes a view zone below its line; an orphan (its line
// left the diff) sits at the end of its file. A view zone lies under Monaco's
// text layer and never gets the mouse, so the box itself is an overlay widget
// kept on top of its zone, the way VS Code's own comment widget is built.

const KIND_MARK = { note: '>>', good: '++', bad: '!!' };
const SEVERITY = { good: 0, note: 1, bad: 2 };
const KIND_COLOR = { note: '#f1fa8c', good: '#8be9fd', bad: '#ff79c6' };
const PRESETS = [['okay', 'good'], ['wtf', 'bad'], ['why?', 'note'], ['nice', 'good'], ['no', 'bad']];
const commentable = (ln) => doc && 'arci'.includes(doc.kinds[ln - 1] || 'x');
const keyStr = (c) => (c.key ? c.key.join('\0') + '\0' + c.file : 'new\0' + c.line);

function initComments() {
  const T = monaco.editor.MouseTargetType;
  editor.onMouseMove((e) => {
    const ln = e.target.position?.lineNumber;
    const show = ln && commentable(ln) && !commentAt(ln) &&
      (e.target.type === T.GUTTER_LINE_DECORATIONS || e.target.type === T.GUTTER_LINE_NUMBERS ||
       e.target.type === T.CONTENT_TEXT || e.target.type === T.CONTENT_EMPTY);
    hoverDecos.set(show ? [{ range: new monaco.Range(ln, 1, ln, 1),
                             options: { linesDecorationsClassName: 'c-add' } }] : []);
  });
  editor.onMouseLeave(() => hoverDecos.clear());
  editor.onMouseDown((e) => {
    if (e.target.type !== T.GUTTER_LINE_DECORATIONS || !e.event.leftButton) return;
    const ln = e.target.position?.lineNumber;
    if (ln && commentable(ln)) openEditor(ln);
  });
  editor.onContextMenu((e) => {
    const ln = e.target.position?.lineNumber;
    if (!ln || !doc || !doc.files.length) return;
    // Monaco's own menu is off, so nothing else stops the browser's.
    e.event.preventDefault();
    showMenu(ln, e.event.posx, e.event.posy);
  });
  let relayout = 0;
  editor.onDidLayoutChange(() => {
    clearTimeout(relayout);
    relayout = setTimeout(() => renderZones(true), 100);
  });
  editor.onDidScrollChange(() => requestAnimationFrame(syncOverlays));
  addEventListener('mousedown', (e) => { if (!$('cmenu').contains(e.target)) hideMenu(); });
  addEventListener('keydown', (e) => { if (e.key === 'Escape') hideMenu(); });
  editor.onDidScrollChange(hideMenu);
  $('copy-agent').addEventListener('click', copyForAgent);
  $('clear-comments').addEventListener('click', clearComments);
}

function commentAt(ln) { return comments.find((c) => c.line === ln && !c.orphan); }

async function loadComments(gen) {
  try {
    const r = await api('/api/comments', { wt: shown.wt, id: doc.id });
    if (gen === wordGen) applyComments(r);
  } catch (e) { flash('comments: ' + e.message); }
}

async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(body) });
  const out = await r.json();
  if (!r.ok) throw new Error(out.error || r.statusText);
  return out;
}

async function saveComment(target, text, kind) {
  const body = { wt: shown.wt, id: doc.id, text, kind };
  if (target.key) Object.assign(body, { key: target.key, file: target.file });
  else body.line = target.line;
  const gen = wordGen;
  try {
    const r = await post('/api/comment', body);
    if (gen !== wordGen) return false;
    editing = null;
    applyComments(r);
    return true;
  } catch (e) { flash(e.message); return false; }
}

function applyComments(r) {
  comments = r.comments;
  commentStamp = r.stamp;
  const T = monaco.editor;
  // An orphan sits after its file's last line, which is not the line it
  // was written on: marked in the minimap and scrollbar, but no gutter bar
  // claiming that line.
  const list = comments.filter((c) => !c.orphan).map((c) => ({
    range: new monaco.Range(c.line, 1, c.line, 1),
    options: { linesDecorationsClassName: 'c-mark c-' + c.kind },
  }));
  // Several comments can share a line (orphans of one file all sit on its
  // last line); their marks would cover each other, so one mark per line
  // in the colour of the most severe.
  const worst = new Map();
  for (const c of comments)
    if (!worst.has(c.line) || SEVERITY[c.kind] > SEVERITY[worst.get(c.line)]) worst.set(c.line, c.kind);
  for (const [line, kind] of worst)
    list.push({ range: new monaco.Range(line, 1, line, 1),
                options: { overviewRuler: { color: KIND_COLOR[kind], position: T.OverviewRulerLane.Left },
                           minimap: { color: KIND_COLOR[kind], position: T.MinimapPosition.Inline } } });
  commentDecos.set(list);
  const perFile = new Map();  // file index -> {n, kind}, kind the most severe
  for (const c of comments) {
    const i = upperBound(fileStarts, c.line) - 1;
    const f = perFile.get(i) || { n: 0, kind: 'good' };
    f.n++;
    if (SEVERITY[c.kind] > SEVERITY[f.kind]) f.kind = c.kind;
    perFile.set(i, f);
  }
  fileRows.forEach((row, i) => {
    const f = perFile.get(i);
    const cell = row.querySelector('.n-cmt');
    cell.textContent = f ? '\u25cf' + f.n : '';
    cell.className = 'n n-cmt' + (f ? ' c-' + f.kind : '');
  });
  const n = (totalComments = comments.length + (r.elsewhere || 0));
  $('ccount').textContent = !doc ? '' : n === 0 ? 'no comments'
    : `${n} comment${n === 1 ? '' : 's'}` + (r.elsewhere ? ` (${r.elsewhere} on files not in this diff)` : '');
  renderZones(false);
}

// Zones are keyed so that only changed boxes are re-created; re-creating
// one above the viewport would shift the view.
function renderZones(all) {
  const width = Math.max(200, editor.getLayoutInfo().contentWidth - 24);
  const want = new Map();
  for (const c of comments) {
    const ed = editing && editing.key && keyStr(editing) === keyStr(c) ? editing : null;
    want.set(keyStr(c), { c, ed });
  }
  if (editing && !editing.key) want.set(keyStr(editing), { c: editing, ed: editing });
  const sigOf = (w) => JSON.stringify([w.c.line, w.c.kind, w.c.text, w.c.moved, w.c.orphan, !!w.ed, width]);
  const anchor = editor.getVisibleRanges()[0]?.startLineNumber;
  const offset = anchor ? editor.getScrollTop() - editor.getTopForLineNumber(anchor) : 0;
  let focus = null;
  const left = editor.getLayoutInfo().contentLeft;
  editor.changeViewZones((acc) => {
    for (const [k, z] of zones) {
      const w = want.get(k);
      if (all || !w || sigOf(w) !== z.sig) {
        acc.removeZone(z.id);
        editor.removeOverlayWidget(z.widget);
        zones.delete(k);
      }
    }
    for (const [k, w] of want) {
      if (zones.has(k)) continue;
      const node = w.ed ? editorBox(w.ed) : commentBox(w.c);
      node.style.width = width + 'px';
      const height = measure(node);
      const overlay = wrapZone(node);
      overlay.style.left = left + 'px';
      overlay.style.display = 'none';  // until the zone reports a position
      const widget = { getId: () => 'gitr.comment.' + k, getDomNode: () => overlay, getPosition: () => null };
      editor.addOverlayWidget(widget);
      const placeholder = el('div');
      const zone = { afterLineNumber: w.c.line, heightInPx: height, domNode: placeholder,
                     onDomNodeTop: (top) => { overlay.style.top = top + 'px'; syncOverlay(z); } };
      const z = { sig: sigOf(w), zone, node, overlay, placeholder, widget };
      z.id = acc.addZone(zone);
      zones.set(k, z);
      if (w.ed) focus = node;
    }
  });
  if (anchor && anchor <= editor.getModel().getLineCount())
    editor.setScrollTop(editor.getTopForLineNumber(anchor) + offset);
  if (focus) requestAnimationFrame(() => focus.querySelector('textarea')?.focus());
}

function wrapZone(node) {
  const outer = el('div', 'c-zone', [node]);
  // Keys typed in a comment must not reach Monaco's keybindings, nor clicks
  // its cursor handling.
  for (const t of ['keydown', 'keypress', 'keyup', 'pointerdown', 'mousedown', 'contextmenu'])
    outer.addEventListener(t, (e) => e.stopPropagation());
  // Overlay widgets sit outside Monaco's scrollable area, so the wheel over a
  // comment would otherwise not scroll the diff.
  outer.addEventListener('wheel', (e) => {
    e.preventDefault();
    const unit = e.deltaMode === 1 ? editor.getOption(monaco.editor.EditorOption.lineHeight) : 1;
    editor.setScrollTop(editor.getScrollTop() + e.deltaY * unit);
    editor.setScrollLeft(editor.getScrollLeft() + e.deltaX * unit);
  }, { passive: false });
  return outer;
}

// Monaco reports a zone's position only while it is on screen and hides the
// zone's own node otherwise; the overlay follows that.
function syncOverlay(z) {
  z.overlay.style.display = z.placeholder.style.display === 'none' ? 'none' : '';
}
function syncOverlays() { for (const z of zones.values()) syncOverlay(z); }

const ZONE_PAD = 8;  // .c-zone padding, top + bottom
function measure(node) {
  const m = $('measure');
  m.append(node);
  const h = node.offsetHeight;
  node.remove();
  return h + ZONE_PAD;
}

function relayoutZone(node) {
  for (const z of zones.values()) {
    if (z.node !== node) continue;
    z.zone.heightInPx = node.offsetHeight + ZONE_PAD;
    editor.changeViewZones((acc) => acc.layoutZone(z.id));
  }
}

function commentBox(c) {
  const head = el('div', 'c-head', [
    el('span', 'c-kind', `${KIND_MARK[c.kind]} ${c.kind}`),
    c.orphan ? el('span', 'c-flag', 'orphaned: line no longer in the diff') : null,
    !c.orphan && c.moved ? el('span', 'c-flag', 'moved') : null,
  ].filter(Boolean));
  const parts = [head];
  if (c.orphan) parts.push(el('div', 'c-quote', c.lineText));
  parts.push(el('div', 'c-text', c.text));
  const box = el('div', 'c-box c-' + c.kind, parts);
  box.title = 'click to edit';
  box.addEventListener('click', () => openEditor(c.line, c));
  return box;
}

function editorBox(ed) {
  const ta = el('textarea', 'c-input');
  ta.value = ed.text;
  ta.rows = Math.max(2, ed.text.split('\n').length);
  ta.placeholder = 'comment (Enter saves, Shift+Enter new line, Esc cancels, Ctrl+1/2/3 kind)';
  const kinds = el('span', 'c-kinds', Object.keys(KIND_MARK).map((k) => {
    const b = el('button', 'c-kindbtn c-' + k, `${KIND_MARK[k]} ${k}`);
    b.dataset.kind = k;
    b.addEventListener('click', () => setKind(k));
    return b;
  }));
  const setKind = (k) => {
    ed.kind = k;
    box.className = 'c-box c-edit c-' + k;
    kinds.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.kind === k));
  };
  const save = async () => {
    ed.text = ta.value;
    if (ed.key || ta.value.trim()) await saveComment(ed, ta.value, ed.kind);
    else cancelEditor();
  };
  const buttons = [el('button', '', 'save'), el('button', '', 'cancel')];
  buttons[0].addEventListener('click', save);
  buttons[1].addEventListener('click', cancelEditor);
  if (ed.key) {
    const del = el('button', 'c-del', 'delete');
    del.addEventListener('click', () => saveComment(ed, '', ed.kind));
    buttons.push(del);
  }
  const box = el('div', 'c-box c-edit', [ta, el('div', 'c-bar', [kinds, el('span', 'grow'), ...buttons])]);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey) { e.preventDefault(); save(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancelEditor(); }
    else if (e.ctrlKey && ['1', '2', '3'].includes(e.key)) {
      e.preventDefault();
      setKind(Object.keys(KIND_MARK)[+e.key - 1]);
    }
  });
  ta.addEventListener('input', () => {
    ed.text = ta.value;
    ta.rows = Math.max(2, ta.value.split('\n').length);
    relayoutZone(box);
  });
  setKind(ed.kind);
  return box;
}

function openEditor(ln, existing) {
  hideMenu();
  const c = existing || commentAt(ln);
  // An open editor with unsaved text keeps the focus rather than losing it.
  if (editing && editing.text.trim() && editing.text !== editing.orig) {
    const z = zones.get(keyStr(editing));
    z?.node.querySelector('textarea')?.focus();
    flash('finish or cancel the open comment first');
    return;
  }
  editing = c ? { line: c.line, key: c.key, file: c.file, kind: c.kind, text: c.text, orig: c.text }
              : { line: ln, key: null, file: null, kind: 'note', text: '', orig: '' };
  renderZones(false);
}

function cancelEditor() {
  editing = null;
  renderZones(false);
  editor.focus();
}

function showMenu(ln, x, y) {
  const c = commentAt(ln);
  const m = $('cmenu');
  m.textContent = '';
  const item = (label, fn, cls) => {
    const it = el('div', 'c-item' + (cls ? ' ' + cls : ''), label);
    it.addEventListener('click', () => { hideMenu(); fn(); });
    m.append(it);
  };
  if (commentable(ln)) {
    item(c ? 'Edit comment...' : 'Add comment...', () => openEditor(ln, c));
    m.append(el('div', 'c-sep'));
    for (const [text, kind] of PRESETS)
      item(`${KIND_MARK[kind]} ${text}`, () => saveComment(c || { line: ln }, text, kind), 'c-' + kind);
    if (c) {
      m.append(el('div', 'c-sep'));
      item('Delete comment', () => saveComment(c, '', c.kind));
    }
    m.append(el('div', 'c-sep'));
  }
  const path = doc.files[Math.max(0, upperBound(fileStarts, ln) - 1)].path;
  const loc = locationOf(ln);
  item(`Copy "${path}"`, () => copyText(path, `copied ${path}`));
  if (loc !== path) item(`Copy "${loc}"`, () => copyText(loc, `copied ${loc}`));
  const [from, to] = linesAround(ln);
  const n = to - from + 1;
  // A file header alone has nothing worth quoting.
  if (n > 1 || doc.kinds[from - 1] !== 'f')
    item(`Copy "${locationOf(from)}" + ${n} line${n === 1 ? '' : 's'}`, () => copyLines(from, to));
  const wrap = $('editor-wrap').getBoundingClientRect();
  m.hidden = false;
  m.style.left = Math.min(x - wrap.left, wrap.width - m.offsetWidth - 4) + 'px';
  m.style.top = Math.min(y - wrap.top, wrap.height - m.offsetHeight - 4) + 'px';
}

function hideMenu() { $('cmenu').hidden = true; }

// --copying lines (gitr's "c", from the context menu) -----------------------
//
// "path:line" plus the lines, in a diff code fence: pasted bare into an
// agent's chat (markdown) the +/- markers would read as list bullets.

// The selection when it holds the line (a selection lingers after clicks
// elsewhere, and copying it instead of the line at hand surprises), else
// the line alone.
function linesAround(ln) {
  const sel = editor.getSelection();
  if (sel && !sel.isEmpty()) {
    let end = sel.endLineNumber;
    if (sel.endColumn === 1 && end > sel.startLineNumber) end--;  // ends at a line start
    if (sel.startLineNumber <= ln && ln <= end) return [sel.startLineNumber, end];
  }
  return [ln, ln];
}

// gitr's location: the file and the new-side line number, a removed line
// taking the number of the new-side line above it; headers give the path.
function locationOf(ln) {
  const f = doc.files[Math.max(0, upperBound(fileStarts, ln) - 1)];
  for (let i = ln; i > f.start; i--) {
    const k = doc.kinds[i - 1];
    if ('aci'.includes(k)) return `${f.path}:${doc.newNo[i - 1]}`;
    if (k === 'h') {
      const m = /\+(\d+)/.exec(editor.getModel().getLineContent(i));
      const no = m ? +m[1] - 1 : 0;
      return no > 0 ? `${f.path}:${no}` : f.path;
    }
  }
  return f.path;
}

// gitr's comment block: the kind's marker, continuation lines indented, a
// moved comment marked with "~".
function commentBlock(c) {
  const lines = c.text.split('\n');
  const first = (c.moved ? '~ ' : '  ') + KIND_MARK[c.kind] + ' ';
  const rest = c.moved ? '~    ' : '     ';
  return lines.map((l, i) => (i ? rest : first) + l).join('\n');
}

function copyLines(from, to) {
  const model = editor.getModel();
  const parts = [];
  for (let ln = from; ln <= to; ln++) {
    parts.push(model.getLineContent(ln));
    // Comments between the copied lines come along, as in gitr, where a
    // selection spans the comment rows.
    if (ln < to) for (const c of comments) if (c.line === ln && !c.orphan) parts.push(commentBlock(c));
  }
  let text = parts.join('\n');
  const longest = Math.max(0, ...(text.match(/`+/g) || []).map((r) => r.length));
  const fence = '`'.repeat(Math.max(3, longest + 1));
  text = `${fence}diff\n${text}\n${fence}`;
  const loc = locationOf(from);
  const n = to - from + 1;
  copyText(`${loc}\n${text}\n`, `copied ${loc} + ${n} line${n === 1 ? '' : 's'}`);
}

async function copyText(text, msg) {
  try {
    await navigator.clipboard.writeText(text);
    flash(msg);
  } catch (e) { flash('copy failed: ' + e.message); }
}

async function copyForAgent() {
  if (!shown) return;
  try {
    const r = await api('/api/export', { wt: shown.wt, id: doc.id });
    await navigator.clipboard.writeText(r.text);
    flash(`copied ${r.count} comment${r.count === 1 ? '' : 's'} for an agent`);
  } catch (e) { flash('copy failed: ' + e.message); }
}

async function clearComments() {
  if (!shown || !totalComments) return;
  if (!confirm(`Delete all comments of ${shown.wt}?\n\nreview.json is first moved to a .bak file beside it.`)) return;
  try {
    editing = null;
    applyComments(await post('/api/comments/clear', { wt: shown.wt, id: doc.id }));
  } catch (e) { flash(e.message); }
}

let flashTimer = 0;
function flash(msg) {
  const t = $('timing');
  if (!flashTimer) t.dataset.saved = t.textContent;
  t.textContent = msg;
  t.classList.add('flash');
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    t.textContent = t.dataset.saved;
    t.classList.remove('flash');
    flashTimer = 0;
  }, 4000);
}

// --change detection -----------------------------------------------------

async function pollChanges() {
  if (!shown || document.hidden || !worktrees.has(shown.wt)) return;
  // A load started while the request was out replaces the doc, so its
  // answer no longer applies.
  const gen = wordGen, fp = doc.fingerprint;
  try {
    const r = await api('/api/fingerprint', shown);
    if (gen !== wordGen) return;
    if (r.fingerprint !== fp) $('changed').hidden = false;
    // review.json written by a gitr window (or another tab)
    if (r.review !== commentStamp && !editing) loadComments(gen);
  } catch { /* transient; next poll retries */ }
}

// --dom helper -----------------------------------------------------------

function el(tag, cls, content) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (Array.isArray(content)) e.append(...content);
  else if (content != null) e.textContent = content;
  return e;
}
