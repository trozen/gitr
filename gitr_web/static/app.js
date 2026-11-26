'use strict';
// gitr web prototype: one Monaco document holds the whole diff; per-line
// metadata comes from the server alongside the text.

// The bundle's worker sits next to it (monaco/build.sh).
window.MonacoEnvironment = { getWorkerUrl: () => 'monaco/editor.worker.js' };

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
const VIEWED_MINIMAP = '#6272a466';  // shade over viewed hunks
// The label box is 1.5 x the label font tall, at 2px per minimap row. The
// font is below Monaco's 9px default so that short files need less padding
// (server.py MIN_FILE_LINES).
const MINIMAP_LABEL_PX = 8;
const MINIMAP_LABEL_ROWS = 5;
const FONT_MIN = 8, FONT_MAX = 28;
// The CSS variable each size sets; the diff's goes to Monaco instead.
const FONTS = [
  { key: 'diff', label: 'Diff', def: 15 },
  { key: 'comments', label: 'Comments', def: 13, css: '--comment-px' },
  { key: 'sidebar', label: 'Sidebar', def: 13, css: '--sidebar-px' },
  { key: 'bars', label: 'Top and status bar', def: 13, css: '--bar-px' },
];
// Per browser rather than per worktree: the right size depends on the screen.
const SETTINGS_KEY = 'gitr.settings';

const $ = (id) => document.getElementById(id);
const timing = (window.__gitrTiming = {});

let editor, decos, wordDecos, fileDecos;
let doc = null;                // current diff payload
let fileStarts = [];           // sorted 1-based start line per file
let fileRows = [];             // DOM rows parallel to doc.files
let fileDirs = [];             // per file, its folder rows from the top: {path, row, kids}
const collapsedDirs = new Map(); // worktree -> Set of collapsed folder paths
let activeFile = -1;
let pinned = null;             // {file, scrollTop} after a click, see onScroll
let decoWindow = [0, 0];
let wordsByLine = new Map();   // line -> [startCol, endCol, ...]
let wordsDone = false;         // all of the doc's windows are in wordsByLine
let wordGen = 0;               // bumps on every load so stale fetches are dropped
let current = null;            // {wt, base, commit} last requested
let shown = null;              // {wt, base, commit} of the diff on screen, null while loading
let numWidth = 4;
const worktrees = new Map();   // worktree path -> {p, w} from /api/projects
let commentDecos, hoverDecos, newsDecos, viewedDecos, shadeDecos;
let comments = [];             // resolved comments of the doc on screen, from the server
let commentStamp = null;       // review.json mtime they were read at
let totalComments = 0;         // including comments on files not in this diff
let editing = null;            // {line, key, file, kind, text} of the open comment editor
const zones = new Map();       // zone key -> {id, sig, zone}

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
      // a file header's status letter in the file list's colour for it
      if (/^[AMDRCT] /.test(line))
        return { tokens: [{ startIndex: 0, scopes: 'status-' + c }, { startIndex: 1, scopes: 'fileheader' }],
                 endState: lineState };
      const scope = c === '+' ? 'inserted' : c === '-' ? 'deleted' : c === ' ' ? 'context'
        : c === '@' ? 'hunk' : c === '\\' ? 'meta' : c === '~' ? 'reindent' : 'meta';
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
      { token: 'fileheader', foreground: 'ffffff', fontStyle: 'bold' },  // white, as the minimap's file labels
      // .st-A and the rest in app.css
      { token: 'status-A', foreground: '50fa7b', fontStyle: 'bold' },
      { token: 'status-M', foreground: 'bd93f9', fontStyle: 'bold' },
      { token: 'status-D', foreground: 'ff5555', fontStyle: 'bold' },
      { token: 'status-R', foreground: 'ff79c6', fontStyle: 'bold' },
      { token: 'status-C', foreground: 'ff79c6', fontStyle: 'bold' },
      { token: 'status-T', foreground: 'bd93f9', fontStyle: 'bold' },
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
      // Monaco skips the minimap decorations on the lines it marks as
      // selected, and a right-click puts the cursor on the line it comments:
      // the comment's mark would not show. No selection marks keeps them.
      'minimap.selectionHighlight': '#00000000',
      'minimap.background': '#21222c', 'editorStickyScroll.background': HEADER_BG,
      'editorStickyScrollHover.background': HEADER_BG,
    },
  });
  editor = monaco.editor.create($('editor'), {
    value: '', language: 'gitrdiff', theme: 'gitr', readOnly: true, domReadOnly: true,
    // Big agent diffs are exactly the case here; without this Monaco turns
    // tokenization (our colours) off above 20 MB / 300k lines.
    largeFileOptimizations: false,
    automaticLayout: true, ...fontOptions(fontSize('diff')), scrollBeyondLastLine: true,
    minimap: { enabled: true, renderCharacters: true, size: 'proportional', scale: 1, maxColumn: 120,
               showSlider: 'always', sectionHeaderFontSize: MINIMAP_LABEL_PX },
    lineNumbers: lineNumber, lineNumbersMinChars: 9, lineDecorationsWidth: 20,
    // Folding regions only for the sticky headers; the gutter of a header
    // is the viewed box's, as a line's is the comment +.
    glyphMargin: false, folding: true, showFoldingControls: 'never',
    foldingMaximumRegions: 200000,
    stickyScroll: { enabled: true, maxLineCount: 2, defaultModel: 'foldingProviderModel' },
    renderLineHighlight: 'line', occurrencesHighlight: 'off', selectionHighlight: false,
    renderWhitespace: 'none', guides: { indentation: false },
    wordWrap: (prefs.wrap ?? true) ? 'on' : 'off', wrappingIndent: 'none',
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
  newsDecos = editor.createDecorationsCollection();
  viewedDecos = editor.createDecorationsCollection();
  shadeDecos = editor.createDecorationsCollection();
  editor.onDidScrollChange(onScroll);
  initComments();

  initBasePicker();
  initMenus();
  $('reload').addEventListener('click', () => current && load(current.wt, current.base, null, viewPosition(),
                                                                 current.commit, shown && doc?.id));
  // Per browser, as the font sizes: they change how the diff looks, not
  // what is reviewed. gitr collapses re-indented lines by default too.
  $('wrap').checked = prefs.wrap ?? true;
  $('reindent').checked = prefs.reindent ?? true;
  $('wrap').addEventListener('change', () => {
    editor.updateOptions({ wordWrap: $('wrap').checked ? 'on' : 'off' });
    prefs = { ...prefs, wrap: $('wrap').checked };
    savePrefs();
  });
  // Only how the lines are shown changes, so the marks of the last reload stay.
  $('reindent').addEventListener('change', () => {
    prefs = { ...prefs, reindent: $('reindent').checked };
    savePrefs();
    if (current) load(current.wt, current.base, null, viewPosition(), current.commit, newsSince);
  });
  initSash();
  initSettings();
  initSwitcher();
  initQuickOpen();
  initViews();
  loadProjects();
  setInterval(pollChanges, POLL_MS);
  setInterval(refreshProjects, PROJECTS_MS);
  checkVersion();
  setInterval(checkVersion, PROJECTS_MS);
  // Also while hidden, unlike the other polls: a page that cannot close
  // itself says the server is gone, and a page asleep finds out on waking.
  checkAlive();
  setInterval(checkAlive, 30000);
  startEvents();
  $('reload-page').addEventListener('click', reloadPage);
  $('news-close').addEventListener('click', clearNews);
  $('outside-close').addEventListener('click', clearOutside);
  addEventListener('focus', () => refreshProjects());
  document.addEventListener('visibilitychange', () => refreshProjects());
}

function initSash() {
  const sash = $('sash'), side = $('sidebar');
  sash.addEventListener('mousedown', (e) => {
    e.preventDefault();
    sash.classList.add('drag');
    // a variable, as the switcher above the sidebar follows its width
    const move = (ev) => document.documentElement.style.setProperty('--side-w',
      Math.max(180, ev.clientX - side.getBoundingClientRect().left) + 'px');
    const up = () => { sash.classList.remove('drag');
      removeEventListener('mousemove', move); removeEventListener('mouseup', up); };
    addEventListener('mousemove', move);
    addEventListener('mouseup', up);
  });
}

let prefs = {};
try { prefs = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch (e) { /* blocked or bad */ }

const clampFont = (px) => Math.min(FONT_MAX, Math.max(FONT_MIN, px));

function fontSize(key) {
  const px = prefs.fonts?.[key];
  return Number.isFinite(px) ? clampFont(px) : FONTS.find((f) => f.key === key).def;
}

// The 14 / 20 ratio tuned earlier; 15 gets 21.
const fontOptions = (px) => ({ fontSize: px, lineHeight: Math.round(px * 10 / 7) });

function applyFont(f) {
  const px = fontSize(f.key);
  if (!f.css) editor.updateOptions(fontOptions(px));
  else document.documentElement.style.setProperty(f.css, px + 'px');
  if (f.out) f.out.textContent = px;
  if (f.reset) f.reset.classList.toggle('off', px === f.def);
}

// A setting's - value + and its reset, which shows only off the default
// (as VS Code's settings have it).
const DISCARD_SVG = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M3.5 2v3.5L4 6h3.5V5H4.979l.941-.941a3.552 3.552 0 1 1 5.023 5.023L5.746 14.28l.72.72 5.198-5.198A4.57 4.57 0 0 0 5.2 3.339l-.7.7V2h-1z"/></svg>';
function stepper(out, titles, def) {
  const down = el('button', null, '\u2212'), up = el('button', null, '+');
  const reset = el('button', 'reset');
  reset.innerHTML = DISCARD_SVG;
  [down.title, up.title] = titles;
  reset.title = `Back to ${def}`;
  return { down, up, reset, box: [el('span', 'stepper', [down, out, up]), reset] };
}

function setFont(f, px) {
  const fonts = { ...prefs.fonts };
  px = clampFont(px);
  if (px === f.def) delete fonts[f.key]; else fonts[f.key] = px;
  prefs = { ...prefs, fonts };
  applyFont(f);
  // Comment boxes have fixed zone heights, measured at their font size.
  if (f.key === 'comments') renderZones(true);
  // the graph is drawn in pixels of the row height
  if (f.key === 'sidebar') renderCommits();
  savePrefs();
}

function savePrefs() {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(prefs)); }
  catch (e) { /* storage blocked: the choice lasts for this page only */ }
}

// --left pane views: changed files or comments -----------------------------

const VIEWS = ['files', 'comments', 'commits'];

function setView(view) {
  for (const b of document.querySelectorAll('#activity .view')) b.classList.toggle('on', b.dataset.view === view);
  for (const v of VIEWS) $(v + '-section').hidden = view !== v;
  if (view === 'commits' && commitsUndrawn) renderCommits();
  if (prefs.view !== view) { prefs = { ...prefs, view }; savePrefs(); }
}

function initViews() {
  $('collapse-all').addEventListener('click', () => collapseAll(true));
  $('next-unviewed').addEventListener('click', () => jumpUnviewed(1));
  $('prev-unviewed').addEventListener('click', () => jumpUnviewed(-1));
  $('viewed-all').addEventListener('click', () => {
    if (!doc) return;
    const all = doc.files.map((f, i) => i);
    markViewed(doc.hunks.map((h, i) => i), viewedState(all) !== 'all');
  });
  $('expand-all').addEventListener('click', () => collapseAll(false));
  for (const b of document.querySelectorAll('#activity .view')) b.addEventListener('click', () => setView(b.dataset.view));
  setView(VIEWS.includes(prefs.view) ? prefs.view : 'files');
  $('commit-all').addEventListener('click', () => selectCommit(null));
  $('range-mode').addEventListener('click', () => setRangeMode(rangeMode ? null : 'first'));
  $('on-base').classList.toggle('on', onBaseShown());
  $('on-base').addEventListener('click', () => {
    prefs = { ...prefs, onBase: !onBaseShown() };
    savePrefs();
    $('on-base').classList.toggle('on', onBaseShown());
    if (current) loadCommits(wordGen);
  });
}

// In diff order under their files, as the zones are stacked; comments on
// files not in the diff at the end.
function renderCommentList() {
  const box = $('comment-list');
  box.textContent = '';
  $('comments-count').textContent = comments.length ? `(${comments.length})` : '';
  const badge = $('comments-badge');
  badge.hidden = !comments.length;
  if (comments.length) {
    const worst = comments.reduce((w, c) => SEVERITY[c.kind] > SEVERITY[w] ? c.kind : w, 'good');
    badge.className = `badge c-${worst}`;
    badge.textContent = comments.length > 99 ? '99+' : String(comments.length);
    badge.title = `${comments.length} comment${comments.length === 1 ? '' : 's'}`;
  }
  if (!doc) return;
  if (!comments.length) { box.append(el('div', 'empty', 'no comments')); return; }
  let file = null, elsewhere = false;
  comments.forEach((c, i) => {
    if (c.elsewhere && !elsewhere) {
      elsewhere = true;
      box.append(el('div', 'row cl-head', [el('span', 'chev-space'), el('span', 'grow', 'on files not in this diff')]));
    }
    if (c.file !== file) {
      file = c.file;
      const head = el('div', 'row cl-file', [el('span', 'chev-space'), el('span', 'grow', file)]);
      head.title = file;
      box.append(head);
    }
    const first = c.text.split('\n')[0];
    const flag = c.orphan ? 'orphaned' : c.moved ? 'moved' : '';
    const row = el('div', 'row cmt c-' + c.kind, [
      el('span', 'guide'), el('span', 'chev-space'),
      el('span', 'c-kind', KIND_MARK[c.kind]), el('span', 'txt', first),
      ...(flag ? [el('span', 'flag', flag)] : []),
      ...(outsideMarks.has(keyStr(c)) ? [outsideFlag(c, 'flag')] : []),
      el('span', 'ln', c.lineNo ? String(c.lineNo) : ''),
      closeButton(c),
    ]);
    row.title = c.text;
    row.addEventListener('click', () => scrollToComment(i));
    row.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      commentMenu(c, e.clientX, e.clientY, () => scrollToComment(i));
    });
    box.append(row);
  });
}

function reloadPage() {
  // The hash brings back the worktree and the scroll position at once,
  // without waiting for the debounced update.
  if (shown) setHash({ ...shown, ...(viewPosition() || {}) });
  location.reload();
}

