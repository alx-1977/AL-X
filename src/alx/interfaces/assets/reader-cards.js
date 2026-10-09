// Room readers (D-041): the compact BHL tile, and the board it opens (ES
// module, no dependencies).
//
//   const tile = surfaceReaderTile(host, summary, onOpen);   // main screen
//   const board = openReaderBoard(document.body, readers, { title, context });
//   board.update(nextReaders);
//   board.close();
//
// The board shows every reader as one dot, grouped by room, so a client with
// sixty readers fits on one line or two; only readers with a problem get a
// card. Like the tile, the renderer composes no wording and decides no state:
// every word and tone comes from the data. One reader:
//
//   {
//     uid:    'ab2d5218',
//     room:   'Majestic 1',
//     tone:   'ok' | 'warn' | 'bad',
//     event:  { title: 'Leadership Workshop', when: 'until 14:30' },   // optional
//     power:  { source: 'usb' | 'battery', percent: 82 },               // optional
//     signal: 64,                                                       // percent, optional
//     issue:  { text: 'Offline · 6 min', who: 'alx' | 'tech', action: 'Resending schedule' }
//   }

const svg = (body) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const ICONS = {
  plug: svg('<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0z"/><path d="M12 16.5V21"/>'),
  alx: svg('<path d="M12 3.5l1.7 4.3 4.3 1.7-4.3 1.7L12 15.5l-1.7-4.3L6 9.5l4.3-1.7z"/><path d="M18 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>'),
  tech: svg('<circle cx="12" cy="8" r="3.5"/><path d="M5 20.5a7 7 0 0 1 14 0"/>'),
  close: svg('<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>'),
  chevron: svg('<path d="M9.5 6l6 6-6 6"/>')
};

const RANK = { bad: 0, warn: 1, ok: 2 };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(name, className = 'rb-icon') {
  const node = el('span', className);
  node.innerHTML = ICONS[name] || '';
  return node;
}

function battery(percent) {
  const level = Math.max(0, Math.min(100, percent));
  const node = el('span', 'rb-battery');
  node.dataset.tone = level < 20 ? 'bad' : level < 40 ? 'warn' : 'ok';
  node.innerHTML = svg(
    '<rect x="2.5" y="7.5" width="17" height="9" rx="2.5"/><path d="M21.5 10.5v3"/>' +
    `<rect x="4.5" y="9.5" width="${((13 * level) / 100).toFixed(1)}" height="5" rx="1" fill="currentColor" stroke="none"/>`
  );
  return node;
}

function signal(percent) {
  const node = el('span', 'rb-signal');
  const lit = percent <= 0 ? 0 : Math.min(4, Math.ceil(percent / 25));
  node.dataset.tone = percent < 30 ? 'warn' : 'ok';
  node.innerHTML = svg([0, 1, 2, 3].map((i) =>
    `<rect x="${3.5 + i * 4.5}" y="${16 - i * 3.5}" width="3" height="${4 + i * 3.5}" rx="1.2" ` +
    `fill="currentColor" stroke="none" opacity="${i < lit ? 1 : 0.25}"/>`).join(''));
  return node;
}

function vitals(reader) {
  const node = el('div', 'rb-vitals');
  if (reader.power) {
    const item = el('span', 'rb-vital');
    item.append(reader.power.source === 'usb' ? icon('plug') : battery(reader.power.percent ?? 0));
    if (Number.isFinite(reader.power.percent)) item.append(el('span', null, `${Math.min(100, reader.power.percent)}%`));
    item.title = reader.power.source === 'usb' ? 'On USB power' : 'On battery';
    node.append(item);
  }
  if (Number.isFinite(reader.signal)) {
    const item = el('span', 'rb-vital');
    item.append(signal(reader.signal), el('span', null, `${reader.signal}%`));
    item.title = 'Signal strength';
    node.append(item);
  }
  return node;
}

// ---- the compact tile on the main screen ---------------------------------

export function surfaceReaderTile(host, summary, onOpen) {
  const tile = el('button', 'rb-tile');
  host.append(tile);
  function render(next) {
    tile.dataset.tone = next.tone || 'ok';
    tile.replaceChildren();
    const text = el('span', 'rb-tile__text');
    text.append(el('span', 'rb-tile__name', next.name || 'BHL'), el('span', 'rb-tile__title', next.title));
    tile.append(el('span', 'rb-light'), text, icon('chevron', 'rb-tile__chevron'));
    tile.setAttribute('aria-label', `${next.name || 'BHL'}: ${next.title}. Open room readers`);
  }
  render(summary);
  tile.addEventListener('click', () => onOpen?.());
  return { update: render, element: tile };
}

// ---- the board ------------------------------------------------------------

export function buildCard(reader, index) {
  const card = el('article', 'rb-card');
  card.dataset.tone = reader.tone || 'ok';
  card.style.setProperty('--i', index);

  const head = el('header', 'rb-card__head');
  head.append(el('span', 'rb-light'), el('span', 'rb-card__room', reader.room || reader.uid), vitals(reader));
  card.append(head);

  card.append(el('p', 'rb-card__problem', reader.issue?.text ?? ''));
  if (reader.event?.title) {
    const event = el('p', 'rb-card__event');
    event.append(el('span', 'rb-card__event-title', reader.event.title));
    if (reader.event.when) event.append(el('span', 'rb-card__when', reader.event.when));
    card.append(event);
  }

  const foot = el('footer', 'rb-card__foot');
  if (reader.issue?.action) {
    const who = el('span', 'rb-card__who');
    who.dataset.who = reader.issue.who || 'alx';
    who.append(icon(who.dataset.who), el('span', null, reader.issue.action));
    foot.append(who);
  }
  foot.append(el('span', 'rb-card__uid', reader.uid));
  card.append(foot);
  return card;
}

