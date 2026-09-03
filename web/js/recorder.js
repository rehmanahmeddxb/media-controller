/**
 * recorder.js — take recording (Phase 7).
 *
 * Countdown 3→2→1→RECORD with cancel. canvas.captureStream(fps) video track +
 * mixed audio track into one MediaRecorder. Codec negotiation VP9→VP8→H.264
 * via isTypeSupported. Chunked timeslice capture bounds memory. Recording
 * start defines t0 shared with the event timeline (P7-07). Per-camera
 * recorders run in parallel and upload separately. Uploads stream to the
 * backend with progress; take metadata + timeline JSON attached after.
 */

import { api, uploadWithProgress } from './api.js';

export class Recorder {
  constructor({ compositor, mixer, layerManager, cameraManager, timeline,
                mediaLibrary, countdownEl, countdownNumEl, indicatorEl, timerEl,
                toast, getProjectId, onStateChange, perf } = {}) {
    Object.assign(this, { compositor, mixer, layerManager, cameraManager, timeline,
                          mediaLibrary, toast, onStateChange, perf });
    this._getProjectId = getProjectId || (() => 'default');
    this.countdownEl = countdownEl;
    this.countdownNumEl = countdownNumEl;
    this.indicatorEl = indicatorEl;
    this.timerEl = timerEl;
    this.recording = false;
    this._chunks = [];
    this._recorder = null;
    this._camRecorders = new Map();     // layerId -> {recorder, chunks, meta}
    this._timerInt = null;
    this._t0Wall = null;
    this._takeId = null;
    this._cancelled = false;
  }

  static pickMime(preferences) {
    if (typeof MediaRecorder === 'undefined') return null;
    for (const mime of preferences || []) {
      try { if (MediaRecorder.isTypeSupported(mime)) return mime; } catch { /* keep trying */ }
    }
    return '';
  }

  /** Start flow: countdown then record (P7-01). Resolves when recording starts. */
  async startCountdown({ seconds = 3, fps = 30, preferences = [] } = {}) {
    this._cancelled = false;
    this.countdownEl.hidden = false;
    const cancelBtn = document.createElement('small');
    cancelBtn.textContent = 'tap to cancel';
    this.countdownEl.append(cancelBtn);
    const onCancel = () => { this._cancelled = true; };
    this.countdownEl.addEventListener('click', onCancel, { once: true });
    for (let n = seconds; n > 0; n--) {
      this.countdownNumEl.textContent = String(n);
      for (let i = 0; i < 10; i++) {
        if (this._cancelled) break;
        await sleep(100);
      }
      if (this._cancelled) break;
    }
    this.countdownEl.removeEventListener('click', onCancel);
    cancelBtn.remove();
    this.countdownEl.hidden = true;
    if (this._cancelled) return false;
    this.countdownNumEl.textContent = '●';
    this.begin({ fps, preferences });
    return true;
  }

  begin({ fps = 30, preferences = [] } = {}) {
    if (this.recording) return;
    if (typeof MediaRecorder === 'undefined') {
      this.toast('MediaRecorder is not available in this browser — cannot record.', 'err');
      return;
    }
    const mime = Recorder.pickMime(preferences);
    // composite stream: canvas video + mixed audio (P7-02, P7-03)
    const { stream, restore } = this.compositor.captureStream(fps);
    const audioTrack = this.mixer?.mixedTrack();
    if (audioTrack) stream.addTrack(audioTrack);

    const canvasEl = this.compositor.canvas;
    const bitrate = Math.round(canvasEl.width * canvasEl.height * fps * 0.12);  // P7-05
    this._recorder = new MediaRecorder(stream, {
      mimeType: mime || undefined,
      videoBitsPerSecond: Math.min(24_000_000, Math.max(2_000_000, bitrate)),
      audioBitsPerSecond: 128000,
    });
    this._chunks = [];
    this._recorder.ondataavailable = (e) => { if (e.data && e.data.size) this._chunks.push(e.data); };  // P7-06
    this._recorder.onerror = (e) => {
      this.toast(`Recording error: ${e.error?.message || 'encoder failure'}`, 'err');
      this.stop().catch(() => {});
    };
    this._recorder.start(1000);   // timeslice chunks (P7-06)

    // t0 shared with the event timeline (P7-07)
    this.timeline.beginTake();
    this._t0Wall = Date.now();
    this._takeId = `take_${new Date(this._t0Wall).toISOString().replace(/[:.]/g, '-')}`;

    // per-camera parallel recordings (P7-09, P4-15)
    for (const [layerId, cam] of this.cameraManager.active) {
      try {
        const mime2 = Recorder.pickMime(preferences) || '';
        const r = new MediaRecorder(cam.stream, { mimeType: mime2 || undefined });
        const entry = { recorder: r, chunks: [], layerId, meta: { width: cam.settings.width, height: cam.settings.height } };
        r.ondataavailable = (e) => { if (e.data && e.data.size) entry.chunks.push(e.data); };
        r.start(1000);
        this._camRecorders.set(layerId, entry);
      } catch (err) {
        this.toast(`Camera recording could not start: ${err.message}`, 'warn');
      }
    }

    this.recording = true;
    this._restoreOverlay = restore;
    this.indicatorEl.hidden = false;
    this.onStateChange?.(true);
    this._timerInt = setInterval(() => {
      const sec = (performance.now() - this.timeline.t0) / 1000;
      this.timerEl.textContent = fmt(sec);
      // live size estimate + disk-space guard (P7-08)
      const bytes = this._chunks.reduce((a, c) => a + c.size, 0);
      if (bytes > 8 * 1024 ** 3) {
        this.toast('Recording is very large — consider stopping soon.', 'warn');
      }
    }, 500);
    this.toast('RECORDING — composited video, mixed audio, per-camera takes and timeline are being captured.', 'ok');
  }

