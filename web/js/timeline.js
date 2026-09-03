/**
 * timeline.js — master clock + event log + scrubber UI (P6-01 … P6-12).
 *
 * The master clock is performance.now() — monotonic, never wall-clock,
 * never frame numbers (GR-10). Events are append-only during a take and
 * ordered by wallMs. High-frequency volume/geometry events are coalesced.
 */

const EVENT_ACTIONS = new Set([
  'play', 'pause', 'seek', 'visibility_on', 'visibility_off',
  'mute', 'unmute', 'volume', 'source_change', 'geometry_change',
  'layer_add', 'layer_remove', 'layer_reorder',
]);

const COALESCE_MS = 400; // merge rapid volume/geometry events (P6-09)
const COALESCE_ACTIONS = new Set(['volume', 'geometry_change']);

export class Timeline {
  constructor() {
    this.events = [];
    this.t0 = null;               // performance.now() at recording start (P7-07)
    this.listeners = new Set();
    this._last = null;            // last coalescable event
  }

  /** Monotonic master clock in ms (GR-10 / P6-01). */
  static now() { return performance.now(); }

  /** Take-relative time in seconds (or null when not recording). */
  takeTime() { return this.t0 === null ? null : Math.max(0, (performance.now() - this.t0) / 1000); }

  resetTake() { this.t0 = null; }

  beginTake() { this.t0 = performance.now(); }

  /**
   * Record one event (P6-02 … P6-07). Coalesces volume/geometry bursts.
   * Returns the stored event (or null if coalesced into a previous one).
   */
  emit(layerId, action, { mediaTime = null, payload = null } = {}) {
    if (!EVENT_ACTIONS.has(action)) return null;
    const ev = {
      layerId,
      action,
      wallMs: Math.round(performance.now()),
      mediaTime: mediaTime === null ? null : Math.round(mediaTime * 1000) / 1000,
      payload,
    };
    if (COALESCE_ACTIONS.has(action) && this._last &&
        this._last.layerId === layerId && this._last.action === action &&
        ev.wallMs - this._last.wallMs < COALESCE_MS) {
      this._last.payload = payload;   // replace value, keep original timestamp
      this._last.mediaTime = ev.mediaTime;
      this._notify();
      return null;
    }
    this.events.push(ev);
    if (COALESCE_ACTIONS.has(action)) this._last = ev;
    else if (this._last && ev.wallMs - this._last.wallMs > COALESCE_MS) this._last = null;
    this._notify();
    return ev;
  }

  on(cb) { this.listeners.add(cb); return () => this.listeners.delete(cb); }
  _notify() { for (const cb of this.listeners) { try { cb(this); } catch { /* listener errors never break the clock */ } } }

  /** Ordered, JSON-serializable copy — round-trips losslessly (P6-12). */
  serialize() { return [...this.events].sort((a, b) => a.wallMs - b.wallMs); }
  load(events) {
    this.events = Array.isArray(events) ? events.filter(e => e && EVENT_ACTIONS.has(e.action)) : [];
    this._last = null;
    this._notify();
  }
  clear() { this.events = []; this._last = null; this._notify(); }
  get count() { return this.events.length; }
}

/** Human-readable one-liner for the event list (P6-11). */
export function describeEvent(ev, layerName = ev.layerId) {
  const p = ev.payload || {};
  switch (ev.action) {
    case 'play': return `${layerName} ▶ play`;
    case 'pause': return `${layerName} ⏸ pause`;
    case 'seek': return `${layerName} ⤓ seek → ${ev.mediaTime?.toFixed(2)}s`;
    case 'visibility_on': return `${layerName} 👁 show`;
    case 'visibility_off': return `${layerName} 🚫 hide`;
    case 'mute': return `${layerName} 🔇 mute`;
    case 'unmute': return `${layerName} 🔊 unmute`;
    case 'volume': return `${layerName} ⚭ vol ${(p.volume ?? 1).toFixed(2)}`;
    case 'source_change': return `${layerName} ⇄ source → ${p.source ?? p.mediaId ?? '?'}`;
    case 'geometry_change': {
      const g = p.geometry || {};
      return `${layerName} ⌗ geo ${Math.round((g.x ?? 0) * 100)},${Math.round((g.y ?? 0) * 100)} ${Math.round((g.w ?? 0) * 100)}×${Math.round((g.h ?? 0) * 100)}%`;
    }
    case 'layer_add': return `${layerName} ＋ layer added`;
    case 'layer_remove': return `${layerName} ✕ layer removed`;
    case 'layer_reorder': return `⇅ reorder: ${(p.order || []).join(' → ')}`;
    default: return `${layerName} ${ev.action}`;
  }
}

export function fmtClock(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return '00:00.0';
  const m = Math.floor(sec / 60), s = sec - m * 60;
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
}

