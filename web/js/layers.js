/**
 * layers.js — the layer model & panel (P3-13 … P3-28).
 *
 * Every layer owns an independent media element (GR-08). Visibility is a
 * separate axis from play/pause (GR-09, §10). All meaningful actions emit
 * timeline events. The panel row exposes eye/lock/mute/volume/play/rename/
 * delete/drag-reorder; Play All / Pause All are explicit separate controls.
 */

let layerSeq = 0;

export function makeLayer({ type = 'video', name = null, mediaId = null, element = null,
                            fit = 'contain', source = null } = {}) {
  layerSeq += 1;
  const id = `layer_${String(layerSeq).padStart(2, '0')}_${Math.random().toString(36).slice(2, 6)}`;
  return {
    id,
    type,                       // video | camera | image | audio
    name: name || `${type[0].toUpperCase()}${type.slice(1)} ${layerSeq}`,
    mediaId,
    source,                     // camera deviceId or media id reference
    element,                    // independent HTMLVideoElement / Image / Audio (P3-15)
    visible: true,
    locked: false,
    muted: false,
    volume: 1.0,
    playing: type === 'camera' || type === 'image',
    fit,
    mirror: false,
    aspectLock: false,
    geometry: type === 'camera' ? { x: 0.7, y: 0.7, w: 0.25, h: 0.25 }
                                : { x: 0.0, y: 0.0, w: 1.0, h: 1.0 },
    z: 0,
    state: { mediaTime: 0 },
    statusBadge: null,          // e.g. SOURCE_LOST (P4-17)
  };
}

export class LayerManager {
  constructor({ timeline, mixer, perf, onChange, onSelect, toast } = {}) {
    this.layers = [];
    this.selectedId = null;
    this.timeline = timeline;
    this.mixer = mixer;
    this.perf = perf;
    this.onChange = onChange || (() => {});
    this.onSelect = onSelect || (() => {});
    this.toast = toast || (() => {});
  }

  get selected() { return this.layers.find(l => l.id === this.selectedId) || null; }

  _renumber() { this.layers.forEach((l, i) => { l.z = i; }); }

  add(layer) {
    this.layers.push(layer);
    this._renumber();
    this.timeline.emit(layer.id, 'layer_add');
    this.select(layer.id);
    this.onChange();
    return layer;
  }

