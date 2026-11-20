/* subfix web UI — vanilla JS, no build step. Contract: docs/WEB_API.md. */
'use strict';

(() => {
  // ---------------------------------------------------------------- constants / state

  const tokenMeta = document.querySelector('meta[name="subfix-token"]');
  const TOKEN = tokenMeta ? tokenMeta.getAttribute('content') || '' : '';
  const TOKEN_OK = TOKEN !== '' && TOKEN !== '__SUBFIX_TOKEN__';

  /** Used until /api/config answers (same lists as subfix.formats). */
  const FALLBACK_FORMATS = {
    video: ['.mkv', '.mp4', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.m2ts'],
    audio: ['.mp3', '.wav', '.m4a', '.flac', '.aac', '.ogg', '.opus'],
    subtitle: ['.srt', '.ass', '.ssa', '.vtt'],
  };
  const FINAL_STATES = new Set(['done', 'failed', 'cancelled']);
  const STATE_RANK = { queued: 0, running: 1, done: 2, failed: 2, cancelled: 2 };
  const STATE_LABEL = {
    queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', cancelled: 'Cancelled',
  };
  const MODE_LABEL = { auto: 'Automatic', generate: 'Generate', sync: 'Sync' };
  const LOG_LIMIT = 200;
  const HISTORY_LIMIT = 100;

  const state = {
    config: null,
    formats: FALLBACK_FORMATS,
    languages: new Map(),
    /** Selected media: {path, name, kind, size_bytes, duration_seconds, sidecar, source, upload_id?} */
    media: null,
    /** Selected subtitle: {path?, upload_id?, name, source} */
    subs: null,
    /** id → {job, el, logs: LogLine[], logLoaded, cancelRequested} */
    jobs: new Map(),
    history: [],
    /** Running upload: {xhr, file} */
    upload: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);

  // ---------------------------------------------------------------- DOM helpers (no innerHTML)

  /** Create an element; string/number children become text nodes (always escaped). */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') el.className = value;
        else if (key === 'text') el.textContent = value;
        else if (key.startsWith('on') && typeof value === 'function') {
          el.addEventListener(key.slice(2), value);
        } else if (key === 'dataset') Object.assign(el.dataset, value);
        else el.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function icon(name, cls = 'icon') {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', cls);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function setText(el, text) {
    const value = text === null || text === undefined ? '' : String(text);
    if (el.textContent !== value) el.textContent = value;
  }

  // ---------------------------------------------------------------- formatting

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
    const s = Math.max(0, Math.round(seconds));
    const hrs = Math.floor(s / 3600);
    const min = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (hrs > 0) return min ? `${hrs} h ${min} min` : `${hrs} h`;
    if (min > 0) return sec ? `${min} min ${sec} s` : `${min} min`;
    return `${sec} s`;
  }

  function formatEta(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '';
    if (seconds < 1) return 'almost done';
    return `${formatDuration(seconds)} left`;
  }

  function formatSize(bytes) {
    if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '';
    const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i += 1;
    }
    if (i === 0) return `${value} bytes`;
    return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
  }

  function formatClock(t) {
    const total = Math.max(0, Number(t) || 0);
    const hrs = Math.floor(total / 3600);
    const min = Math.floor((total % 3600) / 60);
    const sec = Math.floor(total % 60);
    const ms = Math.floor((total - Math.floor(total)) * 1000);
    const pad = (n, w = 2) => String(n).padStart(w, '0');
    return `${pad(hrs)}:${pad(min)}:${pad(sec)},${pad(ms, 3)}`;
  }

  function formatWhen(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return `Today ${time}`;
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return `Yesterday ${time}`;
    return `${d.toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' })} ${time}`;
  }

  function formatLogTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }

  function ordinalPosition(n) {
    return n === 1 ? 'Next in line' : `Position ${n} in the queue`;
  }

  function languageName(code) {
    if (!code) return '';
    return state.languages.get(code) || code;
  }

  // ---------------------------------------------------------------- file types

  function extOf(name) {
    const base = String(name).split(/[\\/]/).pop() || '';
    const dot = base.lastIndexOf('.');
    return dot > 0 || (dot === 0 && base.length > 1) ? base.slice(dot).toLowerCase() : '';
  }

  function baseName(path) {
    return String(path).split(/[\\/]/).pop() || String(path);
  }

  function classify(name) {
    const ext = extOf(name);
    for (const kind of ['video', 'audio', 'subtitle']) {
      if ((state.formats[kind] || []).includes(ext)) return kind;
    }
    return null;
  }

  function unsupportedMessage(name) {
    const ext = extOf(name);
    const f = state.formats;
    return `${baseName(name)}: unsupported file type '${ext || '(none)'}'. Supported — video: ` +
      `${f.video.join(' ')}; audio: ${f.audio.join(' ')}; subtitles: ${f.subtitle.join(' ')}`;
  }

  /** Clean a pasted path: whitespace, surrounding quotes, file:/// URLs. */
  function cleanPath(raw) {
    let s = String(raw || '').trim();
    for (;;) {
      const first = s[0];
      const last = s[s.length - 1];
      if (s.length >= 2 && (first === '"' || first === "'") && first === last) {
        s = s.slice(1, -1).trim();
      } else break;
    }
    // A single stray quote (e.g. a half-copied "Copy as path").
    s = s.replace(/^["']+/, '').replace(/["']+$/, '').trim();
    if (/^file:\/\//i.test(s)) {
      try {
        s = decodeURIComponent(s.replace(/^file:\/\/\/?/i, '')).replace(/\//g, '\\');
      } catch {
        /* keep as is */
      }
    }
    return s;
  }

  // ---------------------------------------------------------------- API

  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }

  async function api(method, path, body) {
    const opts = { method, headers: { 'X-Subfix-Token': TOKEN }, cache: 'no-store' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch {
      throw new ApiError(0, 'network', 'Cannot reach the subfix server. Is it still running?');
    }
    if (res.status === 204) return null;
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok) throw errorFromBody(res.status, data);
    return data;
  }

  function errorFromBody(status, data) {
    const err = (data && data.error) || {};
    let message = err.message || `Request failed (HTTP ${status}).`;
    if (status === 403 && (err.code === 'forbidden' || !err.code)) {
      message = `${err.message ? `${err.message} — ` : ''}reload the page to reconnect to subfix.`;
      message = message.charAt(0).toUpperCase() + message.slice(1);
    }
    return new ApiError(status, err.code || `http_${status}`, message, err.details);
  }

  // ---------------------------------------------------------------- error banner

  const errorEl = $('#error');

  function showError(message) {
    const text = h('span', { class: 'banner-text' }, String(message));
    const close = h('button', {
      class: 'banner-close', type: 'button', 'aria-label': 'Dismiss error', title: 'Dismiss',
      onclick: clearError,
    }, icon('x'));
    errorEl.replaceChildren(icon('alert', 'banner-icon'), text, close);
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.hidden = true;
    errorEl.replaceChildren();
  }

  function reportError(err) {
    if (err && err.code === 'aborted') return;
    console.error(err);
    showError(err && err.message ? err.message : String(err));
  }

  /** Run an async UI action: disables the button meanwhile and shows API errors. */
  async function action(button, fn) {
    if (button && button.dataset.busy === '1') return undefined;
    if (button) {
      button.dataset.busy = '1';
      button.setAttribute('aria-busy', 'true');
    }
    try {
      return await fn();
    } catch (err) {
      reportError(err);
      return undefined;
    } finally {
      if (button) {
        delete button.dataset.busy;
        button.removeAttribute('aria-busy');
      }
    }
  }

  // ---------------------------------------------------------------- config / header / footer

  async function loadConfig() {
    const cfg = await api('GET', '/api/config');
    state.config = cfg;
    if (cfg.formats) state.formats = cfg.formats;
    state.languages = new Map((cfg.languages || []).map((l) => [l.code, l.name]));
    renderConfig();
  }

  function shortGpu(name) {
    return String(name || 'GPU')
      .replace(/^NVIDIA\s+/i, '')
      .replace(/^GeForce\s+/i, '')
      .replace(/\s+Laptop GPU$/i, '')
      .replace(/\s+GPU$/i, '')
      .trim();
  }

  function modelName(desc) {
    return desc ? String(desc).split(' on ')[0].trim() : '';
  }

  function renderConfig() {
    const cfg = state.config;
    const dev = cfg.device || {};
    const model = modelName(cfg.defaults && cfg.defaults.transcribe);
    const where = dev.cuda ? `GPU: ${shortGpu(dev.gpu)}` : 'CPU only';
    setText($('#device-text'), model ? `${where} · ${model}` : where);
    const titleParts = [];
    if (dev.gpu) titleParts.push(`${dev.gpu}${dev.vram_mb ? ` (${Math.round(dev.vram_mb / 1024)} GB VRAM)` : ''}`);
    if (cfg.defaults && cfg.defaults.transcribe) titleParts.push(`Transcribe: ${cfg.defaults.transcribe}`);
    if (cfg.defaults && cfg.defaults.translate) titleParts.push(`Translate: ${cfg.defaults.translate}`);
    $('#device').title = titleParts.join('\n');
    $('#device').dataset.device = dev.cuda ? 'gpu' : 'cpu';

    const translateModel = modelName(cfg.defaults && cfg.defaults.translate);
    if (translateModel) {
      setText($('#translate-hint'),
        `For movies in another language. Slower — uses the ${translateModel} model.`);
    }
    if (cfg.upload_dir) setText($('#upload-dir'), cfg.upload_dir);
    if (cfg.version) setText($('#footer-version'), `subfix ${cfg.version} · runs entirely on this computer.`);

    // Languages (keep the first "Auto-detect" option and the current choice).
    const select = $('#language');
    const current = select.value;
    const opts = [h('option', { value: '' }, 'Auto-detect')];
    const langs = [...(cfg.languages || [])].sort((a, b) => a.name.localeCompare(b.name));
    for (const lang of langs) opts.push(h('option', { value: lang.code }, `${lang.name} (${lang.code})`));
    select.replaceChildren(...opts);
    select.value = state.languages.has(current) ? current : '';

    // Sync modes from config, keeping the friendly labels.
    if (Array.isArray(cfg.sync_modes) && cfg.sync_modes.length) {
      const syncSelect = $('#sync-mode');
      const labels = new Map([...syncSelect.options].map((o) => [o.value, o.textContent]));
      const cur = syncSelect.value || 'auto';
      syncSelect.replaceChildren(...cfg.sync_modes.map((m) => h('option', { value: m }, labels.get(m) || m)));
      syncSelect.value = cfg.sync_modes.includes(cur) ? cur : cfg.sync_modes[0];
    }
    renderFormats();
  }

  function renderFormats() {
    const f = state.formats;
    const groups = [['Video', f.video], ['Audio', f.audio], ['Subtitles (to sync)', f.subtitle]];
    const out = [];
    for (const [label, exts] of groups) {
      out.push(h('dt', null, label));
      out.push(h('dd', null, (exts || []).map((e) => h('span', { class: 'fmt' }, e))));
    }
    $('#formats').replaceChildren(...out);
    $('#file-input').setAttribute('accept', [...f.video, ...f.audio, ...f.subtitle].join(','));
  }

  // ---------------------------------------------------------------- selection: media + subs

  function setMedia(media) {
    const prev = state.media;
    if (prev && prev.upload_id && (!media || prev.upload_id !== media.upload_id)) discardUpload(prev.upload_id);
    state.media = media;
    renderMedia();
    renderOptionsState();
  }

  function setSubs(subs) {
    const prev = state.subs;
    if (prev && prev.upload_id && (!subs || prev.upload_id !== subs.upload_id)) discardUpload(prev.upload_id);
    state.subs = subs;
    if (subs) {
      const generate = document.querySelector('input[name=mode][value=generate]');
      if (generate.checked) document.querySelector('input[name=mode][value=sync]').checked = true;
    }
    renderSubs();
    renderOptionsState();
  }

  function discardUpload(uploadId) {
    api('DELETE', `/api/uploads/${encodeURIComponent(uploadId)}`).catch(() => {});
  }

  function renderMedia() {
    const card = $('#media-card');
    const m = state.media;
    if (!m) {
      card.hidden = true;
      card.replaceChildren();
      return;
    }
    const kindLabel = m.kind === 'audio' ? 'Audio' : 'Video';
    const meta = [kindLabel];
    if (m.duration_seconds !== null && m.duration_seconds !== undefined) meta.push(formatDuration(m.duration_seconds));
    if (m.size_bytes !== null && m.size_bytes !== undefined) meta.push(formatSize(m.size_bytes));
    const source = m.source === 'upload'
      ? h('span', { class: 'tag tag-warn' }, 'uploaded copy')
      : h('span', { class: 'tag' }, 'used in place');
    const info = h('div', { class: 'file-info' },
      h('div', { class: 'file-name', title: m.path || m.name }, m.name),
      h('div', { class: 'file-meta' }, meta.join(' · '), ' ', source),
      m.source === 'path' && m.path ? h('div', { class: 'file-path', title: m.path }, m.path) : null,
      m.sidecar
        ? h('div', { class: 'sidecar' }, icon('check', 'icon icon-ok'),
          h('span', null, 'Subtitle found next to the movie: ', h('strong', null, m.sidecar.name)))
        : null);
    const remove = h('button', {
      class: 'btn btn-ghost btn-square', id: 'clear-media', type: 'button',
      'aria-label': `Remove ${m.name}`, title: 'Remove', onclick: () => setMedia(null),
    }, icon('x'));
    card.replaceChildren(
      h('div', { class: `file-icon kind-${m.kind}` }, icon(m.kind === 'audio' ? 'audio' : 'film')),
      info, remove);
    card.hidden = false;
  }

  function renderSubs() {
    const s = state.subs;
    const nameEl = $('#subs-name');
    if (s) {
      setText(nameEl, s.name);
      nameEl.title = s.path || s.name;
      nameEl.classList.add('has-subs');
    } else {
      setText(nameEl, 'None chosen');
      nameEl.title = '';
      nameEl.classList.remove('has-subs');
    }
    $('#clear-subs').disabled = !s;
  }

  function currentMode() {
    const checked = document.querySelector('input[name=mode]:checked');
    return checked ? checked.value : 'auto';
  }

  /** Dim the options the chosen mode doesn't use and explain why. */
  function renderOptionsState() {
    const mode = currentMode();
    const m = state.media;
    const willSync = mode === 'sync' || (mode === 'auto' && (state.subs || (m && m.sidecar)));
    $('#subs-field').classList.toggle('is-dim', mode === 'generate');
    $('#sync-field').classList.toggle('is-dim', mode === 'generate');
    $('#translate-field').classList.toggle('is-dim', mode === 'sync');
    let hint;
    if (mode === 'generate') hint = 'Not used when generating new subtitles.';
    else if (state.subs) hint = state.subs.source === 'upload' ? 'This uploaded subtitle will be synced to the movie.' : 'This subtitle will be synced to the movie.';
    else if (m && m.sidecar) hint = `Leave empty to use ${m.sidecar.name} (found next to the movie).`;
    else if (mode === 'sync') hint = 'Sync needs a subtitle: choose one, drop it above, or put it next to the movie with the same name.';
    else hint = 'Leave empty to use the subtitle next to the movie (same name).';
    setText($('#subs-hint'), hint);
    $('#translate-field').title = willSync ? 'Translation only applies when new subtitles are generated.' : '';
  }

  async function inspectPath(path) {
    return api('POST', '/api/inspect', { path });
  }

  async function useInspected(info) {
    if (info.kind === 'subtitle') {
      setSubs({ path: info.path, name: info.name, source: 'path' });
    } else {
      setMedia({
        path: info.path, name: info.name, kind: info.kind, size_bytes: info.size_bytes,
        duration_seconds: info.duration_seconds, sidecar: info.sidecar || null, source: 'path',
      });
    }
    clearError();
  }

  async function chooseMedia() {
    const res = await api('POST', '/api/pick', { kind: 'media' });
    if (!res || !res.path) return;
    await useInspected(await inspectPath(res.path));
  }

  async function chooseSubs() {
    const res = await api('POST', '/api/pick', { kind: 'subtitle' });
    if (!res || !res.path) return;
    const kind = classify(res.path);
    if (kind && kind !== 'subtitle') {
      showError(`${baseName(res.path)} is not a subtitle file. Choose a ${state.formats.subtitle.join(' ')} file.`);
      return;
    }
    const info = await inspectPath(res.path);
    if (info.kind !== 'subtitle') {
      showError(`${info.name} is not a subtitle file. Choose a ${state.formats.subtitle.join(' ')} file.`);
      return;
    }
    setSubs({ path: info.path, name: info.name, source: 'path' });
    clearError();
  }

  async function addPastedPath() {
    const input = $('#path-input');
    const path = cleanPath(input.value);
    input.value = path;
    if (!path) {
      showError('Paste the full path of a movie, e.g. D:\\Movies\\Movie.mkv');
      input.focus();
      return;
    }
    if (!classify(path)) {
      showError(unsupportedMessage(path));
      return;
    }
    await useInspected(await inspectPath(path));
    input.value = '';
  }

  // ---------------------------------------------------------------- uploads (drag and drop)

  function uploadFile(file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const url = `/api/uploads?filename=${encodeURIComponent(file.name)}&size=${file.size}`;
      xhr.open('POST', url);
      xhr.setRequestHeader('X-Subfix-Token', TOKEN);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      state.upload = { xhr, file };
      showUpload(file, 0, file.size);
      xhr.upload.addEventListener('progress', (e) => {
        showUpload(file, e.loaded, e.lengthComputable ? e.total : file.size);
      });
      xhr.addEventListener('load', () => {
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          data = null;
        }
        if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
        else reject(errorFromBody(xhr.status, data));
      });
      xhr.addEventListener('error', () => reject(new ApiError(0, 'network', `Upload of ${file.name} failed — the connection to subfix was lost.`)));
      xhr.addEventListener('abort', () => reject(new ApiError(0, 'aborted', 'Upload cancelled.')));
      xhr.send(file);
    }).finally(() => {
      state.upload = null;
      hideUpload();
    });
  }

  function showUpload(file, loaded, total) {
    const pct = total > 0 ? Math.min(100, Math.floor((loaded / total) * 100)) : 0;
    $('#upload').hidden = false;
    setText($('#upload-name'), `Uploading ${file.name}`);
    setText($('#upload-pct'), `${pct}%`);
    $('#upload-fill').style.width = `${pct}%`;
    setText($('#upload-detail'), `${formatSize(loaded)} of ${formatSize(total)}`);
    $('#drop-zone').classList.add('is-busy');
  }

  function hideUpload() {
    $('#upload').hidden = true;
    $('#upload-fill').style.width = '0%';
    $('#drop-zone').classList.remove('is-busy');
  }

  async function handleFiles(fileList) {
    const files = [...(fileList || [])];
    if (!files.length) return;
    if (state.upload) {
      showError('Please wait until the current upload has finished.');
      return;
    }
    const bad = files.filter((f) => !classify(f.name));
    if (bad.length) {
      showError(unsupportedMessage(bad[0].name));
      if (bad.length === files.length) return;
    } else {
      clearError();
    }
    // At most one media file and one subtitle per drop; media first.
    const media = files.find((f) => ['video', 'audio'].includes(classify(f.name)));
    const subs = files.find((f) => classify(f.name) === 'subtitle');
    for (const file of [media, subs]) {
      if (!file) continue;
      const kind = classify(file.name);
      const res = await uploadFile(file);
      if (kind === 'subtitle' || res.kind === 'subtitle') {
        setSubs({ upload_id: res.upload_id, path: res.path, name: res.name || file.name, source: 'upload' });
      } else {
        const media = {
          upload_id: res.upload_id, path: res.path, name: res.name || file.name, kind: res.kind || kind,
          size_bytes: res.size_bytes ?? file.size, duration_seconds: null, sidecar: null, source: 'upload',
        };
        setMedia(media);
        // Duration is nice to have; ignore failures.
        if (res.path) {
          inspectPath(res.path).then((info) => {
            if (state.media && state.media.upload_id === media.upload_id && info) {
              state.media = { ...state.media, duration_seconds: info.duration_seconds };
              renderMedia();
            }
          }).catch(() => {});
        }
      }
    }
  }

  // ---------------------------------------------------------------- submit

  async function submitJob() {
    const m = state.media;
    if (state.upload) {
      showError('Please wait until the upload has finished.');
      return;
    }
    if (!m) {
      showError('Choose a movie first — or paste its path, or drop it on the page.');
      $('#choose-media').focus();
      return;
    }
    const mode = currentMode();
    if (mode === 'sync' && !state.subs && !m.sidecar && m.source === 'upload') {
      showError('Sync needs a subtitle file: choose one, or drop it on the page together with the movie.');
      return;
    }
    const language = $('#language').value;
    const body = {
      media: m.upload_id ? { upload_id: m.upload_id } : { path: m.path },
      subs: state.subs ? (state.subs.upload_id ? { upload_id: state.subs.upload_id } : { path: state.subs.path }) : null,
      mode,
      sync_mode: $('#sync-mode').value || 'auto',
      translate: $('#translate').checked,
      language: language || null,
    };
    const job = await api('POST', '/api/jobs', body);
    clearError();
    // The uploads now belong to the job (the server deletes them when it finishes).
    state.media = null;
    state.subs = null;
    renderMedia();
    renderSubs();
    renderOptionsState();
    const rec = upsertJob(job);
    if (rec) {
      rec.el.classList.add('is-new');
      setTimeout(() => rec.el.classList.remove('is-new'), 1600);
      rec.el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  // ---------------------------------------------------------------- jobs

  const jobsEl = $('#jobs');

  function upsertJob(incoming) {
    if (!incoming || !incoming.id) return null;
    const { log_tail: logTail, ...job } = incoming;
    let rec = state.jobs.get(job.id);
    if (!rec) {
      rec = { job, el: null, logs: [], logLoaded: false, cancelRequested: false };
      rec.el = buildJobCard(rec);
      state.jobs.set(job.id, rec);
    } else {
      const cur = rec.job;
      // Never go backwards (a re-fetch can race with newer SSE events).
      if (STATE_RANK[job.state] < STATE_RANK[cur.state]) return rec;
      if (FINAL_STATES.has(cur.state) && cur.state !== job.state) return rec;
      if (job.state === 'running' && cur.state === 'running' && job.progress && cur.progress &&
          job.progress.overall < cur.progress.overall) {
        job.progress = { ...job.progress, overall: cur.progress.overall };
      }
      rec.job = { ...cur, ...job };
    }
    if (Array.isArray(logTail)) setLogs(rec, logTail);
    if (rec.job.state !== 'running') rec.cancelRequested = false;
    updateJobCard(rec);
    orderJobs();
    return rec;
  }

  function buildJobCard(rec) {
    const { job } = rec;
    const el = h('article', { class: 'job', dataset: { jobId: job.id, state: job.state } });
    const cancel = h('button', { class: 'btn btn-sm btn-danger-ghost cancel', type: 'button' }, 'Cancel');
    cancel.addEventListener('click', () => action(cancel, () => cancelJob(rec)));

    const bar = h('div', {
      class: 'progress-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': '0', 'aria-label': `Progress of ${job.media ? job.media.name : job.id}`,
    }, h('div', { class: 'progress-fill' }));

    const log = h('pre', { class: 'log', tabindex: '0', 'aria-label': 'Job log' });
    const logWrap = h('details', { class: 'log-wrap' },
      h('summary', null, h('span', null, 'Live log'), h('span', { class: 'log-count muted' })),
      log);
    logWrap.addEventListener('toggle', () => {
      if (logWrap.open) {
        if (!rec.logLoaded) loadLogTail(rec);
        log.scrollTop = log.scrollHeight;
      }
    });

    const download = h('a', { class: 'btn btn-primary download', download: '' },
      icon('download', 'btn-icon'), 'Download .srt');
    const openFolder = h('button', { class: 'btn open-folder', type: 'button' }, icon('folder', 'btn-icon'), 'Open folder');
    openFolder.addEventListener('click', () => action(openFolder, async () => {
      await api('POST', `/api/jobs/${encodeURIComponent(rec.job.id)}/open-folder`);
      flashLabel(openFolder, 'Opened');
    }));

    el.append(
      h('div', { class: 'job-head' },
        h('div', { class: 'job-icon' }),
        h('div', { class: 'job-title' },
          h('h3', { class: 'job-name' }),
          h('div', { class: 'job-meta muted small' })),
        h('span', { class: 'chip state-chip' }),
        cancel),
      h('div', { class: 'job-progress' },
        h('div', { class: 'progress-line' },
          h('span', { class: 'stage' }),
          h('span', { class: 'pct' })),
        bar,
        h('div', { class: 'progress-sub small' },
          h('span', { class: 'message muted' }),
          h('span', { class: 'eta' })),
        h('p', { class: 'queue-position small' })),
      h('div', { class: 'job-result' },
        h('p', { class: 'summary' }, icon('check', 'icon icon-ok'), h('span', { class: 'summary-text' }),
          h('span', { class: 'summary-detail' })),
        h('p', { class: 'output small' }, h('span', { class: 'muted' }, 'Saved to '), h('code', { class: 'output-path' })),
        h('ul', { class: 'notes small' }),
        h('div', { class: 'actions' }, download, openFolder),
        h('details', { class: 'preview-wrap', open: true },
          h('summary', null, 'Preview'),
          h('div', { class: 'preview-scroll' },
            h('table', { class: 'preview' },
              h('thead', null, h('tr', null, h('th', { scope: 'col' }, '#'), h('th', { scope: 'col' }, 'Time'), h('th', { scope: 'col' }, 'Text'))),
              h('tbody'))))),
      h('div', { class: 'job-error', role: 'alert' }, icon('alert', 'icon'), h('div', null,
        h('strong', null, 'Failed: '), h('span', { class: 'error-message' }))),
      h('p', { class: 'job-cancelled muted' }, 'Cancelled'),
      logWrap);
    return el;
  }

  function flashLabel(button, text) {
    const original = [...button.childNodes];
    button.replaceChildren(icon('check', 'btn-icon'), text);
    setTimeout(() => button.replaceChildren(...original), 1500);
  }

  function updateJobCard(rec) {
    const { job, el } = rec;
    const st = job.state;
    el.dataset.state = st;
    const media = job.media || {};
    const p = job.progress || {};

    // Head.
    const iconBox = $('.job-icon', el);
    const iconName = media.kind === 'audio' ? 'audio' : 'film';
    if (iconBox.dataset.icon !== iconName) {
      iconBox.dataset.icon = iconName;
      iconBox.replaceChildren(icon(iconName));
    }
    const nameEl = $('.job-name', el);
    setText(nameEl, media.name || job.id);
    nameEl.title = media.path || '';
    const opts = job.options || {};
    const meta = [MODE_LABEL[opts.mode] || opts.mode || 'Automatic'];
    if (opts.translate) meta.push('→ English');
    if (opts.language) meta.push(languageName(opts.language));
    if (media.duration_seconds) meta.push(formatDuration(media.duration_seconds));
    if (media.size_bytes) meta.push(formatSize(media.size_bytes));
    if (media.source === 'upload') meta.push('uploaded copy');
    if (job.subs && job.subs.name) meta.push(`subtitle: ${job.subs.name}`);
    setText($('.job-meta', el), meta.join(' · '));

    const chip = $('.state-chip', el);
    chip.dataset.state = st;
    setText(chip, rec.cancelRequested && st === 'running' ? 'Cancelling…' : STATE_LABEL[st] || st);

    const cancel = $('.cancel', el);
    cancel.hidden = !(st === 'queued' || st === 'running');
    cancel.disabled = rec.cancelRequested;
    setText(cancel, rec.cancelRequested ? 'Cancelling…' : 'Cancel');

    // Progress.
    const progressBox = $('.job-progress', el);
    progressBox.hidden = FINAL_STATES.has(st);
    const overall = st === 'done' ? 1 : Math.max(0, Math.min(1, Number(p.overall) || 0));
    const pct = Math.floor(overall * 100);
    const bar = $('.progress-bar', el);
    bar.setAttribute('aria-valuenow', String(pct));
    bar.style.setProperty('--p', `${overall * 100}%`);
    $('.progress-fill', el).style.width = `${overall * 100}%`;
    bar.classList.toggle('is-indeterminate', st === 'running' && (p.stage_progress === null || p.stage_progress === undefined));
    let stageText = p.stage_label || (st === 'queued' ? 'Waiting in queue' : 'Starting…');
    if (st === 'running' && rec.cancelRequested) stageText = 'Cancelling…';
    setText($('.stage', el), stageText);
    setText($('.pct', el), st === 'queued' ? '' : `${pct}%`);
    setText($('.message', el), st === 'running' ? p.message || '' : '');
    const etaEl = $('.eta', el);
    const showEta = st === 'running' && p.eta_seconds !== null && p.eta_seconds !== undefined;
    etaEl.hidden = !showEta;
    setText(etaEl, showEta ? formatEta(p.eta_seconds) : '');
    const posEl = $('.queue-position', el);
    posEl.hidden = st !== 'queued';

    // Result.
    const resultBox = $('.job-result', el);
    resultBox.hidden = st !== 'done' || !job.result;
    if (st === 'done' && job.result) renderResult(rec, resultBox);

    const errBox = $('.job-error', el);
    errBox.hidden = st !== 'failed';
    if (st === 'failed') {
      const e = job.error || {};
      setText($('.error-message', el), e.message || 'Something went wrong.');
      errBox.title = e.code ? `Error code: ${e.code}` : '';
    }
    $('.job-cancelled', el).hidden = st !== 'cancelled';
    renderLogCount(rec);
  }

  function renderResult(rec, box) {
    const r = rec.job.result;
    if (box.dataset.rendered === '1') return;
    box.dataset.rendered = '1';
    const elapsed = r.elapsed_seconds !== null && r.elapsed_seconds !== undefined ? ` in ${formatDuration(r.elapsed_seconds)}` : '';
    const what = r.kind === 'synced' ? 'Synced' : 'Generated';
    const lang = r.language ? ` · ${languageName(r.language)}` : '';
    setText($('.summary-text', box), `${what} ${r.cues} subtitle lines${elapsed}${lang}`);
    $('.summary-text', box).title = r.summary || '';
    // For syncs the backend summary says what was corrected (e.g. "drift: offset +1.2 s").
    setText($('.summary-detail', box), r.kind === 'synced' && r.summary ? r.summary : '');
    const pathEl = $('.output-path', box);
    setText(pathEl, r.output_path || r.output_name || '');
    pathEl.title = r.output_path || '';
    const notes = $('.notes', box);
    notes.replaceChildren(...(r.notes || []).map((n) => h('li', null, n)));
    notes.hidden = !(r.notes && r.notes.length);
    const dl = $('.download', box);
    dl.href = `/api/jobs/${encodeURIComponent(rec.job.id)}/srt?token=${encodeURIComponent(TOKEN)}`;
    if (r.output_name) dl.setAttribute('download', r.output_name);
    const rows = (r.preview || []).slice(0, 20).map((c) => h('tr', null,
      h('td', { class: 'num' }, c.index),
      h('td', { class: 'time' }, h('span', null, formatClock(c.start)), ' → ', h('span', null, formatClock(c.end))),
      h('td', { class: 'text' }, c.text)));
    $('.preview tbody', box).replaceChildren(...rows);
    const previewWrap = $('.preview-wrap', box);
    previewWrap.hidden = rows.length === 0;
    setText($('summary', previewWrap), `Preview — first ${rows.length} of ${r.cues} lines`);
  }

  async function cancelJob(rec) {
    rec.cancelRequested = rec.job.state === 'running';
    updateJobCard(rec);
    try {
      const job = await api('POST', `/api/jobs/${encodeURIComponent(rec.job.id)}/cancel`);
      upsertJob(job);
    } catch (err) {
      rec.cancelRequested = false;
      updateJobCard(rec);
      if (err.code === 'job_finished') return; // already done; the event will update the card
      throw err;
    }
  }

  function sortKey(job) {
    const rank = job.state === 'running' ? 0 : job.state === 'queued' ? 1 : 2;
    return rank;
  }

  function compareJobs(a, b) {
    const ra = sortKey(a);
    const rb = sortKey(b);
    if (ra !== rb) return ra - rb;
    if (ra === 1) {
      const pa = a.queue_position ?? Number.MAX_SAFE_INTEGER;
      const pb = b.queue_position ?? Number.MAX_SAFE_INTEGER;
      if (pa !== pb) return pa - pb;
      return String(a.created_at).localeCompare(String(b.created_at));
    }
    const fa = String(a.finished_at || a.created_at || '');
    const fb = String(b.finished_at || b.created_at || '');
    return fb.localeCompare(fa);
  }

  let orderScheduled = false;
  function orderJobs() {
    if (orderScheduled) return;
    orderScheduled = true;
    queueMicrotask(() => {
      orderScheduled = false;
      const recs = [...state.jobs.values()].sort((a, b) => compareJobs(a.job, b.job));
      recs.forEach((rec, i) => {
        if (jobsEl.children[i] !== rec.el) jobsEl.insertBefore(rec.el, jobsEl.children[i] || null);
      });
      // Queue positions are numbered locally so they stay right even if the server only
      // sends events for jobs whose state changed.
      let pos = 0;
      for (const rec of recs) {
        if (rec.job.state !== 'queued') continue;
        pos += 1;
        const el = $('.queue-position', rec.el);
        rec.el.dataset.position = String(pos);
        setText(el, pos === 1 ? `${ordinalPosition(pos)} — starts when the current job finishes` : ordinalPosition(pos));
      }
      renderQueueSummary(recs);
    });
  }

  function renderQueueSummary(recs) {
    const running = recs.filter((r) => r.job.state === 'running');
    const queued = recs.filter((r) => r.job.state === 'queued').length;
    const parts = [];
    if (running.length) parts.push('1 running');
    if (queued) parts.push(`${queued} waiting`);
    setText($('#queue-summary'), parts.join(' · '));
    $('#jobs-empty').hidden = recs.length > 0;
    if (running.length) {
      const p = running[0].job.progress || {};
      document.title = `${Math.floor((p.overall || 0) * 100)}% · ${running[0].job.media ? running[0].job.media.name : ''} — subfix`;
    } else {
      document.title = 'subfix';
    }
  }

  // ---------------------------------------------------------------- logs

  function logKey(line) {
    return `${line.time}|${line.level}|${line.message}`;
  }

  function setLogs(rec, lines) {
    // Keep lines that arrived by SSE after the tail was taken.
    const known = new Set(lines.map(logKey));
    const extra = rec.logs.filter((l) => !known.has(logKey(l)) && String(l.time) >= String((lines.at(-1) || {}).time || ''));
    rec.logs = [...lines, ...extra].slice(-LOG_LIMIT);
    rec.logLoaded = true;
    renderLog(rec, true);
  }

  function appendLog(line) {
    const rec = state.jobs.get(line.job_id);
    if (!rec) return;
    rec.logs.push(line);
    if (rec.logs.length > LOG_LIMIT) rec.logs.splice(0, rec.logs.length - LOG_LIMIT);
    const log = $('.log', rec.el);
    const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    if (log.childNodes.length >= LOG_LIMIT) log.firstChild.remove();
    log.append(logLineNode(line));
    if (stick) log.scrollTop = log.scrollHeight;
    renderLogCount(rec);
  }

  function logLineNode(line) {
    const level = String(line.level || 'INFO').toUpperCase();
    return h('span', { class: `log-line lvl-${level.toLowerCase()}` },
      h('span', { class: 'log-time' }, formatLogTime(line.time)), ' ',
      level !== 'INFO' ? h('span', { class: 'log-level' }, `${level} `) : null,
      line.message, '\n');
  }

  function renderLog(rec, scroll) {
    const log = $('.log', rec.el);
    log.replaceChildren(...rec.logs.map(logLineNode));
    if (scroll) log.scrollTop = log.scrollHeight;
    renderLogCount(rec);
  }

  function renderLogCount(rec) {
    const n = rec.logs.length;
    setText($('.log-count', rec.el), n ? `${n} line${n === 1 ? '' : 's'}` : '');
  }

  async function loadLogTail(rec) {
    try {
      const job = await api('GET', `/api/jobs/${encodeURIComponent(rec.job.id)}`);
      upsertJob(job);
    } catch (err) {
      if (err.code !== 'job_not_found') reportError(err);
    }
  }

  // ---------------------------------------------------------------- history

  const historyEl = $('#history');

  async function loadHistory() {
    const res = await api('GET', `/api/history?limit=${HISTORY_LIMIT}`);
    state.history = (res && res.items) || [];
    renderHistory();
  }

  function addHistoryItem(item) {
    if (!item || !item.id) return;
    state.history = [item, ...state.history.filter((x) => x.id !== item.id)].slice(0, HISTORY_LIMIT);
    renderHistory();
  }

  function renderHistory() {
    const rows = state.history.map((item) => {
      const mode = [MODE_LABEL[item.mode] || item.mode];
      if (item.translate) mode.push('→ English');
      const lang = item.language ? languageName(item.language) : '';
      const result = h('td', { class: 'h-state' },
        h('span', { class: 'chip', dataset: { state: item.state } }, STATE_LABEL[item.state] || item.state));
      if (item.state === 'failed' && item.error && item.error.message) {
        result.append(h('div', { class: 'h-error small' }, item.error.message));
      } else if (item.summary) {
        result.title = item.summary;
      }
      return h('tr', { class: 'history-row', dataset: { jobId: item.id, state: item.state } },
        h('td', { class: 'h-name' }, h('span', { class: 'truncate', title: item.media_path || item.name }, item.name)),
        h('td', null, mode.join(' '), lang ? h('div', { class: 'muted small' }, lang) : null),
        h('td', { class: 'num' }, formatDuration(item.media_duration_seconds)),
        h('td', { class: 'num' }, formatDuration(item.elapsed_seconds)),
        result,
        h('td', { class: 'h-output' }, item.output_path
          ? h('code', { class: 'truncate', title: item.output_path }, item.output_path)
          : h('span', { class: 'muted' }, '—')),
        h('td', { class: 'h-when' }, h('time', { datetime: item.finished_at || '' }, formatWhen(item.finished_at))));
    });
    historyEl.replaceChildren(...rows);
    $('#history-empty').hidden = rows.length > 0;
    $('#clear-history').disabled = rows.length === 0;
  }

  async function clearHistory() {
    await api('DELETE', '/api/history');
    state.history = [];
    renderHistory();
  }

  // ---------------------------------------------------------------- live events (SSE)

  let source = null;
  let retryTimer = null;
  let resyncSeq = 0;

  function setConn(kind, text) {
    const el = $('#conn');
    el.dataset.conn = kind;
    setText($('#conn-text'), text);
  }

  function connectEvents() {
    clearTimeout(retryTimer);
    if (source) source.close();
    setConn('connecting', 'Connecting…');
    const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
    source = es;
    es.addEventListener('open', () => {
      setConn('live', 'Live');
      resync();
    });
    es.addEventListener('hello', () => setConn('live', 'Live'));
    es.addEventListener('job', (e) => withJson(e, upsertJob));
    es.addEventListener('log', (e) => withJson(e, appendLog));
    es.addEventListener('history', (e) => withJson(e, addHistoryItem));
    es.addEventListener('error', () => {
      if (source !== es) return;
      setConn('offline', 'Reconnecting…');
      if (es.readyState === EventSource.CLOSED) {
        // The browser gave up (e.g. HTTP error); retry ourselves after checking the server.
        retryTimer = setTimeout(checkAndReconnect, 2000);
      }
    });
  }

  function withJson(e, fn) {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch {
      return;
    }
    fn(data);
  }

  async function checkAndReconnect() {
    try {
      await api('GET', '/api/config');
      connectEvents();
    } catch (err) {
      if (err.status === 403) {
        setConn('offline', 'Disconnected');
        showError('The subfix server was restarted. Reload this page to reconnect.');
        return;
      }
      retryTimer = setTimeout(checkAndReconnect, 3000);
    }
  }

  /** Re-fetch everything after (re)connecting; SSE events then keep it fresh. */
  async function resync() {
    const seq = ++resyncSeq;
    try {
      const [jobsRes] = await Promise.all([api('GET', '/api/jobs'), loadHistory()]);
      if (seq !== resyncSeq) return;
      for (const job of (jobsRes && jobsRes.jobs) || []) upsertJob(job);
      // Refresh logs of running jobs and of any log the user has open.
      for (const rec of state.jobs.values()) {
        const open = $('.log-wrap', rec.el).open;
        if (rec.job.state === 'running' || (open && !FINAL_STATES.has(rec.job.state)) || (open && !rec.logLoaded)) {
          loadLogTail(rec);
        }
      }
    } catch (err) {
      reportError(err);
    }
  }

  // ---------------------------------------------------------------- wiring

  function wire() {
    $('#choose-media').addEventListener('click', (e) => action(e.currentTarget, chooseMedia));
    $('#path-form').addEventListener('submit', (e) => {
      e.preventDefault();
      action($('#path-add'), addPastedPath);
    });
    $('#path-input').addEventListener('paste', () => {
      // Clean the value right after the paste lands (quotes from "Copy as path").
      setTimeout(() => {
        const input = $('#path-input');
        const cleaned = cleanPath(input.value);
        if (cleaned !== input.value) input.value = cleaned;
      }, 0);
    });
    $('#choose-subs').addEventListener('click', (e) => action(e.currentTarget, chooseSubs));
    $('#clear-subs').addEventListener('click', () => setSubs(null));
    $('#submit').addEventListener('click', (e) => action(e.currentTarget, submitJob));
    $('#clear-history').addEventListener('click', (e) => action(e.currentTarget, clearHistory));
    $('#upload-cancel').addEventListener('click', () => {
      if (state.upload) state.upload.xhr.abort();
    });
    for (const radio of document.querySelectorAll('input[name=mode]')) {
      radio.addEventListener('change', renderOptionsState);
    }

    // Drop zone: click / keyboard opens the browser's file chooser (an upload, like a drop).
    const zone = $('#drop-zone');
    const input = $('#file-input');
    zone.addEventListener('click', (e) => {
      if (e.target === input) return;
      input.click();
    });
    zone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        input.click();
      }
    });
    input.addEventListener('change', () => {
      const files = [...input.files];
      input.value = '';
      action(null, () => handleFiles(files));
    });

    // Drag and drop anywhere on the page lands in the drop zone.
    let dragDepth = 0;
    const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
    window.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth += 1;
      document.body.classList.add('is-dragging');
    });
    window.addEventListener('dragleave', () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) document.body.classList.remove('is-dragging');
    });
    window.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      zone.classList.toggle('is-over', zone.contains(e.target));
    });
    window.addEventListener('drop', (e) => {
      e.preventDefault();
      dragDepth = 0;
      document.body.classList.remove('is-dragging');
      zone.classList.remove('is-over');
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        action(null, () => handleFiles(e.dataTransfer.files));
      }
    });

    window.addEventListener('beforeunload', (e) => {
      if (state.upload) {
        e.preventDefault();
        e.returnValue = '';
      }
    });
  }

  async function init() {
    wire();
    renderFormats();
    renderSubs();
    renderOptionsState();
    renderHistory();
    orderJobs();
    if (!TOKEN_OK) {
      setConn('offline', 'Not connected');
      showError('This page must be opened by subfix (run "subfix web"), not as a file.');
      return;
    }
    try {
      await loadConfig();
    } catch (err) {
      reportError(err);
    }
    connectEvents();
  }

  // Exposed for debugging in the browser console only.
  window.subfixUI = { state, formatEta, formatDuration, cleanPath };

  init();
})();
