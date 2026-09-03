/**
 * pip-editor.js — PiP drag / resize / presets (P3-29 … P3-45).
 *
 * Eight handles (4 corners + 4 sides), Pointer Events only (mouse/touch/
 * stylus) with setPointerCapture. Hit-testing in logical coordinates.
 * Corner handles resize w+h anchored at the opposite corner; side handles
 * change one dimension. Clamps + optional aspect lock. Snap guides to
 * canvas center/edges. Keyboard nudging. Geometry events on commit only.
 * 14 presets computed from the live canvas aspect.
 */

export const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];

const HANDLE_HIT = 14;            // logical px hit radius
const SNAP_THRESHOLD = 0.012;     // normalized snap distance (P3-37)
const MIN_SIZE = 0.03;
const NUDGE = 0.002, NUDGE_BIG = 0.02;

export class PipEditor {
  constructor(compositor, layerManager, { guidesEl, guideV, guideH } = {}) {
    this.comp = compositor;
    this.lm = layerManager;
    this.guidesEl = guidesEl || null;
    this.guideV = guideV || null;
    this.guideH = guideH || null;
    this.snapEnabled = true;

    this.comp.overlayRenderer = (ctx, layer) => this.drawOverlay(ctx, layer);

    // ---- Pointer Events on the canvas (P3-30) ----
    const canvas = compositor.canvas;
    canvas.addEventListener('pointerdown', (e) => this.onPointerDown(e));
    canvas.addEventListener('pointermove', (e) => this.onPointerMove(e));
    canvas.addEventListener('pointerup', (e) => this.onPointerUp(e));
    canvas.addEventListener('pointercancel', (e) => this.onPointerUp(e));

    this._drag = null;   // {mode, layerId, startGeo, startPoint, aspect}
  }

  /** Overlay pass: selection outline + 8 handles (P3-29, P3-11). */
  drawOverlay(ctx, layer) {
    const W = this.comp.logicalW, H = this.comp.logicalH;
    const g = layer.geometry;
    const x = g.x * W, y = g.y * H, w = g.w * W, h = g.h * H;
    ctx.save();
    ctx.strokeStyle = '#4f8cff';
    ctx.lineWidth = Math.max(2, W / 640);
    ctx.setLineDash(layer.locked ? [10, 6] : []);
    ctx.strokeRect(x, y, w, h);

    const r = Math.max(HANDLE_HIT / 1.6, W / 320);
    for (const handle of HANDLES) {
      const [hx, hy] = this.handlePos(handle, x, y, w, h);
      ctx.beginPath();
      ctx.arc(hx, hy, r, 0, Math.PI * 2);
      ctx.fillStyle = layer.locked ? '#9aa3b5' : '#ffffff';
      ctx.fill();
      ctx.strokeStyle = '#4f8cff';
      ctx.stroke();
    }
    ctx.restore();
  }

  handlePos(handle, x, y, w, h) {
    const cx = x + w / 2, cy = y + h / 2;
    return {
      nw: [x, y], n: [cx, y], ne: [x + w, y],
      e: [x + w, cy], se: [x + w, y + h], s: [cx, y + h],
      sw: [x, y + h], w: [x, cy],
    }[handle];
  }

  /** Hit-testing in logical coords (P3-31) — handles first, then layer body. */
  hitTest(lx, ly) {
    const layer = this.lm.selected;
    if (!layer) return null;
    const W = this.comp.logicalW, H = this.comp.logicalH;
    const g = layer.geometry;
    const x = g.x * W, y = g.y * H, w = g.w * W, h = g.h * H;
    // enlarged hit area on coarse pointers is achieved via HANDLE_HIT × dpr scale
    const hit = HANDLE_HIT * Math.max(1, 640 / W) * (window.matchMedia?.('(pointer: coarse)').matches ? 1.8 : 1);
    for (const handle of HANDLES) {
      const [hx, hy] = this.handlePos(handle, x, y, w, h);
      if (Math.abs(lx - hx) <= hit && Math.abs(ly - hy) <= hit) return { handle };
    }
    if (lx >= x && lx <= x + w && ly >= y && ly <= y + h) return { body: true };
    // click on another layer? select it
    for (let i = this.lm.layers.length - 1; i >= 0; i--) {
      const l = this.lm.layers[i];
      if (!l.visible) continue;
      const lg = l.geometry;
      const lx1 = lg.x * W, ly1 = lg.y * H;
      if (lx >= lx1 && lx <= lx1 + lg.w * W && ly >= ly1 && ly <= ly1 + lg.h * H) {
        return { select: l.id };
      }
    }
    return null;
  }

