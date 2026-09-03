/**
 * compositor.js — the live multi-layer canvas compositor (P3-01 … P3-12).
 *
 * Canvas 2D with DPR-aware backing store. Stable logical coordinate system
 * (1920×1080 / 1080×1920 / 1080×1080) independent of display size. Render
 * loop: rVFC when a video is active, rAF fallback. Dirty-flag rendering.
 * Draw order = z-order; visibility checked at draw time — fully decoupled
 * from play/pause (GR-09). Fit modes contain/cover/stretch per layer.
 * Overlay handles drawn above content and EXCLUDED from capture (P3-11):
 * they live in a separate overlay pass controlled by `drawOverlay`.
 */

export const CANVAS_SIZES = {
  '16:9': { width: 1920, height: 1080 },
  '9:16': { width: 1080, height: 1920 },
  '1:1': { width: 1080, height: 1080 },
};

export class Compositor {
  constructor(canvas, { onFrameRendered } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
    this.layers = [];                 // ordered bottom→top by z
    this.aspect = '16:9';
    this.background = '#000000';
    this.selectedId = null;
    this.drawOverlay = true;          // handles; disabled during capture (P7-16)
    this.overlayRenderer = null;      // pip-editor hook
    this.onFrameRendered = onFrameRendered || null;

    this._raf = null;
    this._rvfcHandle = null;
    this._dirty = true;
    this._lastRenderMs = 0;
    this._rvfcVideo = null;
    this._fpsCap = null;
    this._lastCapTime = 0;
    this.previewScale = 1.0;          // degradation (P2-36)
  }

  setLayers(layers) {
    this.layers = [...layers].sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
    this.markDirty();
    this._refreshLoop();
  }

  setAspect(aspect) {
    if (!CANVAS_SIZES[aspect]) return;
    this.aspect = aspect;
    this._applySize();
    this.markDirty();
  }

  _applySize() {
    const { width, height } = CANVAS_SIZES[this.aspect];
    const dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
    // Backing store: logical resolution × preview scale (degradation-aware),
    // displayed responsively via CSS while the logical system stays stable (P3-03/04).
    const scale = this.previewScale * (CANVAS_SIZES[this.aspect].width > 1080 ? 1 : 1);
    this.logicalW = width;
    this.logicalH = height;
    const bw = Math.round(width * scale);
    const bh = Math.round(height * scale);
    this.canvas.width = Math.max(2, bw - (bw % 2));
    this.canvas.height = Math.max(2, bh - (bh % 2));
    this.canvas.style.aspectRatio = `${width} / ${height}`;
    this._scaleX = this.canvas.width / width;   // logical → device pixels
    this._scaleY = this.canvas.height / height;
    void dpr;
  }

  markDirty() { this._dirty = true; }

  /** Pointer client coords → logical canvas coords (P3-31 helper). */
  toLogical(clientX, clientY) {
    const r = this.canvas.getBoundingClientRect();
    return {
      x: (clientX - r.left) / r.width * this.logicalW,
      y: (clientY - r.top) / r.height * this.logicalH,
    };
  }

  _refreshLoop() {
    const playingVideo = this.layers.find(l => l.visible && !l.lockedHidden && l.element
      && l.element.tagName === 'VIDEO' && !l.element.paused && !l.element.ended);
    // rVFC when any video layer is active (P3-05); rAF otherwise (P3-06)
    const wantRvfc = playingVideo && typeof playingVideo.element.requestVideoFrameCallback === 'function';
    if (wantRvfc && this._rvfcVideo !== playingVideo.element) {
      this._stopLoops();
      this._rvfcVideo = playingVideo.element;
      const cb = () => {
        this._render();
        if (this._rvfcVideo) this._rvfcHandle = this._rvfcVideo.requestVideoFrameCallback(cb);
      };
      this._rvfcHandle = playingVideo.element.requestVideoFrameCallback(cb);
    } else if (!wantRvfc && !this._raf) {
      this._startRaf();
    } else if (wantRvfc && this._raf) {
      this._stopRaf();
    }
    this.markDirty();
  }

