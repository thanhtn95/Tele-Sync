import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { api, fileUrl } from '../lib/api.js';
import { formatAgo } from '../lib/format.js';

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

/** Step 1: enter a site; step 2: tick its categories; then start a job. */
function NewArchive({ onStarted }) {
  const [url, setUrl] = useState('');
  const [site, setSite] = useState(null); // {url, title, categories}
  const [picked, setPicked] = useState(new Set());
  const [filter, setFilter] = useState('');
  const [maxPages, setMaxPages] = useState(100);
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

  const shown = useMemo(() => {
    if (!site) return [];
    const f = filter.trim().toLowerCase();
    return f
      ? site.categories.filter((c) => c.label.toLowerCase().includes(f) || c.prefix.toLowerCase().includes(f))
      : site.categories;
  }, [site, filter]);

  const toggle = (prefix) =>
    setPicked((p) => {
      const n = new Set(p);
      n.has(prefix) ? n.delete(prefix) : n.add(prefix);
      return n;
    });
  const setAll = (on) =>
    setPicked((p) => {
      const n = new Set(p);
      shown.forEach((c) => (on ? n.add(c.prefix) : n.delete(c.prefix)));
      return n;
    });

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const cats = site.categories
        .filter((c) => picked.has(c.prefix))
        .map(({ prefix, label, url: u }) => ({ prefix, label, url: u }));
      const job = await api.archiveStart(site.url, cats, Number(maxPages) || 100);
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
          {busy && !site ? 'Reading…' : 'Find categories'}
        </button>
      </form>
      {error && <div className="err small">{error}</div>}

      {site && (
        <div className="archive-pick">
          <div className="archive-pick-head">
            <strong>{site.title || host(site.url)}</strong>
            <span className="muted small">{site.url}</span>
          </div>
          {site.categories.length === 0 ? (
            <p className="muted small">No categories found on this page. Try the address of a section instead.</p>
          ) : (
            <>
              <div className="archive-pick-tools">
                {site.categories.length > 8 && (
                  <input
                    type="search"
                    placeholder="Filter categories"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  />
                )}
                <button className="link-btn" onClick={() => setAll(true)}>
                  Select all
                </button>
                <button className="link-btn" onClick={() => setAll(false)}>
                  None
                </button>
              </div>
              <ul className="archive-cats">
                {shown.map((c) => (
                  <li key={c.prefix}>
                    <label>
                      <input type="checkbox" checked={picked.has(c.prefix)} onChange={() => toggle(c.prefix)} />
                      <span className="archive-cat-label">{c.label}</span>
                      {c.in_nav && <span className="archive-tag">menu</span>}
                      <span className="muted small archive-cat-path">
                        {c.prefix} · {c.links} link{c.links === 1 ? '' : 's'}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </>
          )}
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
              Archive {picked.size} categor{picked.size === 1 ? 'y' : 'ies'}
            </button>
          </div>
          <p className="muted small">
            Saves the home page and every page linked inside the picked categories, one page per second, skipping
            what the site's robots.txt disallows.
          </p>
        </div>
      )}
    </section>
  );
}

function JobRow({ job, onChanged }) {
  const active = ACTIVE.has(job.status);
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
          {job.pages_failed ? ` · ${job.pages_failed} failed` : ''} · {formatAgo(job.created_at)}
          {job.error ? ` · ${job.error}` : ''}
        </span>
      </a>
      {active ? (
        <button onClick={cancel}>Stop</button>
      ) : (
        <button className="icon-btn" onClick={remove} title="Delete this archive">
          🗑
        </button>
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
          {job.pages_failed ? ` · ${job.pages_failed} failed` : ''} · up to {job.max_pages} pages
          {job.error ? ` · ${job.error}` : ''}
        </span>
        {ACTIVE.has(job.status) && (
          <span className="muted small">Links between saved pages are switched to the copies when it finishes.</span>
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
