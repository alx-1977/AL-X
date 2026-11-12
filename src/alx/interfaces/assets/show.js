// D-073: draws what AL/X shows. It decides nothing: every colour comes from her
// marks, every shape from the board file she named, and a datasheet section is
// the page as printed, never redrawn.
//
// The drawing rules (Friedl, 11 Nov 2026): each board fills its half of the
// screen; text is screen-sized; a part's name sits right beside the part, or
// inside it when the part is large, and never at the end of a leader line; a
// dimension is the line she named with end ticks and level text beside it.
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const TONE = { ok: '#3dffa8', bad: '#ff4d6d', info: '#7fe9ff' };
  const COPPER = '#2fa8ff';
  const SILK = 'rgba(236,244,248,.92)';
  const HEAD = 64;   // room above a view for its label and sub line
  const SIDE = 70;   // room either side of a view for names and dimensions
  const GAP = 90;    // between two views
  const CLEAR = 3;   // text keeps this far from copper, parts and other text
  const LABEL_ROOM = 190;  // beside a datasheet section, for her box labels
  const tone = (value, fallback) => TONE[value] || fallback;
  const overlap = (a, b, gap = 0) => a.x - gap < b.x + b.w && b.x - gap < a.x + a.w
    && a.y - gap < b.y + b.h && b.y - gap < a.y + a.h;

  // Where a part's name goes: inside a large part, otherwise beside it - right,
  // left, above, below, then the corners - and failing those the nearest free
  // place close by. Never a leader line: a name that needs one is a name the
  // eye has to hunt for.
  function nameSpot(body, w, h, free) {
    const cx = body.x + body.w / 2 - w / 2, cy = body.y + body.h / 2 - h / 2, g = 4;
    const tries = body.large ? [[cx, cy], [cx, cy + h + 8], [cx, cy - h - 8]]
      : [[body.x + body.w + g, cy], [body.x - g - w, cy], [cx, body.y - g - h], [cx, body.y + body.h + g],
         [body.x + body.w + g, body.y - h], [body.x - g - w, body.y - h],
         [body.x + body.w + g, body.y + body.h], [body.x - g - w, body.y + body.h]];
    for (let r = 4; r <= 80; r += 3) {
      for (let k = 0; k < 16; k += 1) {
        const a = k / 16 * 2 * Math.PI;
        tries.push([body.x + body.w / 2 + Math.cos(a) * (body.w / 2 + w / 2 + r) - w / 2,
          body.y + body.h / 2 + Math.sin(a) * (body.h / 2 + h / 2 + r) - h / 2]);
      }
    }
    return settle(tries, w, h, free);
  }

  // The first free place in order; when none is free, the place that covers
  // the least - chosen on purpose, not by falling back to the first try.
  function settle(tries, w, h, free) {
    const spot = tries.find(([x, y]) => free({ x, y, w, h }));
    if (spot) return spot;
    let best = null, least = Infinity;
    for (const [x, y] of tries) {
      const covered = free({ x, y, w, h }, true);
      if (covered < least) { least = covered; best = [x, y]; }
    }
    return best;   // null only when every place lies outside the view
  }

  // Where a dimension's text goes: level, beside the middle of its line.
  function dimensionSpot(a, b, w, h, free) {
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    const level = Math.abs(b[0] - a[0]) >= Math.abs(b[1] - a[1]);
    const tries = [];
    // Beside the middle first, then stepping further out, never turned.
    for (let step = 0; step <= 60; step += 6) {
      tries.push(...(level
        ? [[mx - w / 2, my - 9 - h - step], [mx - w / 2, my + 9 + step],
           [Math.max(a[0], b[0]) + 14 + step, my - h / 2], [Math.min(a[0], b[0]) - 14 - w - step, my - h / 2]]
        : [[mx + 10 + step, my - h / 2], [mx - 10 - w - step, my - h / 2],
           [mx - w / 2, Math.min(a[1], b[1]) - 14 - h - step], [mx - w / 2, Math.max(a[1], b[1]) + 14 + step]]));
    }
    return settle(tries, w, h, free);
  }

  // One scale for every view, so the same millimetre is the same size. On a
  // narrow screen the spacing gives way before the boards do.
  function fitScale(width, height, sizes) {
    const spacing = Math.min(sizes.length * 2 * SIDE + (sizes.length - 1) * GAP, width * 0.4);
    const widest = sizes.reduce((sum, [w]) => sum + w, 0);
    const tallest = Math.max(...sizes.map(([, h]) => h));
    return Math.max(0.01, Math.min((width - spacing) / widest, (Math.max(height - HEAD - 8, height * 0.5)) / tallest));
  }

  // The datasheet's width: as tall as the page allows, but never wider than
  // the room left beside its box labels.
  function fitReference(width, height, pages, gap) {
    const shape = pages.reduce((s, p) => s + p.h / p.w, 0);
    const byHeight = (height - gap * (pages.length - 1)) / shape;
    return Math.max(1, Math.min(byHeight, width - 2 * LABEL_ROOM));
  }

  // Node tests exercise the same pure functions as the renderer.
  if (typeof module !== 'undefined') { module.exports = { nameSpot, dimensionSpot, overlap, CLEAR, fitScale, fitReference }; return; }

  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    parent.appendChild(node);
    return node;
  };
  // Screen text, with a dark halo so it reads over copper and the wave.
  const write = (parent, str, attrs, family = '"Courier New",monospace') => {
    const node = el('text', { 'font-size': 14, ...attrs,
      style: `font-family:${family};paint-order:stroke;stroke:#020c16;stroke-width:3px;stroke-linejoin:round` }, parent);
    node.textContent = str;
    return node;
  };

  function close() { window.parent.postMessage({ type: 'show-close' }, '*'); }
  $('close').addEventListener('click', close);
  window.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('refpage').hidden) $('refpage').hidden = true;
    else close();
  });
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

  // A view's size in mm, on screen axes.
  const extent = (item) => {
    const [x0, y0, x1, y1] = item.geometry.window;
    return item.rotate ? [y1 - y0, x1 - x0] : [x1 - x0, y1 - y0];
  };

  function view(svg, item, ox, oy, S) {
    const g = el('g', { transform: `translate(${ox},${oy})` }, svg);
    const geo = item.geometry, marks = item.marks || {};
    const [x0, y0, x1, y1] = geo.window;
    // Rotated a quarter turn anticlockwise: the board's y runs left to right
    // and its x bottom to top. One axis is reversed, so the board is turned,
    // never mirrored.
    const P = item.rotate ? (x, y) => [(y - y0) * S, (x1 - x) * S] : (x, y) => [(x - x0) * S, (y - y0) * S];
    const [wmm, hmm] = extent(item), W = wmm * S, H = hmm * S;
    const path = (polygons) => polygons.map((poly) => [poly.outer, ...(poly.holes || [])]
      .map((ring) => 'M' + ring.map((p) => P(...p).join(',')).join('L') + 'Z').join('')).join('');
    const bounds = (points) => {
      const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
      return { x: Math.min(...xs), y: Math.min(...ys),
        w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
    };
    const clip = 'clip' + ox;
    el('rect', { x: 0, y: 0, width: W, height: H }, el('clipPath', { id: clip }, g));
    const c = el('g', { 'clip-path': `url(#${clip})` }, g);

    // The board's own area; nothing past its edge is board.
    const area = (geo.board || []).length ? path(geo.board) : `M0,0H${W}V${H}H0Z`;
    el('path', { d: area, fill: 'rgba(8,30,52,.72)', 'fill-rule': 'evenodd' }, c);
    for (const plane of geo.planes || []) {
      el('path', { d: path(plane.polygons), 'fill-rule': 'evenodd',
        fill: tone(marks['net:' + plane.net], COPPER), 'fill-opacity': 0.10 }, c);
    }
    for (const keepout of geo.keepouts || []) {
      el('path', { d: path(keepout.polygons), fill: 'none', 'stroke-dasharray': '7 5', 'stroke-width': 1.3,
        stroke: tone(marks.keepout, 'rgba(127,233,255,.5)') }, c);
    }
    for (const edge of geo.edges || []) {
      el('polyline', { points: edge.map((p) => P(...p).join(',')).join(' '), fill: 'none',
        stroke: TONE.info, 'stroke-width': 1.6 }, c);
    }
    const taken = [];   // screen boxes text keeps clear of
    for (const track of geo.tracks || []) {
      const width = Math.max(1.2, track.width * S);
      el('polyline', { points: track.points.map((p) => P(...p).join(',')).join(' '), fill: 'none',
        stroke: tone(marks['net:' + track.net], COPPER), 'stroke-width': width,
        'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: track.layer === 'F' ? 1 : 0.4 }, c);
      for (let i = 1; i < track.points.length; i += 1) {
        const b = bounds([P(...track.points[i - 1]), P(...track.points[i])]);
        taken.push({ x: b.x - width / 2, y: b.y - width / 2, w: b.w + width, h: b.h + width });
      }
    }
    for (const shape of geo.shapes || []) {
      el('path', { d: path(shape.polygons), fill: tone(marks['net:' + shape.net], COPPER),
        opacity: shape.layer === 'F' ? 1 : 0.4 }, c);
      taken.push(bounds(shape.polygons.flatMap((poly) => poly.outer.map((p) => P(...p)))));
    }
    for (const via of geo.vias || []) {
      const [vx, vy] = P(...via.at);
      el('circle', { cx: vx, cy: vy, r: Math.max(2, via.diameter / 2 * S), fill: '#020c16',
        stroke: tone(marks['net:' + via.net], COPPER), 'stroke-width': 1.2 }, c);
    }

    // Parts over the copper: a dark body (see-through when large, an antenna
    // or a module), coloured pads, a silkscreen outline.
    const corners = {};
    for (const pad of geo.pads || []) {
      (corners[pad.ref] ??= []).push(...pad.polygons.flatMap((poly) => poly.outer.map((p) => P(...p))));
    }
    const bodies = {}, named = {};
    for (const [ref, points] of Object.entries(corners)) {
      const b = bounds(points), margin = 0.2 * S;
      const body = { x: b.x - margin, y: b.y - margin, w: b.w + 2 * margin, h: b.h + 2 * margin };
      body.large = Math.max(body.w, body.h) > 5 * S;
      bodies[ref] = body;
      el('rect', { x: body.x, y: body.y, width: body.w, height: body.h,
        fill: body.large ? 'rgba(127,233,255,.05)' : '#020c16' }, c);
      // A name belongs to what is on screen: the visible part of a part that
      // crosses the window edge, and no name at all for a part outside it.
      const left = Math.max(0, body.x), top = Math.max(0, body.y);
      const right = Math.min(W, body.x + body.w), bottom = Math.min(H, body.y + body.h);
      if (right > left && bottom > top) {
        named[ref] = { x: left, y: top, w: right - left, h: bottom - top, large: body.large };
      }
    }
    for (const pad of geo.pads || []) {
      el('path', { d: path(pad.polygons), fill: tone(marks[pad.ref] ?? marks['net:' + pad.net], COPPER) }, c);
      taken.push(bounds(pad.polygons.flatMap((poly) => poly.outer.map((p) => P(...p)))));
    }
    for (const body of Object.values(bodies)) {
      el('rect', { x: body.x, y: body.y, width: body.w, height: body.h, fill: 'none', 'stroke-width': 1,
        stroke: body.large ? 'rgba(127,233,255,.75)' : SILK }, c);
      // A large part's name goes inside it, so only small bodies are obstacles.
      if (!body.large) taken.push(body);
    }

    // Whether a place is free; asked for a cost, how much it would cover.
    const free = (b, cost = false) => {
      const inside = b.x >= -SIDE + 4 && b.x + b.w <= W + SIDE - 4 && b.y >= 0 && b.y + b.h <= H;
      if (!cost) return inside && !taken.some((o) => overlap(b, o, CLEAR));
      const covered = taken.reduce((sum, o) => sum + Math.max(0, Math.min(b.x + b.w, o.x + o.w) - Math.max(b.x, o.x))
        * Math.max(0, Math.min(b.y + b.h, o.y + o.h) - Math.max(b.y, o.y)), 0);
      return inside ? covered : Infinity;
    };
    const place = (node, spot) => {
      if (!spot) { node.remove(); return; }
      const [x, y] = spot;
      const box = node.getBBox();
      node.setAttribute('x', x);
      node.setAttribute('y', y - box.y);
      taken.push({ x, y, w: box.width, h: box.height });
    };

    for (const dim of item.dims || []) {
      const colour = tone(dim.tone, TONE.info);
      const a = P(...dim.from), b = P(...dim.to);
      const length = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      const ux = (b[0] - a[0]) / length, uy = (b[1] - a[1]) / length, nx = -uy, ny = ux;
      const line = (p, q) => el('line', { x1: p[0], y1: p[1], x2: q[0], y2: q[1], stroke: colour, 'stroke-width': 1.2 }, g);
      line(a, b);
      for (const p of [a, b]) line([p[0] - nx * 6, p[1] - ny * 6], [p[0] + nx * 6, p[1] + ny * 6]);
      // A span too short to read gets short stubs beyond its ends.
      if (length < 18) {
        line(a, [a[0] - ux * 10, a[1] - uy * 10]);
        line(b, [b[0] + ux * 10, b[1] + uy * 10]);
      }
      taken.push({ x: Math.min(a[0], b[0]) - 6, y: Math.min(a[1], b[1]) - 6,
        w: Math.abs(b[0] - a[0]) + 12, h: Math.abs(b[1] - a[1]) + 12 });
      const text = write(g, dim.text, { fill: colour, 'font-weight': 600, 'letter-spacing': '.08em' });
      const size = text.getBBox();
      place(text, dimensionSpot(a, b, size.width, size.height, free));
    }
    for (const [ref, body] of Object.entries(named)) {
      const text = write(g, ref, { fill: marks[ref] === 'bad' ? TONE.bad : SILK, 'letter-spacing': '.03em' },
        '"Helvetica Neue",Arial,sans-serif');
      const size = text.getBBox();
      place(text, nameSpot(body, size.width, size.height, free));
    }
    write(g, (item.label || '').toUpperCase(), { x: 0, y: -HEAD + 20, fill: tone(item.tone, TONE.info),
      'font-size': 17, 'font-weight': 700, 'letter-spacing': '.16em' });
    if (item.sublabel) write(g, item.sublabel, { x: 0, y: -HEAD + 46, fill: '#c4e2ea', 'letter-spacing': '.06em' });
    return W;
  }

  // The datasheet section as printed: black lines on white become her cyan
  // line art (the filter, not a redraw), her boxes on top.
  function reference(svg, ref, assetUrl, H) {
    const box = svg.getBoundingClientRect();
    const g = el('g', {}, svg);
    const gap = 18;
    const W = fitReference(box.width, H, ref.pages, gap);
    g.setAttribute('transform', `translate(${Math.max(LABEL_ROOM, (box.width - W) / 2)},${HEAD})`);
    // The filter covers the image only: past its edge (transparent black) it
    // would turn every empty pixel solid cyan.
    const filter = el('filter', { id: 'lineart', 'color-interpolation-filters': 'sRGB',
      x: 0, y: 0, width: 1, height: 1 }, el('defs', {}, svg));
    el('feColorMatrix', { type: 'matrix', values:
      '0 0 0 0 0.498  0 0 0 0 0.914  0 0 0 0 1  -0.2126 -0.7152 -0.0722 0 1' }, filter);
    write(g, (ref.label || '').toUpperCase(), { x: 0, y: -HEAD + 20, fill: TONE.info,
      'font-size': 17, 'font-weight': 700, 'letter-spacing': '.16em' });
    if (ref.sublabel) write(g, ref.sublabel, { x: 0, y: -HEAD + 46, fill: '#c4e2ea', 'letter-spacing': '.06em' });
    let y = 0;
    for (const page of ref.pages) {
      const h = W * page.h / page.w;
      el('image', { href: assetUrl(page.asset), x: 0, y, width: W, height: h, filter: 'url(#lineart)',
        preserveAspectRatio: 'none' }, g);
      for (const mark of page.boxes || []) {
        const colour = tone(mark.tone, TONE.bad);
        const bx = mark.x * W, by = y + mark.y * h, bw = mark.w * W, bh = mark.h * h;
        el('rect', { x: bx, y: by, width: bw, height: bh, rx: 3, fill: colour, 'fill-opacity': 0.10,
          stroke: colour, 'stroke-width': 1.6 }, g);
        const below = mark.side === 'below', left = mark.side === 'left';
        write(g, (mark.label || '').toUpperCase(), { fill: colour, 'font-size': 12, 'font-weight': 600,
          'letter-spacing': '.08em', 'text-anchor': left ? 'end' : 'start',
          x: left ? bx - 6 : below ? bx : bx + bw + 6, y: below ? by + bh + 15 : by + bh / 2 + 4 });
      }
      if (page.caption) write(g, page.caption, { x: 0, y: y + h + 14, fill: '#8fc4d4', 'font-size': 11 });
      y += h + gap;
    }
  }

  async function load() {
    const response = await fetch('/display.json', { cache: 'no-store' });
    const { id, version, display } = await response.json();
    if (!display) { close(); return; }
    column(display);
    const svg = $('view');
    svg.replaceChildren();
    const area = svg.getBoundingClientRect();
    svg.setAttribute('viewBox', `0 0 ${area.width} ${area.height}`);
    const views = display.views || [];
    if (views.length) {
      const S = fitScale(area.width, area.height, views.map(extent));
      const spacing = Math.min(views.length * 2 * SIDE + (views.length - 1) * GAP, area.width * 0.4);
      const unit = spacing / (views.length * 2 * SIDE + (views.length - 1) * GAP);
      let x = SIDE * unit;
      for (const item of views) x += view(svg, item, x, HEAD, S) + (2 * SIDE + GAP) * unit;
    }
    const link = $('reflink');
    link.hidden = !display.reference;
    if (display.reference) {
      $('reflabel').textContent = display.reference.label + ' ↗';
      const assetUrl = (n) => `/display-asset.png?id=${encodeURIComponent(id)}&version=${version}&n=${n}`;
      link.onclick = () => {
        $('refpage').hidden = false;
        const page = $('refview');
        page.replaceChildren();
        const box = page.getBoundingClientRect();
        page.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
        reference(page, display.reference, assetUrl, box.height - HEAD - 8);
      };
      $('refback').onclick = () => { $('refpage').hidden = true; };
    }
    // Only now is it on screen; she may say so only after this report.
    await fetch(`/display-drawn.json?id=${encodeURIComponent(id)}&version=${version}`, { cache: 'no-store' });
  }
  load().catch(() => close());
})();