  _startRaf() {
    const loop = () => {
      this._render();
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  _stopRaf() { if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; } }
  _stopLoops() {
    this._stopRaf();
    this._rvfcVideo = null;
    this._rvfcHandle = null;
  }

  stop() { this._stopLoops(); }

  _shouldRender() {
    // Dirty-flag rendering (P3-07): redraw when dirty OR any layer is animating
    if (this._dirty) return true;
    return this.layers.some(l =>
      (l.visible && l.element && l.element.tagName === 'VIDEO' && !l.element.paused && !l.element.ended) ||
      l.type === 'camera');
  }

  _render() {
    // preview FPS cap during degradation (P2-36)
    if (this._fpsCap) {
      const now = performance.now();
      if (now - this._lastCapTime < 1000 / this._fpsCap) return;
      this._lastCapTime = now;
    }
    const t0 = performance.now();
    const ctx = this.ctx;
    const W = this.logicalW, H = this.logicalH;
    ctx.setTransform(this._scaleX, 0, 0, this._scaleY, 0, 0);
    ctx.fillStyle = this.background;
    ctx.fillRect(0, 0, W, H);

    for (const layer of this.layers) {           // z-order: last drawn = on top (P3-08)
      if (!layer.visible) continue;              // draw-time visibility (P3-09, GR-09)
      const el = layer.element;
      if (!el) continue;
      const geo = layer.geometry || { x: 0, y: 0, w: 1, h: 1 };
      const x = geo.x * W, y = geo.y * H, w = geo.w * W, h = geo.h * H;
      if (w < 1 || h < 1) continue;
      ctx.save();
      if (layer.mirror) {                        // front-camera mirror (P4-13)
        ctx.translate(x + w, y);
        ctx.scale(-1, 1);
        this._drawFitted(ctx, el, layer, 0, 0, w, h);
      } else {
        this._drawFitted(ctx, el, layer, x, y, w, h);
      }
      ctx.restore();
      if (layer.statusBadge === 'SOURCE_LOST') {
        ctx.fillStyle = 'rgba(255,93,115,.85)';
        ctx.fillRect(x, y, Math.max(w, 60), Math.max(h, 24));
        ctx.fillStyle = '#fff';
        ctx.font = `${Math.max(12, h * 0.08)}px system-ui`;
        ctx.textAlign = 'center';
        ctx.fillText('SOURCE LOST', x + w / 2, y + h / 2);
      }
    }

    // overlay pass (handles) — above content, excluded from capture (P3-11, P7-16)
    if (this.drawOverlay && this.overlayRenderer && this.selectedId) {
      const sel = this.layers.find(l => l.id === this.selectedId);
      if (sel) this.overlayRenderer(ctx, sel);
    }
    this._dirty = false;
    const ms = performance.now() - t0;
    this._lastRenderMs = ms;
    this.onFrameRendered?.(ms);
  }

  _drawFitted(ctx, el, layer, x, y, w, h) {
    const mode = layer.fit || 'contain';
    let sw = el.videoWidth || el.naturalWidth || el.width || 0;
    let sh = el.videoHeight || el.naturalHeight || el.height || 0;
    if (!sw || !sh) {   // stream-backed <video> may not have dims on frame 0
      ctx.fillStyle = '#111';
      ctx.fillRect(x, y, w, h);
      return;
    }
    if (mode === 'stretch') {
      ctx.drawImage(el, x, y, w, h);
      return;
    }
    const scale = mode === 'cover'
      ? Math.max(w / sw, h / sh)
      : Math.min(w / sw, h / sh);
    const dw = sw * scale, dh = sh * scale;
    const dx = x + (w - dw) / 2, dy = y + (h - dh) / 2;
    if (mode === 'cover') {
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.clip();
      ctx.drawImage(el, dx, dy, dw, dh);
      ctx.restore();
    } else {
      ctx.fillStyle = '#000';
      ctx.fillRect(x, y, w, h);
      ctx.drawImage(el, dx, dy, dw, dh);
    }
  }

  /** Frame-grab / capture stream without overlay chrome (P7-02, P7-16). */
  captureStream(fps) {
    this.drawOverlay = false;
    this.markDirty();
    this._render();
    const stream = this.canvas.captureStream(fps);
    const restore = () => { this.drawOverlay = true; this.markDirty(); };
    return { stream, restore };
  }

  setDegradation({ previewScale = null, fpsCap = null } = {}) {
    if (previewScale && previewScale !== this.previewScale) {
      this.previewScale = previewScale;
      this._applySize();
    }
    this._fpsCap = fpsCap;
    this.markDirty();
  }
}
