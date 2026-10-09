// The BHL tile (D-041, amended 2026-10-09): a small pill on the main screen
// with one coloured light and one line, which opens one small trace per
// reader with a problem, in the look of the AL/X Execution Trace (ES module,
// no dependencies).
//
//   const view = surfaceReaderTraces(host, data);   // add the pill to host
//   view.update(nextData);                          // same pill, new state
//   view.dismiss();                                 // remove pill and traces
//
// The renderer composes no wording and decides no state: every word and tone
// comes from the server (interfaces/reader_tile.py). Shape:
//
//   {
//     tone: 'ok' | 'warn' | 'bad', name: 'BHL', title: '1 error · 2 warnings',
//     readers: [{
//       uid: 'ab2d5218', room: 'Majestic 1', tone: 'warn',
//       event:  { title: 'Leadership Workshop', when: 'until 14:30' } | null,
//       power:  { source: 'usb' | 'battery', percent: 82 } | null,
//       signal: 64 | null,
//       issue:  { text: 'Schedule not confirmed', who: 'alx' | 'tech' | 'reader', action: 'Sending the schedule' },
//       since:  '2026-10-09T14:46:31+00:00' | null,
//       trace:  [['14:46:31', 'sending schedule', 'alx'], ...]
//     }]
//   }

const svg = (body) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const PLUG = svg('<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0z"/><path d="M12 16.5V21"/>');
const CHEVRON = svg('<path d="M9.5 6l6 6-6 6"/>');

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function battery(percent) {
  const level = Math.max(0, Math.min(100, percent ?? 0));
  const node = el('span', 'rt-battery');
  node.dataset.tone = level < 20 ? 'bad' : level < 40 ? 'warn' : 'ok';
  node.innerHTML = svg(
    '<rect x="2.5" y="7.5" width="17" height="9" rx="2.5"/><path d="M21.5 10.5v3"/>' +
    `<rect x="4.5" y="9.5" width="${((13 * level) / 100).toFixed(1)}" height="5" rx="1" fill="currentColor" stroke="none"/>`
  );
  return node;
}

function signal(percent) {
  const node = el('span', 'rt-signal');
  const lit = percent <= 0 ? 0 : Math.min(4, Math.ceil(percent / 25));
  node.dataset.tone = percent < 30 ? 'warn' : 'ok';
  node.innerHTML = svg([0, 1, 2, 3].map((i) =>
    `<rect x="${3.5 + i * 4.5}" y="${16 - i * 3.5}" width="3" height="${4 + i * 3.5}" rx="1.2" ` +
    `fill="currentColor" stroke="none" opacity="${i < lit ? 1 : 0.25}"/>`).join(''));
  return node;
}

function vitals(reader) {
  const node = el('div', 'rt-vitals');
  if (reader.power) {
    const item = el('span', 'rt-vital');
    if (reader.power.source === 'usb') {
      const plug = el('span', 'rt-plug');
      plug.innerHTML = PLUG;
      item.append(plug);
    } else {
      item.append(battery(reader.power.percent));
    }
    if (Number.isFinite(reader.power.percent)) item.append(el('span', null, `${reader.power.percent}%`));
    item.title = reader.power.source === 'usb' ? 'On USB power' : 'On battery';
    node.append(item);
  }
  if (Number.isFinite(reader.signal)) {
    const item = el('span', 'rt-vital');
    item.append(signal(reader.signal), el('span', null, `${reader.signal}%`));
    item.title = 'Signal strength';
    node.append(item);
  }
  return node;
}

function elapsed(since) {
  const seconds = Math.max(0, Math.floor((Date.now() - since) / 1000));
  const hours = Math.floor(seconds / 3600);
  const pad = (value) => String(value).padStart(2, '0');
  const rest = `${pad(Math.floor((seconds % 3600) / 60))}:${pad(seconds % 60)}`;
  return hours ? `${hours}:${rest}` : rest;
}

