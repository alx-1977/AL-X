// Static fixtures for the AL/X status tile, and the page that shows them.
//
// Sample data only: nothing here reads Particle, BehaviorLive or a device.
// The three states Friedl approved on 2026-10-07 are plain data for the one
// renderer in tile.js; `?state=` picks one, and there is no on-screen control.

import { surfaceTile } from '/tile.js';

const LINK_OK = { icon: 'link', value: 'BHL link', tone: 'ok' };

const FIXTURES = {
  healthy: {
    tone: 'ok',
    name: 'BHL',
    context: '5 events today · Majestic',
    state: { title: 'All systems normal', detail: 'Event under way until 17:05' },
    alx: { text: 'monitoring' },
    chips: [{ icon: 'reader', value: '2/2', tone: 'ok' }, LINK_OK],
  },
  warning: {
    tone: 'warn',
    name: 'BHL',
    context: '5 events today · Majestic',
    state: { title: '2 room readers offline', detail: 'First event at 16:05 · schedules not confirmed' },
    alx: { text: 'not monitoring yet', idle: true },
    chips: [{ icon: 'reader', value: '0/2', tone: 'warn' }, LINK_OK],
  },
  fault: {
    tone: 'bad',
    name: 'BHL',
    context: '42 events today · 15 rooms',
    state: { title: '3 room readers offline', detail: '9 rooms in session · next starts 15:30 (6 rooms)' },
    alx: { text: 'restarted 1 · client alerted 15:12' },
    chips: [
      { icon: 'reader', value: '27/30', tone: 'bad' },
      { icon: 'scanner', value: '10/10', tone: 'ok' },
      { icon: 'plug', value: '5/5', tone: 'ok' },
      LINK_OK,
    ],
  },
};

const requested = new URLSearchParams(location.search).get('state');
surfaceTile(document.getElementById('tiles'), FIXTURES[Object.hasOwn(FIXTURES, requested) ? requested : 'healthy']);
