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

  // The sole label-position authority. All coordinates and measured ink boxes
  // are in SVG units. Finite obstacles cannot exhaust the expanding search.
  function placeLabels(labels, obstacles) {
    const occupied = [...obstacles], placed = [];
    const key = (l) => JSON.stringify([l.kind, l.text, l.anchor, l.part, l.end]);
    for (const input of [...labels].sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0)) {
      const label = { ...input, short: input.kind === 'dim' && input.length < input.width + 16 };
      const w = label.width + 6, h = label.height + 6;
      const [ax, ay] = label.anchor;
      let found = false;
      for (let ring = 0; !found; ring += 1) {
        const gap = 6 + ring * (Math.max(w, h) + 6);
        let candidates;
        if (label.kind === 'ref') {
          const [left, top, right, bottom] = label.part;
          candidates = [[ax, top - gap - h / 2], [ax, bottom + gap + h / 2],
            [left - gap - w / 2, ay], [right + gap + w / 2, ay]];
        } else if (label.kind === 'dim') {
          const [dx, dy] = label.direction, nx = -dy, ny = dx;
          const side = Math.abs(nx) * w / 2 + Math.abs(ny) * h / 2 + gap;
          const base = label.short ? label.end : label.anchor;
          const beyond = label.short ? Math.abs(dx) * w / 2 + Math.abs(dy) * h / 2 + gap : 0;
          candidates = [1, -1].map((sign) => [base[0] + dx * beyond + sign * nx * side,
            base[1] + dy * beyond + sign * ny * side]);
        } else candidates = [[ax, ay - ring * (h + 6)]];
        for (const [cx, cy] of candidates) {
          const box = { x: cx - w / 2, y: cy - h / 2, width: w, height: h };
          if (occupied.some((b) => box.x < b.x + b.width && b.x < box.x + w
            && box.y < b.y + b.height && b.y < box.y + h)) continue;
          const target = [Math.max(box.x, Math.min(ax, box.x + w)),
            Math.max(box.y, Math.min(ay, box.y + h))];
          placed.push({ ...label, box, x: box.x + 3, y: box.y + 3,
            leader: label.kind === 'ref' || label.short || ring > 0 ? [label.anchor, target] : null });
          occupied.push(box);
          found = true;
          break;
        }
      }
    }
    return placed;
  }
  // Node tests exercise the same pure function as the renderer.
  if (typeof module !== 'undefined') { module.exports = { placeLabels }; return; }

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
    const labels = [], obstacles = [], dimensionPoints = [];
    const measure = (text, kind, size, spacing, weight, colour, attrs) => {
      if (!text) return;
      const node = el('text', { fill: colour, 'font-size': Math.max(12, size),
        'font-family': kind === 'ref' ? '"Helvetica Neue",Arial,sans-serif' : '"Courier New",monospace',
        'font-weight': weight, 'letter-spacing': spacing + 'em', visibility: 'hidden' }, g);
      node.textContent = text;
      const measured = node.getBBox();
      labels.push({ text, kind, width: measured.width, height: measured.height, ...attrs,
        node, measured });
    };
    for (const [ref, box] of Object.entries(extent)) {
      rect(box, 0.22, { fill: 'none', stroke: large(box) ? 'rgba(127,233,255,.8)' : SILK, 'stroke-width': 0.8 });
      const [ax, ay] = P(box[0] - 0.22, box[1] - 0.22);
      const [bx, by] = P(box[2] + 0.22, box[3] + 0.22);
      measure(ref, 'ref', 12, 0.03, 400, SILK, {
        anchor: [(ax + bx) / 2, (ay + by) / 2],
        part: [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)] });
    }
    for (const dim of item.dims || []) {
      const colour = tone(dim.tone, TONE.info);
      const a = P(...dim.from), b = P(...dim.to);
      const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const direction = length ? [(b[0] - a[0]) / length, (b[1] - a[1]) / length] : [1, 0];
      el('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: colour }, g);
      for (const [point, sign] of [[a, 1], [b, -1]]) {
        const [x, y] = point, [dx, dy] = direction;
        const base = [x + sign * dx * 4, y + sign * dy * 4];
        const vertices = [point, [base[0] - dy * 2, base[1] + dx * 2],
          [base[0] + dy * 2, base[1] - dx * 2]];
        el('polygon', { points: vertices.map((p) => p.join(',')).join(' '), fill: colour }, g);
        obstacles.push({ x: x - 5, y: y - 5, width: 10, height: 10 });
        dimensionPoints.push(point);
      }
      measure(dim.text, 'dim', 12, 0.08, 600, colour, {
        anchor: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], end: b, direction, length });
    }
    measure((item.label || '').toUpperCase(), 'title', 13, 0.16, 600,
      tone(item.tone, TONE.info), { anchor: [width / 2, -36] });
    measure(item.sublabel || '', 'subtitle', 12, 0.06, 400, '#c4e2ea', { anchor: [width / 2, -16] });
    const layout = placeLabels(labels.map(({ node, measured, ...label }) => label), obstacles);
    // Consume matches in stable order, including coincident identical labels.
    const unused = [...labels];
    for (const label of layout) {
      const index = unused.findIndex((entry) => entry.kind === label.kind && entry.text === label.text
        && entry.anchor[0] === label.anchor[0] && entry.anchor[1] === label.anchor[1]);
      const { node, measured } = unused.splice(index, 1)[0];
      if (label.leader) {
        const [a, b] = label.leader;
        el('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1],
          stroke: node.getAttribute('fill'), 'stroke-width': 0.6 }, g);
      }
      node.setAttribute('x', label.x - measured.x);
      node.setAttribute('y', label.y - measured.y);
      node.removeAttribute('visibility');
      g.appendChild(node);
    }
    const left = Math.min(0, ...layout.map((l) => l.box.x), ...dimensionPoints.map((p) => p[0] - 5)) - 10;
    const top = Math.min(0, ...layout.map((l) => l.box.y), ...dimensionPoints.map((p) => p[1] - 5)) - 10;
    const right = Math.max(width, ...layout.map((l) => l.box.x + l.box.width), ...dimensionPoints.map((p) => p[0] + 5)) + 10;
    const bottom = Math.max(height, ...layout.map((l) => l.box.y + l.box.height), ...dimensionPoints.map((p) => p[1] + 5)) + 10;
    g.setAttribute('transform', `translate(${offsetX - left},${-top})`);
    return [right - left, bottom - top];
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
    if (x) svg.setAttribute('viewBox', `0 0 ${x - 120} ${tallest}`);
  }
  load().catch(() => close());
})();
