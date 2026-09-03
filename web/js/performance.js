/**
 * performance.js — playback health monitor + auto-degradation (P2-31 … P2-39, P4-14).
 *
 * Metrics: FPS (rVFC/rAF), dropped/decoded frames via getVideoPlaybackQuality(),
 * render time, compositor time, audio drift, camera frame-interval variance.
 * Health: EXCELLENT / GOOD / DEGRADED / CRITICAL.
 * Degradation ladder (preview only — NEVER export settings, GR-11):
 *   resolution scale → FPS cap → lighter proxy → compositor workload → safe mode.
 */

export const HEALTH = { EXCELLENT: 'excellent', GOOD: 'good', DEGRADED: 'degraded', CRITICAL: 'critical' };

export class PerformanceMonitor {
  constructor({ onStatus, onDegrade } = {}) {
    this.onStatus = onStatus || (() => {});
    this.onDegrade = onDegrade || (() => {});
    this.targetFps = 60;
    this.mode = 'normal';          // normal | reduced | light | safe (P2-36)
    this.previewScale = 1.0;       // compositor backing-store scale
    this.fpsCap = null;
    this.metrics = {
      fps: 0, target: 60, droppedWindow: 0, decoded: 0, dropped: 0,
      renderMs: 0, compositorMs: 0, driftMs: 0, cameraJitterMs: 0,
      bufferHealth: 1, status: HEALTH.GOOD,
    };
    this._frames = 0;
    this._lastSample = performance.now();
    this._lastDropped = 0;
    this._degradeSteps = 0;
    this._badWindows = 0;
    this._cameraIntervals = [];
    this._lastCameraTime = 0;
    this.sources = new Map();       // layerId -> {getPlaybackQuality, currentTime, playing}
    this._rafLoop = null;
  }

  start() {
    if (this._rafLoop) return;
    const sample = () => {
      this._sample();
      this._rafLoop = setTimeout(sample, 1000);
    };
    this._rafLoop = setTimeout(sample, 1000);
  }

  stop() { clearTimeout(this._rafLoop); this._rafLoop = null; }

  /** Register a video element for quality sampling (P2-32). */
  watch(layerId, videoEl, { playing = () => true } = {}) {
    this.sources.set(layerId, { videoEl, playing });
  }
  unwatch(layerId) { this.sources.delete(layerId); }

  /** Called by the compositor each rendered frame. */
  frameRendered(renderMs) {
    this._frames++;
    this.metrics.renderMs = renderMs;
  }
  compositorTick(ms) { this.metrics.compositorMs = ms; }

  /** Camera frame callback — track interval variance (P2-34, P4-14). */
  cameraFrame(layerId) {
    const now = performance.now();
    if (this._lastCameraTime) {
      const dt = now - this._lastCameraTime;
      if (dt > 5 && dt < 2000) {
        this._cameraIntervals.push(dt);
        if (this._cameraIntervals.length > 120) this._cameraIntervals.shift();
      }
    }
    this._lastCameraTime = now;
  }

  /** Audio drift report from audio.js (P2-33, P5-10). */
  reportDrift(ms) { this.metrics.driftMs = ms; }

  _sample() {
    const now = performance.now();
    const dt = (now - this._lastSample) / 1000;
    const fps = dt > 0 ? this._frames / dt : 0;
    this._frames = 0;
    this._lastSample = now;

    let decoded = 0, dropped = 0, buffered = 0, bufferedCount = 0;
    for (const { videoEl, playing } of this.sources.values()) {
      if (!videoEl || videoEl.tagName !== 'VIDEO' || !playing()) continue;
      if (typeof videoEl.getVideoPlaybackQuality === 'function') {
        const q = videoEl.getVideoPlaybackQuality();
        decoded += q.totalVideoFrames || 0;
        dropped += q.droppedVideoFrames || 0;
      }
      try {
        if (videoEl.buffered.length) {
          const end = videoEl.buffered.end(videoEl.buffered.length - 1);
          const ahead = end - videoEl.currentTime;
          buffered += Math.max(0, ahead); bufferedCount++;
        }
      } catch { /* element mid-seek */ }
    }
    const droppedWindow = Math.max(0, dropped - this._lastDropped);
    this._lastDropped = dropped;

    // camera jitter = stddev of intervals
    let jitter = 0;
    if (this._cameraIntervals.length > 10) {
      const xs = this._cameraIntervals;
      const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
      jitter = Math.sqrt(xs.reduce((a, b) => a + (b - mean) ** 2, 0) / xs.length);
    }

    const m = this.metrics;
    m.fps = Math.round(fps * 10) / 10;
    m.target = this.fpsCap || this.targetFps;
    m.decoded = decoded; m.dropped = dropped; m.droppedWindow = droppedWindow;
    m.cameraJitterMs = Math.round(jitter * 10) / 10;
    m.bufferHealth = bufferedCount ? buffered / bufferedCount : 1;

    // status classification (P2-35)
    const ratio = fps / (this.fpsCap || this.targetFps);
    if (ratio >= 0.95 && droppedWindow <= 2 && Math.abs(m.driftMs) <= 25) m.status = HEALTH.EXCELLENT;
    else if (ratio >= 0.85 && droppedWindow <= 10) m.status = HEALTH.GOOD;
    else if (ratio >= 0.6) m.status = HEALTH.DEGRADED;
    else m.status = HEALTH.CRITICAL;

    // auto-degradation ladder (P2-36) — bounded, preview-only (P2-37, GR-11)
    if (m.status === HEALTH.DEGRADED || m.status === HEALTH.CRITICAL) {
      this._badWindows++;
      if (this._badWindows >= 3 && this._degradeSteps < 4) {
        this._badWindows = 0;
        this._degrade();
      }
    } else {
      this._badWindows = Math.max(0, this._badWindows - 1);
    }
    this.onStatus({ ...m });
  }

  _degrade() {
    this._degradeSteps++;
    const prev = this.mode;
    if (this.previewScale > 0.66) {
      this.previewScale = 0.66;                     // 1) reduce preview resolution
      this.mode = 'reduced';
    } else if (!this.fpsCap) {
      this.fpsCap = 30;                             // 2) reduce preview FPS
      this.mode = 'reduced';
    } else if (this.previewScale > 0.5) {
      this.previewScale = 0.5; this.mode = 'light'; // 3) lighter proxy / smaller backing store
    } else {
      this.mode = 'safe';                           // 4) safe preview mode
    }
    if (prev !== this.mode || this._degradeSteps <= 4) {
      this.onDegrade({ mode: this.mode, previewScale: this.previewScale, fpsCap: this.fpsCap });
    }
  }

  /** Manual reset after the user fixes conditions (e.g. switched to proxy). */
  reset() {
    this._degradeSteps = 0; this._badWindows = 0;
    this.previewScale = 1.0; this.fpsCap = null; this.mode = 'normal';
  }
}