function initSettings() {
  const panel = $('settings'), btn = $('settings-btn');
  for (const f of FONTS) {
    f.out = el('span', 'size');
    const { down, up, reset, box } = stepper(f.out, ['Smaller', 'Larger'], f.def);
    f.reset = reset;
    down.addEventListener('click', () => setFont(f, fontSize(f.key) - 1));
    up.addEventListener('click', () => setFont(f, fontSize(f.key) + 1));
    reset.addEventListener('click', () => setFont(f, f.def));
    $('font-rows').append(el('div', 'set-row', [el('span', 'grow', f.label), ...box]));
    applyFont(f);
  }
  const out = el('input', 'size');
  const { down, up, reset, box } = stepper(out, ['Fewer', 'More'], OLDER_DEF);
  reset.classList.toggle('off', olderCount() === OLDER_DEF);
  out.type = 'number'; out.min = 0; out.max = OLDER_MAX;
  out.value = olderCount();
  const setOlder = (n) => {
    n = Number.isFinite(n) ? Math.min(OLDER_MAX, Math.max(0, Math.round(n))) : olderCount();
    out.value = n;
    reset.classList.toggle('off', n === OLDER_DEF);
    if (n === olderCount()) return;
    prefs = { ...prefs, older: n };
    savePrefs();
    if (current) loadCommits(wordGen);
  };
  // on Enter or leaving the field, not per keystroke: 1 on the way to 150
  // would be a reload for nothing
  out.addEventListener('change', () => setOlder(parseInt(out.value, 10)));
  out.addEventListener('keydown', (e) => { if (e.key === 'Enter') out.blur(); });
  down.addEventListener('click', () => setOlder(olderCount() - OLDER_STEP));
  up.addEventListener('click', () => setOlder(olderCount() + OLDER_STEP));
  reset.addEventListener('click', () => setOlder(OLDER_DEF));
  $('older-row').append(...box);
  const show = (on) => { panel.hidden = !on; btn.classList.toggle('on', on); };
  btn.addEventListener('click', () => show(panel.hidden));
  // An installed app has no browser reload button.
  $('reload-app').addEventListener('click', reloadPage);
  addEventListener('mousedown', (e) => {
    if (!panel.contains(e.target) && !btn.contains(e.target)) show(false);
  });
  addEventListener('keydown', (e) => { if (e.key === 'Escape') show(false); });
}

// --base picker -------------------------------------------------------------

function aheadBehind(ahead, behind) {
  if (ahead == null) return [];
  const out = [el('span', 'n-add', `+${ahead}`)];
  if (behind) out.push(' ', el('span', 'n-rem', `-${behind}`));
  return out;
}

let baseRefs = null;   // /api/refs for the worktree the picker opened on
let baseTyped = false; // the list filters only once the user types

function applyBase(name) {
  hideBasePicker();
  $('base').blur();
  // A new base is a new review: back from a single commit to all changes.
  if (current) load(current.wt, name.trim() || current.defaultBase, null,
                    current.commit ? reviewPosition() : viewPosition());
}

function hideBasePicker() {
  $('basepop').hidden = true;
  $('base').setAttribute('aria-expanded', 'false');
}

async function showBasePicker() {
  if (!current) return;
  const pop = $('basepop'), r = $('base').getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8)) + 'px';
  pop.style.top = r.bottom + 4 + 'px';
  pop.hidden = false;
  $('base').setAttribute('aria-expanded', 'true');
  baseTyped = false;
  baseRefs = null;
  renderBaseList();
  const wt = current.wt;
  try {
    const refs = await api('/api/refs', { wt });
    if (current?.wt === wt) { baseRefs = refs; renderBaseList(); }
  } catch (e) {
    $('base-list').textContent = '';
    $('base-list').append(el('div', 'err', e.message));
  }
}

// The default base first, then the upstream (what is not pushed yet), the
// branches of the project's other worktrees (agents branch off each
// other's work), and every branch, remote branch and tag, most recent first.
function baseSections() {
  const byName = new Map([...(baseRefs?.commits || []), ...(baseRefs?.refs || [])].map((r) => [r.name, r]));
  const entry = worktrees.get(current.wt);
  const others = entry ? entry.p.worktrees.filter((w) => w.path !== current.wt && w.branch).map((w) => w.branch) : [];
  const refs = baseRefs?.refs || [];
  const sections = [
    ['default', [current.defaultBase]],
    ['upstream', baseRefs?.upstream ? [baseRefs.upstream] : []],
    ['other worktrees', others],
    ['recent commits', (baseRefs?.commits || []).map((c) => c.name)],
    ['branches', refs.filter((r) => r.kind === 'branch').map((r) => r.name)],
    ['remote branches', refs.filter((r) => r.kind === 'remote').map((r) => r.name)],
    ['tags', refs.filter((r) => r.kind === 'tag').map((r) => r.name)],
  ];
  // The worktree's own branch would diff HEAD against itself.
  const seen = new Set(entry?.w.branch ? [entry.w.branch] : []);
  return sections.map(([title, names]) => [title, names.filter((n) => !seen.has(n) && seen.add(n))
    .map((n) => byName.get(n) || { name: n })]);
}

function renderBaseList() {
  const box = $('base-list');
  box.textContent = '';
  const words = baseTyped ? $('base').value.toLowerCase().split(/\s+/).filter(Boolean) : [];
  let any = false;
  for (const [title, refs] of baseSections()) {
    const hay = (r) => (r.name + ' ' + (r.subject || '')).toLowerCase();
    const rows = refs.filter((r) => words.every((w) => hay(r).includes(w)));
    if (!rows.length) continue;
    any = true;
    box.append(el('h2', null, title));
    for (const r of rows) {
      // a commit: its hash, subject and age; a ref: its name and counts
      const row = r.subject != null
        ? el('div', 'row commit', [el('span', 'ref', r.name), el('span', 'grow subj', r.subject),
                                   el('span', 'ab', age(r.time))])
        : el('div', 'row', [el('span', 'grow ref', r.name), el('span', 'ab', aheadBehind(r.ahead, r.behind))]);
      row.setAttribute('role', 'option');
      row.classList.toggle('active', r.name === current.base);
      if (r.ahead != null) row.title = `HEAD: ${r.ahead} ahead, ${r.behind} behind`;
      if (r.subject != null) row.title = `${r.name} ${r.subject}\nas the base: review the changes after this commit`;
      // mousedown, not click: the field's blur must not close the list first
      row.addEventListener('mousedown', (e) => { e.preventDefault(); applyBase(r.name); });
      box.append(row);
    }
  }
  if (!baseRefs) box.append(el('div', 'none', 'loading branches ...'));
  else if (!any) box.append(el('div', 'none', 'no match; Enter uses it as typed'));
}

function initBasePicker() {
  const input = $('base'), pop = $('basepop');
  input.addEventListener('focus', () => { input.select(); showBasePicker(); });
  // also when the field already has the focus, e.g. after Enter
  input.addEventListener('click', () => { if (pop.hidden) showBasePicker(); });
  input.addEventListener('input', () => { baseTyped = true; if (!pop.hidden) renderBaseList(); });
  // The chevron opens and closes the list. preventDefault: the label
  // around it would focus the field, which opens it again.
  const chev = $('base-chev');
  chev.addEventListener('mousedown', (e) => e.preventDefault());
  chev.addEventListener('click', (e) => {
    e.preventDefault();
    if (!pop.hidden) input.blur();
    else if (document.activeElement === input) showBasePicker();
    else input.focus();
  });
  // Enter takes the text as typed: any ref or commit, listed or not.
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') applyBase(input.value);
    // preventDefault: a search field's own Escape would empty it first
    if (e.key === 'Escape') { e.preventDefault(); input.value = current?.base || ''; hideBasePicker(); input.blur(); }
  });
  input.addEventListener('blur', () => {
    hideBasePicker();
    if (current && input.value !== current.base) input.value = current.base;
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
    // A link names its base; else the one saved for the worktree.
    const st = settingsFor(initial);
    const pos = hash.get('file') ? { file: hash.get('file'), at: +hash.get('at') || 0, off: +hash.get('off') || 0 }
                                 : worktrees.get(initial).w.position;
    load(initial, hash.get('base') || st.base, worktrees.get(initial).p.defaultBase, pos, hash.get('commit'));
  } else {
    showSwitcher(true);
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
    // a checkout of a multi-repo directory: the directories leading to it
    // as a paler prefix
    const [dir, name] = projectParts(p);
    const head = el('div', 'row project', [el('span', 'grow', [el('span', 'pre', dir && dir + ' \u203a '), name])]);
    head.title = p.path;
    const group = el('div', 'group', [head]);
    if (p.error) group.append(el('div', 'err', p.error));
    for (const w of p.worktrees) {
      const rel = w.path === p.path ? '' : w.path.split('/').pop();
      const row = el('div', 'row wt', [el('span', 'grow branch', wtLabel(w)), el('span', 'dim', rel)]);
      row.title = w.path;
      row.dataset.wt = w.path;
      row.dataset.hay = [p.group || '', p.name, wtLabel(w), rel].join(' ').toLowerCase();
      row.classList.toggle('active', w.path === current?.wt);
      worktrees.set(w.path, { p, w });
      row.addEventListener('click', () => { showSwitcher(false); openWorktree(w.path); });
      group.append(row);
    }
    box.append(group);
  }
  box.append(el('div', 'none', 'no match'));
  filterSwitcher();
  updateRemoved();
  updateCrumb();
}

function openWorktree(path) {
  const { p, w } = worktrees.get(path);
  if (path !== shown?.wt && kept.has(path)) return showKept(path);
  load(path, settingsFor(path).base, p.defaultBase, w.position);
}

const wtLabel = (w) => w.branch || (w.head ? w.head + ' (detached)' : '?');
const projectName = (p) => (p.group ? p.group + '/' : '') + p.name;
// a multi-repo directory's checkout as [its directory, the repo's folder]
const projectParts = (p) => [p.group || '', p.name];

function updateCrumb() {
  const entry = current && worktrees.get(current.wt);
  $('crumb-text').textContent = !current ? 'select a worktree'
    : entry ? `${projectParts(entry.p).filter(Boolean).join(' \u203a ')} \u203a ${wtLabel(entry.w)}` : current.wt;
  $('crumb').title = (current ? current.wt + '\n' : '') + 'Switch project or worktree';
  // the version, and which worktree: several windows tell apart in the
  // taskbar; the server's title names its machine ("gitr - host")
  document.title = [APP_TITLE.replace('gitr', 'gitr' + (gitrVersion ? ' ' + gitrVersion : '')),
                    current ? $('crumb-text').textContent : ''].filter(Boolean).join(' - ');
  // An installed app's window shows its manifest name instead, unless this
  // says otherwise (Chrome 134 and later; not 157 for one)
  document.querySelector('meta[name=application-title]').content = document.title;
  // hence the version in the settings too
  $('gitr-version').textContent = gitrVersion ? 'gitr ' + gitrVersion : '';
}
let gitrVersion = '';

// Started by herdr's plugin: said so in the settings popup, with an update
// button for an installed one (a linked checkout is updated with git).
async function showPlugin() {
  const info = await api('/api/plugin', {}).catch(() => null);
  if (!info?.plugin) return;
  const linked = info.source === 'local';
  $('plugin-info').textContent = 'started by herdr\u2019s plugin' + (linked ? ' (linked)' : '');
  $('plugin-row').hidden = false;
  const btn = $('plugin-update'), status = $('plugin-status');
  btn.hidden = linked;
  const done = (text) => { status.textContent = text; btn.textContent = 'update'; btn.disabled = false; };
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = 'updating\u2026';
    status.textContent = '';
    // the server as it is now, to tell its successor by
    const was = await api('/api/version', {}).then((x) => x.version, () => null);
    let updated;
    try {
      updated = (await post('/api/plugin/update', {})).updated;
    } catch (e) {
      // no answer at all: the handover may have stopped the server while
      // herdr was still at it; any other failure is herdr's, shown
      if (!(e instanceof TypeError)) return done(String(e.message || e));
      updated = null;
    }
    if (updated === false) return done('up to date');
    status.textContent = updated ? 'updated, restarting\u2026' : 'restarting?\u2026';
    // the server hands over to the new copy within seconds: the page
    // follows it once it answers as another server
    for (let i = 0; i < 60; i++) {
      await new Promise((ok) => setTimeout(ok, 1000));
      const v = await api('/api/version', {}).then((x) => x.version, () => null);
      if (v && was && v !== was) { location.reload(); return; }
    }
    done(updated ? 'updated, but gitr serve did not restart (see gitr status)' : 'no restart; see gitr status');
  };
}
const APP_TITLE = document.title;

function showSwitcher(on) {
  const sw = $('switcher');
  sw.hidden = !on;
  $('crumb').classList.toggle('on', on);
  if (!on) return;
  const r = $('crumb').getBoundingClientRect();
  sw.style.width = Math.max(440, r.width - 8) + 'px';
  sw.style.left = r.left + 4 + 'px';
  sw.style.top = r.bottom + 2 + 'px';
  const f = $('switch-filter');
  f.value = '';
  filterSwitcher();
  f.focus();
  $('projects').querySelector('.wt.active')?.scrollIntoView({ block: 'nearest' });
}

// Every word typed has to appear in the project name, branch or folder.
function filterSwitcher() {
  const words = $('switch-filter').value.toLowerCase().split(/\s+/).filter(Boolean);
  let any = false;
  for (const g of $('projects').querySelectorAll('.group')) {
    let shown = 0;
    for (const row of g.querySelectorAll('.wt')) {
      row.hidden = !words.every((w) => row.dataset.hay.includes(w));
      if (!row.hidden) shown++;
    }
    g.hidden = words.length > 0 && !shown;
    any ||= !g.hidden;
  }
  $('projects').querySelector('.none').hidden = any;
  // the current worktree while it matches, else the first match
  const rows = visibleWorktrees();
  setSwitchSel(rows.find((r) => r.classList.contains('active')) || rows[0]);
}

function visibleWorktrees() {
  return [...$('projects').querySelectorAll('.group:not([hidden]) .wt:not([hidden])')];
}

function setSwitchSel(row) {
  $('projects').querySelector('.wt.sel')?.classList.remove('sel');
  row?.classList.add('sel');
  row?.scrollIntoView({ block: 'nearest' });
}

// Arrows wrap around, pages stop at the ends, as in the go to file list.
function moveSwitchSel(d, page) {
  const rows = visibleWorktrees();
  if (!rows.length) return;
  let i = rows.findIndex((r) => r.classList.contains('sel'));
  if (page) {
    const list = $('projects');
    const per = Math.max(1, Math.floor(list.clientHeight / rows[0].offsetHeight) - 1);
    i = Math.max(0, Math.min(rows.length - 1, i + d * per));
  } else i = (i + d + rows.length) % rows.length;
  setSwitchSel(rows[i]);
}