/** Timeline scrubber UI: clock, event count, per-layer bands, event list. */
export class TimelineUI {
  constructor(timeline, { clockEl, scrubEl, countEl, bandsEl, eventListEl }) {
    this.tl = timeline;
    this.els = { clockEl, scrubEl, countEl, bandsEl, eventListEl };
    this.layerNames = new Map();       // layerId -> name
    this.duration = 0;                 // current take duration (for band scale)
    this.playheadCb = null;
    timeline.on(() => this.render());
    setInterval(() => this._tickClock(), 200);
  }

  setLayerNames(map) { this.layerNames = new Map(map); }

  _tickClock() {
    const t = this.tl.takeTime();
    if (t !== null) {
      this.els.clockEl.textContent = fmtClock(t);
      if (this.duration > 0) this.els.scrubEl.value = String(Math.min(1000, Math.round(1000 * t / this.duration)));
      if (this.playheadCb) this.playheadCb(t);
    } else {
      const last = this.tl.events[this.tl.events.length - 1];
      this.els.clockEl.textContent = last
        ? fmtClock((last.wallMs - (this.tl.events[0]?.wallMs ?? last.wallMs)) / 1000) : '00:00.0';
    }
  }

  /** Compute per-layer play/freeze/hidden bands from the event log (P6-10). */
  computeBands() {
    if (!this.tl.events.length) return [];
    const t0 = this.tl.t0 ?? this.tl.events[0].wallMs;
    const tEnd = this.tl.t0 !== null ? Math.max(performance.now(), ...this.tl.events.map(e => e.wallMs))
                                     : Math.max(...this.tl.events.map(e => e.wallMs));
    const dur = Math.max(0.001, tEnd - t0);
    const layers = new Map();
    const state = (id) => {
      if (!layers.has(id)) {
        layers.set(id, { id, playing: true, visible: true, bands: [] });
      }
      return layers.get(id);
    };
    for (const ev of this.tl.events) {
      const st = state(ev.layerId);
      st.bands.push({ t: (ev.wallMs - t0) / dur, action: ev.action });
      switch (ev.action) {
        case 'play': st.playing = true; break;
        case 'pause': st.playing = false; break;
        case 'visibility_on': st.visible = true; break;
        case 'visibility_off': st.visible = false; break;
      }
    }
    return [...layers.values()].map(st => ({ ...st, duration: dur }));
  }

  render() {
    this.els.countEl.textContent = `${this.tl.count} event${this.tl.count === 1 ? '' : 's'}`;
    const bands = this.computeBands();
    this.els.bandsEl.innerHTML = '';
    for (const layer of bands.slice(-6)) {  // show up to 6 layer bands
      const row = document.createElement('div');
      row.className = 'tl-band';
      const name = document.createElement('span');
      name.textContent = this.layerNames.get(layer.id) || layer.id;
      name.style.cssText = 'min-width:70px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      const track = document.createElement('div');
      track.className = 'tl-band-track';
      // draw segments: visible+playing (blue), visible+paused (amber), hidden (striped)
      let playing = layer.bands.length ? null : true;
      let visible = true;
      let cursor = 0;
      const segs = [];
      for (const b of layer.bands) {
        if (playing !== null) {
          segs.push({ from: cursor, to: b.t, cls: visible ? (playing ? 'play' : 'freeze') : 'hidden-period' });
        }
        if (b.action === 'play') playing = true;
        else if (b.action === 'pause') playing = false;
        else if (b.action === 'visibility_on') visible = true;
        else if (b.action === 'visibility_off') visible = false;
        else if (b.action === 'layer_add') { playing = true; visible = true; }
        else if (b.action === 'layer_remove') { playing = null; }
        cursor = b.t;
      }
      if (playing !== null) segs.push({ from: cursor, to: 1, cls: visible ? (playing ? 'play' : 'freeze') : 'hidden-period' });
      for (const s of segs) {
        if (s.to - s.from <= 0.001) continue;
        const el = document.createElement('div');
        el.className = `tl-band-seg ${s.cls}`;
        el.style.left = `${s.from * 100}%`;
        el.style.width = `${(s.to - s.from) * 100}%`;
        track.appendChild(el);
      }
      row.append(name, track);
      this.els.bandsEl.appendChild(row);
    }
    // event list (latest last, scroll to bottom)
    const list = this.els.eventListEl;
    list.innerHTML = '';
    const events = this.tl.serialize().slice(-80);
    for (const ev of events) {
      const row = document.createElement('div');
      row.className = 'ev';
      const t0 = this.tl.t0 ?? this.tl.events[0]?.wallMs ?? ev.wallMs;
      const t = document.createElement('span');
      t.className = 'ev-t';
      t.textContent = fmtClock(Math.max(0, (ev.wallMs - t0) / 1000));
      const d = document.createElement('span');
      d.textContent = describeEvent(ev, this.layerNames.get(ev.layerId));
      row.append(t, d);
      row.addEventListener('click', () => this.onJump && this.onJump((ev.wallMs - t0) / 1000));
      list.appendChild(row);
    }
    list.scrollTop = list.scrollHeight;
  }
}