  onPointerDown(e) {
    if (this.comp.aspect == null) return;
    const pt = this.comp.toLogical(e.clientX, e.clientY);
    const hit = this.hitTest(pt.x, pt.y);
    if (!hit) { this.lm.select(null); this.hideGuides(); return; }
    if (hit.select) { this.lm.select(hit.select); return; }
    const layer = this.lm.selected;
    if (!layer || layer.locked) return;
    e.preventDefault();
    this.comp.canvas.setPointerCapture(e.pointerId);   // P3-30
    const g = layer.geometry;
    this._drag = {
      mode: hit.handle || 'move',
      layerId: layer.id,
      startGeo: { ...g },
      startPoint: pt,
      aspect: g.w > 0.001 ? g.h / g.w : 1,
      moved: false,
    };
    this.comp.canvas.style.cursor = this.cursorFor(this._drag.mode);
  }

  cursorFor(mode) {
    return { move: 'move', nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize',
             sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize',
             w: 'ew-resize' }[mode] || 'default';
  }

  onPointerMove(e) {
    if (!this._drag) {
      // hover cursor
      const pt = this.comp.toLogical(e.clientX, e.clientY);
      const hit = this.hitTest(pt.x, pt.y);
      this.comp.canvas.style.cursor = hit ? this.cursorFor(hit.handle || 'move') : 'default';
      return;
    }
    e.preventDefault();
    const pt = this.comp.toLogical(e.clientX, e.clientY);
    const W = this.comp.logicalW, H = this.comp.logicalH;
    const d = this._drag;
    const g0 = d.startGeo;
    const dx = (pt.x - d.startPoint.x) / W;
    const dy = (pt.y - d.startPoint.y) / H;
    d.moved = true;
    let g = { ...g0 };

    if (d.mode === 'move') {   // P3-32 — drag with clamp to bounds
      g.x = clamp(g0.x + dx, 0, 1 - g0.w);
      g.y = clamp(g0.y + dy, 0, 1 - g0.h);
      if (this.snapEnabled) g = this.applySnap(g);
    } else {
      const lockAspect = this.lm.selected?.aspectLock;
      let x1 = g0.x, y1 = g0.y, x2 = g0.x + g0.w, y2 = g0.y + g0.h;
      if (d.mode.includes('w')) x1 = g0.x + dx;
      if (d.mode.includes('e')) x2 = g0.x + g0.w + dx;
      if (d.mode.includes('n')) y1 = g0.y + dy;
      if (d.mode.includes('s')) y2 = g0.y + g0.h + dy;
      // normalize when inverted
      if (x2 < x1) [x1, x2] = [x2, x1];
      if (y2 < y1) [y1, y2] = [y2, y1];
      // min-size clamp + canvas clamp (P3-35)
      x1 = clamp(x1, 0, 1); y1 = clamp(y1, 0, 1);
      x2 = clamp(x2, 0, 1); y2 = clamp(y2, 0, 1);
      let w = Math.max(MIN_SIZE, x2 - x1), h = Math.max(MIN_SIZE, y2 - y1);
      if (lockAspect) {   // P3-36 — aspect lock keeps source ratio
        const target = d.aspect * w;
        if (Math.abs(target - h) > 0.001) {
          h = Math.min(target, 1 - y1);
          w = h / d.aspect;
        }
      }
      g = { x: x1, y: y1, w, h };
    }
    // live update without emitting events (events on commit — P3-40)
    const layer = this.lm.layers.find(l => l.id === d.layerId);
    if (layer) layer.geometry = g;
    this.comp.markDirty();
  }

  applySnap(g) {   // P3-37 — snap guides to center/edges
    const cands = [
      { axis: 'x', val: 0 }, { axis: 'x', val: 0.5 }, { axis: 'x', val: 1 - g.w },
      { axis: 'y', val: 0 }, { axis: 'y', val: 0.5 }, { axis: 'y', val: 1 - g.h },
      { axis: 'x', val: 0.5 - g.w / 2 }, { axis: 'y', val: 0.5 - g.h / 2 },
    ];
    let snappedX = null, snappedY = null;
    for (const c of cands) {
      if (Math.abs(g[c.axis] - c.val) < SNAP_THRESHOLD) {
        g[c.axis] = c.val;
        if (c.axis === 'x') snappedX = c.val;
        else snappedY = c.val;
      }
    }
    this.showGuides(snappedX !== null, snappedY !== null, snappedX, snappedY);
    return g;
  }