function initSwitcher() {
  const sw = $('switcher'), btn = $('crumb'), f = $('switch-filter');
  btn.addEventListener('click', () => showSwitcher(sw.hidden));
  f.addEventListener('input', filterSwitcher);
  f.addEventListener('keydown', (e) => {
    const move = { ArrowDown: [1], ArrowUp: [-1], PageDown: [1, true], PageUp: [-1, true] }[e.key];
    if (move) { e.preventDefault(); moveSwitchSel(...move); }
    else if (e.key === 'Enter') sw.querySelector('.wt.sel')?.click();
  });
  addEventListener('mousedown', (e) => {
    if (!sw.contains(e.target) && !btn.contains(e.target)) showSwitcher(false);
  });
  addEventListener('keydown', (e) => { if (e.key === 'Escape') showSwitcher(false); });
}

// --go to file (Ctrl+P, as VS Code's quick open) ----------------------------

// Every query character in order (spaces ignored), scored as VS Code does
// roughly: runs of consecutive characters and the starts of words count,
// and a match inside the file name beats one spread over the folders.
// Returns the score and the matched positions in path, or null.
function fuzzyMatch(query, path) {
  const q = query.toLowerCase().replace(/\s+/g, '');
  if (!q) return { score: 0, hits: [] };
  const p = path.toLowerCase();
  const nameStart = path.lastIndexOf('/') + 1;
  const wordStart = (i) => i === 0 || '/._- '.includes(path[i - 1]) ||
    (path[i] !== p[i] && path[i - 1] === p[i - 1]);  // camelCase hump
  // Greedy from each place the first character occurs; the best wins.
  let best = null;
  for (let start = p.indexOf(q[0]); start !== -1; start = p.indexOf(q[0], start + 1)) {
    const hits = [start];
    let i = start + 1;
    for (let k = 1; k < q.length; k++) {
      // prefer the next word start holding the character, else the next one
      let at = -1;
      for (let j = i; j < p.length; j++) {
        if (p[j] !== q[k]) continue;
        if (j === i || wordStart(j)) { at = j; break; }
        if (at === -1) at = j;
      }
      if (at === -1) return best;
      hits.push(at);
      i = at + 1;
    }
    let score = 0;
    hits.forEach((h, n) => {
      score += 1;
      if (n && h === hits[n - 1] + 1) score += 5;
      if (wordStart(h)) score += 3;
      if (h >= nameStart) score += 2;
    });
    if (hits[0] === nameStart) score += 6;
    score -= (hits[hits.length - 1] - hits[0]) * 0.05;  // tighter is better
    if (!best || score > best.score) best = { score, hits };
  }
  return best;
}

let qoItems = [], qoSel = 0;
const QO_MAX = 300;

function showQuickOpen(on) {
  const qo = $('quickopen');
  if (!on) { qo.hidden = true; return; }
  if (!worktrees.size) return;
  const r = $('editor-wrap').getBoundingClientRect();
  const w = Math.min(600, r.width - 40);
  qo.style.width = w + 'px';
  qo.style.left = r.left + (r.width - w) / 2 + 'px';
  qo.style.top = r.top + 6 + 'px';
  qo.hidden = false;
  const input = $('qo-input');
  input.value = '';
  renderQuickOpen();
  input.focus();
}

function highlighted(text, hits, offset) {
  const out = [];
  let plain = '';
  for (let i = 0; i < text.length; i++) {
    if (hits.has(i + offset)) {
      if (plain) { out.push(plain); plain = ''; }
      out.push(el('span', 'hl', text[i]));
    } else plain += text[i];
  }
  if (plain) out.push(plain);
  return out;
}

// The changed files, then the worktrees of all projects, matched as
// "project/branch" so that the branch counts as the name.
function renderQuickOpen() {
  const q = $('qo-input').value;
  const files = [], wts = [];
  (doc?.files || []).forEach((f, i) => {
    const m = fuzzyMatch(q, f.path);
    if (m) files.push({ i, ...m });
  });
  let k = 0;
  for (const [path, { p, w }] of worktrees) {
    const key = `${projectName(p)}/${wtLabel(w)}`;
    const m = fuzzyMatch(q, key);
    if (m) wts.push({ wt: path, key, k, ...m });
    k++;
  }
  // Unfiltered, the diff's own order; filtered, the best first.
  if (q.trim()) {
    files.sort((a, b) => b.score - a.score || a.i - b.i);
    wts.sort((a, b) => b.score - a.score || a.k - b.k);
  }
  qoItems = [...files.slice(0, QO_MAX), ...wts];
  qoSel = 0;
  const box = $('qo-list');
  box.textContent = '';
  const perFile = commentsPerFile();
  for (const [n, it] of qoItems.entries()) {
    if (it.wt) {
      if (it === wts[0]) box.append(el('h2', null, 'worktrees'));
      const { p, w } = worktrees.get(it.wt);
      const cut = projectName(p).length + 1;
      const hits = new Set(it.hits);
      const rel = w.path === p.path ? '' : w.path.split('/').pop();
      const row = el('div', 'row' + (n === qoSel ? ' sel' : '') + (it.wt === current?.wt ? ' current' : ''), [
        el('span', 'qo-name', highlighted(wtLabel(w), hits, cut)),
        el('span', 'qo-dir', [el('bdi', null, highlighted(projectName(p), hits, 0))]),
        el('span', 'n', rel),
      ]);
      row.setAttribute('role', 'option');
      row.title = w.path;
      row.addEventListener('mousedown', (e) => { e.preventDefault(); openQuickItem(n); });
      box.append(row);
      continue;
    }
    if (n === 0 && wts.length) box.append(el('h2', null, 'changed files'));
    const f = doc.files[it.i];
    const cut = f.path.lastIndexOf('/') + 1;
    const hits = new Set(it.hits);
    const c = perFile.get(it.i);
    const row = el('div', 'row' + (n === qoSel ? ' sel' : ''), [
      el('span', 'qo-name st-' + f.status, highlighted(f.path.slice(cut), hits, cut)),
      // rtl keeps the end of a long folder path; the bdi stops it
      // reordering the slashes
      el('span', 'qo-dir', [el('bdi', null, highlighted(f.path.slice(0, Math.max(0, cut - 1)), hits, 0))]),
      c ? el('span', 'n c-' + c.kind, '\u25cf' + c.n) : null,
      el('span', 'n n-add', f.add ? '+' + f.add : ''),
      el('span', 'n n-rem', f.del ? '-' + f.del : ''),
      el('span', 'st st-' + f.status, f.status),
    ].filter(Boolean));
    row.setAttribute('role', 'option');
    row.title = f.path;
    // mousedown: a click would blur the field first, which closes the popup
    row.addEventListener('mousedown', (e) => { e.preventDefault(); openQuickItem(n); });
    box.append(row);
  }
  if (!qoItems.length) box.append(el('div', 'none', 'no matching file or worktree'));
}

// Arrows wrap around, pages stop at the ends (as in VS Code).
function moveQuickSel(d, page) {
  const list = $('qo-list'), rows = list.querySelectorAll('.row');
  if (!rows.length) return;
  rows[qoSel]?.classList.remove('sel');
  if (page) {
    const per = Math.max(1, Math.floor(list.clientHeight / rows[0].offsetHeight) - 1);
    qoSel = Math.max(0, Math.min(rows.length - 1, qoSel + d * per));
  } else qoSel = (qoSel + d + rows.length) % rows.length;
  rows[qoSel].classList.add('sel');
  rows[qoSel].scrollIntoView({ block: 'nearest' });
}

function openQuickItem(n) {
  const it = qoItems[n];
  showQuickOpen(false);
  if (!it) return;
  if (it.wt) { if (it.wt !== current?.wt) openWorktree(it.wt); return; }
  jumpToFile(it.i);
  editor.focus();
}

function initQuickOpen() {
  const input = $('qo-input');
  // Capture, so that it works with the focus in Monaco too; it replaces
  // the browser's print dialog, which this page has no use for.
  addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'p') {
      e.preventDefault();
      e.stopPropagation();
      showQuickOpen(true);
    }
  }, true);
  $('go-to-file').addEventListener('click', () => showQuickOpen(true));
  input.addEventListener('input', renderQuickOpen);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveQuickSel(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveQuickSel(-1); }
    else if (e.key === 'PageDown') { e.preventDefault(); moveQuickSel(1, true); }
    else if (e.key === 'PageUp') { e.preventDefault(); moveQuickSel(-1, true); }
    else if (e.key === 'Enter') { e.preventDefault(); openQuickItem(qoSel); }
    else if (e.key === 'Escape') { e.preventDefault(); showQuickOpen(false); editor.focus(); }
  });
  input.addEventListener('blur', () => showQuickOpen(false));
}

// --commits pane: base..HEAD as a graph, as gitk draws it ------------------

let commitList = null;   // the last /api/commits answer for current wt and base
let commitsFp = null;    // the fingerprint it was loaded at
let commitsUndrawn = false;
const LANE_W = 12;
// Commits before the base, faded below the review's own: per browser, as
// the font sizes, since they only add context.
const OLDER_DEF = 30, OLDER_STEP = 5, OLDER_MAX = 1000;
const olderCount = () => (Number.isFinite(prefs.older)
  ? Math.min(OLDER_MAX, Math.max(0, Math.round(prefs.older))) : OLDER_DEF);
// The base's commits since the branch left it, per browser as well.
const onBaseShown = () => prefs.onBase !== false;
// listed for context, outside the review: older than the base, or the base's own
const isFaded = (r) => !!(r?.old || r?.onBase);
const commitsQuery = () => ({ wt: shown.wt, base: shown.base, older: olderCount(), onBase: onBaseShown() ? 1 : 0 });
const FADED = .55;  // = the opacity of the faded rows' text in app.css
const LANE_COLORS = ['#8be9fd', '#50fa7b', '#ffb86c', '#ff79c6', '#bd93f9', '#f1fa8c'];

// Only the last request counts: a refresh still on its way when the older
// count changed would bring back the list for the count before.
let commitsSeq = 0;

async function refreshCommits(gen) {
  const seq = ++commitsSeq;
  // an edit can move a commented line from a commit to the uncommitted
  // changes even when the list stays as it is
  refreshMarks(gen);
  try {
    const r = await api('/api/commits', commitsQuery());
    // Most worktree changes are edits, which leave the list as it is: a
    // thousand rows and their graph are not redrawn for nothing.
    if (gen === wordGen && seq === commitsSeq && JSON.stringify(r) !== JSON.stringify(commitList)) {
      commitList = r;
      renderCommits();
    }
  } catch { /* transient; next poll retries */ }
}

async function loadCommits(gen) {
  const seq = ++commitsSeq;
  const at = doc.fingerprintAll;
  try {
    const r = await api('/api/commits', commitsQuery());
    if (gen !== wordGen || seq !== commitsSeq) return;
    commitList = r;
    commitsFp = at;
    renderCommits();
    refreshMarks(gen);
  } catch (e) {
    if (gen !== wordGen || seq !== commitsSeq) return;
    commitList = null;
    $('commit-list').textContent = '';
    $('commit-list').append(el('div', 'empty', 'commits: ' + e.message));
  }
}

// Per commit, the comments on lines it changed (server.py comment_commits),
// marked as the files are (a dot and the count in the colour of the most
// severe kind): which commits the review has got to. Fetched apart from the
// list, as comments change on their own.
let commitMarks = null;  // {marks: {hash: {kind: count}}, stamp, wt, base}
let marksSeq = 0;

async function refreshMarks(gen) {
  const seq = ++marksSeq;
  try {
    const r = await api('/api/commit-marks', { wt: shown.wt, base: shown.base });
    if (gen !== wordGen || seq !== marksSeq) return;
    commitMarks = { ...r, wt: shown.wt, base: shown.base };
    paintMarks();
  } catch { /* transient; the next refresh retries */ }
}

function paintMarks() {
  // another worktree's (whose commits can be the same) until its own come
  const marks = commitMarks?.wt === shown?.wt && commitMarks.base === shown.base ? commitMarks.marks : {};
  for (const row of $('commit-list').querySelectorAll('.cm')) {
    row.querySelector('.n-cmt')?.remove();
    const m = marks[row.dataset.hash];
    if (!m) continue;
    const kinds = Object.keys(m).sort((a, b) => SEVERITY[b] - SEVERITY[a]);
    const n = kinds.reduce((t, k) => t + m[k], 0);
    const mark = el('span', 'n n-cmt c-' + kinds[0], '\u25cf' + n);
    mark.title = `${plural(n, 'comment')} on lines this changed: ` + kinds.map((k) => `${m[k]} ${k}`).join(', ');
    // after the subject, as the file list has it after the name
    row.querySelector('.subj').after(mark);
  }
}

// git lists the boundary (commits outside base..HEAD with a child inside:
// the merge base, and what merges brought in from the base) after all the
// rest, so every merge from the base would draw a lane down to the end.
// Each goes right below its last child instead, closing that lane at once.
// With older commits listed the lanes go on into them anyway, and the
// boundary keeps its place among them (the server's order). The base's own
// commits end in the boundary like the review's: placed the same way.
function commitRows() {
  if (!commitList) return [];
  const rows = commitList.commits.filter((c) => !c.boundary);
  if (commitList.commits.some((c) => c.old)) return withLocal(commitList.commits.slice());
  const at = new Map(rows.map((c, i) => [c.hash, i]));
  const below = new Map();  // row index -> boundary commits to put after it
  for (const b of commitList.commits.filter((c) => c.boundary)) {
    let last = -1;
    for (const c of rows) if (c.parents.includes(b.hash)) last = Math.max(last, at.get(c.hash));
    if (!below.has(last)) below.set(last, []);
    below.get(last).push(b);
  }
  for (const i of [...below.keys()].sort((x, y) => y - x)) rows.splice(i + 1, 0, ...below.get(i));
  return withLocal(rows);
}

function withLocal(rows) {
  // As gitk draws local changes: nodes above HEAD, the untracked files on top.
  if (commitList.uncommitted)
    rows.unshift({ hash: 'uncommitted', parents: [commitList.head], subject: 'Uncommitted changes', refs: [] });
  if (commitList.untracked)
    rows.unshift({ hash: 'untracked', parents: [commitList.uncommitted ? 'uncommitted' : commitList.head],
                   subject: `Untracked files (${commitList.untracked})`, refs: [] });
  return rows;
}

