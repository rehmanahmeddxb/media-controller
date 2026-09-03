/**
 * audio.js — per-layer Web Audio mixer (P5-01 … P5-15).
 *
 * One shared AudioContext. Each layer: MediaElementSource (created ONCE per
 * element, P5-03) → GainNode → master bus → destination + MediaStreamDestination
 * for recording. Equal-power volume curve. Mute via gain=0 (graph intact).
 * Camera streams enter the same mixer. Master meter + clipping indicator.
 */
import { PerformanceMonitor } from './performance.js';

export class AudioMixer {
  constructor({ performanceMonitor = null, driftToleranceMs = 45 } = {}) {
    this.ctx = null;
    this.masterGain = null;
    this.mixDestination = null;   // MediaStreamAudioDestinationNode (P5-07)
    this.analyser = null;
    this.layers = new Map();      // layerId -> { gain, srcNode?, mediaEl?, streamNode? }
    this.perf = performanceMonitor;
    this.driftToleranceMs = driftToleranceMs;
    this._meterData = null;
    this._clip = false;
    this._driftCheckTimer = null;
    this._resumeBound = () => this.resume();
  }

  /** Lazily create the shared context; resume on first user gesture (P5-01/02). */
  ensure() {
    if (!this.ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      this.ctx = new Ctx({ latencyHint: 'interactive' });
      this.masterGain = this.ctx.createGain();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.mixDestination = this.ctx.createMediaStreamDestination();
      this.masterGain.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);      // monitoring
      this.masterGain.connect(this.mixDestination);     // recording feed (P5-07)
      this._meterData = new Uint8Array(this.analyser.fftSize);
      document.addEventListener('pointerdown', this._resumeBound, { once: false });
      document.addEventListener('keydown', this._resumeBound, { once: false });
      this.startDriftCheck();
    }
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
    return this.ctx;
  }

  resume() { if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume().catch(() => {}); }

  /** Attach a media element layer — source node created exactly once (P5-03). */
  attachElement(layerId, mediaEl) {
    this.ensure();
    if (this.layers.has(layerId)) {
      const existing = this.layers.get(layerId);
      if (existing.mediaEl === mediaEl) return existing;   // reuse, never recreate
      this.detach(layerId);
    }
    const srcNode = this.ctx.createMediaElementSource(mediaEl);
    const gain = this.ctx.createGain();
    srcNode.connect(gain);
    gain.connect(this.masterGain);
    const entry = { gain, srcNode, mediaEl };
    this.layers.set(layerId, entry);
    return entry;
  }

  /** Attach a camera/live stream (P5-08). */
  attachStream(layerId, stream) {
    this.ensure();
    if (this.layers.has(layerId)) this.detach(layerId);
    const srcNode = this.ctx.createMediaStreamSource(stream);
    const gain = this.ctx.createGain();
    srcNode.connect(gain);
    gain.connect(this.masterGain);
    const entry = { gain, srcNode, stream };
    this.layers.set(layerId, entry);
    return entry;
  }

  /** Explicit disconnect — no node leaks (P5-09). */
  detach(layerId) {
    const entry = this.layers.get(layerId);
    if (!entry) return;
    try { entry.gain.disconnect(); } catch { /* already gone */ }
    try { if (entry.srcNode) entry.srcNode.disconnect(); } catch { /* ignore */ }
    this.layers.delete(layerId);
  }

  /** Equal-power perceptual volume (P5-06): sin taper — 0.5 → −3 dB. */
  setVolume(layerId, v) {
    const entry = this.layers.get(layerId);
    if (!entry) return;
    const x = Math.max(0, Math.min(1, v));
    entry.gain.gain.value = Math.sin(x * Math.PI / 2);
    entry.volume = x;
    entry.muted = false;
  }

  /** Mute via gain — graph stays intact (P5-05). */
  setMuted(layerId, muted) {
    const entry = this.layers.get(layerId);
    if (!entry) return;
    entry.muted = muted;
    entry.gain.gain.value = muted ? 0 : (entry.volume ?? 1);
  }

  setMasterVolume(v) {
    this.ensure();
    this.masterGain.gain.value = Math.max(0, Math.min(1, v));
  }

  /** Master level meter + clipping indicator (P5-12). */
  levels() {
    if (!this.analyser) return { rms: 0, peak: 0, clipping: this._clip };
    this.analyser.getByteTimeDomainData(this._meterData);
    let peak = 0, sum = 0;
    for (let i = 0; i < this._meterData.length; i++) {
      const v = Math.abs(this._meterData[i] - 128) / 128;
      if (v > peak) peak = v;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / this._meterData.length);
    if (peak >= 0.99) { this._clip = true; setTimeout(() => { this._clip = false; }, 1500); }
    return { rms, peak, clipping: this._clip };
  }

  /** Recording-ready mixed audio track (P5-07). */
  mixedTrack() {
    this.ensure();
    return this.mixDestination.stream.getAudioTracks()[0] || null;
  }

  /**
   * Drift measurement vs the master clock (P5-10): compare a playing video's
   * currentTime against wall-time progress since a reference point.
   * Bounded correction strategy: report only; correction is user/preset driven.
   */
  startDriftCheck() {
    if (this._driftCheckTimer) return;
    this._ref = new Map(); // layerId -> {t: mediaTime, wall: performance.now()}
    this._driftCheckTimer = setInterval(() => {
      if (!this.perf) return;
      let worst = 0;
      for (const [layerId, entry] of this.layers) {
        const el = entry.mediaEl;
        if (!el || el.paused || el.seeking) { this._ref.delete(layerId); continue; }
        const now = performance.now();
        let ref = this._ref.get(layerId);
        if (!ref || el.currentTime < ref.t - 0.05) { // seek happened — reset reference
          this._ref.set(layerId, { t: el.currentTime, wall: now });
          continue;
        }
        const driftSec = (el.currentTime - ref.t) - (now - ref.wall) / 1000;
        // re-anchor every ~5s to keep the window small
        if (now - ref.wall > 5000) this._ref.set(layerId, { t: el.currentTime, wall: now });
        worst = Math.max(worst, Math.abs(driftSec) * 1000);
      }
      this.perf.reportDrift(Math.round(worst * 10) / 10);
    }, 1000);
  }

  destroy() {
    clearInterval(this._driftCheckTimer);
    this._driftCheckTimer = null;
    for (const id of [...this.layers.keys()]) this.detach(id);
    document.removeEventListener('pointerdown', this._resumeBound);
    document.removeEventListener('keydown', this._resumeBound);
    if (this.ctx) { this.ctx.close().catch(() => {}); this.ctx = null; }
  }
}

/** Audio-only layer support hook (P5-13): an element with no visual. */
export function isAudioOnlyLayer(layer) { return layer.type === 'audio'; }
