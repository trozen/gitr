// One /api/events stream for all of a browser's pages of this server (see
// Events in server.py): each page says what it shows, the server sends what
// changed in it, and this passes each page its part. A page holds a lock
// named after it while it lives; once this gets the lock, the page is gone
// (a shared worker hears nothing when a page closes).

const pages = new Map();  // port -> {tab, sub: {key, wt, base, commit} | null}
const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
let es = null, open = false, posted = null, posting = false, retry = 1000, retryTimer = null;

onconnect = (e) => {
  const port = e.ports[0];
  port.onmessage = (m) => message(port, m.data);
  port.start();
};

function message(port, m) {
  if (m.type === 'hello') {
    pages.set(port, { tab: m.tab, sub: null });
    navigator.locks.request(m.tab, () => { pages.delete(port); changed(); });
    if (!es && !retryTimer) connect();
    else if (open) port.postMessage({ type: 'open' });
  } else if (m.type === 'watch' && pages.has(port)) {
    pages.get(port).sub = m.sub;
    changed();
  }
}

function changed() {
  if (pages.size) return postWatch();
  // no page left: `gitr` takes the closed stream as its cue to stop
  clearTimeout(retryTimer);
  retryTimer = null;
  es?.close();
  es = null;
  open = false;
}

// One at a time, so that an older post cannot land after a newer one; one
// that failed is tried again, as the pages poll no more while the stream is
// open and would hear of nothing.
async function postWatch() {
  if (!open || posting) return;  // posted on opening, or after the one out
  const subs = {};
  for (const { sub } of pages.values())
    if (sub) subs[sub.key] = { wt: sub.wt, base: sub.base, commit: sub.commit };
  const body = JSON.stringify({ id, subs });
  if (body === posted) return;
  posting = true;
  let ok = false;
  try {
    ok = (await fetch('/api/watch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })).ok;
  } catch { /* below */ }
  posting = false;
  if (ok) posted = body;
  if (ok) postWatch();  // the pages may have moved on meanwhile
  else setTimeout(postWatch, 1000);
}

function all(msg) {
  for (const port of pages.keys()) port.postMessage(msg);
}

function connect() {
  retryTimer = null;
  es = new EventSource('/api/events?id=' + id);
  es.onopen = () => {
    open = true;
    retry = 1000;
    posted = null;
    postWatch();
    all({ type: 'open' });
  };
  // EventSource would try again every second for as long as the server is
  // away (a page left open on a stopped gitr-serve): slower and slower instead.
  es.onerror = () => {
    open = false;
    es.close();
    es = null;
    all({ type: 'error' });
    if (!pages.size) return;
    retryTimer = setTimeout(connect, retry);
    retry = Math.min(retry * 2, 30000);
  };
  es.addEventListener('fp', (e) => {
    const data = JSON.parse(e.data);
    for (const [port, p] of pages) if (p.sub?.key === data.key) port.postMessage({ type: 'fp', data });
  });
  es.addEventListener('projects', (e) => all({ type: 'projects', data: JSON.parse(e.data) }));
  es.addEventListener('version', (e) => all({ type: 'version', data: JSON.parse(e.data) }));
  es.addEventListener('bye', () => all({ type: 'bye' }));
}
