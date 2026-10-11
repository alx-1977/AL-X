// Blank-canvas mockup: the event day as light on the scene. Sample data
// only; the sentences stand in for what AL/X would write herself.
// `?state=ok` shows a calm day; `?open` opens it. BHL, Esc or a click toggles.

const DAY_START = 8;   // 08:00
const DAY_END = 18;    // 18:00
const NOW = 14 + 52 / 60;

const ROOMS = ['Atrium', 'Ballroom', 'Boardroom', 'Conference A', 'Conference B', 'Courtyard',
  'Garden Room', 'Lakeside', 'Library', 'Majestic', 'Pavilion', 'Studio', 'Terrace'];

// Each room's sessions as [start, end] in hours, local time.
let seed = 11;
const random = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const SESSIONS = Object.fromEntries(ROOMS.map((room) => {
  const list = [];
  let t = 8.5 + random() * 0.75;
  while (t < 17) {
    const length = 0.75 + Math.round(random() * 3) * 0.25;
    list.push([t, Math.min(17.75, t + length)]);
    t += length + 0.25 + Math.round(random() * 2) * 0.25;
  }
  return [room, list];
}));

const PROBLEMS = [
  { room: 'Majestic', label: 'Majestic · OUT', tone: 'bad', problem: 'Offline · event running', since: '10:00',
    event: 'Ethics in Practice: Case Studies · until 15:30',
    line: 'Stopped answering at 14:45 during its session — someone needs to look at it in the room',
    log: [['14:38:10', 'card · ev 1100 · bat 96% · sig 52%', ''], ['14:45:52', 'offline · ping unanswered', 'error']] },
  { room: 'Conference B', label: 'Conference B · IN', tone: 'bad', problem: 'On the wrong event', since: '01:04',
    event: 'Behaviour Skills Training · until 15:00',
    line: 'Showing event 1097 while 1100 is running; its schedule is current, so the reader itself needs a look',
    log: [['14:50:05', 'card · ev 1097 · bat 100% · sig 46%', ''], ['14:50:05', 'on event 1097 · expected 1100', 'error']] },
  { room: 'Terrace', label: 'Terrace · IN', tone: 'warn', problem: 'Battery low · 14%', since: '',
    event: 'Data Collection in Schools · at 16:30',
    line: 'At 14% and falling about 1% every 4 minutes — plug it in before its 16:30 session',
    log: [['14:18:40', 'card · ev 1102 · bat 18% · sig 84%', ''], ['14:31:07', 'card · ev 1102 · bat 14% · sig 85%', '']] },
  { room: 'Atrium', label: 'Atrium · OUT', tone: 'warn', problem: 'Weak signal · 18%', since: '',
    event: 'Parent Training Essentials · until 15:45',
    line: 'Watching the signal; the reader holds its scans until they go through',
    log: [['14:40:02', 'card · ev 1099 · bat 100% · sig 18%', '']] },
  { room: 'Studio', label: 'Studio · IN', tone: 'warn', problem: 'Schedule not confirmed', since: '03:04',
    event: 'Supervision Round Table · at 15:30',
    line: 'Sending today’s schedule again — the last try timed out',
    log: [['14:46:31', 'sending schedule', 'alx'], ['14:47:31', 'schedule not sent · device timeout', 'warn'],
      ['14:56:31', 'sending schedule', 'alx']] },
];

