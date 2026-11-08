// D-073: draws what AL/X shows. It decides nothing: every colour comes from her
// marks, every shape from the board file she named.
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const TONE = { ok: '#3dffa8', bad: '#ff4d6d', info: '#7fe9ff' };
  const COPPER = '#2fa8ff';
  const SILK = 'rgba(236,244,248,.92)';
  const S = 10; // px per mm in the view's own units; the SVG scales to fit
  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    parent.appendChild(node);
    return node;
  };
  const tone = (value, fallback) => TONE[value] || fallback;

  function close() { window.parent.postMessage({ type: 'show-close' }, '*'); }
  $('close').addEventListener('click', close);
  window.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  // The wave page asks to close on its own Esc too.
  window.addEventListener('message', (event) => { if (event.data?.type === 'bhl-field-close') close(); });

  function column(display) {
    $('context').textContent = display.context || '';
    $('title').textContent = display.title || '';
    $('subtitle').textContent = display.subtitle || '';
    const verdict = $('verdict');
    verdict.textContent = display.verdict?.text || '';
    verdict.className = 'verdict ' + (display.verdict?.tone || 'info');
    const items = $('items');
    items.replaceChildren();
    for (const item of display.items || []) {
      const tag = document.createElement('div');
      tag.className = 'tag ' + (item.tone || 'info');
      tag.textContent = item.title;
      if (item.detail) {
        const small = document.createElement('small');
        small.textContent = item.detail;
        tag.append(small);
      }
      items.append(tag);
    }
    $('note').textContent = display.note || '';
    $('said').textContent = display.said || '';
    $('saidBox').hidden = !display.said;
  }

  function view(svg, item, offsetX) {
    const g = el('g', { transform: `translate(${offsetX},0)` }, svg);
    const geometry = item.geometry;
    const [x0, y0, x1, y1] = geometry.window;
    const marks = item.marks || {};
    // Rotated a quarter turn anticlockwise: the board's y runs left to right
    // and its x bottom to top. One axis is reversed, so the board is turned,
    // never mirrored.
    const P = item.rotate ? (x, y) => [(y - y0) * S, (x1 - x) * S] : (x, y) => [(x - x0) * S, (y - y0) * S];
    const width = (item.rotate ? y1 - y0 : x1 - x0) * S;
    const height = (item.rotate ? x1 - x0 : y1 - y0) * S;
    const path = (polygons) => polygons.map((poly) => [poly.outer, ...(poly.holes || [])]
      .map((ring) => 'M' + ring.map((p) => P(...p).join(',')).join('L') + 'Z').join('')).join('');
    const clip = 'clip' + offsetX;
    el('rect', { x: 0, y: 0, width, height }, el('clipPath', { id: clip }, g));
    const c = el('g', { 'clip-path': `url(#${clip})` }, g);

    el('rect', { x: 0, y: 0, width, height, fill: 'rgba(6,34,58,.6)' }, c);
    for (const plane of geometry.planes || []) {
      el('path', { d: path(plane.polygons), fill: tone(marks['net:' + plane.net], COPPER),
        'fill-opacity': 0.12, 'fill-rule': 'evenodd' }, c);
    }
    for (const keepout of geometry.keepouts || []) {
      el('path', { d: path(keepout.polygons), fill: 'none', 'stroke-dasharray': '6 4', 'stroke-width': 1.2,
        stroke: tone(marks.keepout, 'rgba(127,233,255,.45)') }, c);
    }
    for (const edge of geometry.edges || []) {
      el('polyline', { points: edge.map((p) => P(...p).join(',')).join(' '), fill: 'none', stroke: TONE.info, 'stroke-width': 1.5 }, c);
    }
    for (const track of geometry.tracks || []) {
      el('polyline', { points: track.points.map((p) => P(...p).join(',')).join(' '), fill: 'none',
        stroke: tone(marks['net:' + track.net], COPPER), 'stroke-width': Math.max(1, track.width * S),
        'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: track.layer === 'F' ? 1 : 0.45 }, c);
    }
    for (const via of geometry.vias || []) {
      const [vx, vy] = P(...via.at);
      el('circle', { cx: vx, cy: vy, r: Math.max(1.5, via.diameter / 2 * S), fill: '#020c16',
        stroke: tone(marks['net:' + via.net], COPPER), 'stroke-width': 1 }, c);
    }
    // Parts over the copper: a dark body, coloured pads, a silkscreen outline.
    const extent = {};
    for (const pad of geometry.pads || []) {
      const box = extent[pad.ref] ??= [Infinity, Infinity, -Infinity, -Infinity];
      for (const poly of pad.polygons) for (const [x, y] of poly.outer) {
        box[0] = Math.min(box[0], x); box[1] = Math.min(box[1], y);
        box[2] = Math.max(box[2], x); box[3] = Math.max(box[3], y);
      }
    }
    const rect = (box, margin, attrs) => {
      const [ax, ay] = P(box[0] - margin, box[1] - margin);
      const [bx, by] = P(box[2] + margin, box[3] + margin);
      return el('rect', { x: Math.min(ax, bx), y: Math.min(ay, by), width: Math.abs(bx - ax), height: Math.abs(by - ay), ...attrs }, c);
    };
    // A small part gets a solid body so it stands out from the copper; a
    // large one (an antenna, a module) is see-through so its copper shows.
    const large = (box) => Math.max(box[2] - box[0], box[3] - box[1]) > 5;
    for (const box of Object.values(extent)) {
      rect(box, 0.22, large(box) ? { fill: 'rgba(127,233,255,.06)' } : { fill: '#020c16' });
    }
    for (const pad of geometry.pads || []) {
      el('path', { d: path(pad.polygons), fill: tone(marks[pad.ref] ?? marks['net:' + pad.net], COPPER) }, c);
    }
    for (const [ref, box] of Object.entries(extent)) {
      rect(box, 0.22, { fill: 'none', stroke: large(box) ? 'rgba(127,233,255,.8)' : SILK, 'stroke-width': 0.8 });
      const [lx, ly] = P((box[0] + box[2]) / 2, (box[1] + box[3]) / 2);
      const [, top] = P(box[0], box[1]);
      const [, bottom] = P(box[2], box[3]);
      const y = Math.min(top, bottom) - 6;
      const label = el('text', { x: lx, y, fill: SILK, 'text-anchor': 'middle',
        style: 'font:10px "Helvetica Neue",Arial,sans-serif;letter-spacing:.03em' }, c);
      label.textContent = ref;
      if (large(box)) label.setAttribute('y', ly + 4); // a large part is named inside
    }
    for (const dim of item.dims || []) {
      const colour = tone(dim.tone, TONE.info);
      const [ax, ay] = P(...dim.from);
      const [bx, by] = P(...dim.to);
      el('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: colour }, g);
      const label = el('text', { x: (ax + bx) / 2, y: (ay + by) / 2 - 8, fill: colour, 'text-anchor': 'middle',
        'font-size': 12, 'font-weight': 600, 'letter-spacing': '.08em' }, g);
      label.textContent = dim.text;
    }
    const title = el('text', { x: 0, y: -28, fill: tone(item.tone, TONE.info), 'font-size': 13, 'font-weight': 600, 'letter-spacing': '.16em' }, g);
    title.textContent = (item.label || '').toUpperCase();
    const sub = el('text', { x: 0, y: -12, fill: '#c4e2ea', 'font-size': 11, 'letter-spacing': '.06em' }, g);
    sub.textContent = item.sublabel || '';
    return [width, height];
  }

  async function load() {
    const response = await fetch('/display.json', { cache: 'no-store' });
    const { display } = await response.json();
    if (!display) { close(); return; }
    column(display);
    const svg = $('view');
    svg.replaceChildren();
    let x = 0, tallest = 0;
    for (const item of display.views || []) {
      const [width, height] = view(svg, item, x);
      x += width + 120;
      tallest = Math.max(tallest, height);
    }
    if (x) svg.setAttribute('viewBox', `-20 -50 ${x - 80} ${tallest + 80}`);
  }
  load().catch(() => close());
})();