export function buildTrace(reader, index = 0) {
  const box = el('article', 'rt');
  box.dataset.tone = reader.tone || 'ok';
  box.style.setProperty('--i', index);

  const head = el('header', 'rt__head');
  const names = el('div', 'rt__names');
  const event = reader.event ? [reader.event.title, reader.event.when].filter(Boolean).join(' · ') : '';
  names.append(el('strong', null, reader.room || reader.uid),
    el('small', null, [event, reader.uid].filter(Boolean).join(' · ')));
  head.append(el('span', 'rt-light'), names, vitals(reader));
  box.append(head);

  const stage = el('div', 'rt__stage');
  const clock = el('time');
  stage.append(el('span', null, reader.issue?.text ?? ''), clock);
  box.append(stage);
  const since = reader.since ? Date.parse(reader.since) : NaN;
  if (Number.isFinite(since)) clock.textContent = elapsed(since);
  box.since = since;
  box.clock = clock;

  if (reader.trace?.length) {
    const log = el('div', 'rt__log');
    for (const [at, text, tone] of reader.trace) {
      const line = el('div', 'rt__line');
      if (tone) line.dataset.tone = tone;
      line.append(el('time', null, at), el('span', null, text));
      log.append(line);
    }
    box.append(log);
  }

  if (reader.issue?.action) {
    const next = el('footer', 'rt__next');
    next.dataset.who = reader.issue.who || 'alx';
    const prompt = { tech: 'You >', reader: 'Reader >' }[next.dataset.who] ?? 'AL/X >';
    next.append(el('span', 'rt__prompt', prompt),
      el('span', null, reader.issue.action));
    box.append(next);
  }
  return box;
}

export function surfaceReaderTraces(host, data) {
  const pill = el('button', 'rt-pill');
  pill.type = 'button';
  const panel = el('div', 'rt-panel');
  panel.hidden = true;
  host.append(pill, panel);
  let current = data;
  let open = false;
  let shown = '';

  function renderPill() {
    pill.dataset.tone = current.tone || 'ok';
    const text = el('span', 'rt-pill__text');
    text.append(el('span', 'rt-pill__name', current.name || 'BHL'), el('span', 'rt-pill__title', current.title));
    const chevron = el('span', 'rt-pill__chevron');
    chevron.innerHTML = CHEVRON;
    pill.replaceChildren(el('span', 'rt-light'), text, chevron);
    const count = current.readers?.length ?? 0;
    pill.disabled = count === 0;
    pill.setAttribute('aria-expanded', String(open && count > 0));
    pill.setAttribute('aria-label', `${current.name || 'BHL'}: ${current.title}`);
  }

  function renderPanel(animate) {
    const readers = current.readers ?? [];
    const key = JSON.stringify(readers);
    if (key === shown && !animate) return;
    shown = key;
    panel.replaceChildren(...readers.map(buildTrace));
    panel.classList.toggle('is-settled', !animate);
    panel.hidden = !open || readers.length === 0;
  }

  function setOpen(next, animate = true) {
    open = next && (current.readers?.length ?? 0) > 0;
    renderPill();
    if (open) renderPanel(animate);
    else panel.hidden = true;
  }

  // The timers tick where they are; the traces are not rebuilt every second.
  const ticker = setInterval(() => {
    if (panel.hidden) return;
    for (const box of panel.children) {
      if (Number.isFinite(box.since)) box.clock.textContent = elapsed(box.since);
    }
  }, 1000);

  const onKey = (event) => { if (event.key === 'Escape' && open) setOpen(false); };
  pill.addEventListener('click', () => setOpen(!open));
  document.addEventListener('keydown', onKey);
  renderPill();

  return {
    update(next) {
      current = next;
      if (!(current.readers?.length)) open = false;
      renderPill();
      if (open) { panel.hidden = false; renderPanel(false); } else panel.hidden = true;
    },
    dismiss() {
      clearInterval(ticker);
      document.removeEventListener('keydown', onKey);
      pill.remove();
      panel.remove();
    }
  };
}