const params = new URLSearchParams(location.search);
const calm = params.get('state') === 'ok';
const problems = calm ? [] : PROBLEMS;
const tone = problems.some((p) => p.tone === 'bad') ? 'bad' : problems.length ? 'warn' : 'ok';
const toneOf = (room) => {
  const found = problems.filter((p) => p.room === room);
  return found.some((p) => p.tone === 'bad') ? 'bad' : found.length ? 'warn' : 'ok';
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
const at = (hours) => `${((hours - DAY_START) / (DAY_END - DAY_START)) * 100}%`;
const hhmm = (hours) => `${String(Math.floor(hours)).padStart(2, '0')}:${String(Math.round((hours % 1) * 60)).padStart(2, '0')}`;

// ---- the menu bar, the edges, the focus pull (as in scene-focus) -----------

const bar = el('div', 'sf-bar');
const item = el('button', 'sf-bar__item');
item.dataset.tone = tone;
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

const glow = el('div', 'sf-glow');
glow.dataset.tone = tone;
const focus = el('div', 'sf-focus');
const info = el('div', 'sf-info');
const page = el('div', 'cv');

// ---- 1. what matters, in one sentence ---------------------------------------

const say = el('div', 'cv-say');
const words = el('div', 'sf-shade');
words.dataset.tone = tone;
const line = el('p', 'cv-say__line');
if (calm) {
  line.append('All 60 readers are on the right event.');
} else {
  line.append(el('em', null, '3 readers'), ' need someone in the room.');
}
const facts = el('p', 'cv-say__facts');
for (const [value, label] of (calm
  ? [['60/60', 'online'], ['1,790', 'scans today'], ['14:52', 'schedules read']]
  : [['59/60', 'online'], ['58', 'on the right event'], ['1,790', 'scans today'], ['2', 'AL/X is handling'],
    ['14:52', 'schedules read']])) {
  const fact = el('span');
  fact.append(el('b', null, value), ` ${label}`);
  facts.append(fact);
}
words.append(line, facts);
const idle = el('p', 'cv-say__idle sf-shade', 'scanners · PSUs\nnot monitored yet');
idle.style.whiteSpace = 'pre-line';
say.append(words, idle);

// ---- 2. the day as light ----------------------------------------------------------

const day = el('div', 'cv-day sf-shade');
const hours = el('div', 'cv-hours');
for (let h = DAY_START; h <= DAY_END; h += 1) {
  const label = el('span', null, String(h).padStart(2, '0'));
  label.style.left = at(h);
  hours.append(label);
}
day.append(el('span'), hours);
for (const room of ROOMS) {
  const roomTone = toneOf(room);
  const name = el('div', 'cv-room');
  name.dataset.tone = roomTone;
  if (roomTone !== 'ok') name.append(el('span', 'sf-led'));
  name.append(el('span', null, room));
  const lane = el('div', 'cv-lane');
  for (const [start, end] of SESSIONS[room]) {
    const bar = el('span', 'cv-session');
    bar.dataset.when = end <= NOW ? 'past' : start <= NOW ? 'now' : 'next';
    bar.dataset.tone = roomTone;
    bar.style.left = at(start);
    bar.style.width = `calc(${at(end)} - ${at(start)})`;
    bar.title = `${room} · ${hhmm(start)}–${hhmm(end)}`;
    lane.append(bar);
  }
  day.append(name, lane);
}
// Now, placed across the whole day: past the room names (9em), at its share
// of the lanes' width.
const now = el('div', 'cv-now');
const share = (NOW - DAY_START) / (DAY_END - DAY_START);
now.style.left = `calc(9em + (100% - 9em) * ${share.toFixed(4)})`;
now.append(el('span', null, hhmm(NOW)));
day.append(now);

// ---- 3. the readers that need attention, as light -------------------------------

const need = el('div', 'cv-need');
for (const reader of problems) {
  const column = el('section', 'sf-reader sf-shade');
  column.dataset.tone = reader.tone;
  const room = el('p', 'sf-room');
  room.append(el('span', 'sf-led'), el('span', null, reader.label));
  const problem = el('p', 'sf-problem');
  problem.append(el('span', null, reader.problem), el('time', null, reader.since));
  const log = el('ul', 'sf-log');
  for (const [time, text, lineTone] of reader.log) {
    const li = el('li');
    if (lineTone) li.dataset.tone = lineTone;
    li.append(el('time', null, time), el('span', null, text));
    log.append(li);
  }
  const next = el('p', 'sf-next');
  const sentence = el('span', 'sf-next__text', reader.line);
  sentence.append(el('span', 'sf-next__cursor'));
  next.append(sentence);
  column.append(room, problem, el('p', 'sf-event', reader.event), log, next);
  need.append(column);
}

page.append(say, day, need);
info.append(page);
const hint = el('div', 'sf-hint', 'Click BHL (top right) · Esc or click to return · ?state=ok for a calm day');
document.body.append(glow, focus, info, bar, hint);

let open = false;
function setOpen(next) {
  open = next;
  focus.classList.toggle('is-on', open);
  info.classList.toggle('is-on', open);
  item.setAttribute('aria-expanded', String(open));
}
item.addEventListener('click', () => setOpen(!open));
focus.addEventListener('click', () => setOpen(false));
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setOpen(false); });
if (params.has('open')) setOpen(true);