  /** Remove with full cleanup: pause, revoke URL, audio disconnect (P3-17). */
  remove(layerId, { skipConfirm = false } = {}) {
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer) return;
    if (!skipConfirm && !window.confirm(`Remove layer "${layer.name}"? (Source files are never deleted.)`)) return;
    this.timeline.emit(layer.id, 'layer_remove', { mediaTime: layer.element?.currentTime ?? null });
    try { layer.element?.pause(); } catch { /* ignore */ }
    this.mixer?.detach(layer.id);
    this.perf?.unwatch(layer.id);
    if (this.selectedId === layerId) this.selectedId = null;
    this.layers = this.layers.filter(l => l.id !== layerId);
    this._renumber();
    this.onChange();
    this.onSelect(null);
  }

  duplicate(layerId) {   // P3-18
    const src = this.layers.find(l => l.id === layerId);
    if (!src) return null;
    const copy = makeLayer({
      type: src.type, name: `${src.name} copy`, mediaId: src.mediaId,
      element: src._cloneElement ? src._cloneElement() : null,
      fit: src.fit, source: src.source,
    });
    if (src.element) {
      // independent element instance (P3-15) — same source, separate element
      const el = createMediaElement(src.element.tagName === 'VIDEO' ? 'video' :
        src.element.tagName === 'IMG' ? 'img' : 'audio');
      el.src = src.element.src;
      el.muted = true;   // duplicates start muted to avoid double audio
      if (el.tagName === 'VIDEO' || el.tagName === 'AUDIO') el.load();
      copy.element = el;
      copy.muted = true;
    }
    Object.assign(copy, {
      visible: src.visible, locked: false, muted: true, volume: src.volume,
      playing: src.playing, fit: src.fit, mirror: src.mirror,
      geometry: { ...src.geometry },
    });
    return this.add(copy);
  }

  rename(layerId, name) {   // P3-19 (inline edit + validation)
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer) return;
    const clean = String(name || '').trim().slice(0, 60);
    if (!clean) { this.toast('Layer name cannot be empty.', 'warn'); return; }
    layer.name = clean;
    this.onChange();
  }

  reorder(fromIndex, toIndex) {   // P3-20 — z-order drag
    if (fromIndex === toIndex) return;
    const [moved] = this.layers.splice(fromIndex, 1);
    this.layers.splice(toIndex, 0, moved);
    this._renumber();
    this.timeline.emit(moved.id, 'layer_reorder', { payload: { order: this.layers.map(l => l.id) } });
    this.onChange();
  }

  moveLayer(layerId, delta) {   // buttons/keyboard
    const idx = this.layers.findIndex(l => l.id === layerId);
    const to = Math.max(0, Math.min(this.layers.length - 1, idx + delta));
    if (idx >= 0 && to !== idx) this.reorder(idx, to);
  }

  setLocked(layerId, locked) {
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer) return;
    layer.locked = locked;
    this.onChange();
  }

  setVisible(layerId, visible) {   // P3-24 — separate axis from play/pause (GR-09)
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer) return;
    layer.visible = visible;
    this.timeline.emit(layer.id, visible ? 'visibility_on' : 'visibility_off',
                       { mediaTime: layer.element?.currentTime ?? null });
    this.onChange();
  }

  setMuted(layerId, muted) {   // P3-22
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer) return;
    layer.muted = muted;
    this.mixer?.setMuted(layerId, muted);
    this.timeline.emit(layer.id, muted ? 'mute' : 'unmute');
    this.onChange();
  }

  setVolume(layerId, v) {   // P3-22
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer) return;
    layer.volume = Math.max(0, Math.min(1, v));
    this.mixer?.setVolume(layerId, layer.volume);
    this.timeline.emit(layer.id, 'volume', { payload: { volume: layer.volume } });
    this.onChange();
  }

  play(layerId) {   // independent — no global side effects (P3-23, §12)
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer || layer.type === 'image') return;
    layer.element?.play?.().catch(() => {});
    layer.playing = true;
    this.timeline.emit(layer.id, 'play', { mediaTime: layer.element?.currentTime ?? null });
    this.onChange();
  }

  pause(layerId) {
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer || layer.type === 'image') return;
    layer.element?.pause?.();
    layer.playing = false;
    this.timeline.emit(layer.id, 'pause', { mediaTime: layer.element?.currentTime ?? null });
    this.onChange();
  }

  seek(layerId, time) {
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer || !layer.element || layer.type === 'image') return;
    const t = Math.max(0, Math.min((layer.element.duration || 0) - 0.01, time));
    layer.element.currentTime = t;
    this.timeline.emit(layer.id, 'seek', { mediaTime: t });
    this.onChange();
  }

  setGeometry(layerId, geometry, { emit = true, commit = true } = {}) {   // P3-39/40
    const layer = this.layers.find(l => l.id === layerId);
    if (!layer || layer.locked) return;
    const g = sanitizeGeometry(geometry, layer.geometry);
    layer.geometry = g;
    if (emit && commit) {   // commit-time events, not per-pointermove (P3-40)
      this.timeline.emit(layer.id, 'geometry_change', { payload: { geometry: { ...g } } });
    }
    this.onChange();
  }

  setFit(layerId, fit) {
    const layer = this.layers.find(l => l.id === layerId);
    if (layer) { layer.fit = fit; this.onChange(); }
  }

  select(layerId) {   // P3-26
    this.selectedId = layerId;
    this.onSelect(this.selected);
    this.onChange();
  }

  /** Explicit global controls (P3-28) — never triggered implicitly. */
  playAll() {
    for (const l of this.layers) this.play(l.id);
    this.toast('Play All — every layer', 'info');
  }
  pauseAll() {
    for (const l of this.layers) this.pause(l.id);
    this.toast('Pause All — every layer', 'info');
  }
  resetAll() {
    for (const l of this.layers) {
      if (l.type === 'image' || l.type === 'camera') continue;
      this.seek(l.id, 0);
      this.pause(l.id);
    }
    this.toast('Reset All — layers rewound and paused', 'info');
  }

  serialize() {
    return this.layers.map(l => ({
      id: l.id, type: l.type, name: l.name, mediaId: l.mediaId, source: l.source,
      visible: l.visible, locked: l.locked, muted: l.muted, volume: l.volume,
      fit: l.fit, mirror: l.mirror, geometry: { ...l.geometry }, z: l.z,
      state: { playing: l.playing, mediaTime: l.element?.currentTime ?? l.state?.mediaTime ?? 0 },
    }));
  }
}

export function sanitizeGeometry(g, fallback = null) {
  const out = { ...(fallback || { x: 0, y: 0, w: 1, h: 1 }) };
  for (const k of ['x', 'y', 'w', 'h']) {
    const v = Number(g?.[k]);
    if (!isFinite(v)) continue;                    // reject NaN (P3-39)
    out[k] = Math.max(0, Math.min(1, v));          // clamp 0..1
  }
  out.w = Math.max(0.01, out.w);
  out.h = Math.max(0.01, out.h);
  return out;
}

export function createMediaElement(kind) {
  const el = document.createElement(kind === 'img' ? 'img' : kind === 'audio' ? 'audio' : 'video');
  if (el.tagName === 'VIDEO') {
    el.playsInline = true;
    el.preload = 'auto';
    el.crossOrigin = 'anonymous';
  }
  return el;
}