  /** Stop flow: flush chunks → blobs → backend upload (P7-10 … P7-12). */
  async stop() {
    if (!this.recording) return null;
    this.recording = false;
    clearInterval(this._timerInt);
    this.indicatorEl.hidden = true;
    const tEndWall = Date.now();
    const takeStartMs = this.timeline.t0;
    const durationSec = (performance.now() - takeStartMs) / 1000;
    this.onStateChange?.(false);
    this._restoreOverlay?.();   // bring editing chrome back (P7-16)

    const uploads = [];
    // composite
    const rec = this._recorder;
    const compositeBlob = await new Promise(resolve => {
      rec.onstop = () => resolve(new Blob(this._chunks, { type: rec.mimeType || 'video/webm' }));
      if (rec.state !== 'inactive') rec.stop(); else resolve(new Blob(this._chunks, { type: 'video/webm' }));
    });
    this._chunks = [];
    this._recorder = null;

    const projectId = this._getProjectId();
    if (compositeBlob.size > 0) {
      const file = new File([compositeBlob], 'composite.webm', { type: compositeBlob.type });
      uploads.push(uploadWithProgress(
        `/api/recording/${encodeURIComponent(projectId)}`, file, 'file',
        null,
        { kind: 'composite', take_id: this._takeId,
          wall_start_ms: takeStartMs, wall_end_ms: takeStartMs + durationSec * 1000,
          codec: compositeBlob.type, width: this.compositor.canvas.width,
          height: this.compositor.canvas.height, fps: 30,
        }).then(r => ({ ...r, kind: 'composite' })).catch(err => {
          this.toast(`Composite upload failed: ${err.message}`, 'err');
          return null;
        }));
    }
    // per-camera
    for (const [layerId, entry] of this._camRecorders) {
      const blob = await new Promise(resolve => {
        entry.recorder.onstop = () => resolve(new Blob(entry.chunks, { type: entry.recorder.mimeType || 'video/webm' }));
        if (entry.recorder.state !== 'inactive') entry.recorder.stop();
        else resolve(new Blob(entry.chunks, { type: 'video/webm' }));
      });
      if (blob.size > 0) {
        const file = new File([blob], `camera_${layerId}.webm`, { type: blob.type });
        uploads.push(uploadWithProgress(
          `/api/recording/${encodeURIComponent(projectId)}`, file, 'file', null,
          { kind: 'camera', take_id: this._takeId, layer_id: layerId,
            wall_start_ms: takeStartMs, wall_end_ms: takeStartMs + durationSec * 1000,
            codec: blob.type, width: entry.meta.width, height: entry.meta.height,
          }).then(r => ({ ...r, kind: 'camera', layerId })).catch(err => {
            this.toast(`Camera upload failed: ${err.message}`, 'warn');
            return null;
          }));
      }
    }
    this._camRecorders.clear();

    const results = (await Promise.all(uploads)).filter(Boolean);
    // take metadata + timeline JSON persisted (P7-13)
    const takeId = results[0]?.take_id || this._takeId;
    try {
      await api.attachTakeMeta(projectId, takeId, {
        wall_start_ms: takeStartMs,
        wall_end_ms: takeStartMs + durationSec * 1000,
        duration_s: durationSec,
        codec: compositeBlob.type,
        resolution: `${this.compositor.canvas.width}x${this.compositor.canvas.height}`,
        fps: 30,
        size_bytes: compositeBlob.size,
        event_count: this.timeline.count,
        timeline: this.timeline.serialize(),
      });
    } catch (err) {
      this.toast(`Take metadata save failed: ${err.message}`, 'warn');
    }
    this.toast(`Take saved (${fmt(durationSec)}). Ready to export.`, 'ok');
    void tEndWall;
    return { takeId, durationSec, results };
  }
}

function fmt(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