const LOCAL = new Set(['uncommitted', 'untracked']);  // rows that are no commits

// Lane layout, newest first: each column waits for one commit (the parent
// of the commit above it in that column). A commit takes the column that
// waits for it; other columns waiting for it too end in it (a fork); its
// first parent continues the column, further parents (a merge) take free
// ones. Parents not in the list (beyond a cut) start no column. A line keeps
// its colour along its branch, not per column: a new line takes the next.
// A line from a faded commit (older than the base, or the base's own) is
// faded down to its parent, also where it passes rows that are not.
function commitGraph(rows) {
  const present = new Set(rows.map((r) => r.hash));
  const older = rows.some((r) => r.old);
  const lanes = [];  // {hash, color} or null
  let colors = 0;
  return rows.map((r) => {
    const before = lanes.map((l) => l && { ...l });
    let col = lanes.findIndex((l) => l && l.hash === r.hash);
    const color = col === -1 ? colors++ : lanes[col].color;
    if (col === -1) { col = lanes.indexOf(null); if (col === -1) col = lanes.length; }
    for (let i = 0; i < lanes.length; i++) if (lanes[i] && lanes[i].hash === r.hash) lanes[i] = null;
    const parents = r.boundary && !older ? [] : r.parents.filter((p) => present.has(p));
    const faded = isFaded(r);
    const outs = [];
    parents.forEach((p, n) => {
      let j = n === 0 ? col : lanes.findIndex((l) => l && l.hash === p);
      if (j === -1) {
        j = lanes.indexOf(null); if (j === -1) j = lanes.length;
        lanes[j] = { hash: p, color: n === 0 ? color : colors++, faded };
      } else if (n === 0) lanes[j] = { hash: p, color, faded };
      outs.push({ j, color: lanes[j].color, faded });
    });
    while (lanes.length && lanes[lanes.length - 1] == null) lanes.pop();
    return { col, color, before, outs };
  });
}

function svgEl(tag, attrs) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function drawGraph(svg, r, g, width, h) {
  svg.setAttribute('width', width);
  svg.setAttribute('height', h);
  const x = (i) => LANE_W / 2 + i * LANE_W, mid = h / 2;
  const paint = (c) => LANE_COLORS[c % LANE_COLORS.length];
  const line = (x1, y1, x2, y2, c, faded) => svg.append(svgEl('path', {
    d: x1 === x2 ? `M${x1} ${y1}V${y2}` : `M${x1} ${y1}C${x1} ${(y1 + y2) / 2} ${x2} ${(y1 + y2) / 2} ${x2} ${y2}`,
    stroke: paint(c), 'stroke-width': 1.5, fill: 'none', opacity: faded ? FADED : 1 }));
  g.before.forEach((l, i) => {
    if (l == null) return;
    if (l.hash === r.hash) line(x(i), 0, x(g.col), mid, l.color, l.faded);
    else line(x(i), 0, x(i), h, l.color, l.faded);
  });
  for (const o of g.outs) line(x(g.col), mid, x(o.j), h, o.color, o.faded);
  const color = LOCAL.has(r.hash) ? 'var(--note)' : paint(g.color);
  const dot = { cx: x(g.col), cy: mid, r: 3.5, 'stroke-width': 1.5 };
  // a faded dot on a disc of the row's background, so the lines do not
  // show through
  if (isFaded(r)) svg.append(svgEl('circle', { ...dot, fill: 'var(--row-bg)', stroke: 'var(--row-bg)' }));
  svg.append(svgEl('circle', { ...dot, fill: r.boundary ? 'var(--row-bg)' : color, stroke: color,
                               opacity: isFaded(r) ? FADED : 1 }));
}

function age(sec) {
  const d = Date.now() / 1000 - sec;
  for (const [n, unit] of [[31536000, 'y'], [2592000, 'mo'], [604800, 'w'], [86400, 'd'], [3600, 'h'], [60, 'm']])
    if (d >= n) return Math.floor(d / n) + unit;
  return 'now';
}

function renderCommits() {
  const box = $('commit-list');
  box.textContent = '';
  const rows = commitRows();
  const inside = commitList ? commitList.commits.filter((c) => !c.boundary && !isFaded(c)).length : 0;
  const cut = commitList?.commits.some((c) => c.cut);
  const theirs = commitList ? commitList.commits.filter((c) => c.onBase).length : 0;
  const theirsCut = commitList?.commits.some((c) => c.onBaseCut);
  $('on-base').title = (onBaseShown() ? 'Hide' : 'Show') + ` the commits on ${shown?.base || 'the base'} that HEAD does not have`
    + (onBaseShown() && commitList ? ` (${theirs ? theirs + (theirsCut ? '+, only the newest listed' : '') : 'none'})` : '');
  $('commits-count').textContent = commitList ? `(${inside}${cut ? '+' : ''})` : '';
  if (!commitList) return;
  const graph = commitGraph(rows);
  const lanes = Math.max(1, ...graph.map((g) => Math.max(g.before.length, g.col + 1, ...g.outs.map((o) => o.j + 1))));
  const width = lanes * LANE_W;
  // The whole review is one of the choices: the row stays highlighted
  // while it is shown, and picking it again goes back from a commit.
  const all = el('div', 'row cm all', [el('span', null), el('span', 'subj', 'All changes'),
                                       el('span', 'what', 'merge base to the working tree')]);
  all.firstChild.style.width = width + 'px';
  all.firstChild.style.flex = 'none';
  all.title = `The whole review: ${shown.base}'s merge base to the working tree, without the`
    + ' untracked files (shift-click "Untracked files" to add them)';
  all.dataset.hash = 'all';
  all.addEventListener('click', (e) => pickCommit('all', e.shiftKey));
  box.append(all);
  const made = rows.map((r) => {
    // sized once the row height is known; unsized, an svg is 300 x 150
    const svg = svgEl('svg', { class: 'graph', width: 0, height: 0 });
    const labels = (r.refs || []).map((ref) => {
      const lab = el('span', `ref-label k-${ref.kind}${ref.head ? ' head' : ''}`, ref.name);
      lab.title = `${ref.kind}${ref.head ? ' (checked out)' : ''}: ${ref.name}`;
      return lab;
    });
    const cls = 'row cm' + (r.boundary ? ' boundary' : '') + (r.old ? ' old' : '') + (r.onBase ? ' onbase' : '')
      + (LOCAL.has(r.hash) ? ' uncommitted' : '');
    const row = el('div', cls, [svg, ...(labels.length ? [el('span', 'refs', labels)] : []),
                                el('span', 'subj', r.subject),
                                el('span', 'age', r.time ? age(r.time) : '')]);
    row.dataset.hash = r.hash;
    row.title = r.hash === 'uncommitted' ? 'The tracked files of the working tree against HEAD'
      : r.hash === 'untracked' ? 'The files git does not track (and does not ignore), not part of "All changes"'
      : `${r.hash}\n${r.author} <${r.email}>\n${new Date(r.time * 1000).toLocaleString()}\n\n${r.subject}` +
        (r.body ? '\n\n' + r.body : '') +
        (r.boundary ? '\n\n(not between the base and HEAD: the merge base, or merged in from the base)' : '') +
        (r.old ? '\n\n(older than the base: not part of "All changes")' : '') +
        (r.onBase ? `\n\n(on ${shown.base}, not in HEAD: not part of "All changes")` : '');
    row.addEventListener('click', (e) => pickCommit(r.hash, e.shiftKey));
    row.addEventListener('contextmenu', (e) => { e.preventDefault(); commitMenu(r, e.clientX, e.clientY); });
    box.append(row);
    return { row, svg };
  });
  if (cut) box.append(el('div', 'cut', `only the newest ${inside} commits are listed`));
  paintMarks();
  if (!made.length) return markShownCommit();
  // Rows are as tall as the sidebar font makes them; the graph is drawn to
  // fit. A hidden pane has no heights: drawn when it is shown.
  const h = made[0].row.getBoundingClientRect().height;  // fractional, so lines meet
  commitsUndrawn = !h;
  if (!h) return markShownCommit();
  made.forEach(({ svg }, i) => drawGraph(svg, rows[i], graph[i], width, h));
  markShownCommit();
}

// The commits a range holds, as git counts them: reachable from the newer
// end but not from the older end's first parent ('all': from the merge base).
function rangeHashes(older, newer) {
  const byHash = new Map(commitRows().map((r) => [r.hash, r]));
  const reach = (start) => {
    const seen = new Set(), todo = start ? [start] : [];
    while (todo.length) {
      const h = todo.pop();
      if (seen.has(h)) continue;
      seen.add(h);
      const r = byHash.get(h);
      if (r) todo.push(...r.parents);
    }
    return seen;
  };
  // from the merge base: the commits since it, not the boundary ones
  const own = (h) => byHash.has(h) && !byHash.get(h).boundary && !isFaded(byHash.get(h));
  if (older === 'all') return new Set([...reach(newer)].filter(own));
  const stop = reach(byHash.get(older)?.parents[0]);
  return new Set([...reach(newer)].filter((h) => !stop.has(h) && byHash.has(h)));
}

function markShownCommit() {
  const box = $('commit-list');
  box.querySelectorAll('.cm.active').forEach((r) => r.classList.remove('active'));
  const c = current?.commit;
  if (!c) { box.querySelector('.cm.all')?.classList.add('active'); return; }
  const rows = [...box.querySelectorAll('.cm')];
  const [older, newer] = c.includes('..') ? c.split('..') : [c, c];
  const marked = c.includes('..') ? rangeHashes(older, newer) : new Set([c]);
  const hit = rows.filter((r) => marked.has(r.dataset.hash));
  hit.forEach((r) => r.classList.add('active'));
  hit[0]?.scrollIntoView({ block: 'nearest' });
  // an end listed after a refresh no more: rebased away, or HEAD moved past it
  const missing = !!commitList && ![older, newer].every((h) => rows.some((r) => r.dataset.hash === h));
  $('commit-shown').classList.toggle('gone', missing);
  if (missing) {
    const where = ['between the base and HEAD', olderCount() && 'among the older commits',
                   onBaseShown() && "among the base's own commits"].filter(Boolean).join(', nor ');
    const hidden = onBaseShown() ? '' : " (the base's own commits are hidden)";
    $('commit-shown').title = c === 'uncommitted' ? 'There are no uncommitted changes now'
      : c === 'untracked' ? 'There are no untracked files now'
      : c.includes('..') ? `An end of this range is no longer listed: not ${where}${hidden}`
      : `This commit is no longer listed: not ${where}${hidden}`;
  }
}

function showCommitShown(info) {
  const box = $('commit-shown'), text = $('commit-shown-text');
  box.hidden = !info;
  box.classList.remove('gone');
  text.textContent = '';
  if (!info) return;
  const end = (h) => ({ all: 'merge base', uncommitted: 'working tree', untracked: 'untracked' })[h] || h.slice(0, 7);
  const plus = (to) => (to === 'untracked' ? ' + untracked' : to === 'uncommitted' ? ' + uncommitted' : '');
  if (info.range && info.from === 'all' && info.to === 'untracked') {
    text.append('all changes + untracked');
    box.title = 'Showing the whole review with the untracked files';
  } else if (info.range && info.from === 'uncommitted') {
    text.append('uncommitted + untracked');
    box.title = 'Showing the working tree against HEAD, the untracked files included';
  } else if (info.range) {
    text.append(el('span', 'hash', `${end(info.from)}..${end(info.to)}`),
                ` ${plural(info.count, 'commit')}${plus(info.to)}`);
    box.title = `Showing ${plural(info.count, 'commit')} combined, from ${info.from === 'all' ? 'the merge base' : info.from}`
      + ` to ${info.to === 'untracked' ? 'the working tree with the untracked files'
               : info.to === 'uncommitted' ? 'the working tree' : info.to}`;
  } else if (info.hash.includes('..')) {
    text.append(el('span', 'hash', info.hash.split('..').map(end).join('..')));
  } else if (info.hash === 'uncommitted') {
    text.append('uncommitted changes');
    box.title = 'Showing the tracked files of the working tree against HEAD';
  } else if (info.hash === 'untracked') {
    text.append('untracked files');
    box.title = 'Showing only the untracked files';
  } else {
    text.append(el('span', 'hash', info.hash.slice(0, 7)), info.subject ? ' ' + info.subject : '');
    box.title = `Showing commit ${info.hash}${info.subject ? ': ' + info.subject : ''} against its first parent`;
  }
  markShownCommit();
}

// The review's last position in this worktree, kept while commits are shown.
function reviewPosition() {
  return current ? worktrees.get(current.wt)?.w.position : null;
}

// Click: one commit (again: back to all changes). Shift-click: every
// commit from the last clicked one to this one, both included; the older
// end is the one lower in the list. "All changes" counts as the lowest row
// (from the merge base), the untracked files and uncommitted changes as
// the top ones (the working tree, without and with the untracked files).
let commitAnchor = null;
// The header button: the next click picks one end (and shows that commit),
// the one after it the other end, which also ends the mode.
let rangeMode = null;  // null: off; 'first': waiting for the first end; 'second'
function setRangeMode(m) {
  rangeMode = m;
  $('range-mode').classList.toggle('on', !!m);
}

function pickCommit(hash, shift) {
  if (!current) return;
  // a second click on the shown commit goes back to all changes, but not
  // while it is the first end of a range
  const toggle = !rangeMode;
  if (rangeMode === 'first') rangeMode = 'second';
  else if (rangeMode === 'second') {
    if (hash === commitAnchor) return;  // the same end again: still waiting for the other
    setRangeMode(null);
    shift = true;
  }
  if (shift && commitAnchor && commitAnchor !== hash) {
    const order = [...commitRows().map((r) => r.hash), 'all'];
    const [a, b] = [order.indexOf(commitAnchor), order.indexOf(hash)];
    if (a !== -1 && b !== -1) {
      const older = order[Math.max(a, b)], newer = order[Math.min(a, b)];
      // The working tree has no parent to start from, so it is only ever the
      // newer end, except the uncommitted changes up to the untracked files.
      // From the merge base to the working tree is "All changes" itself.
      // An older commit comes before the merge base, so not after "All changes".
      // The base's own commits are not on the way from the merge base to HEAD
      // or the working tree: a range from them ends on them.
      const byHash = new Map(commitRows().map((r) => [r.hash, r]));
      const onBase = (h) => !!byHash.get(h)?.onBase;
      const ok = onBase(older) !== onBase(newer) ? false
        : older === 'uncommitted' ? newer === 'untracked'
        : !(older === 'all' && (newer === 'uncommitted' || isFaded(byHash.get(newer))));
      if (ok) return selectCommit(`${older}..${newer}`, true);
    }
  }
  commitAnchor = hash;
  const target = hash === 'all' || (toggle && current.commit === hash) ? null : hash;
  selectCommit(target, true);
  // back on all changes, a shift-click from there adds the untracked files
  if (!target) commitAnchor = 'all';
}

