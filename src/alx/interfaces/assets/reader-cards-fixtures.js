// Sample readers for the room reader preview: sixty readers in fifteen rooms.
// Sample data only: nothing here reads Particle, BehaviorLive or a device.
// `?state=ok|warn|bad` picks a day, `?look=dark|frost|minimal` a look, and
// `?open` opens the board. 1, 2 and 3 switch looks.

import { surfaceReaderTile, openReaderBoard, buildCard, buildTrace } from '/reader-cards.js';

const ROOMS = [
  ['Majestic 1', 'Leadership in Behaviour Analysis', 'until 14:30'],
  ['Majestic 2', 'Ethics for Supervisors', 'until 14:30'],
  ['Majestic 3', 'Ethics in Practice: Case Studies', 'until 14:30'],
  ['Boardroom', 'Verbal Behaviour: Mands and Tacts', 'until 15:00'],
  ['Garden Room', 'Functional Assessment Workshop', 'until 16:00'],
  ['Library', 'Closing Keynote', 'next 17:00'],
  ['Terrace', 'Data Collection in Schools', 'next 16:30'],
  ['Atrium', 'Parent Training Essentials', 'until 15:45'],
  ['Studio', 'Supervision Round Table', 'next 15:30'],
  ['Conference A', 'Precision Teaching', 'until 15:00'],
  ['Conference B', 'Behaviour Skills Training', 'until 15:00'],
  ['Ballroom', 'Plenary: The Next Decade', 'until 16:15'],
  ['Lakeside', 'Toilet Training Protocols', 'next 15:30'],
  ['Pavilion', 'Feeding Disorders', 'until 15:20'],
  ['Courtyard', 'Social Skills Groups', 'next 16:00'],
];

let seed = 7;
const random = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const uid = () => Math.floor(random() * 0xffffffff).toString(16).padStart(8, '0');

const HEALTHY = ROOMS.flatMap(([room, title, when]) =>
  [0, 1, 2, 3].map(() => ({
    uid: uid(), room, tone: 'ok', event: { title, when },
    power: random() < 0.7 ? { source: 'usb', percent: 100 } : { source: 'battery', percent: 45 + Math.floor(random() * 50) },
    signal: 40 + Math.floor(random() * 55),
  })));

const ago = (minutes) => Date.now() - minutes * 60_000;
const ISSUES = {
  'Terrace': ['warn', { power: { source: 'battery', percent: 14 }, since: ago(22),
    issue: { text: 'Battery low · 14%', who: 'tech', action: 'Plug into USB before 16:30' },
    trace: [['14:02:11', 'status card · bat 21% · on battery'], ['14:18:40', 'status card · bat 18%'],
      ['14:31:07', 'status card · bat 14%', 'warn'], ['14:31:07', 'below 20% · next event 16:30', 'warn']] }],
  'Atrium': ['warn', { signal: 18, since: ago(9),
    issue: { text: 'Weak signal · 18%', who: 'alx', action: 'Watching · scans are kept safely' },
    trace: [['14:40:02', 'status card · sig 18% · sq 22%', 'warn'], ['14:41:15', 'scan queued on the reader'],
      ['14:42:48', 'scan delivered to BehaviorLive', 'ok']] }],
  'Studio': ['warn', { since: ago(3),
    issue: { text: 'Schedule not confirmed', who: 'alx', action: 'Resending the schedule' },
    trace: [['14:46:30', 'calendar changed · 3 events'], ['14:46:31', 'schedule sent · v b33522e1', 'active'],
      ['14:47:31', 'no confirmation from the reader', 'warn'], ['14:49:02', 'resending schedule', 'alx']] }],
  'Majestic 3': ['bad', { power: undefined, signal: undefined, since: ago(6),
    issue: { text: 'Offline · event running', who: 'tech', action: 'Check the reader in the room' },
    trace: [['14:38:10', 'status card · bat 96% · usb', 'ok'], ['14:45:52', 'ping unanswered', 'error'],
      ['14:45:52', 'offline · event until 14:30', 'error'], ['14:46:52', 'ping unanswered', 'error'],
      ['14:47:00', 'told Friedl · technician needed', 'alx']] }],
  'Conference B': ['bad', { since: ago(1),
    issue: { text: 'On the wrong event', who: 'alx', action: 'Sending the right schedule' },
    trace: [['14:50:05', 'status card · event 1097 · expected 1100', 'error'], ['14:50:05', 'reader holds v 8fbf6da6 · current b33522e1'],
      ['14:50:06', 'sending schedule b33522e1', 'alx']] }],
};

function day(kinds) {
  const readers = HEALTHY.map((reader) => ({ ...reader }));
  for (const [room, [tone, change]] of Object.entries(ISSUES)) {
    if (!kinds.includes(tone)) continue;
    const reader = readers.find((item) => item.room === room);
    Object.assign(reader, { tone }, change);
  }
  return readers;
}

const DAYS = {
  ok: { readers: day([]), title: 'All OK' },
  warn: { readers: day(['warn']), title: '3 warnings' },
  bad: { readers: day(['warn', 'bad']), title: '2 errors · 3 warnings' },
};
const TONES = { ok: 'ok', warn: 'warn', bad: 'bad' };
const LOOKS = ['dark', 'frost', 'minimal'];

const params = new URLSearchParams(location.search);
const state = Object.hasOwn(DAYS, params.get('state')) ? params.get('state') : 'bad';
let look = 'dark';
document.documentElement.dataset.look = look;

// Default: the two concepts side by side. `?tile` shows concept A as it
// would open from the BHL tile.
if (!params.has('tile')) {
  const problems = DAYS[state].readers
    .filter((reader) => reader.tone !== 'ok')
    .sort((a, b) => (a.tone === b.tone ? 0 : a.tone === 'bad' ? -1 : 1));
  const compare = document.createElement('div');
  compare.className = 'rb-compare';
  for (const [label, build] of [['A · Glass cards', buildCard], ['B · Mini traces', buildTrace]]) {
    const column = document.createElement('section');
    column.className = 'rb-compare__column';
    const heading = document.createElement('h2');
    heading.className = 'rb-compare__label';
    heading.textContent = label;
    const grid = document.createElement('div');
    grid.className = 'rb-grid';
    grid.append(...problems.map(build));
    column.append(heading, grid);
    compare.append(column);
  }
  document.body.append(compare);
} else {
  let board = null;
  const open = () => {
    if (board) return;
    board = openReaderBoard(document.body, DAYS[state].readers, {
      title: 'Room readers',
      onClose: () => { board = null; },
    });
  };
  surfaceReaderTile(document.body, { tone: TONES[state], name: 'BHL', title: DAYS[state].title }, open);
  if (params.has('open')) open();
}
