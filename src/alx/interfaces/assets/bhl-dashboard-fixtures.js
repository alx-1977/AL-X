// Mockup of the BHL dashboard with sample data. Nothing here reads Particle,
// BehaviorLive or a device. `?state=ok` shows a calm day. Click BHL in the
// menu bar to open it; Esc or × closes it.

import { surfaceReaderTraces } from '/reader-traces.js';
import { openDashboard } from '/bhl-dashboard.js';

const ago = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();

const TRACES = [
  { uid: '93d2a1bc', room: 'Majestic · OUT', tone: 'bad', event: { title: 'Ethics in Practice: Case Studies', when: 'until 15:30' },
    power: { source: 'usb', percent: 96 }, signal: 52, since: ago(10),
    issue: { text: 'Offline · event running', who: 'tech', action: 'Check the reader in the room' },
    trace: [['14:38:10', 'card · ev 1100 · bat 96% · sig 52%', ''], ['14:45:52', 'offline · ping unanswered', 'error']] },
  { uid: '7f8932b8', room: 'Conference B · IN', tone: 'bad', event: { title: 'Behaviour Skills Training', when: 'until 15:00' },
    power: { source: 'usb', percent: 100 }, signal: 46, since: ago(1),
    issue: { text: 'On the wrong event', who: 'tech', action: 'Check the reader in the room' },
    trace: [['14:50:05', 'card · ev 1097 · bat 100% · sig 46%', ''], ['14:50:05', 'on event 1097 · expected 1100', 'error']] },
  { uid: 'cb72d794', room: 'Terrace · IN', tone: 'warn', event: { title: 'Data Collection in Schools', when: 'at 16:30' },
    power: { source: 'battery', percent: 14 }, signal: 85, since: null,
    issue: { text: 'Battery low · 14%', who: 'tech', action: 'Plug it into USB' },
    trace: [['14:02:11', 'card · ev 1102 · bat 21% · sig 85%', ''], ['14:31:07', 'card · ev 1102 · bat 14% · sig 85%', '']] },
  { uid: '234689c8', room: 'Atrium · OUT', tone: 'warn', event: { title: 'Parent Training Essentials', when: 'until 15:45' },
    power: { source: 'usb', percent: 100 }, signal: 18, since: null,
    issue: { text: 'Weak signal · 18%', who: 'reader', action: 'Keeps scans until sent' },
    trace: [['14:40:02', 'card · ev 1099 · bat 100% · sig 18%', '']] },
  { uid: 'bdbbeefa', room: 'Studio · IN', tone: 'warn', event: { title: 'Supervision Round Table', when: 'at 15:30' },
    power: { source: 'usb', percent: 100 }, signal: 77, since: ago(3),
    issue: { text: 'Schedule not confirmed', who: 'alx', action: 'Sending the schedule' },
    trace: [['14:46:30', 'reader asked for its schedule', ''], ['14:46:31', 'sending schedule', 'alx'],
      ['14:47:31', 'schedule not sent · device timeout', 'warn'], ['14:56:31', 'sending schedule', 'alx']] },
];

const DAYS = {
  bad: {
    tone: 'bad', errors: 2, warnings: 3, sessions_today: 30, updated_at: ago(0.05), place: 'Cape Town',
    uplink: { tone: 'ok', state: 'Online', last_read: '14:52', confirmed: '59/60', interval: '5 min', note: 'All good' },
    readers: { tone: 'bad', online: 59, total: 60, scans_today: 1790, offline: 1, correct: '58/59',
      note: 'Majestic · OUT · offline 10 min' },
    ring: { ok: 55, warn: 3, bad: 2 },
    traces: TRACES,
  },
  ok: {
    tone: 'ok', errors: 0, warnings: 0, sessions_today: 30, updated_at: ago(0.05), place: 'Cape Town',
    uplink: { tone: 'ok', state: 'Online', last_read: '14:52', confirmed: '60/60', interval: '5 min', note: 'All good' },
    readers: { tone: 'ok', online: 60, total: 60, scans_today: 1790, offline: 0, correct: '60/60', note: 'All good' },
    ring: { ok: 60, warn: 0, bad: 0 },
    traces: [],
  },
};

const params = new URLSearchParams(location.search);
const day = DAYS[params.get('state') === 'ok' ? 'ok' : 'bad'];

surfaceReaderTraces(document.body, { tone: day.tone, name: 'BHL', title: '', readers: day.traces.length ? day.traces : [{}] });
let board = null;
const open = () => { if (!board) board = openDashboard(document.body, day, { onClose: () => { board = null; } }); };
// In the mockup the BHL item opens the dashboard instead of the traces.
document.querySelector('.rt-bar__item').addEventListener('click', (event) => {
  event.stopImmediatePropagation();
  if (board) { board.close(); } else { open(); }
}, true);
if (!params.has('closed')) open();
