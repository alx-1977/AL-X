// The BHL dashboard (mockup): opened from the BHL item in the menu bar (ES
// module, no dependencies beyond the reader traces).
//
//   const board = openDashboard(host, data, { onClose });
//   board.update(nextData);
//   board.close();
//
// Like the traces, it composes no wording and decides no state. Shape:
//
//   {
//     tone: 'ok' | 'warn' | 'bad', errors: 2, warnings: 3,
//     sessions_today: 6, updated_at: '2026-10-09T21:56:03+00:00', place: 'Cape Town',
//     uplink:  { tone, state: 'Online', last_read: '21:52', confirmed: '24/25', interval: '5 min', note: 'All good' },
//     readers: { tone, online: 24, total: 25, scans_today: 1790, offline: 1, correct: '23/24', note: '…' },
//     ring:    { ok: 20, warn: 3, bad: 2 },
//     traces:  [ ...reader traces, as reader-traces.js takes them ]
//   }

import { buildTrace } from '/reader-traces.js';

const svg = (body, extra = '') =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra}>${body}</svg>`;

const ICONS = {
  uplink: svg('<path d="M7 17.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9.5a4 4 0 0 1 .5 7.97"/><path d="M10 13v7M10 20l-2-2M10 20l2-2M14 20v-7M14 13l-2 2M14 13l2 2"/>'),
  reader: svg('<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><circle cx="12" cy="9" r="2.2"/><path d="M10 14.5h4"/>'),
  scanner: svg('<rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M9.5 6h5v3h-5z"/><path d="M9.5 12.5h.01M12 12.5h.01M14.5 12.5h.01M9.5 15h.01M12 15h.01M14.5 15h.01M9.5 17.5h.01M12 17.5h.01M14.5 17.5h.01"/>'),
  power: svg('<circle cx="12" cy="12" r="9.5"/><path d="M13 5.5L8.5 13H12l-1 5.5 4.5-7.5H12z"/>'),
  alert: svg('<path d="M12 3.5L2.8 19.5h18.4z"/><path d="M12 10v4.5M12 17h.01"/>'),
  check: svg('<circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.7 2.7L16 9.6"/>'),
  close: svg('<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>'),
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(name, className) {
  const node = el('span', className);
  node.innerHTML = ICONS[name];
  return node;
}

function card({ tone, icon: name, title, subtitle, big, bigUnit, stats, note, noteTone, idle }) {
  const node = el('section', 'bd-card');
  if (idle) node.dataset.idle = '';
  else if (tone) node.dataset.tone = tone;
  const top = el('div', 'bd-card__top');
  const names = el('div', 'bd-card__names');
  names.append(el('strong', null, title), el('span', null, subtitle));
  const value = el('div', 'bd-card__big', big);
  if (bigUnit) value.append(el('small', null, bigUnit));
  top.append(icon(name, 'bd-card__icon'), names, value);
  const row = el('div', 'bd-card__stats');
  for (const [figure, label] of stats) {
    const stat = el('div', 'bd-card__stat');
    stat.append(el('b', null, figure), el('span', null, label));
    row.append(stat);
  }
  const foot = el('div', 'bd-card__foot');
  const line = el('span', 'bd-card__note', note);
  if (noteTone) line.dataset.tone = noteTone;
  foot.append(line);
  node.append(top, row, foot);
  return node;
}

function ring(data) {
  const wrap = el('div', 'bd__ring');
  wrap.dataset.tone = data.tone;
  const counts = data.ring ?? { ok: 1, warn: 0, bad: 0 };
  const total = Math.max(1, counts.ok + counts.warn + counts.bad);
  const r = 46;
  const circumference = 2 * Math.PI * r;
  const gap = total > 1 ? 2.2 : 0;
  let offset = 0;
  const arcs = [];
  for (const [tone, colour] of [['ok', '#4ade80'], ['warn', '#fbbf24'], ['bad', '#fb6b6b']]) {
    const share = counts[tone] / total;
    if (!share) continue;
    const length = Math.max(0.8, share * circumference - gap);
    arcs.push(`<circle class="bd__ring-arc" cx="50" cy="50" r="${r}" stroke="${colour}" style="--glow:${colour}" ` +
      `stroke-dasharray="${length.toFixed(2)} ${circumference.toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}" ` +
      `transform="rotate(-90 50 50)"/>`);
    offset += share * circumference;
  }
  wrap.innerHTML =
    `<svg viewBox="0 0 100 100" aria-hidden="true">` +
    `<circle class="bd__ring-ticks" cx="50" cy="50" r="50"/>` +
    `<circle class="bd__ring-track" cx="50" cy="50" r="${r}"/>${arcs.join('')}` +
    `<circle class="bd__ring-core" cx="50" cy="50" r="40"/></svg>`;
  const text = el('div', 'bd__ring-text');
  const calm = data.tone === 'ok';
  const parts = [];
  if (data.errors) parts.push(`${data.errors} ${data.errors === 1 ? 'error' : 'errors'}`);
  if (data.warnings) parts.push(`${data.warnings} ${data.warnings === 1 ? 'warning' : 'warnings'}`);
  text.append(icon(calm ? 'check' : 'alert', 'bd__ring-icon'),
    el('p', 'bd__ring-title', calm ? 'ALL SYSTEMS NORMAL' : 'ATTENTION NEEDED'),
    el('p', 'bd__ring-sub', parts.join(' · ') || 'every reader is fine'),
    el('p', 'bd__ring-meta', `Event day · ${data.sessions_today} sessions today`));
  wrap.append(text);
  return wrap;
}

function ago(iso) {
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return seconds < 60 ? `${seconds} s ago` : `${Math.round(seconds / 60)} min ago`;
}

export function openDashboard(host, data, options = {}) {
  const root = el('div', 'bd');
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-label', 'BHL event system');
  host.append(root);
  let current = data;

  const live = el('span', 'bd__live');
  const clock = el('time', 'bd__clock');
  const date = el('div', 'bd__date');

  function tick() {
    const now = new Date();
    clock.textContent = now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
    date.replaceChildren(
      el('div', null, now.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })),
      el('div', null, current.place ?? ''));
    live.textContent = `Data live · updated ${ago(current.updated_at)}`;
  }

  function render() {
    const head = el('header', 'bd__head');
    const system = el('div', 'bd__system');
    system.append(el('strong', null, 'BHL EVENT SYSTEM'), el('span', null, 'LIVE OPERATIONS'));
    const close = el('button', 'bd__close');
    close.innerHTML = ICONS.close;
    close.setAttribute('aria-label', 'Close');
    close.addEventListener('click', finish);
    head.append(el('div', 'bd__logo', 'ALX'), system, el('span', 'bd__space'), live, clock, date, close);

    const up = current.uplink;
    const rd = current.readers;
    const main = el('div', 'bd__main');
    main.append(
      card({ tone: up.tone, icon: 'uplink', title: 'ALX ↔ BHL UPLINK', subtitle: 'Schedules from BehaviorLive',
        big: up.state, stats: [[up.last_read, 'Last schedule read'], [up.confirmed, 'Schedules confirmed'],
          [up.interval, 'Read every']], note: up.note, noteTone: up.tone === 'ok' ? null : up.tone }),
      ring(current),
      card({ tone: rd.tone, icon: 'reader', title: 'ROOM READERS', subtitle: 'Session attendance',
        big: `${rd.online}/${rd.total}`, bigUnit: 'ONLINE',
        stats: [[rd.scans_today.toLocaleString(), 'Scans today'], [String(rd.offline), 'Offline'],
          [rd.correct, 'On the correct event']], note: rd.note, noteTone: rd.tone === 'ok' ? null : rd.tone }),
      card({ idle: true, icon: 'scanner', title: 'REGISTRATION SCANNERS', subtitle: 'Front desk check-in',
        big: 'NOT MONITORED YET', stats: [['—', 'Scans today'], ['—', 'Errors'], ['—', 'Last scan']],
        note: 'AL/X does not watch these yet' }),
      card({ idle: true, icon: 'power', title: 'STREAMCASE PSUs', subtitle: 'UPS power',
        big: 'NOT MONITORED YET', stats: [['—', 'On mains'], ['—', 'Lowest battery'], ['—', 'Shortest runtime']],
        note: 'AL/X does not watch these yet' }),
    );

    const attention = el('section', 'bd__attention');
    attention.dataset.tone = current.tone;
    const top = el('div', 'bd__attention-head');
    top.append(el('strong', null, 'NEEDS ATTENTION'), el('b', null, String(current.traces.length).padStart(2, '0')));
    const traces = el('div', 'bd__traces');
    if (current.traces.length) traces.append(...current.traces.map(buildTrace));
    else traces.append(el('p', 'bd__calm', 'Nothing needs attention.'));
    attention.append(top, traces);

    root.replaceChildren(head, main, attention);
    tick();
  }

  const ticker = setInterval(tick, 1000);

  function finish() {
    clearInterval(ticker);
    document.removeEventListener('keydown', onKey);
    root.remove();
    options.onClose?.();
  }
  function onKey(event) { if (event.key === 'Escape') finish(); }
  document.addEventListener('keydown', onKey);

  render();
  return { update(next) { current = next; render(); }, close: finish };
}
