// Runs display.js against a scripted /display.json and prints what happened.
// Used by tests/test_show_on_screen.py (D-073); node only, no browser.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const script = readFileSync(process.argv[2], 'utf8');
const answers = JSON.parse(process.argv[3]);
const frames = [];
let messageHandler = null;
let tick = null;
let requests = 0;

const element = (tag) => {
  const node = { tag, className: '', removed: false, contentWindow: {}, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener() {}, remove() { this.removed = true; } };
  if (tag === 'iframe') frames.push(node);
  return node;
};
function fetch(url, options) {
  const answer = answers[requests++];
  if (answer === 'stall') {
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  }
  if (answer === 'error') return Promise.reject(new Error('network'));
  return Promise.resolve({ ok: true, json: () => Promise.resolve(answer) });
}
const context = {
  document: { createElement: element, body: { append() {} } },
  window: { addEventListener(type, handler) { if (type === 'message') messageHandler = handler; } },
  fetch, AbortController,
  setInterval(fn) { tick = fn; },
  setTimeout: (fn) => setTimeout(fn, 5), clearTimeout,
  console,
};
vm.runInNewContext(script, context);
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
const log = [];
await settle();
for (let step = 1; step < answers.length; step += 1) {
  if (step === 2) {  // Friedl closes the viewer that opened at step 1
    const open = frames.filter((frame) => !frame.removed).at(-1);
    messageHandler({ source: open.contentWindow, data: { type: 'show-close' } });
  }
  tick();
  await settle();
  log.push(frames.filter((frame) => !frame.removed).length);
}
console.log(JSON.stringify({ open_after_each_step: log, opened: frames.length, requests }));