function buildFleet(readers) {
  const rooms = new Map();
  for (const reader of readers) {
    const key = reader.room || '—';
    if (!rooms.has(key)) rooms.set(key, []);
    rooms.get(key).push(reader);
  }
  const fleet = el('div', 'rb-fleet');
  for (const [room, members] of rooms) {
    const group = el('div', 'rb-room');
    const worst = members.reduce((tone, r) => (RANK[r.tone] < RANK[tone] ? r.tone : tone), 'ok');
    group.dataset.tone = worst;
    const dots = el('div', 'rb-room__dots');
    for (const reader of members) {
      const dot = el('span', 'rb-dot');
      dot.dataset.tone = reader.tone || 'ok';
      dot.dataset.label = `${room} · ${reader.uid}`;
      dot.setAttribute('aria-label', `${room}, reader ${reader.uid}: ${reader.tone}`);
      dots.append(dot);
    }
    group.append(dots, el('span', 'rb-room__name', room));
    fleet.append(group);
  }
  return fleet;
}

export function openReaderBoard(host, readers, options = {}) {
  let current = readers;
  const scrim = el('div', 'rb-board');
  const sheet = el('section', 'rb-sheet');
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', options.title || 'Room readers');
  scrim.append(sheet);
  host.append(scrim);

  function render(animate) {
    const sorted = [...current].sort((a, b) =>
      (RANK[a.tone] ?? 2) - (RANK[b.tone] ?? 2) || String(a.room).localeCompare(String(b.room)));
    const problems = sorted.filter((reader) => reader.tone !== 'ok');
    const bad = problems.filter((reader) => reader.tone === 'bad').length;
    const warn = problems.length - bad;

    // Friedl, 2026-10-09: the cards and nothing else.
    const section = el('div', 'rb-grid');
    if (problems.length) section.append(...problems.map(buildCard));
    else section.append(el('p', 'rb-calm', 'Every reader is where it should be.'));
    sheet.replaceChildren(section);
    sheet.classList.toggle('is-settled', !animate);
  }

  function finish() {
    document.removeEventListener('keydown', onKey);
    scrim.classList.add('is-leaving');
    const done = () => scrim.remove();
    scrim.addEventListener('animationend', done, { once: true });
    if (getComputedStyle(scrim).animationName === 'none') done();
    options.onClose?.();
  }
  function onKey(event) { if (event.key === 'Escape') finish(); }
  scrim.addEventListener('click', (event) => { if (event.target === scrim) finish(); });
  document.addEventListener('keydown', onKey);

  render(true);
  return { update(next) { current = next; render(false); }, close: finish };
}

// ---- concept B: one small trace per reader with a problem ------------------
//
// The same reader data, plus what AL/X has seen and done, shown the way the
// AL/X Execution Trace shows her work: a header, the current state with how
// long it has lasted, and a short log. Extra fields:
//
//   since: Date.parse(...)                     // when the problem began
//   trace: [['14:21:05', 'ping unanswered', 'error' | 'warn' | 'ok' | 'active' | 'alx'], ...]

function elapsed(since) {
  const seconds = Math.max(0, Math.floor((Date.now() - since) / 1000));
  const m = String(Math.floor(seconds / 60)).padStart(2, '0');
  return `${m}:${String(seconds % 60).padStart(2, '0')}`;
}

export function buildTrace(reader, index) {
  const box = el('article', 'rt');
  box.dataset.tone = reader.tone || 'ok';
  box.style.setProperty('--i', index);

  const head = el('header', 'rt__head');
  const names = el('div', 'rt__names');
  names.append(el('strong', null, reader.room || reader.uid),
    el('small', null, [reader.event?.title, reader.uid].filter(Boolean).join(' · ')));
  head.append(el('span', 'rb-light'), names, vitals(reader));
  box.append(head);

  const stage = el('div', 'rt__stage');
  const clock = el('time', null, reader.since ? elapsed(reader.since) : '');
  stage.append(el('span', null, reader.issue?.text ?? ''), clock);
  box.append(stage);
  if (reader.since) {
    const timer = setInterval(() => {
      if (!clock.isConnected) { clearInterval(timer); return; }
      clock.textContent = elapsed(reader.since);
    }, 1000);
  }

  const log = el('div', 'rt__log');
  for (const [at, text, tone] of reader.trace ?? []) {
    const line = el('div', 'rt__line');
    if (tone) line.dataset.tone = tone;
    line.append(el('time', null, at), el('span', null, text));
    log.append(line);
  }
  box.append(log);

  if (reader.issue?.action) {
    const next = el('footer', 'rt__next');
    next.dataset.who = reader.issue.who || 'alx';
    next.append(el('span', 'rt__prompt', reader.issue.who === 'tech' ? 'You >' : 'AL/X >'),
      el('span', null, reader.issue.action));
    box.append(next);
  }
  return box;
}
