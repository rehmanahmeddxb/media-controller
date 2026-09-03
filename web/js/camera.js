/**
 * camera.js — camera enumeration, policies and lifecycle (Phase 4).
 *
 * Android: hard cap of 2 simultaneous camera sources (GR-15/NN-16) with an
 * honest message — no fake multi-cam. Windows: capability-based limits.
 * Constraint ladder 1080p→720p→480p, full error taxonomy, device-change
 * listener, device-lost detection (SOURCE_LOST), clean teardown (light off),
 * front-camera mirroring, per-camera recording streams + audio.
 */

const LADDER = [
  { width: 1920, height: 1080, frameRate: 30 },
  { width: 1280, height: 720, frameRate: 30 },
  { width: 640, height: 480, frameRate: 30 },
];  // mirror of config; kept locally for instant fallback (P4-07)

export function detectPlatform() {
  const ua = navigator.userAgent || '';
  if (/Android/i.test(ua)) return 'android';
  if (/Windows/i.test(ua)) return 'windows';
  return 'other';
}

export const CAMERA_ERRORS = {
  NotAllowedError: 'Camera permission was denied. Allow camera access in your browser (the padlock icon in the address bar) and try again.',
  NotFoundError: 'No camera device was found. Connect a camera and try again.',
  NotReadableError: 'The camera is busy (another app may be using it). Close other camera apps and try again.',
  OverconstrainedError: 'The camera cannot satisfy the requested resolution. A lower resolution was tried.',
  AbortError: 'The camera start was interrupted. Try again.',
  SecurityError: 'Camera access was blocked by browser security policy. Use https:// or localhost.',
};

export class CameraManager {
  constructor({ toast, maxSourcesAndroid = 2, maxSourcesWindows = 8, perf } = {}) {
    this.toast = toast || (() => {});
    this.perf = perf || null;
    this.platform = detectPlatform();
    this.maxSources = this.platform === 'android' ? maxSourcesAndroid : maxSourcesWindows;
    this.devices = [];                 // [{deviceId, label, backwards}]
    this.active = new Map();           // layerId -> { stream, deviceId, settings, ended }
    this.onDevicesChanged = null;
    this.onSourceLost = null;
    navigator.mediaDevices?.addEventListener?.('devicechange', () => this.refreshDevices());  // P4-02
  }

  get activeCount() { return this.active.size; }

  /** Enumerate videoinput devices; labels fill in after permission (P4-01). */
  async refreshDevices() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      this.devices = devices
        .filter(d => d.kind === 'videoinput')
        .map((d, i) => ({
          deviceId: d.deviceId,
          label: d.label || `Camera ${i + 1}`,
          facing: d.facingMode || null,
        }));
      this.onDevicesChanged?.(this.devices);
      return this.devices;
    } catch {
      return [];
    }
  }

  /** Policy gate with an explicit, honest message (P4-04, P4-05). */
  canOpenAnother() {
    if (this.active.size >= this.maxSources) {
      if (this.platform === 'android' && this.maxSources <= 2) {
        this.toast(
          `Android supports at most ${this.maxSources} simultaneous camera sources in this studio. ` +
          'Remove a camera layer before adding another — this is a device limit, not a bug.',
          'warn', 8000);
      } else {
        this.toast(`Camera limit reached (${this.maxSources} active). Free one first.`, 'warn');
      }
      return false;
    }
    return true;
  }

  /**
   * Open a camera for a layer with the constraint ladder (P4-07, P4-08):
   * ideal 1080p -> 720p -> 480p; exact:false everywhere.
   */
  async open(layerId, { deviceId = null, audio = false } = {}) {
    if (!this.canOpenAnother()) return null;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('This browser does not support camera access (getUserMedia).');
    }
    let lastErr = null;
    for (const rung of LADDER) {
      const constraints = {
        video: {
          width: { ideal: rung.width },
          height: { ideal: rung.height },
          frameRate: { ideal: rung.frameRate, max: 60 },
          ...(deviceId ? { deviceId: { exact: deviceId } } : { facingMode: 'user' }),
        },
        audio,
      };
      try {
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        this._register(layerId, stream, deviceId, audio);
        await this.refreshDevices();   // labels now available
        return stream;
      } catch (err) {
        lastErr = err;
        if (err.name === 'OverconstrainedError' || err.name === 'NotFoundError') {
          if (deviceId) { deviceId = null; continue; }  // device vanished — try default
          continue;  // ladder down (P4-07)
        }
        if (['NotAllowedError', 'SecurityError', 'NotReadableError', 'AbortError'].includes(err.name)) {
          break;  // deterministic — do not hammer the ladder
        }
      }
    }
    const friendly = CAMERA_ERRORS[lastErr?.name] || `Camera failed: ${lastErr?.message || 'unknown error'}`;
    throw new Error(friendly);
  }

  _register(layerId, stream, deviceId, withAudio) {
    const [track] = stream.getVideoTracks();
    const settings = track?.getSettings?.() || {};
    const rec = { stream, deviceId: deviceId || settings.deviceId || null, settings, ended: false, withAudio };
    this.active.set(layerId, rec);
    track?.addEventListener('ended', () => this._handleLost(layerId));  // P4-10
    // frame-stability feed (P4-14)
    if (this.perf && track && typeof track.requestVideoFrameCallback === 'function') {
      const cb = () => {
        this.perf.cameraFrame(layerId);
        if (this.active.has(layerId)) track.requestVideoFrameCallback(cb);
      };
      track.requestVideoFrameCallback(cb);
    }
    console.info('[camera] opened for layer', layerId, settings);
  }

  _handleLost(layerId) {
    const rec = this.active.get(layerId);
    if (!rec || rec.ended) return;
    rec.ended = true;
    this.active.delete(layerId);
    this.toast('A camera was disconnected. Its layer is marked SOURCE_LOST; other layers keep running.', 'err', 9000);
    this.onSourceLost?.(layerId);   // app marks layer SOURCE_LOST (P4-17)
  }

  /** Clean teardown so the camera light turns off (P4-12). */
  close(layerId) {
    const rec = this.active.get(layerId);
    if (!rec) return;
    rec.ended = true;
    for (const t of rec.stream.getTracks()) {
      try { t.stop(); } catch { /* already stopped */ }
    }
    this.active.delete(layerId);
  }

  closeAll() { for (const id of [...this.active.keys()]) this.close(id); }

  isFrontFacing(layerId) {
    const rec = this.active.get(layerId);
    if (!rec) return false;
    const f = rec.settings.facingMode || this.devices.find(d => d.deviceId === rec.deviceId)?.facing;
    return f === 'user' || !f;  // default assumption: front (P4-13)
  }

  /** Stream for per-camera parallel recording (P4-15). */
  recordingStream(layerId) { return this.active.get(layerId)?.stream || null; }
}
