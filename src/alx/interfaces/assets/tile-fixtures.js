// Static fixtures for the AL/X status tile, and the page that shows them.
//
// UI shell only: nothing here reads Particle, the BHL APIs or a live device.
// Both BHL states are plain data for the one renderer in tile.js; `?state=`
// picks one, and there is no on-screen control. The visual is a placeholder
// cropped from the design reference in docs/Tile example.

import { surfaceTile } from '/tile.js';

const BHL = {
  visual: { src: '/tile-bhl-venue.jpg', alt: '' },
  icon: 'device',
  title: 'BHL Event Hardware',
  subtitle: 'Hardware operations',
  place: 'Cape Town, South Africa',
};

const PSUS = { icon: 'plug', label: 'PSUs', tone: 'disabled', note: 'In development' };

const FIXTURES = {
  healthy: {
    ...BHL,
    tone: 'ok',
    activity: 'Monitoring',
    state: { title: 'All systems normal' },
    facts: [
      { icon: 'scanner', value: '10/10', label: 'Registration Scanners', tone: 'ok' },
      { icon: 'reader', value: '25/25', label: 'Room Readers', tone: 'ok' },
      PSUS,
    ],
  },
  issue: {
    ...BHL,
    tone: 'attention',
    activity: 'AL/X is attempting recovery',
    state: { title: '1 issue detected', detail: 'Room reader R07 offline' },
    facts: [
      { icon: 'scanner', value: '10/10', label: 'Registration Scanners', tone: 'ok' },
      { icon: 'reader', value: '24/25', label: 'Room Readers', tone: 'attention' },
      PSUS,
    ],
  },
};

const requested = new URLSearchParams(location.search).get('state');
surfaceTile(document.getElementById('tiles'), FIXTURES[Object.hasOwn(FIXTURES, requested) ? requested : 'healthy']);
