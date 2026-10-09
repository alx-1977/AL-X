// The BHL tile on the main page (D-041). The server says whether today has
// BHL events and what is wrong with which reader; this only puts that on
// screen, keeps it current, and removes it when the server returns none.

import { surfaceReaderTraces } from '/reader-traces.js';

const host = document.body;
// Every few seconds: AL/X learns of a reader within seconds (its own messages,
// pings, status cards), and this only asks her, on this machine.
const EVERY_MS = 5_000;
let tile = null;
let shown = '';

async function check() {
  let data = null;
  try {
    const response = await fetch('/reader-tile.json', { cache: 'no-store' });
    if (!response.ok) return;
    data = (await response.json()).tile;
  } catch {
    return; // The server is restarting; keep what is on screen and try again.
  }
  if (!data) {
    if (tile) tile.dismiss();
    tile = null;
    shown = '';
    return;
  }
  const next = JSON.stringify(data);
  if (next === shown) return;
  if (tile) tile.update(data);
  else tile = surfaceReaderTraces(host, data);
  shown = next;
}

check();
setInterval(check, EVERY_MS);
