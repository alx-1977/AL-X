// Mockup: the BHL display as part of the scene. Sample data only.
// `?state=ok|warn|bad` picks a day; `?open` opens it; BHL in the menu bar,
// Esc or a click anywhere toggles it.

const READERS = [
  { room: 'Majestic · OUT', tone: 'bad', problem: 'Offline · event running', since: '10:00',
    event: 'Ethics in Practice: Case Studies · until 15:30', state: 'person', line: 'Stopped answering at 14:45 during its session — someone needs to look at it in the room',
    log: [['14:38:10', 'card · ev 1100 · bat 96% · sig 52%', ''], ['14:45:52', 'offline · ping unanswered', 'error']] },
  { room: 'Conference B · IN', tone: 'bad', problem: 'On the wrong event', since: '01:04',
    event: 'Behaviour Skills Training · until 15:00', state: 'person', line: 'Showing event 1097 while 1100 is running; its schedule is current, so the reader itself needs a look',
    log: [['14:50:05', 'card · ev 1097 · bat 100% · sig 46%', ''], ['14:50:05', 'on event 1097 · expected 1100', 'error']] },
  { room: 'Terrace · IN', tone: 'warn', problem: 'Battery low · 14%', since: '',
    event: 'Data Collection in Schools · at 16:30', state: 'person', line: 'At 14% and falling about 1% every 4 minutes — plug it in before its 16:30 session',
    log: [['14:18:40', 'card · ev 1102 · bat 18% · sig 84%', ''], ['14:31:07', 'card · ev 1102 · bat 14% · sig 85%', '']] },
  { room: 'Atrium · OUT', tone: 'warn', problem: 'Weak signal · 18%', since: '',
    event: 'Parent Training Essentials · until 15:45', state: 'waiting', line: 'Watching the signal; the reader holds its scans until they go through',
    log: [['14:40:02', 'card · ev 1099 · bat 100% · sig 18%', '']] },
  { room: 'Studio · IN', tone: 'warn', problem: 'Schedule not confirmed', since: '03:04',
    event: 'Supervision Round Table · at 15:30', state: 'working', line: 'Sending today’s schedule again — the last try timed out',
    log: [['14:46:31', 'sending schedule', 'alx'], ['14:47:31', 'schedule not sent · device timeout', 'warn'],
      ['14:56:31', 'sending schedule', 'alx']] },
];

const DAYS = {
  bad: { tone: 'bad', state: 'Attention needed · 2 errors · 3 warnings', online: '59/60', readers: READERS },
  warn: { tone: 'warn', state: '3 warnings', online: '60/60', readers: READERS.filter((r) => r.tone === 'warn') },
  ok: { tone: 'ok', state: 'All systems normal', online: '60/60', readers: [] },
};

const params = new URLSearchParams(location.search);
const day = DAYS[Object.hasOwn(DAYS, params.get('state')) ? params.get('state') : 'bad'];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The menu bar.
const bar = el('div', 'sf-bar');
const item = el('button', 'sf-bar__item');
item.dataset.tone = day.tone;
item.append(el('span', 'sf-led'), el('span', null, 'BHL'));
const clock = el('time', 'sf-bar__clock');
bar.append(el('span', 'sf-bar__app', 'AL/X'), el('span', 'sf-bar__space'), item, clock);
const tick = () => {
  clock.textContent = new Date().toLocaleString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  }).replace(/,/g, '');
};
tick();
setInterval(tick, 1000);

// 1. The scene's edges.
const glow = el('div', 'sf-glow');
glow.dataset.tone = day.tone;

// 2. Focus pull and the information.
const focus = el('div', 'sf-focus');
const info = el('div', 'sf-info');

const top = el('div', 'sf-top');
const hero = el('div', 'sf-hero sf-shade');
hero.dataset.tone = day.tone;
const state = el('p', 'sf-hero__state');
state.dataset.tone = day.tone;
state.append(el('span', 'sf-led'), el('span', null, day.state));
const figure = el('p', 'sf-hero__figure', day.online);
figure.append(el('small', null, 'READERS ONLINE'));
hero.append(el('p', 'sf-hero__label', 'BHL · LIVE'), state, figure);

const facts = el('div', 'sf-facts sf-shade');
for (const [value, label, idle] of [['1,790', 'scans today'], ['58/59', 'on the correct event'],
  ['59/60', 'schedules confirmed'], ['14:52', 'last read from BehaviorLive'],
  ['NOT MONITORED', 'scanners · PSUs', true]]) {
  const fact = el('div', 'sf-fact');
  if (idle) fact.dataset.idle = '';
  fact.append(el('b', null, value), el('span', null, label));
  facts.append(fact);
}
top.append(hero, facts);

const bottom = el('div', 'sf-readers');
if (day.readers.length) {
  for (const reader of day.readers) {
    const column = el('section', 'sf-reader sf-shade');
    column.dataset.tone = reader.tone;
    const room = el('p', 'sf-room');
    room.append(el('span', 'sf-led'), el('span', null, reader.room));
    const problem = el('p', 'sf-problem');
    problem.append(el('span', null, reader.problem), el('time', null, reader.since));
    const log = el('ul', 'sf-log');
    for (const [at, text, tone] of reader.log) {
      const line = el('li');
      if (tone) line.dataset.tone = tone;
      line.append(el('time', null, at), el('span', null, text));
      log.append(line);
    }
    // The last line: what is happening now, in AL/X's words (sample text
    // here), and how: working, waiting for an answer, or needing a person.
    const next = el('p', 'sf-next');
    next.dataset.state = reader.state;
    // The cursor sits right after the last word, like a terminal's.
    const words = el('span', 'sf-next__text', reader.line);
    words.append(el('span', 'sf-next__cursor'));
    next.append(words);
    column.append(room, problem, el('p', 'sf-event', reader.event), log, next);
    bottom.append(column);
  }
} else {
  bottom.style.display = 'block';
  bottom.append(el('p', 'sf-calm sf-shade', 'Every reader is where it should be.'));
}
info.append(top, el('div'), bottom);

const hint = el('div', 'sf-hint', 'Click BHL (top right) to pull focus · Esc or click to return');
document.body.append(glow, focus, info, bar, hint);

let open = false;
function setOpen(next) {
  open = next;
  // The scene softens and the information follows at once (its own short
  // delay is in the stylesheet); closing, both go together.
  focus.classList.toggle('is-on', open);
  info.classList.toggle('is-on', open);
  item.setAttribute('aria-expanded', String(open));
}
item.addEventListener('click', () => setOpen(!open));
focus.addEventListener('click', () => setOpen(false));
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setOpen(false); });
if (params.has('open')) setOpen(true);
if (params.has('demo')) setTimeout(() => setOpen(true), 500);
