// AL/X status tile — one compact card, any domain (ES module, no dependencies).
//
//   const tile = surfaceTile(host, data);   // add the card to host
//   tile.update(nextData);                  // same card, new state
//   tile.dismiss();                         // fade out and remove
//
// The renderer composes no wording and decides no state. Every word, icon
// name and tone comes from `data`; a healthy card and a card with a problem
// are the same markup with different data. Its size is fixed by design: more
// device types add chips, never blocks. Shape:
//
//   {
//     tone:    'ok' | 'warn' | 'bad',               // green, yellow, red
//     name:    'BHL',
//     context: '5 events today · Majestic',          // optional
//     state:   { title: '…', detail: '…' },          // detail optional
//     alx:     { text: 'not monitoring yet', idle: true },
//     chips:   [{ icon: 'reader', value: '0/2', tone: 'warn', label: 'Room readers online' }]
//   }

const svg = (body) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const ICONS = {
  reader: svg('<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M12 9v4"/><path d="M12 7h.01"/>'),
  scanner: svg('<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M8 8v8M10.5 8v8M13 8v5M15.5 8v8"/>'),
  plug: svg('<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0z"/><path d="M12 16.5V21"/>'),
  link: svg('<path d="M4 14a8 8 0 0 1 16 0"/><path d="M8 14a4 4 0 0 1 8 0"/><circle cx="12" cy="17" r="1.3"/>')
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function buildChip(chip) {
  const item = el('li', 'alx-tile__chip');
  item.dataset.tone = chip.tone || 'ok';
  const icon = el('span', 'alx-tile__chip-icon');
  icon.innerHTML = ICONS[chip.icon] || '';
  item.append(icon, el('span', null, chip.value), el('span', 'alx-tile__dot'));
  // The icon and the dot are only seen; say what they show, from the data.
  item.setAttribute('aria-label', `${chip.label || chip.icon}: ${chip.value} (${item.dataset.tone})`);
  if (chip.label) item.title = chip.label;
  return item;
}

function build(data) {
  const card = el('article', 'alx-tile');
  card.dataset.tone = data.tone || 'ok';
  card.setAttribute('aria-label', data.name);

  const head = el('header', 'alx-tile__head');
  head.append(el('span', 'alx-tile__name', data.name));
  if (data.context) head.append(el('span', 'alx-tile__context', data.context));
  card.append(head);

  const state = el('div', 'alx-tile__state');
  state.append(el('span', 'alx-tile__light'), el('p', 'alx-tile__title', data.state?.title ?? ''));
  card.append(state);
  if (data.state?.detail) card.append(el('p', 'alx-tile__detail', data.state.detail));

  if (data.alx?.text) {
    const alx = el('p', 'alx-tile__alx');
    if (data.alx.idle) alx.dataset.idle = '';
    alx.append(el('b', null, 'AL/X'), el('span', null, data.alx.text));
    card.append(alx);
  }

  if (data.chips?.length) {
    const chips = el('ul', 'alx-tile__chips');
    chips.append(...data.chips.map(buildChip));
    card.append(chips);
  }
  return card;
}

export function surfaceTile(host, data) {
  let card = build(data);
  host.append(card);
  return {
    update(next) {
      const replacement = build(next);
      replacement.classList.add('is-settled');
      card.replaceWith(replacement);
      card = replacement;
    },
    dismiss() {
      const leaving = card;
      leaving.classList.add('is-leaving');
      // Only the card's own exit ends it; child animations bubble here too.
      const done = (event) => {
        if (event && (event.target !== leaving || event.animationName !== 'alx-tile-out')) return;
        leaving.removeEventListener('animationend', done);
        leaving.remove();
      };
      leaving.addEventListener('animationend', done);
      // Reduced motion has no animation to end.
      if (getComputedStyle(leaving).animationName === 'none') done();
    }
  };
}