// keep: a pick within the commits pane, which keeps its anchor and range mode
function selectCommit(hash, keep = false) {
  if (!hash && !keep) { commitAnchor = null; setRangeMode(null); }
  if (!current) return;
  load(current.wt, current.base, null, hash ? null : reviewPosition(), hash);
}

// What the list shows; settings and positions are not part of it.
function listSignature(projects) {
  return JSON.stringify(projects.map((p) => [p.group, p.name, p.path, p.error,
    p.worktrees.map((w) => [w.path, w.branch, w.head, w.detached])]));
}

// Agents add and remove worktrees and switch branches; the list follows,
// every PROJECTS_MS while the page is visible and when it comes back.
let refreshing = false;
async function refreshProjects(now = false) {
  if (refreshing || (!now && (document.hidden || pushed))) return;
  refreshing = true;
  try {
    const projects = await api('/api/projects', {});
    if (listSignature(projects) !== projectsSig) renderProjects(projects);
  } catch { /* transient; the next refresh retries */ }
  refreshing = false;
}

// The server tells its version (its start and the page files' times); a
// different one than at page load means this page's code may not fit.
// The change poll carries the version too, so with a worktree open a
// restart shows within POLL_MS; this check covers the page without one.
let serverVersion = null;
function noteVersion(v) {
  if (serverVersion === null) serverVersion = v;
  else if (v !== serverVersion) $('updated').hidden = false;
}

async function checkVersion() {
  if (document.hidden || pushed) return;
  try { noteVersion((await api('/api/version', {})).version); }
  catch { /* transient, or the server is restarting */ }
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
  return { base: p.defaultBase, ...(p.settings || {}), ...(w.settings || {}) };
}

async function saveSettings(wt, base) {
  const st = { base };
  const entry = worktrees.get(wt);
  if (entry) entry.w.settings = st;
  try {
    // the server decides what the project keeps (not a commit as its base)
    const r = await post('/api/settings', { wt, ...st });
    if (entry) entry.p.settings = r.project;
  } catch (e) { flash('settings not saved: ' + e.message); }
}

// --loading a diff -------------------------------------------------------

// commit: a single commit's diff (a hash, or 'uncommitted') instead of the
// whole review; null for the review.
// `since`: the id of the diff whose lines the new one is compared with, to
// mark what a reload brought (only the server's cache still has it).
async function load(wt, base, defaultBase, restore, commit = null, since = null) {
  const { gen, state } = startLoad(wt, base, defaultBase, restore, commit);
  showProgress(`loading diff against ${base} ...`);
  const t0 = performance.now();
  let d;
  try {
    d = await api('/api/diff', { ...state, reindent: $('reindent').checked ? '1' : '0',
                                 ...(since ? { since } : {}) });
  } catch (e) {
    if (gen === wordGen) {
      showProgress('error: ' + e.message);
      $('base').classList.add('bad');
      $('base').title = e.message;
    }
    return;
  }
  if (gen !== wordGen) return;
  await render(d, gen, state, since, restore, t0);
}

// Thrown while drawing, an error would leave "rendering" up for good.
async function render(d, gen, state, since, restore, t0, k = null) {
  try {
    await showDiff(d, gen, state, since, restore, t0, k);
  } catch (e) {
    showProgress('error showing the diff: ' + e.message + '\n(reloading the page may help)');
    throw e;
  }
}

// What load() and showKept() do before the diff is there.
function startLoad(wt, base, defaultBase, restore, commit) {
  if (shown && shown.wt !== wt) {
    flushPosition();
    keep();
  }
  // Another view: the notes about this one would stand until it renders, or
  // for good if it fails to load. A reload or re-render replaces them.
  if (!shown || shown.wt !== wt || shown.base !== base || (shown.commit || null) !== commit) {
    $('news').hidden = true;
    $('outside').hidden = true;
  }
  const gen = ++wordGen;
  if (current?.wt !== wt || current?.base !== base) commitList = null;
  current = { wt, base, defaultBase: defaultBase || current?.defaultBase || base, commit };
  // a single commit restored from the address is where a shift-click starts
  if (commit && !commit.includes('..') && !commitAnchor) commitAnchor = commit;
  // on the whole review, a shift-click goes from "All changes"
  if (!commit) commitAnchor = 'all';
  const state = { wt, base, ...(commit ? { commit } : {}) };
  setHash({ ...state, ...(restore || {}) });
  showCommitShown(commit ? { hash: commit } : null);
  document.querySelectorAll('.wt').forEach((r) => r.classList.toggle('active', r.dataset.wt === wt));
  $('base').value = base;
  updateCrumb();
  $('changed').hidden = true;
  $('changed-what').textContent = '';
  changesFp = null;
  changesAt = 0;
  $('removed').hidden = true;
  shown = null;
  return { gen, state };
}

// --worktrees switched away from ------------------------------------------
//
// Each keeps its diff as it was left, with its place, an open comment
// editor and the marks of its last reload, and shows it again on switching
// back: a worktree an agent changed meanwhile says so in the banner, and
// changes only when reload is clicked. The server keeps each worktree's
// last document for the comments and word diff of the kept one.

const KEEP_LINES = 1000000;  // all kept diffs together; the oldest go first
const kept = new Map();      // worktree -> what keep() saved, oldest first

function keep() {
  if (!doc || !shown) return;
  kept.delete(shown.wt);
  kept.set(shown.wt, { d: doc, state: shown, defaultBase: current.defaultBase, pos: viewPosition(),
                       editing, words: wordsDone ? wordsByLine : null, news: newsShown,
                       reindent: $('reindent').checked, viewed, viewedStamp });
  let total = 0;
  for (const k of kept.values()) total += k.d.kinds.length;
  for (const [wt, k] of kept) {
    if (total <= KEEP_LINES) break;
    kept.delete(wt);
    total -= k.d.kinds.length;
  }
}

async function showKept(wt) {
  const k = kept.get(wt);
  kept.delete(wt);
  const before = wordGen;
  let ok = false;
  try { ok = (await api('/api/kept', { wt, id: k.d.id })).kept; } catch { /* load it anew */ }
  // something else loaded meanwhile; kept again unless that was this worktree
  if (wordGen !== before) { if (current?.wt !== wt) kept.set(wt, k); return; }
  const { base, commit = null } = k.state;
  // The server dropped it (it restarted, or too many big diffs since), or
  // re-indenting was switched since: as reload does, with the changes since
  // marked when it still can.
  if (!ok || k.reindent !== $('reindent').checked)
    return load(wt, base, k.defaultBase, k.pos, commit, commit ? null : k.d.id);
  const { gen, state } = startLoad(wt, base, k.defaultBase, k.pos, commit);
  await render(k.d, gen, state, null, k.pos, performance.now(), k);
}

// `k`: a kept diff shown again, see keep().
async function showDiff(d, gen, state, since, restore, t0, k = null) {
  const { wt, base, commit } = state;
  const t1 = performance.now();
  showProgress(`rendering ${d.files.length} files, ${d.kinds.length} lines ...`);
  await nextFrame();
  const wasViewed = since && doc ? viewedPaths() : new Set();
  doc = d;
  shown = state;
  watchShown();
  showCommitShown(d.commit);
  $('base').classList.remove('bad');
  $('base').title = 'Any branch, tag or commit; click for a list';
  // only a base that loaded is worth remembering; a kept one was, and may
  // since have been changed in another tab
  if (!k) saveSettings(wt, base);
  fileStarts = d.files.map((f) => f.start);
  let maxNo = 1;
  for (let i = 0; i < d.oldNo.length; i++) maxNo = Math.max(maxNo, d.oldNo[i], d.newNo[i]);
  numWidth = String(maxNo).length;
  wordsByLine = k?.words || new Map();
  wordsDone = !!k?.words;
  editing = null;
  outsideMarks = new Map();
  $('outside').hidden = true;
  applyComments({ comments: [], elsewhere: 0, stamp: null });
  decoWindow = [0, 0];
  activeFile = -1;
  pinned = null;
  editor.updateOptions({ lineNumbersMinChars: numWidth * 2 + 2 });
  editor.getModel().setValue(d.text);
  editor.setScrollTop(0);
  if (restore) scrollToPosition(restore);
  initViewed(d);
  // marks made after the diff loaded, not those it came with
  if (k) ({ viewed, viewedStamp } = k);
  renderFiles();
  markHeadersInMinimap();
  // files still in the diff that were viewed and no longer are
  const nowViewed = viewedPaths(), inDiff = new Set(d.files.map((f) => f.path));
  if (k) applyNews(k.news.news, k.news.since, k.news.unviewed);
  else applyNews(d.news, since, [...wasViewed].filter((p) => inDiff.has(p) && !nowViewed.has(p)).length);
  markNewFiles(newsShown.news);
  showViewed();
  updateDecorations(true);
  if (d.files.length) hideProgress();
  else if (commit === 'uncommitted') showProgress('no uncommitted changes');
  else if (commit) showProgress('this commit changes no files (against its first parent)');
  else showProgress(`no changes against ${base} (merge-base ${d.mergeBase})`);
  await nextFrame();
  const t2 = performance.now();
  Object.assign(timing, { fetchMs: Math.round(t1 - t0), renderMs: Math.round(t2 - t1),
                          serverGit: d.timing.git_ms, serverDoc: d.timing.doc_ms,
                          lines: d.kinds.length, files: d.files.length,
                          bytes: d.text.length });
  $('mergebase').textContent = '';
  $('mergebase').append('merge-base ' + d.mergeBase + '  ', ...aheadBehind(d.ahead, d.behind));
  const mbc = d.mergeBaseCommit;
  $('mergebase').title = `HEAD is ${d.ahead} commit${d.ahead === 1 ? '' : 's'} ahead of ${base}`
    + (d.behind ? `, which has ${d.behind} commit${d.behind === 1 ? '' : 's'} not in HEAD` : '')
    + (mbc ? `\n\nmerge base ${d.mergeBase}: ${mbc.subject}\n${mbc.author}, `
             + `${new Date(mbc.time * 1000).toLocaleString()} (${age(mbc.time) === 'now' ? 'just now' : age(mbc.time) + ' ago'})` : '');
  const add = d.files.reduce((s, f) => s + f.add, 0), del = d.files.reduce((s, f) => s + f.del, 0);
  $('stats').textContent = `${d.files.length} files  +${add} -${del}  ${d.kinds.length} lines`;
  if (k) setTiming(`kept diff, render ${timing.renderMs} ms`);
  else setTiming(`git ${d.timing.git_ms} ms, parse ${d.timing.doc_ms} ms, ` +
                 `fetch ${timing.fetchMs} ms, render ${timing.renderMs} ms`);
  onScroll();
  if (!wordsDone) fetchWords(gen);
  loadCommits(gen);
  await loadComments(gen);
  if (k && gen === wordGen) {
    // not for a comment deleted meanwhile: there would be no editor to show
    if (k.editing && !editing && (!k.editing.key || comments.some((c) => keyStr(c) === keyStr(k.editing)))) {
      editing = k.editing;
      renderZones(false);
    }
    // the banner if the worktree changed while away, the marks if viewed.json did
    pollChanges();
  }
}

// --what a reload brought ---------------------------------------------------

let newsSince = null;          // id of the diff the marks compare with, see load()
let newsShown = {};            // applyNews' arguments, for keep()

// Returns the summary to show, if there is something new to say.
// The note in the top bar stays until the next load, or its x clears the
// marks with it.
function applyNews(news, since, unviewed = 0) {
  newsShown = { news, since, unviewed };
  newsSince = news && !news.missing ? since : null;
  $('news').hidden = !news;
  $('news').classList.toggle('missing', !!news?.missing);
  if (!news || news.missing) {
    newsDecos.set([]);
    $('news-text').textContent = news ? 'no marks for this reload' : '';
    $('news').title = 'The server no longer has the diff before the reload (it restarted, or that'
      + ' was too many loads ago), so there is nothing to compare with';
    return;
  }
  const T = monaco.editor;
  const color = getComputedStyle(document.documentElement).getPropertyValue('--new').trim();
  const marks = { overviewRuler: { color, position: T.OverviewRulerLane.Right },
                  minimap: { color, position: T.MinimapPosition.Gutter } };
  const list = news.runs.map(([a, b]) => ({ range: new monaco.Range(a, 1, b, 1),
                                            options: { isWholeLine: true, marginClassName: 'm-new', ...marks } }));
  for (const [lines, cls] of [[news.goneAfter, 'gone-after'], [news.goneBefore, 'gone-before']])
    for (const ln of lines)
      list.push({ range: new monaco.Range(ln, 1, ln, 1),
                  options: { isWholeLine: true, className: cls, marginClassName: cls, ...marks } });
  newsDecos.set(list);
  $('news-text').replaceChildren(...newsSummary(news));
  if (unviewed) $('news-text').append(`; ${plural(unviewed, 'viewed file')} changed`);
  $('news').title = 'Marked in the diff, the scrollbar, the minimap and the file list; the x clears the marks';
}

function clearNews() {
  applyNews(null, null);
  document.querySelectorAll('#files .row.new').forEach((r) => {
    r.classList.remove('new');
    r.title = r.title.replace(NEW_TIP, '');
  });
}

const NEW_TIP = '\nchanged since the last load';

// The folders above a marked file are marked too, so that a collapsed one
// still shows it.
function markNewFiles(news) {
  const paths = new Set(news?.paths || []);  // none when missing
  doc.files.forEach((f, i) => {
    if (!paths.has(f.path)) return;
    fileRows[i].classList.add('new');
    fileRows[i].title += NEW_TIP;
    for (const d of fileDirs[i]) d.row.classList.add('new');
  });
}

const plural = (k, word) => `${k} ${word}${k === 1 ? '' : 's'}`;
// [a, b, c] -> [a, ', ', b, ', ', c]
const joined = (parts) => parts.flatMap((p, i) => (i ? [', ', p] : [p]));