  showGuides(showV, showH, xv, yv) {
    if (!this.guidesEl) return;
    if (!showV && !showH) { this.guidesEl.hidden = true; return; }
    this.guidesEl.hidden = false;
    if (showV) {
      const rect = this.comp.canvas.getBoundingClientRect();
      const px = (xv + (this.lm.selected?.geometry.w ?? 0) / 2);
      this.guideV.style.left = `${Math.round(rect.width * (xv ?? 0.5))}px`;
    }
    if (showH) {
      this.guideH.style.top = `${Math.round(this.comp.canvas.getBoundingClientRect().height * (yv ?? 0.5))}px`;
    }
  }
  hideGuides() { if (this.guidesEl) this.guidesEl.hidden = true; }

  onPointerUp(e) {
    if (!this._drag) return;
    try { this.comp.canvas.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
    const d = this._drag;
    this._drag = null;
    this.hideGuides();
    this.comp.canvas.style.cursor = 'default';
    if (d.moved) {
      // commit with validation + single geometry event (P3-39, P3-40)
      const layer = this.lm.layers.find(l => l.id === d.layerId);
      if (layer) this.lm.setGeometry(layer.id, layer.geometry, { emit: true, commit: true });
    }
    this.comp.markDirty();
  }

  /** Keyboard nudging (P3-38): arrows move, shift+arrows resize. */
  key(e, layer) {
    if (!layer || layer.locked) return false;
    const big = e.shiftKey;
    const step = big ? NUDGE_BIG : NUDGE;
    const g = { ...layer.geometry };
    let handled = true;
    switch (e.key) {
      case 'ArrowLeft':  big ? g.w = Math.max(MIN_SIZE, g.w - step) : g.x = Math.max(0, g.x - step); break;
      case 'ArrowRight': big ? g.w = Math.min(1 - g.x, g.w + step) : g.x = Math.min(1 - g.w, g.x + step); break;
      case 'ArrowUp':    big ? g.h = Math.max(MIN_SIZE, g.h - step) : g.y = Math.max(0, g.y - step); break;
      case 'ArrowDown':  big ? g.h = Math.min(1 - g.y, g.h + step) : g.y = Math.min(1 - g.h, g.y + step); break;
      default: handled = false;
    }
    if (handled) {
      e.preventDefault();
      this.lm.setGeometry(layer.id, g, { emit: true, commit: true });
    }
    return handled;
  }

  /** 14 presets computed for the current canvas aspect (P3-42 … P3-45). */
  applyPreset(name, layer) {
    if (!layer) return;
    const aspect = this.comp.logicalW / this.comp.logicalH;   // P3-44
    // PiP box: fixed width fraction; height follows the canvas aspect so the
    // box looks right on 16:9, 9:16 and 1:1 alike.
    const box = (w = 0.25) => ({ w, h: Math.min(1, w * aspect) });
    const at = (x, y, b) => ({ x, y, w: b.w, h: b.h });
    const presets = {
      'top-left':       () => at(0.02, 0.02, box()),
      'top-center':     () => at(0.5 - box().w / 2, 0.02, box()),
      'top-right':      () => at(0.98 - box().w, 0.02, box()),
      'center-left':    () => at(0.02, 0.5 - box().h / 2, box()),
      'center':         () => at(0.5 - box().w / 2, 0.5 - box().h / 2, box()),
      'center-right':   () => at(0.98 - box().w, 0.5 - box().h / 2, box()),
      'bottom-left':    () => at(0.02, 0.98 - box().h, box()),
      'bottom-center':  () => at(0.5 - box().w / 2, 0.98 - box().h, box()),
      'bottom-right':   () => at(0.98 - box().w, 0.98 - box().h, box()),
      '50/50':          () => ({ x: 0.5, y: 0, w: 0.5, h: 1 }),
      '70/30':          () => ({ x: 0.70, y: 0, w: 0.30, h: 1 }),
      'quarter-screen': () => ({ x: 0.5, y: 0, w: 0.5, h: 0.5 }),
      'full-screen':    () => ({ x: 0, y: 0, w: 1, h: 1 }),
      'custom':         () => ({ ...layer.geometry }),
    };
    const fn = presets[name];
    if (!fn) return;
    this.lm.setGeometry(layer.id, fn(), { emit: true, commit: true });   // undoable, emits event (P3-45)
  }
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
