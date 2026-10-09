import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { api, fileUrl } from '../lib/api.js';
import { formatAgo, formatSize } from '../lib/format.js';

const ACTIVE = new Set(['queued', 'running']);
const STATUS = {
  queued: 'waiting…',
  running: 'archiving…',
  done: 'done',
  cancelled: 'stopped',
  failed: 'failed',
  interrupted: 'interrupted (app restarted)',
};

const host = (url) => {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
};

/** Re-run `load` every few seconds while `active`. */
function usePoll(load, active) {
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load, active]);
}

/** Indexes of the items under item i in a menu (the following items that sit deeper). */
function descendants(items, i) {
  const out = [];
  for (let j = i + 1; j < items.length && items[j].depth > items[i].depth; j++) out.push(j);
  return out;
}

/** One of the site's menus, drawn as it is nested on the site; ticking an item ticks its sub-items. */
function MenuTree({ menu, picked, setPicked, filter }) {
  const f = filter.trim().toLowerCase();
  const items = menu.items;
  const toggle = (i) =>
    setPicked((p) => {
      const n = new Set(p);
      const on = !n.has(items[i].url);
      [i, ...descendants(items, i)].forEach((j) => (on ? n.add(items[j].url) : n.delete(items[j].url)));
      return n;
    });
  const setAll = (on) =>
    setPicked((p) => {
      const n = new Set(p);
      items.forEach((it) => (on ? n.add(it.url) : n.delete(it.url)));
      return n;
    });
  const shown = items
    .map((it, i) => ({ it, i }))
    .filter(({ it }) => !f || it.label.toLowerCase().includes(f) || it.prefix.toLowerCase().includes(f));
  if (shown.length === 0) return null;
  return (
    <section className="archive-menu">
      <div className="archive-menu-head">
        <strong className="small">{menu.name}</strong>
        <span className="muted small">{items.length}</span>
        <button className="link-btn small" onClick={() => setAll(true)}>
          All
        </button>
        <button className="link-btn small" onClick={() => setAll(false)}>
          None
        </button>
      </div>
      <ul className="archive-cats">
        {shown.map(({ it, i }) => {
          const subs = descendants(items, i);
          const some = subs.some((j) => picked.has(items[j].url));
          return (
            <li key={it.url} style={{ paddingLeft: `${(f ? 0 : it.depth) * 22}px` }}>
              <label className={it.depth === 0 ? 'archive-top' : undefined}>
                <input
                  type="checkbox"
                  checked={picked.has(it.url)}
                  ref={(el) => el && (el.indeterminate = !picked.has(it.url) && some)}
                  onChange={() => toggle(i)}
                />
                <span className="archive-cat-label">{it.label}</span>
                {subs.length > 0 && <span className="muted small">+{subs.length}</span>}
                <span className="muted small archive-cat-path">{it.prefix}</span>
              </label>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** Step 1: enter a site; step 2: tick items of its navbar; then start a job. */
function NewArchive({ onStarted }) {
  const [url, setUrl] = useState('');
  const [site, setSite] = useState(null); // {url, title, menus: [{name, items: [{label, url, prefix, depth}]}]}
  const [picked, setPicked] = useState(new Set()); // item urls
  const [filter, setFilter] = useState('');
  const [maxPages, setMaxPages] = useState(100);
  const [images, setImages] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const discover = async (e) => {
    e.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    setError(null);
    setSite(null);
    try {
      const s = await api.archiveDiscover(url.trim());
      setSite(s);
      setPicked(new Set());
      setFilter('');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const allItems = useMemo(() => (site ? site.menus.flatMap((m) => m.items) : []), [site]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const seen = new Set();
      const cats = allItems
        .filter((it) => picked.has(it.url) && !seen.has(it.url) && seen.add(it.url))
        .map(({ prefix, label, url: u }) => ({ prefix, label, url: u }));
      const job = await api.archiveStart(site.url, cats, Number(maxPages) || 100, images);
      setSite(null);
      setUrl('');
      onStarted(job);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="archive-new">
      <form className="archive-url" onSubmit={discover}>
        <input
          type="text"
          inputMode="url"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          placeholder="Website address, e.g. https://example.com"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          aria-label="Website address"
        />
        <button className="primary" disabled={busy || !url.trim()}>
          {busy && !site ? 'Reading…' : 'Read menu'}
        </button>
      </form>
      {error && <div className="err small">{error}</div>}

      {site && (
        <div className="archive-pick">
          <div className="archive-pick-head">
            <strong>{site.title || host(site.url)}</strong>
            <span className="muted small">{site.url}</span>
          </div>
          {site.menus.length === 0 ? (
            <p className="muted small">No menu found on this page. Try the address of a section instead.</p>
          ) : (
            <>
              <p className="muted small">Tick what to archive from the site's menu (an item includes its sub-items).</p>
              {allItems.length > 12 && (
                <input
                  type="search"
                  className="archive-filter"
                  placeholder="Filter menu items"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
              )}
              <div className="archive-menus">
                {site.menus.map((m, i) => (
                  <MenuTree key={i} menu={m} picked={picked} setPicked={setPicked} filter={filter} />
                ))}
              </div>
            </>
          )}
          <label className="small archive-check">
            <input type="checkbox" checked={images} onChange={(e) => setImages(e.target.checked)} /> Save images too
            <span className="muted"> (styles and fonts are always saved)</span>
          </label>
          <div className="archive-go">
            <label className="small">
              Up to{' '}
              <input
                type="number"
                min="1"
                max="2000"
                value={maxPages}
                onChange={(e) => setMaxPages(e.target.value)}
                aria-label="Maximum pages"
              />{' '}
              pages
            </label>
            <button className="primary" disabled={busy || picked.size === 0} onClick={start}>
              Archive {picked.size} item{picked.size === 1 ? '' : 's'}
            </button>
          </div>
          <p className="muted small">
            Saves the home page and the pages of each picked item, with their look (CSS, fonts
            {images ? ', images' : ''}), one page per second, skipping what the site's robots.txt disallows.
          </p>
        </div>
      )}
    </section>
  );
}

/** Ask, then archive the job's site again with the same menu items (old pages are replaced). */
async function rerunJob(job) {
  const ok = window.confirm(
    `Archive ${host(job.url)} again with the same menu items?\n\n` +
      `The ${job.pages_saved} saved pages are replaced by fresh copies (with the site's CSS and images).`,
  );
  if (!ok) return false;
  try {
    await api.archiveRerun(job.id);
    return true;
  } catch (e) {
    window.alert(e.message);
    return false;
  }
}

function JobRow({ job, onChanged }) {
  const active = ACTIVE.has(job.status);
  const rerun = async () => {
    if (await rerunJob(job)) onChanged();
  };
  const cancel = async () => {
    await api.archiveCancel(job.id).catch(() => {});
    onChanged();
  };
  const remove = async () => {
    if (!window.confirm(`Delete the archive of ${host(job.url)} and its ${job.pages_saved} saved pages?`)) return;
    await api.archiveDelete(job.id).catch(() => {});
    onChanged();
  };
  return (
    <li className="archive-job">
      <a href={`#/archive/${job.id}`} className="archive-job-main">
        <strong>{job.title || host(job.url)}</strong>
        <span className="muted small">
          {job.categories.map((c) => c.label).join(', ')}
        </span>
        <span className={`small ${job.status === 'failed' ? 'err' : 'muted'}`}>
          {STATUS[job.status] || job.status} · {job.pages_saved} saved
          {job.pages_bytes + job.assets_bytes > 0 ? ` · ${formatSize(job.pages_bytes + job.assets_bytes)}` : ''}
          {job.pages_failed ? ` · ${job.pages_failed} failed` : ''} · {formatAgo(job.created_at)}
          {job.error ? ` · ${job.error}` : ''}
        </span>
      </a>
      {active ? (
        <button onClick={cancel}>Stop</button>
      ) : (
        <>
          <button onClick={rerun} title="Archive this site again with the same menu items">
            ↻ Re-archive
          </button>
          <button className="icon-btn" onClick={remove} title="Delete this archive">
            🗑
          </button>
        </>
      )}
    </li>
  );
}

function JobList() {
  const [jobs, setJobs] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(() => {
    api.archiveJobs().then(
      (j) => (setJobs(j), setError(null)),
      (e) => setError(e.message),
    );
  }, []);
  usePoll(load, !!jobs?.some((j) => ACTIVE.has(j.status)));
  return (
    <>
      <NewArchive onStarted={load} />
      <h2 className="muted small archive-h">Archives</h2>
      {error && <div className="err small">{error}</div>}
      {jobs && jobs.length === 0 && <p className="muted small">Nothing archived yet.</p>}
      <ul className="archive-jobs">
        {jobs?.map((j) => (
          <JobRow key={j.id} job={j} onChanged={load} />
        ))}
      </ul>
    </>
  );
}

function JobDetail({ jobId }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(() => {
    api.archiveJob(jobId).then(
      (j) => (setJob(j), setError(null)),
      (e) => setError(e.message),
    );
  }, [jobId]);
  usePoll(load, !!job && ACTIVE.has(job.status));
  if (error) return <div className="err small">{error}</div>;
  if (!job) return <p className="muted small">Loading…</p>;

  const groups = [{ prefix: null, label: 'Home page' }, ...job.categories].map((c) => ({
    ...c,
    pages: job.pages.filter((p) => p.category === c.prefix),
  }));
  return (
    <>
      <div className="archive-pick-head">
        <strong>{job.title || host(job.url)}</strong>
        <a className="small" href={job.url} target="_blank" rel="noreferrer noopener">
          {job.url}
        </a>
        <span className={`small ${job.status === 'failed' ? 'err' : 'muted'}`}>
          {STATUS[job.status] || job.status} · {job.pages_saved} saved
          {job.pages_bytes + job.assets_bytes > 0 ? ` · ${formatSize(job.pages_bytes + job.assets_bytes)}` : ''}
          {job.pages_failed ? ` · ${job.pages_failed} failed` : ''} · up to {job.max_pages} pages
          {job.error ? ` · ${job.error}` : ''}
        </span>
        {ACTIVE.has(job.status) ? (
          <span className="muted small">Links between saved pages are switched to the copies when it finishes.</span>
        ) : (
          <div>
            <button onClick={async () => (await rerunJob(job)) && load()}>↻ Re-archive</button>
          </div>
        )}
      </div>
      {groups.map((g) => (
        <section key={g.prefix ?? '-'} className="archive-group">
          <h2 className="muted small archive-h">
            {g.label} {g.prefix && <span>· {g.prefix}</span>} · {g.pages.length}
          </h2>
          <ul className="archive-pages">
            {g.pages.map((p) => (
              <li key={p.id}>
                {p.file_path ? (
                  <a href={fileUrl(p.file_path)} target="_blank" rel="noreferrer noopener">
                    {p.title || p.final_url}
                  </a>
                ) : (
                  <span className="muted">{p.url}</span>
                )}
                <span className={`small ${p.error ? 'err' : 'muted'}`}>
                  {p.error || new URL(p.final_url).pathname}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}

export default function WebArchive({ jobId }) {
  useEffect(() => {
    document.title = 'Website archive';
  }, []);
  return (
    <div className="chat-list-page archive-page">
      <header className="top">
        <h1>
          <a className="back" href={jobId ? '#/archive' : '#/'} aria-label="Back">
            ‹
          </a>{' '}
          Website archive
        </h1>
      </header>
      {jobId ? <JobDetail jobId={jobId} /> : <JobList />}
    </div>
  );
}
