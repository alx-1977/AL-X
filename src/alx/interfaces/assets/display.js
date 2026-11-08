// D-073: when AL/X shows something, it opens over her screen the way the
// reader field does: an overlay, the scene softened behind it. She publishes;
// this page notices a new version and opens /show. Esc or Close shuts it.
(() => {
  let seen = null;
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
  async function poll() {
    try {
      const response = await fetch('/display.json', { cache: 'no-store' });
      if (!response.ok) return;
      const { version, display } = await response.json();
      // On load, what was shown before is not reopened.
      if (seen === null) { seen = version; return; }
      if (version > seen && display) { seen = version; open(); }
    } catch { /* the next poll tries again */ }
  }
  window.addEventListener('message', (event) => {
    if (event.source === frame?.contentWindow && event.data?.type === 'show-close') close();
  });
  poll();
  setInterval(poll, 2000);
})();