// nodes for the note: the counts coloured, the rest plain
function newsSummary(n) {
  if (!n.files && !n.filesGone.length) return ['reload: the diff is the same'];
  const parts = [];
  if (n.lines) parts.push(el('span', 'hi', plural(n.lines, 'new changed line')));
  if (n.gone) parts.push(el('span', 'hi', `${n.gone} gone`));
  const out = ['reload: ', ...joined(parts)];
  // a file without + and - lines (binary, empty, renamed) has no lines to count
  if (n.files) out.push(parts.length ? ` in ${plural(n.files, 'file')}` : el('span', 'hi', plural(n.files, 'changed file')));
  if (n.filesGone.length) out.push(`${out.length > 1 ? '; ' : ''}${plural(n.filesGone.length, 'file')} no longer changed`);
  return out;
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

// 'g' is not a server kind: the second line of a file's header band, which
// is the file's first hunk header (or the empty line of a hunkless file).
// Only hunk headers further down are drawn as plain separated lines.
// 'A' and 'R' are not server kinds either: lines added or removed whole,
// tinted stronger than an edited line ('a', 'r'), whose changed words carry
// the bright tint; else a new line next to an edited one reads as the
// edited line's unchanged part. An added line is an edited one only if its
// run of changed lines removes some too and the word diff paired it, which
// the word ranges tell (a [0, 0] entry for a line it left unpaired).
const LINE_CLASS = { a: 'l-add', r: 'l-rem', A: 'l-add-all', R: 'l-rem-all',
                     f: 'l-file', g: 'l-file2', h: 'l-head', k: 'l-commit' };
const MARGIN_CLASS = { f: 'l-file', g: 'l-file2', h: 'l-head' };
const decoKind = (ln) => {
  const k = doc.kinds[ln - 1];
  if (k === 'a' || k === 'r')
    return mixedRuns()[ln - 1] && wordsByLine.get(ln)?.[0] !== 0 ? k : k.toUpperCase();
  return k === 'h' && doc.kinds[ln - 2] === 'f' ? 'g' : k;
};

// Per line, 1 where its run of changed lines both adds and removes. A "\ No
// newline at end of file" line ('m') can sit inside a run.
function mixedRuns() {
  if (doc.mixed) return doc.mixed;
  const kinds = doc.kinds, n = kinds.length;
  const mixed = new Uint8Array(n);
  for (let i = 0; i < n;) {
    let j = i, add = false, rem = false;
    for (; j < n && 'arim'.includes(kinds[j]); j++) {
      add ||= kinds[j] === 'a';
      rem ||= kinds[j] === 'r';
    }
    if (add && rem) mixed.fill(1, i, j);
    i = Math.max(j, i + 1);
  }
  return (doc.mixed = mixed);
}

// Unlike the line backgrounds these cover the whole document (one per
// file), since the minimap shows all of it.
function markHeadersInMinimap() {
  const list = [];
  for (const f of doc.files) {
    // Monaco draws a section label on a box of 70% minimap background and
    // offers no option to change it; colouring the rows underneath tints
    // that box, which keeps the label and still marks the file.
    // One decoration per row: Monaco (0.52 and 0.57 alike) does not paint
    // a multi-line minimap decoration evenly across its rows.
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
  let runStart = from, runKind = decoKind(from);
  const flush = (end) => {
    const cls = LINE_CLASS[runKind];
    if (cls) list.push({ range: new monaco.Range(runStart, 1, end, 1),
                         options: { isWholeLine: true, className: cls,
                                    marginClassName: MARGIN_CLASS[runKind] } });
    // an edited line's text dimmed too, its changed words (w-add, w-rem)
    // in full colour
    if (runKind === 'a' || runKind === 'r')
      list.push({ range: new monaco.Range(runStart, 1, end, Infinity),
                  options: { inlineClassName: 'e-' + runKind } });
  };
  for (let ln = from + 1; ln <= to; ln++) {
    const k = decoKind(ln);
    // File and hunk headers are runs of their own: each draws its line
    // above, and an empty file's header is directly followed by the next.
    if (k !== runKind || k === 'f' || k === 'h') { flush(ln - 1); runStart = ln; runKind = k; }
  }
  flush(to);
  decos.set(list);
  updateWordDecorations();
  shadeViewed();
}

function updateWordDecorations() {
  const [from, to] = decoWindow;
  const list = [];
  for (let ln = from; ln <= to; ln++) {
    const r = wordsByLine.get(ln);
    if (!r) continue;
    const cls = doc.kinds[ln - 1] === 'a' ? 'w-add' : 'w-rem';
    for (let i = 0; i < r.length; i += 2)
      if (r[i]) list.push({ range: new monaco.Range(ln, r[i], ln, r[i + 1]), options: { inlineClassName: cls } });
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
    // A block's lines can run past the window it starts in, so what is
    // redrawn goes by the lines that came.
    let shown = false, lone = false;
    for (let i = 0; i < flat.length; i += 3) {
      let r = wordsByLine.get(flat[i]);
      if (!r) wordsByLine.set(flat[i], (r = []));
      r.push(flat[i + 1], flat[i + 2]);
      if (flat[i] >= decoWindow[0] && flat[i] <= decoWindow[1]) {
        shown = true;
        lone ||= flat[i + 1] === 0;
      }
    }
    if (lone) updateDecorations(true);
    else if (shown) updateWordDecorations();
  }
  wordsDone = true;
  timing.wordsMs = Math.round(performance.now() - t0);
}

// --viewed marks -----------------------------------------------------------
//
// Kept per hunk in the worktree's .gitr/viewed.json under a key of the
// hunk's content (server.py hunk_key), so a hunk an agent changed comes back
// unviewed while the rest of its file stays viewed. A file is viewed when
// all its hunks are.

let viewed = new Set();   // keys of the doc's hunks that are viewed
let viewedStamp = null;   // viewed.json mtime they were read at
let fileHunks = [];       // per file, the indexes of its hunks in doc.hunks
let viewedBoxes = new Map();  // header line -> 'all' | 'some' | 'none' of its box

function initViewed(d) {
  viewed = new Set(d.viewed);
  viewedStamp = d.viewedStamp;
  fileHunks = d.files.map(() => []);
  let fi = 0;
  d.hunks.forEach(([start], h) => {
    while (fi + 1 < d.files.length && d.files[fi + 1].start <= start) fi++;
    fileHunks[fi].push(h);
  });
}

// 'all', 'some' or 'none' of the hunks of these files
function viewedState(files) {
  let on = 0, n = 0;
  for (const i of files) for (const h of fileHunks[i]) { n++; if (viewed.has(doc.hunks[h][2])) on++; }
  return !n || !on ? 'none' : on === n ? 'all' : 'some';
}

const viewedPaths = () => new Set(doc.files.filter((f, i) => viewedState([i]) === 'all').map((f) => f.path));

function viewedBox(files) {
  const b = el('span', 'vw');
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    const fs = files();
    markViewed(fs.flatMap((i) => fileHunks[i]), viewedState(fs) !== 'all');
  });
  return b;
}

// a click on one of the boxes showViewed puts in the diff
function toggleViewedAt(ln) {
  const fi = fileOf(ln);
  if (doc.kinds[ln - 1] === 'f') {
    markViewed(fileHunks[fi], viewedState([fi]) !== 'all');
  } else {
    const h = hunkAt(ln);
    if (h >= 0) markViewed([h], !viewed.has(doc.hunks[h][2]));
  }
}

function hunkAt(ln) {
  const h = upperBound(doc.hunks.map((x) => x[0]), ln) - 1;
  return h >= 0 && ln <= doc.hunks[h][1] ? h : -1;
}

let viewedSaving = 0;  // marks on their way to the server, see pollChanges

async function markViewed(hunks, on) {
  // shown is null while another diff loads over this one
  if (!doc || !shown || !hunks.length) return;
  const keys = hunks.map((h) => doc.hunks[h][2]);
  for (const k of keys) if (on) viewed.add(k); else viewed.delete(k);
  showViewed();
  const gen = wordGen;
  viewedSaving++;
  try {
    const r = await post('/api/viewed', { wt: shown.wt, keys, viewed: on });
    if (gen === wordGen) viewedStamp = r.viewedStamp;
  } catch (e) {
    flash('could not save: ' + e.message);
    viewedStamp = null;  // the next poll reads back what is stored
  } finally {
    viewedSaving--;
  }
}

// viewed.json changed outside this page (another tab, or this one's
// earlier write whose stamp a poll saw first)
async function reloadViewed(gen) {
  if (!shown) return;
  let r;
  try { r = await api('/api/viewed', { wt: shown.wt }); } catch { return; }
  // a mark made meanwhile is newer than this answer; the poll after it reads again
  if (gen !== wordGen || viewedSaving) return;
  const stored = new Set(r.viewed);
  viewed = new Set(doc.hunks.map((h) => h[2]).filter((k) => stored.has(k)));
  viewedStamp = r.viewedStamp;
  showViewed();
}

function showViewed() {
  const T = monaco.editor;
  const color = getComputedStyle(document.documentElement).getPropertyValue('--viewed').trim();
  const ruler = { overviewRuler: { color, position: T.OverviewRulerLane.Left } };
  const list = [], model = editor.getModel();
  doc.hunks.forEach(([a, b, key]) => {
    if (viewed.has(key))
      list.push({ range: new monaco.Range(a, 1, b, model.getLineMaxColumn(b)), options: { inlineClassName: 'vw-dim', ...ruler } });
  });
  // A box in the gutter of each file header and (in a file of several)
  // hunk header, where other lines show the + that adds a comment on
  // hover: clicked to mark it while reading the diff.
  viewedBoxes = new Map();
  const box = (ln, st) => {
    viewedBoxes.set(ln, st);
    list.push({ range: new monaco.Range(ln, 1, ln, 1), options: { linesDecorationsClassName: 'vw-gut vw-' + st } });
  };
  let all = 0;
  doc.files.forEach((f, i) => {
    const st = viewedState([i]);
    fileRows[i].dataset.viewed = st;
    box(f.start, st);
    if (fileHunks[i].length > 1)
      for (const h of fileHunks[i]) box(doc.hunks[h][0], viewed.has(doc.hunks[h][2]) ? 'all' : 'none');
    if (st === 'all') {
      all++;
      list.push({ range: new monaco.Range(f.start, 1, f.start, model.getLineMaxColumn(f.start)), options: { inlineClassName: 'vw-dim' } });
    }
  });
  for (const d of dirEntries) d.row.dataset.viewed = viewedState(d.files);
  viewedDecos.set(list);
  shadeViewed();
  const n = doc.files.length;
  $('files-count').textContent = !n ? '' : all ? `(${all}/${n})` : `(${n})`;
  $('files-count').title = all ? `${all} of ${plural(n, 'file')} viewed` : '';
  $('viewed-all').parentElement.dataset.viewed = viewedState(doc.files.map((f, i) => i));
  $('viewed-all').hidden = !n;
}

// The minimap cannot dim its text; a grey shade over the rows does instead.
// One decoration per row (as for the file headers), so only in the window
// around the view the line backgrounds cover (updateDecorations), which
// holds more than the minimap shows: a diff of 200k viewed lines would take
// a quarter of a second per click otherwise.
function shadeViewed() {
  const [from, to] = decoWindow;
  const shade = { minimap: { color: VIEWED_MINIMAP, position: monaco.editor.MinimapPosition.Inline } };
  const list = [];
  for (const [a, b, key] of doc.hunks) {
    if (b < from || a > to || !viewed.has(key)) continue;
    for (let ln = Math.max(a, from); ln <= Math.min(b, to); ln++)
      list.push({ range: new monaco.Range(ln, 1, ln, 1), options: shade });
  }
  shadeDecos.set(list);
}

// The next (dir 1) or previous (-1) hunk not viewed from the top of the
// view, going round at the ends.
function jumpUnviewed(dir) {
  if (!doc) return;
  const top = editor.getVisibleRanges()[0]?.startLineNumber || 1;
  const open = doc.hunks.map((h, i) => i).filter((i) => !viewed.has(doc.hunks[i][2]));
  if (!open.length) return flash('all viewed');
  // the top of the view sits on the header of a file it jumped to
  const at = (i) => { const s = doc.hunks[i][0]; return fileHunks[fileOf(s)][0] === i ? fileStarts[fileOf(s)] : s; };
  const h = dir > 0 ? open.find((i) => at(i) > top) ?? open[0]
    : open.findLast((i) => at(i) < top) ?? open[open.length - 1];
  editor.setScrollTop(editor.getTopForLineNumber(at(h)));
}

const fileOf = (ln) => Math.max(0, upperBound(fileStarts, ln) - 1);

// --file list ------------------------------------------------------------

// A tree like gitr's (and VS Code's compact folders): a directory holding
// only one subdirectory shares its row ("web/static"), and siblings are in
// diff order, so the list reads in the order the diff scrolls.
function closeButton(c) {
  const b = el('button', 'cl-del');
  b.title = 'Delete this comment';
  b.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8.70701 8.00001L12.353 4.35401C12.548 4.15901 12.548 3.84201 12.353 3.64701C12.158 3.45201 11.841 3.45201 11.646 3.64701L8.00001 7.29301L4.35401 3.64701C4.15901 3.45201 3.84201 3.45201 3.64701 3.64701C3.45201 3.84201 3.45201 4.15901 3.64701 4.35401L7.29301 8.00001L3.64701 11.646C3.45201 11.841 3.45201 12.158 3.64701 12.353C3.74501 12.451 3.87301 12.499 4.00101 12.499C4.12901 12.499 4.25701 12.45 4.35501 12.353L8.00101 8.70701L11.647 12.353C11.745 12.451 11.873 12.499 12.001 12.499C12.129 12.499 12.257 12.45 12.355 12.353C12.55 12.158 12.55 11.841 12.355 11.646L8.70901 8.00001H8.70701Z"/></svg>';
  b.addEventListener('click', (e) => { e.stopPropagation(); saveComment(c, '', c.kind); });
  return b;
}

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
          viewedBox(() => [i]),
        ]);
        row.title = f.oldPath && 'RC'.includes(f.status) ? `${f.oldPath} -> ${f.path}` : f.path;
        row.addEventListener('click', () => jumpToFile(i));
        row.addEventListener('contextmenu', (e) => { e.preventDefault(); fileMenu(i, e.clientX, e.clientY); });
        into.append(row);
        fileRows[i] = row;
        fileDirs[i] = ancestors;
        for (const d of ancestors) d.files.push(i);
        continue;
      }
      let name = c.name, d = c.dir;
      while (!d.files.length && d.dirs.size === 1) {
        const [sub, subNode] = d.dirs.entries().next().value;
        name += '/' + sub;
        d = subNode;
      }
      const path = prefix + name;
      const entry = { path, files: [] };
      const chev = el('span', 'chev');
      const row = el('div', 'row dir', [...guides(depth), chev, el('span', 'grow', name),
                                        viewedBox(() => entry.files)]);
      row.title = path;
      const kids = el('div', 'kids');
      Object.assign(entry, { row, kids });
      dirEntries.push(entry);
      // Only the chevron: a click meant for the viewed box at the other end
      // of the row would otherwise fold the folder when it missed.
      entry.toggle = () => {
        if (collapsed.has(path)) collapsed.delete(path); else collapsed.add(path);
        showCollapsed(entry, collapsed.has(path));
        setActive(activeFile, true);
      };
      chev.addEventListener('click', entry.toggle);
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        dirMenu(entry, collapsed.has(path), e.clientX, e.clientY);
      });
      showCollapsed(entry, collapsed.has(path));
      into.append(row, kids);
      walk(d, depth + 1, path + '/', kids, [...ancestors, entry]);
    }
  };
  dirEntries = [];
  const frag = document.createDocumentFragment();
  walk(buildTree(doc.files), 0, '', frag, []);
  box.append(frag);
}

