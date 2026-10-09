// Sample data for the BHL tile and its reader traces (D-041, amended
// 2026-10-09). Sample data only: nothing here reads Particle, BehaviorLive or
// a device. `?state=ok|warn|bad` picks a day; `?open` opens the traces.

import { surfaceReaderTraces } from '/reader-traces.js';

const ago = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();

const WARNINGS = [
  { uid: 'cb72d794', room: 'Terrace', tone: 'warn', event: { title: 'Data Collection in Schools', when: 'at 16:30' },
    power: { source: 'battery', percent: 14 }, signal: 85, since: null,
    issue: { text: 'Battery low · 14%', who: 'tech', action: 'Plug it into USB' },
    trace: [['14:02:11', 'card · event 1102 · bat 21% · sig 85%', ''],
      ['14:18:40', 'card · event 1102 · bat 18% · sig 84%', ''],
      ['14:31:07', 'card · event 1102 · bat 14% · sig 85%', '']] },
  { uid: '234689c8', room: 'Atrium', tone: 'warn', event: { title: 'Parent Training Essentials', when: 'until 15:45' },
    power: { source: 'usb', percent: 100 }, signal: 18, since: null,
    issue: { text: 'Weak signal · 18%', who: 'reader', action: 'Keeps scans until they are sent' },
    trace: [['14:40:02', 'card · event 1099 · bat 100% · sig 18%', '']] },
  { uid: 'bdbbeefa', room: 'Studio', tone: 'warn', event: { title: 'Supervision Round Table', when: 'at 15:30' },
    power: { source: 'usb', percent: 100 }, signal: 77, since: ago(3),
    issue: { text: 'Schedule not confirmed', who: 'alx', action: 'Sending the schedule' },
    trace: [['14:46:30', 'reader asked for its schedule', ''], ['14:46:31', 'sending schedule', 'alx'],
      ['14:47:31', 'schedule not sent · device timeout', 'warn'], ['14:56:31', 'sending schedule', 'alx']] },
];
const ERRORS = [
  { uid: '93d2a1bc', room: 'Majestic 3', tone: 'bad', event: { title: 'Ethics in Practice: Case Studies', when: 'until 15:30' },
    power: { source: 'usb', percent: 96 }, signal: 52, since: ago(6),
    issue: { text: 'Offline · event running', who: 'tech', action: 'Check the reader in the room' },
    trace: [['14:38:10', 'card · event 1100 · bat 96% · sig 52%', ''],
      ['14:45:52', 'offline · ping unanswered', 'error']] },
  { uid: '7f8932b8', room: 'Conference B', tone: 'bad', event: { title: 'Behaviour Skills Training', when: 'until 15:00' },
    power: { source: 'usb', percent: 100 }, signal: 46, since: ago(1),
    issue: { text: 'On the wrong event', who: 'tech', action: 'Check the reader in the room' },
    trace: [['14:50:05', 'card · event 1097 · bat 100% · sig 46%', ''],
      ['14:50:05', 'on event 1097 · expected 1100', 'error']] },
];

const DAYS = {
  ok: { tone: 'ok', name: 'BHL', title: 'All OK', readers: [] },
  warn: { tone: 'warn', name: 'BHL', title: '3 warnings', readers: WARNINGS },
  bad: { tone: 'bad', name: 'BHL', title: '2 errors · 3 warnings', readers: [...ERRORS, ...WARNINGS] },
};

const params = new URLSearchParams(location.search);
const view = surfaceReaderTraces(document.body, DAYS[Object.hasOwn(DAYS, params.get('state')) ? params.get('state') : 'bad']);
if (params.has('open')) document.querySelector('.rt-pill').click();
void view;
