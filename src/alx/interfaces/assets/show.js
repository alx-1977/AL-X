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

  const CLEARANCE = 4; // SVG units, measured from ink / stroke edges
  const expand = (b, gap) => ({ x: b.x - gap, y: b.y - gap,
    width: b.width + 2 * gap, height: b.height + 2 * gap });
  const overlap = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width
    && a.y < b.y + b.height && b.y < a.y + a.height;
  function intersects([a, b], box) {
    let low = 0, high = 1;
    for (const [axis, start, size] of [[0, box.x, box.width], [1, box.y, box.height]]) {
      const delta = b[axis] - a[axis];
      if (Math.abs(delta) < 1e-10) {
        if (a[axis] < start || a[axis] > start + size) return false;
      } else {
        const ends = [(start - a[axis]) / delta, (start + size - a[axis]) / delta];
        low = Math.max(low, Math.min(...ends));
        high = Math.min(high, Math.max(...ends));
        if (low > high) return false;
      }
    }
    return true;
  }
  const segments = (points) => points.slice(1).map((p, i) => [points[i], p]);
  const crosses = (points, box) => segments(points).some(s => intersects(s, box));

  // One router per reference, reused for every overflow destination. Horizontal
  // free-space bands form a sparse, complete rectilinear graph: at most
  // (2N + 1)(N + 1) cells and four overlaps per cell on average. Construction
  // and traversal are O(N²), rather than an all-pairs corner visibility search
  // against N obstacles for each destination. There is no search cutoff that
  // can turn an otherwise reachable overflow column into layout exhaustion.
  function leaderRouter(start, boxes) {
    const contains = (p, b) => intersects([p, p], b);
    const origins = new Set(boxes.filter(b => contains(start, b)));
    const clear = (a, b, escaping = false) => !boxes.some(box =>
      !(escaping && origins.has(box)) && intersects([a, b], box));
    let graph = null;
    function build() {
      // The extra tenth keeps graph edges outside the closed collision boxes.
      const rects = boxes.map(b => {
        // Do not swallow an otherwise free origin in the numerical margin.
        const distance = Math.max(b.x - start[0], start[0] - b.x - b.width,
          b.y - start[1], start[1] - b.y - b.height, 0);
        return expand(b, distance ? Math.min(0.1, distance / 2) : 0.1);
      }).sort((a, b) => a.x - b.x);
      const cuts = [...new Set(rects.flatMap(b => [b.y, b.y + b.height]))].sort((a, b) => a - b);
      const levels = [-Infinity, ...cuts, Infinity], bands = [], queue = [];
      for (let row = 1; row < levels.length; row += 1) {
        const top = levels[row - 1], bottom = levels[row], cells = [];
        let left = -Infinity;
        const cell = (right) => {
          if (left < right) cells.push({ left, right, edges: [], reached: false });
        };
        for (const b of rects) {
          if (b.y >= bottom || b.y + b.height <= top) continue;
          cell(b.x);
          left = Math.max(left, b.x + b.width);
        }
        cell(Infinity);
        const previous = bands[bands.length - 1] || [];
        let i = 0, j = 0;
        while (i < previous.length && j < cells.length) {
          const a = previous[i], b = cells[j];
          const lo = Math.max(a.left, b.left), hi = Math.min(a.right, b.right);
          if (lo < hi) {
            const x = Number.isFinite(lo) && Number.isFinite(hi) ? (lo + hi) / 2
              : Number.isFinite(lo) ? Math.max(start[0], lo + 1)
                : Number.isFinite(hi) ? Math.min(start[0], hi - 1) : start[0];
            const crossing = [x, top];
            a.edges.push({ cell: b, crossing }); b.edges.push({ cell: a, crossing });
          }
          if (a.right <= b.right) i += 1;
          else j += 1;
        }
        bands.push(cells);
      }
      const locate = (p) => {
        let lo = 0, hi = cuts.length;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (cuts[mid] <= p[1]) lo = mid + 1;
          else hi = mid;
        }
        const cells = bands[lo];
        lo = 0; hi = cells.length;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (cells[mid].right <= p[0]) lo = mid + 1;
          else hi = mid;
        }
        return cells[lo]?.left < p[0] ? cells[lo] : null;
      };
      const seed = (p) => {
        const cell = locate(p);
        if (!cell || cell.reached || !clear(start, p, true)) return;
        cell.reached = true; cell.seed = p; queue.push(cell);
      };
      seed(start);
      if (origins.size) {
        // A visible obstacle corner is a possible first bend of any escaping
        // path. Only this initial straight segment may cross origin boxes;
        // every subsequent edge uses free space, including around those boxes.
        for (const b of rects) for (const x of [b.x - 0.1, b.x + b.width + 0.1]) {
          for (const y of [b.y - 0.1, b.y + b.height + 0.1]) seed([x, y]);
        }
      }
      for (let head = 0; head < queue.length; head += 1) {
        const cell = queue[head];
        for (const edge of cell.edges) if (!edge.cell.reached) {
          edge.cell.reached = true; edge.cell.previous = cell;
          edge.cell.crossing = edge.crossing; queue.push(edge.cell);
        }
      }
      return locate;
    }
    return (target) => {
      if ([...origins].some(box => contains(target, box))) return null;
      if (clear(start, target, true)) return [start, target];
      if (!graph) graph = build();
      let cell = graph(target);
      if (!cell?.reached) return null;
      const crossings = [];
      while (cell.previous) {
        crossings.push(cell.crossing); cell = cell.previous;
      }
      const points = [start];
      if (cell.seed !== start) points.push(cell.seed);
      for (const p of [...crossings.reverse(), target]) {
        const last = points[points.length - 1];
        if (last[0] !== p[0]) points.push([p[0], last[1]]);
        if (points[points.length - 1][1] !== p[1]) points.push(p);
      }
      return points;
    };
  }

  // The sole label-position authority, shared by both board views. Measured
  // ink bounds (including rotated dimensions) drive clearance and framing.
  function placeLabels(labels, obstacles) {
    const placed = [];
    const key = (l) => JSON.stringify([l.text, l.anchor, l.part, l.end]);
    const rank = { dim: 0, ref: 1, subtitle: 2, title: 3 };
    const ordered = [...labels].sort((a, b) => (rank[a.kind] - rank[b.kind])
      || (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
    const blocked = (box) => obstacles.some((o) => o.segment
      ? intersects(o.segment, expand(box, CLEARANCE + (o.stroke || 0) / 2))
      : overlap(expand(box, CLEARANCE), o))
      || placed.some((p) => overlap(expand(box, CLEARANCE), p.box)
        || (p.leader && crosses(p.leader, expand(box, CLEARANCE + 0.3))));
    const allBoxes = obstacles.filter(o => !o.segment).concat(labels.filter(l => l.part)
      .map(l => ({ x: l.part[0], y: l.part[1], width: l.part[2] - l.part[0], height: l.part[3] - l.part[1] })));
    const left = Math.min(0, ...allBoxes.map(b => b.x));
    const right = Math.max(0, ...allBoxes.map(b => b.x + b.width));
    const rowHeight = Math.max(12, ...labels.filter(l => l.kind === 'ref').map(l => l.height)) + 2 * CLEARANCE;
    // Normalize static pad/measurement geometry once, not at each destination.
    const routingObstacles = obstacles.filter(o => !o.copper && !o.body).map(o => {
      if (!o.segment) return { box: expand(o, 0.8), owner: o.owner };
      const [a, b] = o.segment;
      return { box: { x: Math.min(a[0], b[0]) - 1, y: Math.min(a[1], b[1]) - 1,
        width: Math.abs(b[0] - a[0]) + 2, height: Math.abs(b[1] - a[1]) + 2 } };
    });
    for (const label of ordered) {
      let router = null;
      let angle = label.kind === 'dim' ? Math.atan2(label.direction[1], label.direction[0]) * 180 / Math.PI : 0;
      if (angle >= 90) angle -= 180;
      if (angle < -90) angle += 180;
      const radians = angle * Math.PI / 180;
      const w = Math.abs(Math.cos(radians)) * label.width + Math.abs(Math.sin(radians)) * label.height;
      const h = Math.abs(Math.sin(radians)) * label.width + Math.abs(Math.cos(radians)) * label.height;
      const [ax, ay] = label.anchor;
      const attempt = (cx, cy, displaced = false) => {
        const box = { x: cx - w / 2, y: cy - h / 2, width: w, height: h };
        if (blocked(box)) return false;
        let leader = null;
        if (displaced) {
          const gap = CLEARANCE + 0.5;
          const target = [Math.max(box.x - gap, Math.min(ax, box.x + w + gap)),
            Math.max(box.y - gap, Math.min(ay, box.y + h + gap))];
          if (!router) {
            const routeBoxes = routingObstacles.filter(o => o.owner !== label.text).map(o => o.box);
            routeBoxes.push(...placed.map(p => expand(p.box, gap)));
            router = leaderRouter(label.anchor, routeBoxes);
          }
          leader = router(target);
          if (!leader) return false;
        }
        // Keep future reference leader origins free.
        if (labels.some(l => l.kind === 'ref' && l !== label
          && intersects([l.anchor, l.anchor], expand(box, CLEARANCE)))) return false;
        placed.push({ ...label, box, center: [cx, cy], angle,
          x: cx - label.width / 2, y: cy - label.height / 2, displaced, leader });
        return true;
      };
      let found = false;
      if (label.kind === 'ref') {
        const [l, t, r, b] = label.part, gap = CLEARANCE + 0.5;
        for (const [cx, cy] of [[ax, t - gap - h / 2], [r + gap + w / 2, ay],
          [ax, b + gap + h / 2], [l - gap - w / 2, ay]]) {
          if (attempt(cx, cy)) { found = true; break; }
        }
        // Fixed column edges and row pitch, no radial scatter.
        const row = Math.round(ay / rowHeight);
        const columns = [right + CLEARANCE * 3 + w / 2, left - CLEARANCE * 3 - w / 2];
        for (let step = 0; !found && step < 512; step += 1) {
          for (const y of step ? [row - step, row + step] : [row]) {
            for (const x of columns) if (attempt(x, y * rowHeight, true)) { found = true; break; }
            if (found) break;
          }
        }
      } else if (label.kind === 'dim') {
        // Keep the midpoint centred along the segment, even for short spans.
        const nx = -Math.sin(radians), ny = Math.cos(radians);
        const offset = label.height / 2 + CLEARANCE + 1;
        for (let row = 0; !found && row < 512; row += 1) {
          const distance = offset + row * (label.height + CLEARANCE);
          found = attempt(ax - nx * distance, ay - ny * distance);
        }
      } else {
        // Headings remain centred in ordered rows above the drawing.
        const top = Math.min(ay, ...placed.map(p => p.box.y - CLEARANCE - h / 2));
        for (let row = 0; !found && row < 512; row += 1) found = attempt(ax, top - row * (h + CLEARANCE));
      }
      if (!found) throw new Error('label_layout_exhausted');
    }
    return placed;
  }
  // Node tests exercise the same pure function as the renderer.
  if (typeof module !== 'undefined') { module.exports = { placeLabels, CLEARANCE }; return; }

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
    // Only visible geometry contributes obstacles: offscreen copper must not
    // move annotations or enlarge either view. Bounding boxes are conservative
    // around polygon holes; text goes outside copper rather than into it.
    const obstacle = (points, margin = 0, attrs = {}) => {
      if (!points.length) return;
      const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
      const l = Math.max(0, Math.min(...xs) - margin), t = Math.max(0, Math.min(...ys) - margin);
      const r = Math.min(width, Math.max(...xs) + margin), b = Math.min(height, Math.max(...ys) + margin);
      if (r > l && b > t) obstacles.push({ x: l, y: t, width: r - l, height: b - t, ...attrs });
    };
    for (const pad of geometry.pads || []) for (const poly of pad.polygons) {
      obstacle(poly.outer.map(p => P(...p)), 0, { owner: pad.ref });
    }
    for (const track of geometry.tracks || []) {
      const points = track.points.map(p => P(...p));
      for (let i = 1; i < points.length; i += 1) {
        obstacle([points[i - 1], points[i]], Math.max(1, track.width * S) / 2, { copper: true });
      }
    }
    for (const plane of geometry.planes || []) for (const poly of plane.polygons) {
      obstacle(poly.outer.map(p => P(...p)), 0, { copper: true });
    }
    for (const via of geometry.vias || []) obstacle([P(...via.at)],
      Math.max(1.5, via.diameter / 2 * S) + 0.5, { copper: true });
    const measure = (text, kind, size, spacing, weight, colour, attrs) => {
      if (!text) return;
      const node = el('text', { fill: colour, 'font-size': Math.max(12, size),
        style: 'font-family:' + (kind === 'ref' ? '"Helvetica Neue",Arial,sans-serif' : '"Courier New",monospace'),
        'font-weight': weight, 'letter-spacing': spacing + 'em', visibility: 'hidden' }, g);
      node.textContent = text;
      const measured = node.getBBox();
      labels.push({ text, kind, width: measured.width, height: measured.height, ...attrs,
        node, measured });
    };
    for (const [ref, box] of Object.entries(extent)) {
      rect(box, 0.22, { fill: 'none', stroke: large(box) ? 'rgba(127,233,255,.8)' : SILK, 'stroke-width': 0.8 });
      const visible = [Math.max(x0, box[0]), Math.max(y0, box[1]),
        Math.min(x1, box[2]), Math.min(y1, box[3])];
      if (visible[0] >= visible[2] || visible[1] >= visible[3]) continue;
      const [ax, ay] = P(Math.max(x0, visible[0] - 0.22), Math.max(y0, visible[1] - 0.22));
      const [bx, by] = P(Math.min(x1, visible[2] + 0.22), Math.min(y1, visible[3] + 0.22));
      measure(ref, 'ref', 12, 0.03, 400, SILK, {
        anchor: [(ax + bx) / 2, (ay + by) / 2],
        part: [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)] });
      obstacle([[ax, ay], [bx, by]], 0.4, { owner: ref, body: true });
    }
    for (const dim of item.dims || []) {
      const colour = tone(dim.tone, TONE.info);
      const a = P(...dim.from), b = P(...dim.to);
      const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const direction = length ? [(b[0] - a[0]) / length, (b[1] - a[1]) / length] : [1, 0];
      el('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: colour }, g);
      obstacles.push({ segment: [a, b], stroke: 1 });
      for (const [point, sign] of [[a, 1], [b, -1]]) {
        const [x, y] = point, [dx, dy] = direction;
        const base = [x + sign * dx * 4, y + sign * dy * 4];
        const vertices = [point, [base[0] - dy * 2, base[1] + dx * 2],
          [base[0] + dy * 2, base[1] - dx * 2]];
        el('polygon', { points: vertices.map((p) => p.join(',')).join(' '), fill: colour }, g);
        obstacles.push({ x: x - 5, y: y - 5, width: 10, height: 10, dimension: true });
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
        for (const [a, b] of segments(label.leader)) {
          el('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1],
            stroke: node.getAttribute('fill'), 'stroke-width': 0.6 }, g);
        }
      }
      node.setAttribute('x', label.x - measured.x);
      node.setAttribute('y', label.y - measured.y);
      if (label.angle) node.setAttribute('transform', `rotate(${label.angle},${label.center.join(',')})`);
      node.removeAttribute('visibility');
      g.appendChild(node);
    }
    const leaderPoints = layout.flatMap(l => l.leader || []);
    const left = Math.min(0, ...layout.map((l) => l.box.x), ...dimensionPoints.map((p) => p[0] - 5), ...leaderPoints.map(p => p[0])) - 10;
    const top = Math.min(0, ...layout.map((l) => l.box.y), ...dimensionPoints.map((p) => p[1] - 5), ...leaderPoints.map(p => p[1])) - 10;
    const right = Math.max(width, ...layout.map((l) => l.box.x + l.box.width), ...dimensionPoints.map((p) => p[0] + 5), ...leaderPoints.map(p => p[0])) + 10;
    const bottom = Math.max(height, ...layout.map((l) => l.box.y + l.box.height), ...dimensionPoints.map((p) => p[1] + 5), ...leaderPoints.map(p => p[1])) + 10;
    g.setAttribute('transform', `translate(${offsetX - left},${-top})`);
    return [right - left, bottom - top];
  }

  async function load() {
    const response = await fetch('/display.json', { cache: 'no-store' });
    const { id, version, display } = await response.json();
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
    // Only now is it on screen; she may say so only after this report.
    await fetch(`/display-drawn.json?id=${encodeURIComponent(id)}&version=${version}`, { cache: 'no-store' });
  }
  load().catch(() => close());
})();