function showCollapsed(entry, isCollapsed) {
  entry.row.classList.toggle('collapsed', isCollapsed);
  entry.kids.hidden = isCollapsed;
}

let dirEntries = [];
function collapseAll(on) {
  if (!shown) return;
  const collapsed = collapsedDirs.get(shown.wt);
  for (const d of dirEntries) {
    if (on) collapsed.add(d.path); else collapsed.delete(d.path);
    showCollapsed(d, on);
  }
  setActive(activeFile, true);
}

function revealFile(i) {
  setView('files');
  const collapsed = collapsedDirs.get(shown.wt);
  for (const d of fileDirs[i]) { collapsed.delete(d.path); showCollapsed(d, false); }
  setActive(activeFile, true);
  const row = fileRows[i];
  row.scrollIntoView({ block: 'center' });
  row.classList.remove('reveal');
  void row.offsetWidth;  // restarts the animation on a second reveal
  row.classList.add('reveal');
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
  const f = doc.files[fileOf(top)];
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
  if (shown.commit) return;
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
    // A commit's view has positions of its own, not the review's.
    if (shown.commit) return;
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
    const on = e.target.type === T.GUTTER_LINE_DECORATIONS || e.target.type === T.GUTTER_LINE_NUMBERS ||
      e.target.type === T.CONTENT_TEXT || e.target.type === T.CONTENT_EMPTY;
    showHover(on ? e.target.position?.lineNumber : null);
  });
  editor.onMouseLeave(() => showHover(null));
  // A header's gutter is also where Monaco folds it, on any click there
  // (its chevrons hidden or not). Clicks in the gutter of a file or hunk
  // header are taken before Monaco sees them: on the viewed box they mark,
  // elsewhere they do nothing, rather than fold with nothing to show how.
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])
    editor.getDomNode().addEventListener(type, (e) => {
      if (!doc || !e.target.closest?.('.margin')) return;
      const t = editor.getTargetAtClientPoint(e.clientX, e.clientY);
      const ln = t?.position?.lineNumber;
      if (t?.type !== T.GUTTER_LINE_DECORATIONS || !'fh'.includes(doc.kinds[ln - 1] || 'x')) return;
      e.stopPropagation();
      e.preventDefault();
      // pointerdown: its preventDefault keeps the browser from sending mousedown
      if (type !== 'pointerdown' || e.button !== 0) return;
      if (e.target.classList.contains('vw-gut') && viewedBoxes.has(ln)) toggleViewedAt(ln);
    }, true);
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
  addEventListener('mousedown', (e) => { if (!$('cmenu').contains(e.target)) hideMenu(); });
  addEventListener('keydown', (e) => { if (e.key === 'Escape') hideMenu(); });
  editor.onDidScrollChange(hideMenu);
  $('prev-comment').addEventListener('click', () => jumpComment(-1));
  $('next-comment').addEventListener('click', () => jumpComment(1));
  $('copy-agent').addEventListener('click', copyForAgent);
  $('clear-comments').addEventListener('click', clearComments);
  $('clear-all').addEventListener('click', clearComments);
}

// the + that adds a comment, in the gutter of the line under the mouse
function showHover(ln) {
  const show = ln && commentable(ln) && !commentAt(ln);
  hoverDecos.set(show ? [{ range: new monaco.Range(ln, 1, ln, 1), options: { linesDecorationsClassName: 'c-add' } }] : []);
}

function commentAt(ln) { return comments.find((c) => c.line === ln && !c.orphan); }

// `outside`: review.json was written by something else (another tab,
// `gitr comments`, an agent), so say what changed and mark it.
async function loadComments(gen, outside = false) {
  try {
    const r = await api('/api/comments', { wt: shown.wt, id: doc.id });
    if (gen !== wordGen) return;
    if (outside) noteOutsideChanges(r.comments);
    applyComments(r);
  } catch (e) { flash('comments: ' + e.message); }
}

// keyStr -> 'new' | 'edited', for the comments the last outside change
// brought; kept until the next one, or a new diff.
let outsideMarks = new Map();
function noteOutsideChanges(next) {
  const before = new Map(comments.map((c) => [keyStr(c), c]));
  const marks = new Map();
  for (const c of next) {
    const old = before.get(keyStr(c));
    before.delete(keyStr(c));
    if (!old) marks.set(keyStr(c), 'new');
    else if (old.text !== c.text || old.kind !== c.kind) marks.set(keyStr(c), 'edited');
  }
  const counts = { new: 0, edited: 0 };
  for (const m of marks.values()) counts[m]++;
  const parts = [];
  if (counts.new) parts.push(el('span', 'hi', `${counts.new} added`));
  if (counts.edited) parts.push(el('span', 'hi', `${counts.edited} edited`));
  if (before.size) parts.push(el('span', 'hi', `${before.size} removed`));
  if (!parts.length) return;  // rewritten as it was
  outsideMarks = marks;
  $('outside-text').replaceChildren('comments changed outside: ', ...joined(parts));
  $('outside').hidden = false;
}

function clearOutside() {
  outsideMarks = new Map();
  $('outside').hidden = true;
  renderZones(false);
  renderCommentList();
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
    if (target.key) outsideMarks.delete(keyStr(target));
    applyComments(r);
    return true;
  } catch (e) { flash(e.message); return false; }
}

// file index -> {n, kind}, kind the most severe
function commentsPerFile() {
  const perFile = new Map();
  for (const c of comments) {
    if (c.elsewhere) continue;
    const i = upperBound(fileStarts, c.line) - 1;
    const f = perFile.get(i) || { n: 0, kind: 'good' };
    f.n++;
    if (SEVERITY[c.kind] > SEVERITY[f.kind]) f.kind = c.kind;
    perFile.set(i, f);
  }
  return perFile;
}

function applyComments(r) {
  comments = r.comments;
  commentStamp = r.stamp;
  if (r.stamp && commitMarks && r.stamp !== commitMarks.stamp) refreshMarks(wordGen);
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
  const perFile = commentsPerFile();
  fileRows.forEach((row, i) => {
    const f = perFile.get(i);
    const cell = row.querySelector('.n-cmt');
    cell.textContent = f ? '\u25cf' + f.n : '';
    cell.className = 'n n-cmt' + (f ? ' c-' + f.kind : '');
  });
  const n = (totalComments = comments.length);
  $('ccount').textContent = !doc ? '' : n === 0 ? 'no comments'
    : `${n} comment${n === 1 ? '' : 's'}` + (r.elsewhere ? ` (${r.elsewhere} on files not in this diff)` : '');
  // With no changes left, the comments on other files are all there is to show.
  // (the "no changes" note stays in #progress, hidden while they show).
  if (doc && !doc.files.length) $('progress').hidden = !!r.elsewhere;
  renderZones(false);
  renderCommentList();
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
  const elsewhere = comments.filter((c) => c.elsewhere);
  if (elsewhere.length)
    want.set('elsewhere', { c: { line: elsewhere[0].line, text: String(elsewhere.length) }, head: true });
  const sigOf = (w) => JSON.stringify([w.c.line, w.c.kind, w.c.text, w.c.moved, w.c.orphan, !!w.ed, width,
                                       outsideMarks.get(keyStr(w.c))]);
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
      const node = w.head ? elsewhereHead(+w.c.text) : w.ed ? editorBox(w.ed) : commentBox(w.c);
      node.style.width = width + 'px';
      const height = measure(node);
      const overlay = wrapZone(node);
      overlay.style.left = left + 'px';
      overlay.style.display = 'none';  // until the zone reports a position
      const widget = { getId: () => 'gitr.comment.' + k, getDomNode: () => overlay, getPosition: () => null };
      editor.addOverlayWidget(widget);
      const placeholder = el('div');
      // Monaco reports a zone's top before showing its node, and a top far
      // above the view for a zone off screen; the overlay follows that.
      // The section of comments on files not in the diff follows everything
      // else after the last line, an orphan of the last file included.
      const ordinal = w.head ? 20000 : w.c.elsewhere ? 20001 : undefined;
      const zone = { afterLineNumber: w.c.line, heightInPx: height, domNode: placeholder, ordinal,
                     onDomNodeTop: (top) => {
                       overlay.style.top = top + 'px';
                       overlay.style.display = top < -100000 ? 'none' : '';
                     } };
      const z = { sig: sigOf(w), zone, node, overlay, widget };
      z.id = acc.addZone(zone);
      zones.set(k, z);
      if (w.ed) focus = node;
    }
  });
  if (anchor && anchor <= editor.getModel().getLineCount())
    editor.setScrollTop(editor.getTopForLineNumber(anchor) + offset);
  if (focus) requestAnimationFrame(() => focus.querySelector('textarea')?.focus());
}

const JUMP_CONTEXT = 3;  // lines of the diff left above a comment jumped to

// Comments come from the server in diff order, those on files not in the
// diff last, as their zones are stacked.
function commentTops() {
  const lh = editor.getOption(monaco.editor.EditorOption.lineHeight);
  const tops = [];
  let line = -1, y = 0;
  for (const c of comments) {
    if (c.line !== line) { line = c.line; y = line ? editor.getBottomForLineNumber(line) : 0; }
    tops.push(Math.max(0, y - JUMP_CONTEXT * lh));
    y += zones.get(keyStr(c))?.zone.heightInPx || 0;
  }
  return tops;
}

function scrollToComment(i) {
  pinned = null;
  editor.setScrollTop(commentTops()[i]);
}

