// D-073: when AL/X shows something, it opens over her screen the way the
// reader field does: an overlay, the scene softened behind it. She publishes;
// this page notices a new version and opens /show. Esc or Close shuts it.
(() => {
  // What was last seen: a restart gives the board a new id; within one id
  // only a higher version is new. One poll at a time, so an older answer
  // arriving late cannot reopen a viewer Friedl has closed.
  let seenId = null;
  let seenVersion = 0;
  let polling = false;
  let frame = null;
  let shade = null;
  const close = () => {
    frame?.remove();
    shade?.remove();
    frame = null;
    shade = null;
  };
  const open = () => {
    close();
    shade = document.createElement('div');
    shade.className = 'rt-field-shade';
    frame = document.createElement('iframe');
    frame.className = 'rt-field';
    frame.src = '/show';
    frame.setAttribute('aria-label', 'AL/X is showing');
    document.body.append(shade, frame);
    frame.addEventListener('load', () => frame?.contentWindow?.focus());
  };
  // A stalled request is abandoned after this long, body included, so the
  // next tick can try again rather than waiting on it for ever.
  const POLL_TIMEOUT_MS = 8000;
  async function poll() {
    if (polling) return;
    polling = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
    try {
      const response = await fetch('/display.json', { cache: 'no-store', signal: controller.signal });
      if (!response.ok) return;
      const { id, version, display } = await response.json();
      // On load, what was shown before is not reopened.
      if (seenId === null) { seenId = id; seenVersion = version; return; }
      const fresh = id !== seenId || version > seenVersion;
      if (!fresh) return;
      seenId = id;
      seenVersion = version;
      if (display) open();
    } catch { /* the next poll tries again */ } finally {
      clearTimeout(timer);
      polling = false;
    }
  }
  window.addEventListener('message', (event) => {
    if (event.source === frame?.contentWindow && event.data?.type === 'show-close') close();
  });
  poll();
  setInterval(poll, 2000);
})();
