// AL/X status tile — one reusable card, any domain (ES module, no dependencies).
//
//   const tile = surfaceTile(host, data);   // add the card to host
//   tile.update(nextData);                  // same card, new state
//   tile.dismiss();                         // fade out and remove
//
// The renderer composes no wording and decides no state. Every word, icon
// name and tone comes from `data`; a healthy card and a card with a problem
// are the same markup with different data. Shape:
//
//   {
//     visual:   { src, alt },                       // contextual image, optional
//     icon:     'device',                           // identity mark
//     title:    'BHL Event Hardware',
//     subtitle: 'Hardware operations',              // optional
//     place:    'Cape Town, South Africa',          // optional
//     tone:     'ok' | 'attention',
//     activity: 'Monitoring',                       // short, top right, optional
//     state:    { title: 'All systems normal', detail: '…' },
//     facts:    [{ icon, value, label, tone: 'ok' | 'attention' | 'disabled', note }]
//   }
//
// A fact with tone 'disabled' is not monitored: it shows its label with the
// note beneath in place of a value, and carries no status dot.

const svg = (body) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const ICONS = {
  device: svg('<rect x="6" y="2.5" width="10" height="17" rx="2.2"/><path d="M16 5.5h1.2a1.8 1.8 0 0 1 1.8 1.8v12.4a1.8 1.8 0 0 1-1.8 1.8H9"/><circle cx="11" cy="12.5" r="2.4"/><path d="M11 6h.01"/>'),
  scanner: svg('<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M8 8v8M10.5 8v8M13 8v5M15.5 8v8M13 15.5v.5"/>'),
  reader: svg('<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M12 9v4"/><path d="M12 7h.01"/>'),
  plug: svg('<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0z"/><path d="M12 16.5V21"/>'),
  place: svg('<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/>'),
  ok: svg('<path d="M6.5 12.5l3.6 3.6 7.4-8"/>'),
  attention: svg('<path d="M12 7v6.5"/><path d="M12 17h.01"/>')
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function iconFor(name, className) {
  const node = el('span', className);
  node.innerHTML = ICONS[name] || '';
  return node;
}

function buildFact(fact) {
  const item = el('li', 'alx-tile__fact');
  item.dataset.tone = fact.tone || 'ok';
  const body = el('span', 'alx-tile__fact-body');
  const monitored = fact.tone !== 'disabled';
  body.append(monitored
    ? el('span', 'alx-tile__fact-value', fact.value)
    : el('span', 'alx-tile__fact-name', fact.label));
  body.append(el('span', 'alx-tile__fact-label', monitored ? fact.label : fact.note));
  item.append(iconFor(fact.icon, 'alx-tile__fact-icon'), body);
  if (monitored) item.append(el('span', 'alx-tile__dot'));
  return item;
}

function build(data) {
  const card = el('article', 'alx-tile');
  card.dataset.tone = data.tone || 'ok';
  card.setAttribute('aria-label', data.title);

  if (data.visual?.src) {
    const visual = el('div', 'alx-tile__visual');
    const image = el('img');
    image.src = data.visual.src;
    image.alt = data.visual.alt || '';
    visual.append(image);
    card.append(visual);
  }

  const head = el('header', 'alx-tile__head');
  const identity = el('div', 'alx-tile__identity');
  identity.append(el('h2', 'alx-tile__title', data.title));
  if (data.subtitle) identity.append(el('p', 'alx-tile__subtitle', data.subtitle));
  head.append(iconFor(data.icon, 'alx-tile__mark'), identity);
  if (data.activity) {
    const activity = el('p', 'alx-tile__activity');
    activity.append(el('span', 'alx-tile__dot'), el('span', null, data.activity));
    head.append(activity);
  }
  card.append(head);

  if (data.place) {
    const place = el('p', 'alx-tile__place');
    place.append(iconFor('place', 'alx-tile__place-icon'), el('span', null, data.place));
    card.append(place);
  }

  const summary = el('div', 'alx-tile__state');
  const words = el('div', 'alx-tile__state-words');
  words.append(el('p', 'alx-tile__state-title', data.state?.title ?? ''));
  if (data.state?.detail) words.append(el('p', 'alx-tile__state-detail', data.state.detail));
  summary.append(iconFor(card.dataset.tone, 'alx-tile__badge'), words);
  card.append(summary);

  if (data.facts?.length) {
    const facts = el('ul', 'alx-tile__facts');
    facts.append(...data.facts.map(buildFact));
    card.append(facts);
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