function jumpComment(dir) {
  if (!comments.length) return;
  const tops = commentTops();
  const now = editor.getScrollTop();
  const i = dir > 0 ? tops.findIndex((t) => t > now + 1) : tops.findLastIndex((t) => t < now - 1);
  if (i < 0) return flash(dir > 0 ? 'no more comments below' : 'no more comments above');
  pinned = null;
  editor.setScrollTop(tops[i]);
  flash(`comment ${i + 1} of ${comments.length}`);
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

// Marked in the text itself, so `gitr comments` and the agent export show
// it too, with no new field in review.json.
const ACCEPTED = '(accepted)';
const accepted = (text) => (text.trimEnd().endsWith(ACCEPTED) ? text : text.trimEnd() + ' ' + ACCEPTED);

function elsewhereHead(n) {
  return el('div', 'c-section', `${n} comment${n === 1 ? '' : 's'} on files not in this diff`);
}

function outsideFlag(c, cls) {
  const f = el('span', cls + ' outside', outsideMarks.get(keyStr(c)));
  f.title = 'Changed outside this page (another tab, gitr comments, an agent)';
  return f;
}

function commentBox(c) {
  const action = (label, title, fn, cls) => {
    const b = el('button', 'c-act' + (cls ? ' ' + cls : ''), label);
    b.title = title;
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
    return b;
  };
  const head = el('div', 'c-head', [
    el('span', 'c-kind', `${KIND_MARK[c.kind]} ${c.kind}`),
    c.elsewhere ? el('span', 'c-loc', c.lineNo ? `${c.file}:${c.lineNo}` : c.file) : null,
    c.elsewhere ? null : c.orphan ? el('span', 'c-flag', 'orphaned: line no longer in the diff') : null,
    !c.orphan && c.moved ? el('span', 'c-flag', 'moved') : null,
    outsideMarks.has(keyStr(c)) ? outsideFlag(c, 'c-flag') : null,
    el('span', 'grow'),
    c.kind === 'good' ? null
      : action('accept', 'Resolved: make it a good comment', () => saveComment(c, accepted(c.text), 'good'), 'c-good'),
    action('copy', 'Copy the comment with its location and line, as "copy for agent" does',
           () => copyText(commentExport(c), `copied the comment on ${c.file}`)),
    action('delete', 'Delete this comment', () => saveComment(c, '', c.kind), 'c-del'),
  ].filter(Boolean));
  const parts = [head];
  if (c.orphan) parts.push(el('div', 'c-quote', c.lineText));
  parts.push(el('div', 'c-text', c.text));
  const box = el('div', 'c-box c-' + c.kind, parts);
  box.title = 'click to edit';
  box.addEventListener('click', () => openEditor(c.line, c));
  box.addEventListener('contextmenu', (e) => {
    if (String(getSelection()).trim()) return;  // the browser's, to copy it
    e.preventDefault();
    commentMenu(c, e.clientX, e.clientY);
  });
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
  if (doc.kinds[ln - 1] === 'k') {
    const h = doc.commit.hash;
    item(`Copy "${h.slice(0, 10)}"`, () => copyText(h, `copied ${h}`));
    item('Find...', () => { editor.focus(); editor.getAction('actions.find').run(); });
    item('Go to file...', () => showQuickOpen(true));
    return placeMenu(m, x, y);
  }
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
  const fi = fileOf(ln);
  const path = doc.files[fi].path;
  const fileOn = fileHunks[fi].every((h) => viewed.has(doc.hunks[h][2]));
  item(fileOn ? 'Mark file not viewed' : 'Mark file viewed', () => markViewed(fileHunks[fi], !fileOn));
  const h = hunkAt(ln);
  if (h >= 0 && fileHunks[fi].length > 1) {
    const on = viewed.has(doc.hunks[h][2]);
    item(on ? 'Mark hunk not viewed' : 'Mark hunk viewed', () => markViewed([h], !on));
  }
  m.append(el('div', 'c-sep'));
  item('Reveal in file list', () => revealFile(fi));
  // Monaco's find widget; it starts from the selected text, if any.
  item('Find...', () => { editor.focus(); editor.getAction('actions.find').run(); });
  item('Go to file...', () => showQuickOpen(true));
  m.append(el('div', 'c-sep'));
  const loc = locationOf(ln);
  item(`Copy "${path}"`, () => copyText(path, `copied ${path}`));
  if (loc !== path) item(`Copy "${loc}"`, () => copyText(loc, `copied ${loc}`));
  const [from, to] = linesAround(ln);
  const n = to - from + 1;
  // A file header alone has nothing worth quoting.
  if (n > 1 || doc.kinds[from - 1] !== 'f')
    item(`Copy "${locationOf(from)}" + ${n} line${n === 1 ? '' : 's'}`, () => copyLines(from, to));
  placeMenu(m, x, y);
}

// Fixed to the window, so the sidebar's rows can have it too.
function placeMenu(m, x, y) {
  m.hidden = false;
  m.style.left = Math.max(4, Math.min(x, innerWidth - m.offsetWidth - 4)) + 'px';
  m.style.top = Math.max(4, Math.min(y, innerHeight - m.offsetHeight - 4)) + 'px';
}

function commitMenu(r, x, y) {
  const m = $('cmenu');
  m.textContent = '';
  const item = (label, fn) => {
    const it = el('div', 'c-item', label);
    it.addEventListener('click', () => { hideMenu(); fn(); });
    m.append(it);
  };
  const shown = current?.commit && !current.commit.includes('..') ? current.commit : null;
  const short = (h) => ({ uncommitted: 'uncommitted changes', untracked: 'untracked files' })[h] || h.slice(0, 7);
  if (!LOCAL.has(r.hash)) {
    item(`Copy "${r.hash.slice(0, 10)}"`, () => copyText(r.hash, `copied ${r.hash}`));
    item('Copy subject', () => copyText(r.subject, 'copied the subject'));
    m.append(el('div', 'c-sep'));
  }
  if (shown && shown !== r.hash) {
    item(`Show range to ${short(shown)}`, () => { commitAnchor = shown; pickCommit(r.hash, true); });
  }
  if (!LOCAL.has(r.hash)) {
    // the base's merge base with HEAD is the commit itself when it is an ancestor
    item('Review changes after this commit (use as base)', () => applyBase(r.hash));
  }
  if (r.hash === 'uncommitted' && commitList?.untracked)
    item('Show with the untracked files', () => { commitAnchor = 'uncommitted'; pickCommit('untracked', true); });
  if (r.hash === 'untracked')
    item('Show all changes with the untracked files', () => { commitAnchor = 'all'; pickCommit('untracked', true); });
  if (!m.childElementCount) return;
  placeMenu(m, x, y);
}

// The page's menu for a list row, from fill(item, sep).
function listMenu(x, y, fill) {
  const m = $('cmenu');
  m.textContent = '';
  const item = (label, fn, cls) => {
    const it = el('div', 'c-item' + (cls ? ' ' + cls : ''), label);
    it.addEventListener('click', () => { hideMenu(); fn(); });
    m.append(it);
  };
  fill(item, () => m.append(el('div', 'c-sep')));
  if (m.childElementCount) placeMenu(m, x, y);
}

function fileMenu(i, x, y) {
  const f = doc.files[i];
  listMenu(x, y, (item, sep) => {
    item('Show in the diff', () => jumpToFile(i));
    const on = viewedState([i]) === 'all';
    item(on ? 'Mark file not viewed' : 'Mark file viewed', () => markViewed(fileHunks[i], !on));
    sep();
    item(`Copy "${f.path}"`, () => copyText(f.path, `copied ${f.path}`));
    if (f.oldPath && f.oldPath !== f.path) item(`Copy "${f.oldPath}"`, () => copyText(f.oldPath, `copied ${f.oldPath}`));
  });
}

function dirMenu(entry, isCollapsed, x, y) {
  listMenu(x, y, (item, sep) => {
    item(isCollapsed ? 'Expand' : 'Collapse', entry.toggle);
    item('Collapse all', () => collapseAll(true));
    item('Expand all', () => collapseAll(false));
    sep();
    const on = viewedState(entry.files) === 'all';
    item(on ? 'Mark folder not viewed' : 'Mark folder viewed',
         () => markViewed(entry.files.flatMap((i) => fileHunks[i]), !on));
    sep();
    item(`Copy "${entry.path}/"`, () => copyText(entry.path + '/', `copied ${entry.path}/`));
  });
}

function commentMenu(c, x, y, show) {
  listMenu(x, y, (item, sep) => {
    if (show) item('Show in the diff', show);
    item('Edit comment...', () => { show?.(); openEditor(c.line, c); });
    sep();
    item('Copy text', () => copyText(c.text, 'copied the comment'));
    sep();
    item('Delete comment', () => saveComment(c, '', c.kind));
  });
}

// Outside the lists and the diff, which have menus of their own, the page
// is an app's window: no browser menu, but in text fields and on selected
// text, where its copy and paste are of use.
function initMenus() {
  document.addEventListener('contextmenu', (e) => {
    if (e.defaultPrevented || e.target.closest('input, textarea, [contenteditable]')) return;
    if (String(getSelection()).trim() && !e.target.closest('.monaco-editor')) return;
    e.preventDefault();
  });
  api('/api/version', {}).then((r) => {
    gitrVersion = r.gitr || '';
    updateCrumb();
    if (r.plugin) showPlugin();
  }).catch(() => {});
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
  const f = doc.files[fileOf(ln)];
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

// One item of gitr's agent export (_format_export), without the indent.
function commentExport(c) {
  const ref = c.lineNo ? `${c.file}:${c.lineNo}` : c.file;
  const side = '+-'.includes(c.key[2]) ? ` (${c.key[2]})` : '';
  const flags = [c.kind, ...(c.orphan ? ['orphaned'] : c.moved ? ['moved'] : [])];
  const fence = codeFence(c.lineText);
  return [`${ref}${side} [${flags.join(', ')}]`, `${fence}diff`, c.lineText, fence, c.text, ''].join('\n');
}

function codeFence(text) {
  const longest = Math.max(0, ...(text.match(/`+/g) || []).map((r) => r.length));
  return '`'.repeat(Math.max(3, longest + 1));
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
  const fence = codeFence(text);
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
// A message flashed there stays up; the new text shows when it ends.
function setTiming(text) {
  const t = $('timing');
  if (flashTimer) t.dataset.saved = text;
  else t.textContent = text;
}

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

// Whether the server still answers. `gitr`'s window closes when its server
// stopped (Ctrl+C, or minutes without a page: a page the browser froze
// meanwhile finds out on waking); a browser lets a page close only the
// window it opened in, so elsewhere, and for gitr serve, which may just be
// restarting, a note says so. Asked twice before giving up, as one
// failed request may be a hiccup. A server that stops on its own says so
// first (the 'bye' event); this is for one that crashed or was killed.
let standalone = false, checking = false;

// --pushed changes ----------------------------------------------------------
//
// A shared worker (events.js) holds one /api/events stream for all of this
// browser's pages of the server, over which the server sends what changed in
// the view each page shows. While it is open the polls below stand by; when
// it drops they take over until it is back. The stream is also what tells
// `gitr` a window is open.

let pushed = false;     // the stream is open: changes come over it
let port = null;        // to the worker
let watchN = 0;         // see watchShown
let watching = null;    // {key, view: the view as JSON} posted last
let lastFp = null;      // the last change check for it
const tabId = 'gitr-tab-' + Math.random().toString(36).slice(2) + Date.now().toString(36);

async function startEvents() {
  if (!window.SharedWorker || !navigator.locks) {
    // The polls bring the changes; a stream of the page's own still tells
    // `gitr` that it is open, and when the server stops.
    const es = new EventSource('/api/events?id=' + tabId);
    es.addEventListener('bye', () => { es.close(); onEvent({ type: 'bye' }); });
    es.onerror = () => checkAlive();
    return;
  }
  let v = '';
  try { v = (await api('/api/version', {})).version; } catch { /* any worker then */ }
  // per server version: a page loaded after an update gets a worker of the
  // new code, not the one its older tabs keep alive
  const w = new SharedWorker('events.js?v=' + encodeURIComponent(v), { name: 'gitr-events' });
  port = w.port;
  port.onmessage = (m) => onEvent(m.data);
  port.start();
  // held while the page lives; the worker waits for it to learn that it went
  navigator.locks.request(tabId, () => {
    port.postMessage({ type: 'hello', tab: tabId });
    watchShown();
    return new Promise(() => {});
  });
  document.addEventListener('visibilitychange', watchShown);
}

function onEvent(m) {
  if (m.type === 'open') {
    // changes to the list while the stream was down came to no one
    if (!pushed) refreshProjects(true);
    pushed = true;
    $('gone').hidden = true;
    return;
  }
  // the server says it is stopping: no need to ask whether it is still there
  if (m.type === 'bye') {
    if (standalone) window.close();
    $('gone').hidden = false;
    return;
  }
  if (m.type === 'error') { pushed = false; checkAlive(); return; }
  if (m.type === 'version') return noteVersion(m.data);
  if (m.type === 'projects') {
    if (listSignature(m.data) !== projectsSig) renderProjects(m.data);
    return;
  }
  if (m.type === 'fp' && watching?.key === m.data.key) {
    lastFp = m.data;
    applyChanges(m.data);
  }
}

// The view on screen, none while hidden (nothing is checked for it then).
// Each load gets a key of its own: the server answers a new one at once, and
// nothing checked before the diff loaded can come in after it.
function watchShown() {
  if (!port) return;
  const view = document.hidden || !shown || !worktrees.has(shown.wt) ? null
    : { wt: shown.wt, base: shown.base, commit: shown.commit || null, doc: doc?.id };
  const v = view && JSON.stringify(view);
  if ((watching?.view ?? null) === v) return;
  // the worker passes on the keys of all of the browser's pages
  watching = v && { key: `${tabId}:${watchN++}`, view: v };
  lastFp = null;
  port.postMessage({ type: 'watch', sub: view && { key: watching.key, wt: view.wt, base: view.base, commit: view.commit } });
}

async function checkAlive() {
  if (checking) return;
  checking = true;
  try {
    for (let i = 0; i < 2; i++) {
      if (i) await new Promise((r) => setTimeout(r, 250));
      try {
        standalone = (await api('/api/alive', {})).standalone;
        $('gone').hidden = true;
        return;
      } catch { /* once more */ }
    }
    if (standalone) window.close();
    $('gone').hidden = false;
  } finally { checking = false; }
}

async function pollChanges() {
  if (!shown || document.hidden || !worktrees.has(shown.wt)) return;
  watchShown();
  // Pushed: the last answer again, for what it had to leave for later (a
  // comment being edited, the summary at most every CHANGES_MS).
  if (pushed) { if (lastFp) applyChanges(lastFp); return; }
  // A load started while the request was out replaces the doc, so its
  // answer no longer applies.
  const gen = wordGen;
  try {
    const r = await api('/api/fingerprint', shown);
    noteVersion(r.version);
    if (gen === wordGen && !pushed) applyChanges(r);
  } catch { checkAlive(); }
}

// An answer of /api/fingerprint, polled or pushed, for the diff on screen.
function applyChanges(r) {
  const gen = wordGen, fp = doc.fingerprint;
  // The list follows the worktree quietly, untracked files included (it
  // counts them); the diff waits for reload.
  // and the base's own commits with the base, the labels with the refs
  if ((r.fingerprintAll !== commitsFp || r.baseTip !== commitList?.baseTip
       || (r.refs && r.refs !== commitList?.refs)) && commitList) {
    commitsFp = r.fingerprintAll;
    refreshCommits(gen);
  }
  // a view showing the untracked files changes with them, the others not
  const now = doc.fingerprintKind === 'all' ? r.fingerprintAll : r.fingerprint;
  if (now !== fp) {
    $('changed').hidden = false;
    // Each summary runs a whole git diff; while an agent keeps editing,
    // at most one per CHANGES_MS. A later poll catches up with the last
    // (pollChanges, also when pushed).
    if (now !== changesFp && Date.now() - changesAt >= CHANGES_MS) {
      changesFp = now;
      changesAt = Date.now();
      showChanges(gen);
    }
  }
  // review.json written by another tab, gitr comments or an agent
  // commentStamp is null until a load's comments arrive: nothing yet to compare with
  if (r.review !== commentStamp && !editing) loadComments(gen, commentStamp !== null);
  // not while a mark is being saved: what is stored may not have it yet
  if (r.viewed !== viewedStamp && !viewedSaving) reloadViewed(gen);
}

// Measured against the loaded diff, not the base: what a reload would add.
const CHANGES_MS = 15000;
let changesFp = null, changesAt = 0;
async function showChanges(gen) {
  let r;
  try { r = await api('/api/changes', { ...shown, id: doc.id }); } catch { return; }
  if (gen !== wordGen) return;
  const what = $('changed-what');
  what.textContent = '';
  const parts = [];
  if (r.rewritten) parts.push(el('span', null, 'history rewritten'));
  else if (r.commits) parts.push(el('span', null, plural(r.commits, 'commit')));
  if (r.files && !r.add && !r.del) {
    // files without + and - lines (binary, empty, renamed)
    parts.push(el('span', null, plural(r.files, 'changed file')));
  } else if (r.files) {
    parts.push(el('span', null, [el('span', 'n-add', '+' + r.add), ' ', el('span', 'n-rem', '-' + r.del),
                                 ` in ${plural(r.files, 'file')}`]));
  } else if (!parts.length && r.files === 0) parts.push(el('span', null, 'diff unchanged'));
  if (parts.length) what.append(': ');
  what.append(...joined(parts));
  $('changed').title = 'Since this diff was loaded: '
    + (r.rewritten ? 'HEAD was rebased or reset; ' : r.commits ? `${plural(r.commits, 'new commit')}; ` : '')
    + (r.files ? `${plural(r.add, 'changed line')} would appear and ${r.del} go, in ${plural(r.files, 'file')}`
       : r.files === 0 ? 'the diff itself is the same' : 'the files changed');
}

// --dom helper -----------------------------------------------------------

function el(tag, cls, content) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (Array.isArray(content)) e.append(...content);
  else if (content != null) e.textContent = content;
  return e;
}

init();
