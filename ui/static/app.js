'use strict';
/* Control room front end. No framework, no external assets. Everything from the server is put on the page as
   text nodes (never as parsed markup), so data can never become markup. */
(function () {
  let lang = 'bg';
  try { lang = localStorage.getItem('lang') || 'bg'; } catch (e) { /* storage blocked: stay with the default */ }
  const T = (bg, en) => (lang === 'bg' ? bg : en);
  let TOKEN = '';
  const svgNS = 'http://www.w3.org/2000/svg';

  // ---------------------------------------------------------------- DOM helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style') el.style.cssText = v;   // via the CSSOM: a style *attribute* would be blocked by the CSP
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    add(el, ...kids);
    return el;
  }
  function add(el, ...kids) {
    for (const k of kids.flat(Infinity)) {
      if (k === null || k === undefined || k === false) continue;
      el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
    }
  }
  function svg(tag, attrs) {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  const fmt = (x, d = 2) => (x === null || x === undefined || x === '' ? '–' : (typeof x === 'number' ? x.toLocaleString(undefined, { maximumFractionDigits: d }) : String(x)));
  const short = (ts) => (ts ? String(ts).replace('T', ' ').replace(/\.\d+Z$/, 'Z').replace(/Z$/, '') : '–');
  function badge(text, kind) { return h('span', { class: 'badge ' + (kind || '') }, text); }
  const kindOf = (s) => ({ passed: 'ok', valid: 'ok', ok: 'ok', NORMAL: 'ok', APPROVED: 'ok', REDUCED: 'warn', REDUCE_ONLY: 'warn', superseded: 'warn',
    DENIED: 'bad', HALTED: 'bad', failed: 'bad', voided: 'bad', running: 'warn' }[s] || 'plain');
  function toast(msg) { const t = h('div', { class: 'toast' }, msg); document.body.appendChild(t); setTimeout(() => t.remove(), 1800); }
  function copyBtn(text) {
    return h('button', { onclick: async () => { try { await navigator.clipboard.writeText(text); toast(T('Копирано', 'Copied')); } catch (e) { toast(T('Не може да се копира', 'Cannot copy')); } } }, T('Копирай', 'Copy'));
  }
  function cmdBlock(label, cmd) { return h('div', { class: 'cmd' }, h('div', { class: 'small muted' }, label), h('pre', {}, cmd), copyBtn(cmd)); }

  function table(cols, rows, opts) {
    opts = opts || {};
    const head = h('tr', {}, cols.map((c) => h('th', { class: c.num ? 'num' : '' }, c.label)));
    const body = h('tbody', {});
    rows.forEach((r) => {
      const tr = h('tr', { class: opts.onrow ? 'click' : '' }, cols.map((c) => {
        const v = c.get ? c.get(r) : r[c.key];
        return h('td', { class: (c.num ? 'num' : '') + (c.nowrap ? ' nowrap' : '') }, v instanceof Node ? v : fmt(v, c.digits === undefined ? 2 : c.digits));
      }));
      if (opts.onrow) tr.addEventListener('click', () => opts.onrow(r, tr));
      body.appendChild(tr);
    });
    return h('div', { class: 'table-wrap', style: opts.maxHeight ? 'max-height:' + opts.maxHeight : null }, h('table', {}, h('thead', {}, head), body));
  }
  function kv(obj, skip) {
    const rows = Object.entries(obj || {}).filter(([k]) => !(skip || []).includes(k));
    return h('div', { class: 'table-wrap' }, h('table', {}, h('tbody', {}, rows.map(([k, v]) =>
      h('tr', {}, h('th', {}, k), h('td', { class: 'mono' }, typeof v === 'object' && v !== null ? JSON.stringify(v) : fmt(v, 6)))))));
  }
  function stat(label, value, sub, kind) {
    return h('div', { class: 'card stat' }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value instanceof Node ? value : String(value)),
      sub ? h('div', { class: 'sub' }, sub) : null);
  }
  function bars(counts, kind) {
    const total = Math.max(1, ...Object.values(counts));
    return h('div', { class: 'bars' }, Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
      h('div', { class: 'bar' }, h('div', { class: 'mono' }, k), h('div', { class: 'track' }, h('div', { class: 'fill ' + (kind || ''), style: 'width:' + (100 * v / total) + '%' })), h('div', { class: 'right' }, v))));
  }
  function pre(obj) { return h('pre', {}, typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2)); }

  // ---------------------------------------------------------------- markdown (to DOM nodes, no HTML)
  function inline(text) {
    const out = [];
    const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[1]) out.push(h('code', {}, m[1].slice(1, -1)));
      else if (m[2]) out.push(h('strong', {}, m[2].slice(2, -2)));
      else { const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(m[3]); out.push(h('span', { title: mm[2] }, mm[1])); }
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  function md(text) {
    const root = h('div', { class: 'md' });
    const lines = text.replace(/\r\n/g, '\n').split('\n');
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++; root.appendChild(h('pre', {}, buf.join('\n'))); continue;
      }
      const hd = /^(#{1,4})\s+(.*)$/.exec(line);
      if (hd) { root.appendChild(h('h' + Math.min(hd[1].length, 4), {}, inline(hd[2]))); i++; continue; }
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
        const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        const head = cells(line); i += 2; const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
        root.appendChild(h('table', {}, h('thead', {}, h('tr', {}, head.map((c) => h('th', {}, inline(c))))),
          h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, inline(c))))))));
        continue;
      }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line); const items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')); i++; }
        root.appendChild(h(ordered ? 'ol' : 'ul', {}, items.map((it) => h('li', {}, inline(it))))); continue;
      }
      if (/^>\s?/.test(line)) { const buf = []; while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, '')); root.appendChild(h('blockquote', {}, inline(buf.join(' ')))); continue; }
      if (/^---+$/.test(line.trim())) { root.appendChild(h('hr')); i++; continue; }
      if (!line.trim()) { i++; continue; }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\s*\||\s*([-*]|\d+\.)\s+)/.test(lines[i])) buf.push(lines[i++]);
      root.appendChild(h('p', {}, inline(buf.join(' '))));
    }
    return root;
  }

  // ---------------------------------------------------------------- API
  async function api(path) {
    const r = await fetch('/api/' + path, { headers: { Accept: 'application/json' } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  async function startJob(key, dataset) {
    const r = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-UI-Token': TOKEN },
      body: JSON.stringify({ key, dataset: dataset || null }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    return body;
  }
  let LAST_JOB = null;
  function showJob(out, job) {
    out.classList.remove('hidden'); clear(out);
    add(out, h('div', { class: 'row' }, badge(job.state, kindOf(job.state)), h('span', { class: 'small muted' }, job.label + ' · ' + job.seconds + ' s' + (job.exit_code !== null ? ' · exit ' + job.exit_code : ''))), h('pre', {}, job.output + (job.truncated ? '\n… [cut]' : '')));
  }
  async function runJob(key, dataset, out) {
    out.classList.remove('hidden'); clear(out);
    const status = h('div', { class: 'row' }, badge(T('Стартира…', 'Starting…'), 'warn'));
    const log = h('pre', {}, '');
    add(out, [status, log]);
    let job;
    try { job = await startJob(key, dataset); } catch (e) { clear(status); add(status, badge(e.message, 'bad')); return null; }
    for (;;) {
      job = await api('jobs/' + job.id);
      clear(status); add(status, [badge(job.state, kindOf(job.state)), h('span', { class: 'small muted' }, job.label + ' · ' + job.seconds + ' s' + (job.exit_code !== null ? ' · exit ' + job.exit_code : ''))]);
      log.textContent = job.output + (job.truncated ? '\n… [cut]' : '');
      log.scrollTop = log.scrollHeight;
      if (job.state !== 'running') { LAST_JOB = job; return job; }
      await new Promise((res) => setTimeout(res, 900));
    }
  }

  // ---------------------------------------------------------------- charts
  function lineChart(series) {
    const pts = series.filter((s) => s.equity !== null && s.equity !== undefined);
    const W = 760, H = 170, P = 34;
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'equity' });
    if (pts.length < 2) { const t = svg('text', { x: 12, y: 24 }); t.textContent = T('Няма достатъчно данни за графика', 'Not enough data for a chart'); root.appendChild(t); return root; }
    const ys = pts.map((p) => p.equity); let lo = Math.min(...ys), hi = Math.max(...ys);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const x = (i) => P + (W - 2 * P) * i / (pts.length - 1), y = (v) => H - P + 8 - (H - 2 * P) * (v - lo) / (hi - lo);
    root.appendChild(svg('line', { class: 'axis', x1: P, y1: H - P + 8, x2: W - P, y2: H - P + 8 }));
    [lo, hi].forEach((v) => { const t = svg('text', { x: 4, y: y(v) + 4 }); t.textContent = v.toLocaleString(undefined, { maximumFractionDigits: 0 }); root.appendChild(t); });
    root.appendChild(svg('polyline', { class: 'line', points: pts.map((p, i) => x(i) + ',' + y(p.equity)).join(' ') }));
    let prev = null;
    pts.forEach((p, i) => { if (prev !== null && p.state !== prev) root.appendChild(svg('circle', { class: 'dot-warn', cx: x(i), cy: y(p.equity), r: 4 })); prev = p.state; });
    const t0 = svg('text', { x: P, y: H - 6 }); t0.textContent = short(pts[0].at); root.appendChild(t0);
    const t1 = svg('text', { x: W - P - 110, y: H - 6 }); t1.textContent = short(pts[pts.length - 1].at); root.appendChild(t1);
    return root;
  }

  // ---------------------------------------------------------------- pages
  async function pageOverview() {
    const o = await api('overview');
    const modeOk = o.mode === 'BACKTEST';
    const root = h('div', {});
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Преглед', 'Overview')),
      h('p', {}, T('Състоянието на проекта накратко. Тук всичко е само за четене: не може да се сменя режим, правила или одобрения.',
        'The project at a glance. Everything here is read-only: no mode, rule or approval can be changed from this page.')))));
    add(root, h('div', { class: 'grid cols-4' },
      stat(T('Режим', 'Mode'), badge(o.mode || '?', modeOk ? 'ok' : 'bad'), T('разрешени: ', 'permitted: ') + (o.permitted_modes || []).join(', ')),
      stat(T('Авариен стоп', 'Kill switch'), badge(o.kill_switch.active ? T('АКТИВЕН', 'ACTIVE') : T('изключен', 'off'), o.kill_switch.active ? 'bad' : 'ok'),
        o.kill_switch.active ? o.kill_switch.files.join(', ') : T('създава се само от човек (файл ops/KILL_SWITCH)', 'set only by a human (file ops/KILL_SWITCH)')),
      stat(T('Одобрения', 'Approvals'), `${o.approvals.valid}/${o.approvals.total}`,
        `${T('валидни', 'valid')} ${o.approvals.valid} · ${T('заменени', 'superseded')} ${o.approvals.superseded} · ${T('анулирани', 'voided')} ${o.approvals.voided}`),
      stat('Git', o.git.branch || '–', (o.git.commit || '') + (o.git.dirty_files ? ` · ${o.git.dirty_files} ${T('непотвърдени файла', 'uncommitted')}` : ''))));
    if (!modeOk) add(root, h('div', { class: 'notice bad' }, T('Режимът не е BACKTEST. Проверете config/runtime.backtest.yaml.', 'The mode is not BACKTEST. Check config/runtime.backtest.yaml.')));
    if (o.approvals.voided) add(root, h('div', { class: 'notice bad' }, T('Има анулирани одобрения: файл е променен след одобрението му. Вижте „Одобрения и правила“.', 'Some approvals are voided: a file changed after its approval. See “Approvals & rules”.')));
    add(root, h('h2', {}, T('Версии в употреба', 'Versions in use')), h('div', { class: 'grid cols-2' }, kv(o.versions), kv(o.prompt_versions)));
    const checks = Object.entries(o.checks || {});
    add(root, h('h2', {}, T('Последни проверки', 'Latest checks')),
      checks.length ? table([{ label: T('Проверка', 'Check'), key: 'k' }, { label: T('Резултат', 'Result'), get: (r) => badge(r.v.state, kindOf(r.v.state)) },
        { label: T('Кога', 'When'), get: (r) => short(r.v.finished) }, { label: T('Последен ред', 'Last line'), get: (r) => r.v.last_line }],
        checks.map(([k, v]) => ({ k, v }))) : h('p', { class: 'muted' }, T('Още няма пускани проверки от тук. Отворете „Проверки“.', 'No checks run from here yet. Open “Checks”.')),
      h('p', {}, h('a', { href: '#/checks' }, T('Към проверките →', 'Go to checks →'))));
    add(root, h('h2', {}, T('Среда', 'Environment')), kv(o.environment));
    add(root, h('div', { class: 'notice' }, T('Какво следва: първият реален тест на модела (виж „Модел и оценка“), после реални данни и нова версия на правилата за риск. Планът е в ', 'What is next: the first real model test (see “Model & evals”), then real data and a new risk policy version. The plan is in '),
      h('a', { href: '#/docs/docs/roadmap.md' }, 'roadmap')));
    return root;
  }

  async function pageRuns(name) {
    const list = await api('runs');
    const root = h('div', {});
    const out = h('div', { class: 'hidden' });
    const demoBtn = h('button', { class: 'primary', onclick: async () => {
      demoBtn.disabled = true; const job = await runJob('demo_run', null, out); demoBtn.disabled = false;
      if (job && job.state === 'passed') { toast(T('Демо пускането е готово', 'Demo run created')); route(); }
    } }, T('Създай демо пускане', 'Create a demo run'));
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Решения', 'Decisions')),
      h('p', {}, T('Всеки цикъл на живия цикъл записва един DecisionPacket: какво видя системата, какво предложи моделът, какво реши рискът и какво се изпълни.',
        'Every cycle of the live loop writes one DecisionPacket: what the system saw, what the model proposed, what risk decided and what was executed.'))), demoBtn), out);
    out.className = 'card hidden';
    if (LAST_JOB && LAST_JOB.key === 'demo_run') showJob(out, LAST_JOB);
    const side = h('div', { class: 'list' });
    const detail = h('div', {});
    if (!list.length) add(side, h('p', { class: 'muted' }, T('Няма пускания в папката runs/. Създайте демо пускане с бутона горе (сървърът ползва фалшиви данни и симулиран брокер).', 'No runs in runs/. Create a demo run with the button above (mock data and the simulated broker).')));
    list.forEach((r) => add(side, h('div', { class: 'li' + (r.name === name ? ' sel' : ''), onclick: () => { location.hash = '#/runs/' + r.name; } },
      h('b', {}, r.name), h('div', { class: 'small muted' }, `${r.packets} ${T('пакета', 'packets')} · ${r.segments} ${T('сегм.', 'seg.')} · ${short(r.modified)}`))));
    add(root, h('div', { class: 'grid split' }, side, detail));
    if (name) await runDetail(name, detail); else if (list.length) { location.replace('#/runs/' + list[0].name); }
    else add(detail, h('div', { class: 'card muted' }, T('Изберете пускане отляво.', 'Pick a run on the left.')));
    return root;
  }

  async function runDetail(name, host) {
    const d = await api('runs/' + encodeURIComponent(name));
    const m = (d.metrics && d.metrics[0]) || {};
    add(host, h('div', { class: 'grid cols-4' },
      stat(T('Решения', 'Decisions'), fmt(m.decisions, 0), Object.entries(d.outcomes).map(([k, v]) => k + ' ' + v).join(' · ')),
      stat(T('Отказани от риска', 'Denied by risk'), fmt(m.risk_denials, 0), Object.entries(m.denial_reasons || {}).map(([k, v]) => k + ' ' + v).join(', ') || T('няма', 'none')),
      stat(T('Симулирани сделки', 'Simulated fills'), fmt(m.simulated_fills, 0), `${T('частични', 'partial')} ${fmt(m.partial_fills, 0)} · ${T('обем', 'notional')} ${fmt(m.filled_notional, 0)}`),
      stat('P&L', fmt(m.pnl === undefined ? null : Number(m.pnl), 2), `${T('макс. спад', 'max drawdown')} ${m.max_drawdown !== undefined ? (100 * m.max_drawdown).toFixed(2) + '%' : '–'} · ${T('край', 'end')}: ${m.end_state || '–'}`)));
    add(host, h('div', { class: 'grid cols-2', style: 'margin-top:12px' },
      h('div', { class: 'card' }, h('h3', {}, T('Проверка на веригите (хешове)', 'Chain verification (hashes)')),
        table([{ label: T('Файл', 'File'), key: 'file' }, { label: T('Записи', 'Entries'), key: 'entries', num: true, digits: 0 },
          { label: T('Резултат', 'Result'), get: (c) => !c.exists ? badge(T('няма', 'absent'), 'plain') : c.ok ? badge(c.torn_tail ? T('ок, скъсан край', 'ok, torn tail') : 'ok', c.torn_tail ? 'warn' : 'ok') : badge(c.error, 'bad') }], d.chains),
        h('p', { class: 'small muted' }, T('Само четене: файловете не се променят при проверката.', 'Read-only: the files are not touched by the check.'))),
      h('div', { class: 'card' }, h('h3', {}, T('Рестарти и възстановяване', 'Restarts and recovery')),
        h('p', {}, `${T('Сегменти', 'Segments')}: ${(d.manifest.segments || []).length} · ${T('рестарти', 'restarts')}: ${fmt(m.restarts, 0)}`),
        (d.manifest.recoveries || []).length ? table([{ label: T('Вид', 'Kind'), key: 'kind' }, { label: T('Състояние след', 'State after'), get: (r) => badge(r.state_after || '–', kindOf(r.state_after)) },
          { label: T('Съгласувано', 'Reconciled'), get: (r) => String(r.reconciled) }], d.manifest.recoveries) : h('p', { class: 'muted' }, T('Няма записани възстановявания.', 'No recoveries recorded.')),
        d.broker_book ? h('p', { class: 'small muted' }, 'broker_book: ' + JSON.stringify(d.broker_book.positions)) : null)));
    add(host, h('h3', {}, T('Капитал по цикли', 'Equity per cycle')), lineChart(d.series),
      h('p', { class: 'small muted' }, T('Жълтите точки са смени на състоянието (NORMAL → REDUCE_ONLY → HALTED).', 'Yellow dots mark state changes (NORMAL → REDUCE_ONLY → HALTED).')));
    const filter = h('select', {}, h('option', { value: '' }, T('всички изходи', 'all outcomes')), Object.keys(d.outcomes).map((o) => h('option', { value: o }, o)));
    const tableHost = h('div', {});
    add(host, h('div', { class: 'row', style: 'margin-top:14px' }, h('h3', { style: 'margin:0' }, T('Пакети', 'Packets')), filter), tableHost);
    async function load() {
      const p = await api(`runs/${encodeURIComponent(name)}/packets?limit=2000` + (filter.value ? '&outcome=' + encodeURIComponent(filter.value) : ''));
      clear(tableHost);
      add(tableHost, h('p', { class: 'small muted' }, `${p.rows.length}/${p.total}`), table([
        { label: '#', key: 'i', num: true, digits: 0 }, { label: T('Време', 'Time'), get: (r) => short(r.at), nowrap: true },
        { label: T('Изход', 'Outcome'), get: (r) => badge(r.outcome, kindOf(r.outcome)) },
        { label: T('Предложение', 'Proposal'), get: (r) => r.decision === 'TRADE' ? `${r.side} ${r.symbol}` : (r.reason || r.decision || '–') },
        { label: T('Риск', 'Risk'), get: (r) => r.verdict ? badge(r.verdict, kindOf(r.verdict === 'DENY' ? 'DENIED' : 'APPROVED')) : '–' },
        { label: T('Причини', 'Reasons'), get: (r) => (r.denied_by || []).join(', ') },
        { label: T('Състояние', 'State'), get: (r) => badge(r.state, kindOf(r.state)) }, { label: T('Сделки', 'Fills'), key: 'fills', num: true, digits: 0 },
        { label: 'ms', key: 'latency_ms', num: true, digits: 0 }], p.rows, { onrow: (r) => openPacket(name, r.i), maxHeight: '480px' }));
    }
    filter.addEventListener('change', load);
    await load();
  }

  async function openPacket(name, i) {
    const p = await api(`runs/${encodeURIComponent(name)}/packets/${i}`);
    const prop = p.proposal || {}, risk = p.risk_decision || {}, critic = p.critic || {};
    const sections = [
      h('h2', {}, `#${i} · ${short(p.at)} `, badge(p.outcome, kindOf(p.outcome)), ' ', badge(`${p.state_before} → ${p.state_after}`, kindOf(p.state_after))),
      h('p', { class: 'small muted' }, `${T('сесия', 'session')} ${p.session_id} · ${p.run_segment} · ${p.packet_id}`),
      h('h3', {}, T('Предложение на модела', 'Model proposal')),
      kv({ decision: prop.decision, symbol: prop.symbol, side: prop.side, limit_price: prop.limit_price, requested_notional: prop.requested_notional,
        reason_code: prop.reason_code, model_version: prop.model_version, prompt_version: prop.prompt_version }),
      prop.public_rationale ? h('div', { class: 'notice' }, prop.public_rationale) : null,
      prop.evidence_refs && prop.evidence_refs.length ? h('p', { class: 'small mono' }, 'evidence: ' + prop.evidence_refs.join(', ')) : null,
      h('h3', {}, T('Критик', 'Critic')), critic.verdict ? kv({ verdict: critic.verdict, reason_codes: (critic.reason_codes || []).join(', '), max_notional: critic.max_notional }) : h('p', { class: 'muted' }, '–'),
      h('h3', {}, T('Решение на риска', 'Risk decision')),
      risk.verdict ? [h('p', {}, badge(risk.verdict, kindOf(risk.verdict === 'DENY' ? 'DENIED' : 'APPROVED')), ` ${T('поискано', 'requested')} ${fmt(Number(risk.requested_notional))} → ${T('одобрено', 'approved')} ${fmt(Number(risk.approved_notional))} · ${risk.policy_version}`),
        table([{ label: T('Правило', 'Rule'), key: 'rule_id' }, { label: T('Резултат', 'Result'), get: (r) => badge(r.passed ? 'pass' : 'FAIL', r.passed ? 'ok' : 'bad') },
          { label: 'Code', key: 'reason_code' }, { label: T('Детайл', 'Detail'), key: 'detail' }], risk.rule_results || [], { maxHeight: '260px' })] : h('p', { class: 'muted' }, T('Рисковият шлюз не е викан.', 'The risk gateway was not called.')),
      h('h3', {}, T('Поръчка и изпълнение', 'Order and execution')),
      p.approved_order ? kv(p.approved_order, ['signature']) : h('p', { class: 'muted' }, T('Няма поръчка.', 'No order.')),
      (p.acks || []).length ? table([{ label: 'Ack', key: 'status' }, { label: T('Причина', 'Reason'), key: 'reason' }], p.acks) : null,
      (p.fills || []).length ? table([{ label: T('Символ', 'Symbol'), key: 'symbol' }, { label: T('Страна', 'Side'), key: 'side' }, { label: T('Кол.', 'Qty'), key: 'quantity' },
        { label: T('Цена', 'Price'), key: 'price' }, { label: T('Такса', 'Fee'), key: 'fee' }], p.fills) : null,
      h('h3', {}, T('Пазарни данни', 'Market data')),
      kv({ venue_status: p.market.venue_status, as_of: p.market.as_of, instrument_status: p.market.instrument_status }),
      table([{ label: T('Символ', 'Symbol'), key: 'sym' }, { label: 'Bid', key: 'bid' }, { label: 'Ask', key: 'ask' }, { label: T('Възраст', 'Quote ts'), get: (r) => short(r.ts) }],
        Object.entries(p.market.quotes || {}).map(([sym, q]) => Object.assign({ sym }, q))),
      (p.data_issues || []).length ? [h('h3', {}, T('Проблеми с данните', 'Data issues')), pre(p.data_issues)] : null,
      (p.errors || []).length ? h('div', { class: 'notice bad' }, p.errors.join(' | ')) : null,
      h('h3', {}, T('Времена (ms)', 'Timings (ms)')), kv(Object.assign({ signal_to_proposal_ms: p.signal_to_proposal_ms }, p.timings_ms)),
      h('h3', {}, T('Суров отговор на модела', 'Raw model output')), p.planner_raw_output ? pre(p.planner_raw_output) : h('p', { class: 'muted' }, T('Моделът не е викан (детерминирана врата или няма сигнал).', 'The model was not called (deterministic gate or no trigger).')),
      h('h3', {}, 'JSON'), h('details', {}, h('summary', {}, T('покажи', 'show')), pre(p)),
    ];
    openDrawer(sections);
  }

  function openDrawer(content) {
    const host = $('#drawer-host'); clear(host);
    const close = () => clear(host);
    const onKey = (e) => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); } };
    document.addEventListener('keydown', onKey);
    host.appendChild(h('div', { class: 'drawer' }, h('button', { class: 'close', onclick: close }, T('Затвори ✕', 'Close ✕')), content));
  }
  const $ = (s) => document.querySelector(s);

  async function pageBacktests(selected) {
    const [sets, reps] = await Promise.all([api('datasets'), api('backtests')]);
    const root = h('div', {});
    const out = h('div', { class: 'card hidden' });
    if (LAST_JOB && LAST_JOB.key === 'backtest') showJob(out, LAST_JOB);
    const sel = h('select', {}, sets.map((s) => h('option', { value: s.id }, s.id + (s.synthetic ? ' (synthetic)' : ''))));
    const btn = h('button', { class: 'primary', onclick: async () => {
      btn.disabled = true; const job = await runJob('backtest', sel.value, out); btn.disabled = false;
      if (job && job.state === 'passed') { toast(T('Готово', 'Done')); route(); }
    } }, T('Пусни бектест', 'Run backtest'));
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Бектест', 'Backtests')),
      h('p', {}, T('Стратегията се пуска върху записани данни със симулиран брокер. Резултатът е изследване, не препоръка за сделка. Засега има само синтетични данни.',
        'The strategy runs on recorded data with the simulated broker. The result is research, not a trade recommendation. Only synthetic data exists so far.')))),
      h('div', { class: 'card' }, h('div', { class: 'row' }, h('span', {}, T('Набор от данни', 'Dataset')), sel, btn,
        h('span', { class: 'small muted' }, T('пуска същата команда като python -m trading_agent.backtest.run_dataset (иска валидно одобрение на симулатора)', 'runs python -m trading_agent.backtest.run_dataset (needs a valid simulator approval)')))), out);
    add(root, h('h2', {}, T('Наличните отчети', 'Existing reports')));
    const view = h('div', { class: 'card hidden' });
    add(root, reps.length ? table([{ label: T('Данни', 'Dataset'), key: 'dataset' }, { label: T('Отчет', 'Report'), key: 'report' }, { label: T('Променен', 'Modified'), get: (r) => short(r.modified) }],
      reps, { onrow: async (r) => { const d = await api(`backtests/${r.dataset}/${r.report}`); clear(view); view.classList.remove('hidden'); add(view, md(d.markdown)); view.scrollIntoView({ behavior: 'smooth' }); } }) : h('p', { class: 'muted' }, T('Още няма отчети.', 'No reports yet.')), view);
    add(root, h('h2', {}, T('Набори от данни', 'Datasets')), sets.map((s) => h('div', { class: 'card', style: 'margin-bottom:8px' }, h('b', {}, s.id), kv(s.manifest, ['files']))));
    return root;
  }

  async function pageModel() {
    const [mi, reports] = await Promise.all([api('model'), api('evals')]);
    const root = h('div', {});
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Модел и оценка', 'Model & evals')),
      h('p', {}, T('Този панел НЕ вика модела и не пипа ключа. Платените пускания се правят в терминала, защото искат паролата на vault-а.',
        'This page does NOT call the model and never touches the key. Paid runs are done in a terminal because they need the vault passphrase.')))));
    add(root, h('div', { class: 'grid cols-3' }, stat(T('Модел', 'Model'), mi.model || '–', T('закован идентификатор, без псевдоними', 'pinned id, no aliases')),
      stat('max_tokens', mi.max_tokens || '–', T('включва токените за разсъждение', 'includes reasoning tokens')),
      stat(T('Записани отговори', 'Recorded outputs'), Object.values(mi.recordings).reduce((a, b) => a + b, 0), Object.keys(mi.recordings).join(', ') || T('още няма реално пускане', 'no real run yet'))));
    add(root, h('h2', {}, T('Как се пуска (в терминала)', 'How to run it (in a terminal)')), mi.commands.map((c) => cmdBlock(c.label, c.cmd)));
    add(root, h('h2', {}, 'Prompts'), table([{ label: T('Файл', 'File'), key: 'file' }, { label: T('Статус', 'Status'), get: (r) => badge(r.status, r.status === 'released' ? 'ok' : 'warn') },
      { label: 'sha256', key: 'sha256' }, { label: T('Знаци', 'Chars'), key: 'chars', num: true, digits: 0 }], mi.prompts));
    if (mi.provenance.length) add(root, h('h2', {}, T('Реални пускания', 'Real runs')), table([{ label: T('Кога', 'When'), key: 'at' }, { label: 'Prompt', key: 'label' }, { label: 'Split', key: 'split' },
      { label: T('Модел', 'Model'), key: 'model' }, { label: 'Effort', key: 'effort' }, { label: T('Внесени', 'Imported'), key: 'imported', num: true, digits: 0 }, { label: T('Без отговор', 'No output'), key: 'no_output', num: true, digits: 0 }], mi.provenance));
    const detail = h('div', {});
    add(root, h('h2', {}, T('Отчети от оценката на планировчика', 'Planner evaluation reports')),
      table([{ label: T('Етикет', 'Label'), key: 'label' }, { label: 'Split', key: 'split' }, { label: T('Набор', 'Set'), key: 'set_version' },
        { label: T('Оценени', 'Graded'), get: (r) => `${r.graded}/${r.n}` }, { label: T('С грешка', 'With a failure'), get: (r) => r.any_failure_rate === null ? '–' : (100 * r.any_failure_rate).toFixed(1) + '%' }],
        reports, { onrow: async (r) => showEvalReport(r.id, detail) }), detail);
    add(root, h('h2', {}, T('Разглеждане на сценарии', 'Scenario explorer')), await scenarioExplorer());
    return root;
  }

  async function showEvalReport(id, host) {
    const r = await api('evals/report?id=' + encodeURIComponent(id));
    clear(host);
    add(host, h('div', { class: 'card' }, h('h3', {}, `${r.label} · ${r.split} · ${r.set_version}`),
      h('div', { class: 'grid cols-2' }, h('div', {}, h('h3', {}, T('Грешки (брой сценарии)', 'Failures (scenarios)')),
        bars(Object.fromEntries(Object.entries(r.failure_rates).map(([k, v]) => [k, v.count])), 'bad')),
        h('div', {}, h('h3', {}, T('По вид сценарий (с грешка / общо)', 'By kind (failed / total)')),
          table([{ label: T('Вид', 'Kind'), key: 'k' }, { label: T('Грешки', 'Failed'), get: (x) => `${x.v.failed}/${x.v.n}` }], Object.entries(r.by_kind).map(([k, v]) => ({ k, v })), { maxHeight: '300px' }))),
      h('p', { class: 'small muted' }, `${T('Свръх-въздържане', 'Over-abstention')}: ${(100 * r.secondary.OVER_ABSTENTION).toFixed(1)}% · ${T('грешна сделка', 'wrong trade')}: ${(100 * r.secondary.WRONG_TRADE).toFixed(1)}%`)));
    host.scrollIntoView({ behavior: 'smooth' });
  }

  async function scenarioExplorer() {
    const host = h('div', { class: 'card' });
    const split = h('select', {}, h('option', { value: 'development' }, 'development'), h('option', { value: 'validation' }, 'validation'));
    const kind = h('select', {}, h('option', { value: '' }, T('всички видове', 'all kinds')));
    const listHost = h('div', {});
    add(host, h('p', { class: 'small muted' }, T('Само development и validation. Запечатаният holdout никога не се показва тук.', 'Development and validation only. The sealed holdout is never shown here.')),
      h('div', { class: 'row' }, split, kind), listHost);
    async function load() {
      const rows = await api('scenarios?split=' + split.value + (kind.value ? '&kind=' + kind.value : ''));
      if (kind.options.length === 1) [...new Set(rows.map((r) => r.kind))].sort().forEach((k) => kind.appendChild(h('option', { value: k }, k)));
      clear(listHost);
      add(listHost, table([{ label: 'ID', key: 'id' }, { label: T('Вид', 'Kind'), key: 'kind' }, { label: T('Дата', 'Date'), key: 'date' },
        { label: T('Очакван отговор', 'Expected'), get: (r) => r.expect.decision === 'TRADE' ? `TRADE ${r.expect.side} ${r.expect.symbol}` : 'NO_TRADE' }], rows,
        { maxHeight: '320px', onrow: (r) => openScenario(split.value, r.id) }));
    }
    split.addEventListener('change', () => { while (kind.options.length > 1) kind.remove(1); kind.value = ''; load(); });
    kind.addEventListener('change', load);
    await load();
    return host;
  }

  async function openScenario(splitName, id) {
    const s = await api(`scenarios/${splitName}/${encodeURIComponent(id)}`);
    const p = s.payload, m = p.market_snapshot;
    openDrawer([
      h('h2', {}, id, ' ', badge(s.kind, 'plain')),
      h('p', { class: 'small muted' }, `${s.date} · ${T('капани', 'traps')}: ${(s.traps || []).join(', ') || '–'}`),
      h('div', { class: 'notice ' + (s.expect.decision === 'TRADE' ? 'ok' : '') }, T('Правилният отговор е ', 'The correct answer is ') + (s.expect.decision === 'TRADE' ? `TRADE ${s.expect.side} ${s.expect.symbol}` : 'NO_TRADE')),
      h('h3', {}, T('Какво вижда моделът', 'What the model sees')),
      kv({ evaluation_time: p.evaluation_time, venue_status: m.venue_status, snapshot_as_of: m.as_of, instrument_status: m.instrument_status, positions: Object.keys(p.account_snapshot.positions).join(', ') || '–',
        open_orders: (p.account_snapshot.open_orders || []).map((o) => o.symbol).join(', ') || '–', realized_pnl_today: p.account_snapshot.realized_pnl_today }),
      table([{ label: T('Символ', 'Symbol'), key: 'sym' }, { label: 'Bid', key: 'bid' }, { label: 'Ask', key: 'ask' }, { label: 'ts', get: (r) => short(r.ts) },
        { label: 'dev_from_sma_10_bps', get: (r) => ((p.features.values[r.sym] || {}).dev_from_sma_10_bps) }], Object.entries(m.quotes).map(([sym, q]) => Object.assign({ sym }, q))),
      (m.news || []).length ? [h('h3', {}, T('Новини (недоверени данни)', 'News (untrusted data)')), table([{ label: T('Източник', 'Source'), key: 'source' }, { label: T('Заглавие', 'Headline'), key: 'headline' }], m.news)] : null,
      h('h3', {}, T('Записани отговори на модели', 'Recorded model outputs')),
      s.recorded_outputs.length ? s.recorded_outputs.map((o) => h('div', { class: 'card', style: 'margin-bottom:8px' }, h('b', {}, o.label), ' ', badge(o.decision || 'invalid', o.failures.length ? 'bad' : 'ok'),
        o.failures.length ? h('div', { class: 'small' }, o.failures.join(', ')) : h('div', { class: 'small muted' }, T('без грешки по таксономията', 'no taxonomy failures')), pre(o.output)))
        : h('p', { class: 'muted' }, T('Още няма записан отговор на реален модел за този сценарий.', 'No real model output is recorded for this scenario yet.')),
      h('details', {}, h('summary', {}, T('Пълният вход (payload)', 'Full input (payload)')), pre(p)),
    ]);
  }

  async function pageGovernance() {
    const [apr, pol] = await Promise.all([api('approvals'), api('policy')]);
    const root = h('div', {});
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Одобрения и правила', 'Approvals & rules')),
      h('p', {}, T('Одобренията са вързани за хеша на файловете. Ако файл се промени, одобрението му става „заменено“ (ако по-ново одобрение покрива новата версия) или „анулирано“.',
        'Approvals are bound to file hashes. If a file changes, its approval becomes “superseded” (a newer approval covers the new version) or “voided”.')))));
    apr.forEach((a) => add(root, h('div', { class: 'card', style: 'margin-bottom:10px' },
      h('div', { class: 'row' }, h('b', {}, a.id), badge(a.state, kindOf(a.state)), h('span', { class: 'muted small' }, `${a.date} · ${a.modes.join(', ')}`)),
      h('p', {}, a.statement), h('details', {}, h('summary', {}, T('Обхват и файлове', 'Scope and files')), h('p', { class: 'small' }, a.code_scope),
        table([{ label: T('Файл', 'File'), key: 'path' }, { label: 'sha256', key: 'sha256' }, { label: T('Сега', 'Now'), get: (f) => badge(f.status + (f.superseded_by ? ' → ' + f.superseded_by : ''), f.status === 'ok' ? 'ok' : (f.superseded_by ? 'warn' : 'bad')) }], a.files)))));
    add(root, h('h2', {}, T('Активната политика за риск', 'Active risk policy') + ' · ' + pol.active_file));
    const p = pol.policy || {};
    const scalars = Object.fromEntries(Object.entries(p).filter(([, v]) => typeof v !== 'object' || v === null));
    add(root, h('div', { class: 'grid cols-2' }, h('div', {}, h('h3', {}, T('Лимити', 'Limits')), kv(scalars)),
      h('div', {}, h('h3', {}, T('Инструменти и сесия', 'Instruments and session')), kv({ instruments: p.instruments, max_sector_exposure: p.max_sector_exposure, session: p.session && Object.assign({}, p.session, { holidays: (p.session.holidays || []).length + ' days' }) }))));
    add(root, h('h2', {}, T('Други правила', 'Other rules')), h('div', { class: 'grid cols-2' },
      h('div', {}, h('h3', {}, 'Ops policy'), kv(pol.ops)), h('div', {}, h('h3', {}, 'Execution assumptions'), kv(pol.execution_assumptions, ['costs'])),
      h('div', {}, h('h3', {}, 'Universe / restricted'), kv({ universe: pol.universe, restricted: pol.restricted })),
      h('div', {}, h('h3', {}, T('Стратегии', 'Strategies')), pol.strategies.map((s) => kv(s.content)))));
    add(root, h('div', { class: 'notice' }, T('Тези файлове не могат да се редактират оттук. Промяната им изисква нова версия, тестове и ваше изрично одобрение (CLAUDE.md).', 'These files cannot be edited from here. Changing them needs a new version, tests and your explicit approval (CLAUDE.md).')));
    return root;
  }

  async function pageChecks() {
    const checks = await api('checks');
    const root = h('div', {});
    const out = h('div', { class: 'card hidden' });
    const rows = {};
    const buttons = [];
    async function run(key) {
      buttons.forEach((b) => { b.disabled = true; });
      const job = await runJob(key, null, out);
      buttons.forEach((b) => { b.disabled = false; });
      if (job && rows[key]) { clear(rows[key]); add(rows[key], badge(job.state, kindOf(job.state)), ' ', h('span', { class: 'small muted' }, job.seconds + ' s')); }
      return job;
    }
    const all = h('button', { class: 'primary', onclick: async () => { for (const c of checks) { const j = await run(c.key); if (!j) break; } } }, T('Пусни всички', 'Run all'));
    buttons.push(all);
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Проверки', 'Checks')),
      h('p', {}, T('Постоянните проверки на проекта. Всяка пуска фиксирана локална команда; нищо не се изпраща навън.', 'The project’s standing checks. Each runs a fixed local command; nothing is sent anywhere.'))), all));
    const body = h('tbody', {});
    checks.forEach((c) => {
      const cell = h('td', {}, c.last ? [badge(c.last.state, kindOf(c.last.state)), ' ', h('span', { class: 'small muted' }, short(c.last.finished) + ' · ' + c.last.seconds + ' s')] : badge(T('не е пускана', 'not run'), 'plain'));
      rows[c.key] = cell;
      const b = h('button', { onclick: () => run(c.key) }, T('Пусни', 'Run'));
      buttons.push(b);
      body.appendChild(h('tr', {}, h('td', {}, c.label), cell, h('td', { class: 'small muted mono' }, c.last ? c.last.last_line : ''), h('td', {}, b)));
    });
    add(root, h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, T('Проверка', 'Check')), h('th', {}, T('Резултат', 'Result')), h('th', {}, T('Последен ред', 'Last line')), h('th', {}))), body)), out);
    add(root, h('p', { class: 'small muted' }, T('Пълният тестов пакет отнема около минута и половина.', 'The full test suite takes about a minute and a half.')));
    return root;
  }

  async function pageDocs(path) {
    const index = await api('docs');
    const root = h('div', {});
    const side = h('div', { class: 'list' });
    const body = h('div', { class: 'card' });
    const groups = {};
    index.forEach((d) => { (groups[d.group] = groups[d.group] || []).push(d); });
    Object.entries(groups).forEach(([g, items]) => {
      add(side, h('div', { class: 'small muted', style: 'margin-top:8px' }, g));
      items.forEach((d) => add(side, h('div', { class: 'li' + (d.path === path ? ' sel' : ''), onclick: () => { location.hash = '#/docs/' + d.path; } }, d.path.split('/').slice(-1)[0], h('div', { class: 'small muted' }, d.path))));
    });
    add(root, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, T('Документи', 'Docs')), h('p', {}, T('Само за четене: документите и конфигурацията на проекта.', 'Read-only: the project’s documents and configuration.')))), h('div', { class: 'grid split' }, side, body));
    if (path) {
      const d = await api('doc?path=' + encodeURIComponent(path));
      add(body, /\.md$/.test(path) ? md(d.text) : pre(d.text));
    } else add(body, h('p', { class: 'muted' }, T('Изберете документ отляво. Добра отправна точка: roadmap и session-handoff.', 'Pick a document on the left. Good starting points: roadmap and session-handoff.')));
    return root;
  }

  // ---------------------------------------------------------------- shell and routing
  const PAGES = [['overview', () => T('Преглед', 'Overview')], ['runs', () => T('Решения', 'Decisions')], ['backtests', () => T('Бектест', 'Backtests')],
    ['model', () => T('Модел и оценка', 'Model & evals')], ['governance', () => T('Одобрения и правила', 'Approvals & rules')], ['checks', () => T('Проверки', 'Checks')], ['docs', () => T('Документи', 'Docs')]];

  function renderNav(active, overview) {
    const nav = clear($('#nav'));
    add(nav, h('div', { class: 'brand' }, 'Control room', h('small', {}, 'trading-agent')));
    if (overview) add(nav, h('div', { class: 'mode-pill' }, h('b', {}, overview.mode || '?'), T('само за четене', 'read-only')));
    PAGES.forEach(([k, label]) => add(nav, h('a', { class: 'item' + (k === active ? ' active' : ''), href: '#/' + k }, label())));
    add(nav, h('div', { class: 'spacer' }), h('div', { class: 'tools' },
      h('button', { onclick: () => { lang = lang === 'bg' ? 'en' : 'bg'; try { localStorage.setItem('lang', lang); } catch (e) { /* ignore */ } route(); } }, lang === 'bg' ? 'EN' : 'БГ'),
      h('button', { onclick: () => route() }, '⟳')));
  }

  let navVersion = 0;
  async function route() {
    const parts = location.hash.replace(/^#\/?/, '').split('/');
    const page = parts[0] || 'overview', arg = parts.slice(1).join('/');
    const main = $('#main'); const myVersion = ++navVersion;
    renderNav(page, null);
    clear($('#drawer-host'));
    try {
      const node = await ({ overview: pageOverview, runs: () => pageRuns(arg), backtests: () => pageBacktests(arg), model: pageModel,
        governance: pageGovernance, checks: pageChecks, docs: () => pageDocs(arg) }[page] || pageOverview)();
      if (myVersion !== navVersion) return;
      clear(main).appendChild(node);
      if (page === 'overview') { try { renderNav(page, await api('overview')); } catch (e) { /* nav only */ } }
    } catch (e) {
      clear(main).appendChild(h('div', { class: 'notice bad' }, T('Грешка: ', 'Error: ') + e.message));
    }
    window.scrollTo(0, 0);
  }

  async function boot() {
    try { TOKEN = (await api('session')).token; } catch (e) { /* the page still works read-only */ }
    window.addEventListener('hashchange', route);
    route();
  }
  boot();
})();
