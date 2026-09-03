/**
 * app.js — Ahmed Reaction Studio bootstrap & orchestration.
 *
 * Wires: config → health/system checks → recovery of the last project →
 * modules (timeline, audio, media, cameras, compositor, pip-editor,
 * performance, recorder, ui). Autosaves with debounce; project JSON is
 * versioned; nothing ever leaves this device (GR-01, GR-19).
 */
import { api } from './api.js';
import { Timeline, TimelineUI } from './timeline.js';
import { PerformanceMonitor, HEALTH } from './performance.js';
import { AudioMixer } from './audio.js';
import { MediaLibrary } from './media.js';
import { CameraManager, detectPlatform } from './camera.js';
import { Compositor, CANVAS_SIZES } from './compositor.js';
import { LayerManager, makeLayer, createMediaElement } from './layers.js';
import { PipEditor } from './pip-editor.js';
import { Recorder } from './recorder.js';
import { Toasts, Dialogs, StatusStrip, renderLayerPanel,
         openExportDialog, openJobsDialog, openDiagnosticsDialog, openHelpDialog, $ } from './ui.js';

const AUTOSAVE_DEBOUNCE_MS = 1500;   // P6-18
const PLATFORM = detectPlatform();

class Studio {
  constructor() {
    this.projectId = null;
    this.project = null;
    this.takeDuration = 0;
    this.lastTake = null;           // {takeId, durationSec}
    this._saveTimer = null;
    this._uiTimer = null;
  }

  async init() {
    this._initModules();
    this._bindToolbar();
    this._bindLayerControls();
    this._bindShortcuts();
    await this._checkBackend();
    await this._loadOrCreateProject();
    this._uiTick();   // periodic UI refresh (playback clocks, badges)
    setInterval(() => this._uiTick(), 500);
    window.addEventListener('beforeunload', () => this._cleanup());
    this.toasts.ok('Studio ready — everything runs locally on this device.', 5000);
  }

  _initModules() {
    // toasts / dialogs / status
    this.toasts = new Toasts($('#toasts'));
    this.dialogs = new Dialogs($('#dlg-backdrop'), $('#dlg'));
    this.dialogs.bindClose();
    this.status = new StatusStrip({
      server: $('#st-server'), ffmpeg: $('#st-ffmpeg'), storage: $('#st-storage'),
      health: $('#st-health'), message: $('#st-message'), project: $('#st-project'),
    });

    // master clock + events
    this.timeline = new Timeline();
    this.timelineUI = new TimelineUI(this.timeline, {
      clockEl: $('#tl-clock'), scrubEl: $('#tl-scrub'), countEl: $('#tl-events-count'),
      bandsEl: $('#tl-bands'), eventListEl: $('#tl-eventlist'),
    });
    this.timelineUI.onJump = () => { /* visual jump only — media seek is per-layer */ };

    // health monitor
    this.perf = new PerformanceMonitor({
      onStatus: (m) => this._renderHealth(m),
      onDegrade: ({ mode, previewScale, fpsCap }) => {
        this.compositor.setDegradation({ previewScale, fpsCap });
        this.toasts.warn(`Preview auto-degraded to protect smoothness (mode: ${mode}). Export quality is never affected.`, 7000);
      },
    });
    this.perf.start();

    // audio
    this.mixer = new AudioMixer({ performanceMonitor: this.perf });

    // media library
    this.mediaLibrary = new MediaLibrary({
      toast: (m, k, d) => this.toasts.show(m, k === 'info' ? 'info' : k, d),
      onRegistered: () => this._refresh(),
    });

    // cameras
    this.cameraManager = new CameraManager({
      toast: (m, k, d) => this.toasts.show(m, k === 'info' ? 'info' : k, d),
      perf: this.perf,
      maxSourcesAndroid: 2, maxSourcesWindows: 8,
    });
    this.cameraManager.onDevicesChanged = () => this._refreshCameraOptions();
    this.cameraManager.onSourceLost = (layerId) => {
      const layer = this.lm?.layers.find(l => l.id === layerId);
      if (layer) { layer.statusBadge = 'SOURCE_LOST'; this._refresh(); }
    };

    // compositor
    this.compositor = new Compositor($('#studio-canvas'), {
      onFrameRendered: (ms) => this.perf.frameRendered(ms),
    });
    this.compositor.setAspect('16:9');
    this.compositor._applySize();
    window.addEventListener('resize', () => { this.compositor._applySize(); this.compositor.markDirty(); });

    // layers
    this.lm = new LayerManager({
      timeline: this.timeline, mixer: this.mixer, perf: this.perf,
      onChange: () => this._refresh(),
      onSelect: (layer) => this._renderLayerControls(layer),
      toast: (m, k, d) => this.toasts.show(m, k === 'info' ? 'info' : k, d),
    });

    // pip editor (handles + presets)
    this.pip = new PipEditor(this.compositor, this.lm, {
      guidesEl: $('#snap-guides'), guideV: $('#guide-v'), guideH: $('#guide-h'),
    });

    // recorder
    this.recorder = new Recorder({
      compositor: this.compositor, mixer: this.mixer, layerManager: this.lm,
      cameraManager: this.cameraManager, timeline: this.timeline,
      mediaLibrary: this.mediaLibrary,
      countdownEl: $('#countdown'), countdownNumEl: $('#countdown-num'),
      indicatorEl: $('#rec-indicator'), timerEl: $('#rec-timer'),
      toast: (m, k, d) => this.toasts.show(m, k === 'info' ? 'info' : k, d),
      getProjectId: () => this.projectId || 'default',
      onStateChange: (rec) => {
        $('#btn-record').classList.toggle('recording', rec);
        $('#btn-record').textContent = rec ? '■ Stop' : '● Record';
      },
      perf: this.perf,
    });
  }

  /* ----------------------------- backend checks ---------------------------- */
  async _checkBackend() {
    this.status.msg('Connecting to local server…');
    try {
      const health = await api.health();
      this.status.set('server', 'ok', `server ${health.version}`);
      this.status.msg(health.recovery?.recovered
        ? `Recovered your last session (${health.recovery.message}).`
        : 'Ready.');
      this.recoveryInfo = health.recovery;
      if (health.recovery?.recovered && health.recovery.snapshot) {
        this._pendingRecoveryProject = health.recovery;
      }
      try {
        const sys = await api.system();
        this.status.set('ffmpeg', sys.ffmpeg.available ? 'ok' : 'err',
                        sys.ffmpeg.available ? `ffmpeg ${sys.ffmpeg.version}` : 'ffmpeg missing');
        this.status.set('storage', sys.storage.writable ? 'ok' : 'err',
                        `storage ${sys.storage.writable ? 'ok' : 'read-only'}`);
        this.ffmpegAvailable = sys.ffmpeg.available;
      } catch { this.status.set('ffmpeg', 'warn', 'ffmpeg unknown'); }
    } catch (err) {
      this.status.set('server', 'err', 'server offline');
      this.status.msg(err.message);
      this.toasts.err(err.message);
    }
  }

  /* ------------------------------ project I/O ------------------------------ */
  async _loadOrCreateProject() {
    try {
      const { projects } = await api.listProjects();
      let target = projects[0];
      if (this._pendingRecoveryProject?.project_id &&
          projects.some(p => p.id === this._pendingRecoveryProject.project_id)) {
        target = projects.find(p => p.id === this._pendingRecoveryProject.project_id);
        // prefer the recovered snapshot content
        this._applyProject(this._pendingRecoveryProject.project_id,
                           this._pendingRecoveryProject.snapshot, { recovered: true });
        return;
      }
      if (target) {
        const { project } = await api.loadProject(target.id);
        this._applyProject(target.id, project);
      } else {
        const { project_id, project } = await api.createProject('My Reaction Project');
        this._applyProject(project_id, project);
      }
    } catch (err) {
      this.toasts.err(`Project load failed: ${err.message}`);
      this._applyProject('local', this._defaultProject());
    }
  }

  _defaultProject() {
    return {
      version: 1, name: 'My Reaction Project',
      canvas: { aspect: '16:9', width: 1920, height: 1080, fps: 30, background: 'black' },
      layers: [], timeline: [], audio: { master_volume: 1.0 }, export: {},
    };
  }

  _applyProject(projectId, project, { recovered = false } = {}) {
    this.projectId = projectId;
    this.project = project;
    $('#project-name').value = project.name || 'Untitled';
    const aspect = project.canvas?.aspect || '16:9';
    $('#aspect-select').value = aspect;
    this.compositor.setAspect(aspect);
    this.compositor.background = project.canvas?.background || 'black';
    this.timeline.load(project.timeline || []);
    this.status.project(`${project.name} · ${projectId}`);
    if (recovered) this.toasts.ok('Last session recovered automatically.', 6000);
    // layers with file-backed sources can't be re-attached across sessions
    // (browsers cannot reopen local files without the user) — the layer shell
    // is restored and the source-relink flow prompts for the file (P6-22).
    for (const saved of project.layers || []) {
      const layer = makeLayer({ type: saved.type, name: saved.name });
      Object.assign(layer, {
        visible: saved.visible, locked: saved.locked, muted: saved.muted,
        volume: saved.volume, fit: saved.fit, mirror: saved.mirror,
        geometry: saved.geometry, z: saved.z, mediaId: saved.mediaId, source: saved.source,
        statusBadge: saved.mediaId ? 'RELINK' : null,
      });
      if (saved.mediaId) {
        layer.statusBadge = 'RELINK';
      }
      this.lm.layers.push(layer);
    }
    if (this.lm.layers.some(l => l.statusBadge === 'RELINK')) {
      const n = this.lm.layers.filter(l => l.statusBadge === 'RELINK').length;
      this.toasts.warn(
        `${n} layer(s) need source relinking — select a layer and re-pick its file. ` +
        'Layers are never silently dropped (P6-22).', 9000);
    }
    this._refresh();
  }

  _projectJson() {
    return {
      version: 1,
      name: $('#project-name').value || 'Untitled',
      canvas: {
        aspect: this.compositor.aspect,
        width: CANVAS_SIZES[this.compositor.aspect].width,
        height: CANVAS_SIZES[this.compositor.aspect].height,
        fps: 30, background: this.compositor.background,
      },
      layers: this.lm.serialize(),
      timeline: this.timeline.serialize(),
      audio: { master_volume: 1.0 },
      export: this.lastExportSettings || {},
      last_take: this.lastTake,
    };
  }

  _scheduleSave() {   // debounced autosave (P6-18)
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this._save(), AUTOSAVE_DEBOUNCE_MS);
    api.markDirty(this.projectId).catch(() => {});
  }

  async _save() {
    if (!this.projectId || this.projectId === 'local') return;
    try {
      await api.saveProject(this.projectId, this._projectJson());
      this.status.project(`${$('#project-name').value} · saved ${new Date().toLocaleTimeString()}`);
    } catch (err) {
      this.status.msg(`Autosave failed: ${err.message}`);
    }
  }

  /* -------------------------------- toolbar -------------------------------- */
  _bindToolbar() {
    $('#project-name').addEventListener('change', () => this._scheduleSave());
    $('#aspect-select').addEventListener('change', (e) => {
      this.compositor.setAspect(e.target.value);
      this._scheduleSave();
    });
    $('#btn-play-all').onclick = () => this.lm.playAll();
    $('#btn-pause-all').onclick = () => this.lm.pauseAll();
    $('#btn-reset-all').onclick = () => this.lm.resetAll();
    $('#btn-diag').onclick = () => openDiagnosticsDialog(this.dialogs, { health: api, toast: this.toasts.show.bind(this.toasts) });
    $('#btn-help').onclick = () => openHelpDialog(this.dialogs);
    $('#btn-jobs').onclick = () => openJobsDialog(this.dialogs, { projectId: this.projectId, toast: this.toasts.show.bind(this.toasts) });
    $('#btn-record').onclick = () => this._toggleRecord();
    $('#btn-export').onclick = () => this._openExport();

    $('#add-local-video').onclick = () => this._addLocalVideo();
    $('#add-image').onclick = () => this._addImage();
    $('#add-audio').onclick = () => this._addAudio();
    $('#add-camera').onclick = () => this._addCamera();

    // presets bar (P3-42/43)
    $('#preset-bar').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-preset]');
      if (!btn) return;
      const layer = this.lm.selected || this.lm.layers[this.lm.layers.length - 1];
      if (!layer) { this.toasts.info('Add a layer first.'); return; }
      this.pip.applyPreset(btn.dataset.preset, layer);
      this._scheduleSave();
    });
  }

  async _addLocalVideo() {
    const files = await this.mediaLibrary.pick({ accept: ['video/*'] });
    for (const file of files) {
      try {
        this.status.msg(`Probing ${file.name}…`);
        const item = await this.mediaLibrary.ingest(file, {
          onProgress: (p) => this.status.msg(`Uploading ${file.name}… ${Math.round(p * 100)}%`),
        });
        const el = createMediaElement('video');
        el.src = this.mediaLibrary.playbackUrl(item);
        el.muted = true; el.volume = 1;   // audio routed through the mixer
        el.load();
        const layer = makeLayer({ type: 'video', name: item.name.replace(/\.[^.]+$/, ''), mediaId: item.id, element: el });
        // first layer fills the canvas; later ones land bottom-right (P3-16)
        if (this.lm.layers.length > 0) layer.geometry = { x: 0.66, y: 0.66, w: 0.3, h: 0.3 };
        el.addEventListener('loadedmetadata', () => {
          this.mixer.attachElement(layer.id, el);
          this.mixer.setVolume(layer.id, layer.volume);
          el.play().then(() => { layer.playing = true; this._refresh(); }).catch(() => {});
          this.perf.watch(layer.id, el);
          this._refresh();
        });
        el.addEventListener('ended', () => { layer.playing = false; this._refresh(); });
        this.lm.add(layer);
        this._hideEmpty();
        this.status.msg(`Added ${file.name}`);
      } catch (err) {
        this.toasts.err(`${file.name}: ${err.message}`);
      }
    }
    this._scheduleSave();
  }

  async _addImage() {
    const files = await this.mediaLibrary.pick({ accept: ['image/*'] });
    for (const file of files) {
      const url = this.mediaLibrary.objectUrl(file);
      const img = createMediaElement('img');
      img.src = url;
      const layer = makeLayer({ type: 'image', name: file.name.replace(/\.[^.]+$/, ''), element: img });
      layer.geometry = this.lm.layers.length ? { x: 0.05, y: 0.05, w: 0.3, h: 0.3 } : { x: 0, y: 0, w: 1, h: 1 };
      img.onload = () => { this._refresh(); this._hideEmpty(); };
      this.lm.add(layer);
      this._hideEmpty();
    }
    this._scheduleSave();
  }

  async _addAudio() {
    const files = await this.mediaLibrary.pick({ accept: ['audio/*'] });
    for (const file of files) {
      try {
        const item = await this.mediaLibrary.ingest(file);
        const el = createMediaElement('audio');
        el.src = this.mediaLibrary.playbackUrl(item);
        el.load();
        const layer = makeLayer({ type: 'audio', name: file.name.replace(/\.[^.]+$/, ''), mediaId: item.id, element: el });
        el.addEventListener('loadedmetadata', () => {
          this.mixer.attachElement(layer.id, el);
          this.mixer.setVolume(layer.id, 1);
          el.play().catch(() => {});
          this._refresh();
        });
        this.lm.add(layer);
        this.toasts.info('Audio-only layer added (P5-13) — it mixes without a visual.');
      } catch (err) { this.toasts.err(`${file.name}: ${err.message}`); }
    }
    this._scheduleSave();
  }

  async _addCamera() {
    await this.cameraManager.refreshDevices();
    if (!this.cameraManager.devices.length) {
      this.toasts.warn('No camera devices found. Connect a camera and retry.');
      return;
    }
    // camera picker dialog (P4-11)
    const el = this.dialogs.open(`
      <h2>Add camera</h2>
      <p class="muted">Platform: ${PLATFORM} · limit: ${this.cameraManager.maxSources} simultaneous · active: ${this.cameraManager.activeCount}</p>
      <div class="field"><span>Device</span><select id="cam-device">
        ${this.cameraManager.devices.map(d => `<option value="${d.deviceId}">${d.label}</option>`).join('')}
      </select></div>
      <label class="check"><input type="checkbox" id="cam-audio" checked> Include this camera's microphone</label>
      <div class="dlg-actions">
        <button class="btn" id="cam-cancel">Cancel</button>
        <button class="btn btn-primary" id="cam-add">Add</button>
      </div>`);
    $('#cam-cancel', el).onclick = () => this.dialogs.close();
    $('#cam-add', el).onclick = async () => {
      const deviceId = $('#cam-device', el).value || null;
      const withAudio = $('#cam-audio', el).checked;
      this.dialogs.close();
      await this._openCameraLayer(deviceId, withAudio);
    };
  }

  async _openCameraLayer(deviceId, withAudio) {
    // create the layer first so the camera manager keys the stream by layer id
    const layer = makeLayer({ type: 'camera', name: 'Camera', source: deviceId });
    let stream = null;
    try {
      this.status.msg('Opening camera…');
      stream = await this.cameraManager.open(layer.id, { deviceId, audio: withAudio });
      if (!stream) return;
      const label = this.cameraManager.devices.find(
        d => d.deviceId === (deviceId || this.cameraManager.active.get(layer.id)?.deviceId))?.label;
      if (label) layer.name = label;
      const video = createMediaElement('video');
      video.srcObject = stream;
      video.muted = true;   // audio via mixer, not the element
      video.play().catch(() => {});
      layer.element = video;
      layer.mirror = true;  // front cameras mirror by default (P4-13)
      this.lm.add(layer);
      if (withAudio) {
        this.mixer.attachStream(layer.id, stream);
        this.mixer.setVolume(layer.id, 1);
      }
      this.perf.watch(layer.id, video);
      this._hideEmpty();
      this.status.msg(`Camera added (${this.cameraManager.activeCount}/${this.cameraManager.maxSources})`);
    } catch (err) {
      this.cameraManager.close(layer.id);   // release any partial stream (P4-12)
      this.toasts.err(err.message, 9000);
      this.status.msg('Camera failed.');
    }
  }

  _refreshCameraOptions() { /* live device list refresh (P4-02) — picker reads it on open */ }

  /* --------------------------- layer controls panel ------------------------ */
  _bindLayerControls() {
    const bind = (sel, fn) => $(sel).addEventListener('input', (e) => fn(e));
    $('#lc-name').addEventListener('change', (e) => {
      if (this.lm.selectedId) { this.lm.rename(this.lm.selectedId, e.target.value); this._scheduleSave(); }
    });
    $('#lc-fit').addEventListener('change', (e) => {
      if (this.lm.selectedId) { this.lm.setFit(this.lm.selectedId, e.target.value); this._scheduleSave(); }
    });
    $('#lc-aspect-lock').addEventListener('change', (e) => {
      const l = this.lm.selected; if (l) l.aspectLock = e.target.checked;
    });
    $('#lc-mirror').addEventListener('change', (e) => {
      const l = this.lm.selected;
      if (l) { l.mirror = e.target.checked; this.compositor.markDirty(); this._scheduleSave(); }
    });
    bind('#lc-volume', (e) => {
      if (this.lm.selectedId) this.lm.setVolume(this.lm.selectedId, Number(e.target.value));
    });
    $('#lc-mute').onclick = () => {
      const l = this.lm.selected; if (l) this.lm.setMuted(l.id, !l.muted);
    };
    for (const [id, key] of [['#lc-x', 'x'], ['#lc-y', 'y'], ['#lc-w', 'w'], ['#lc-h', 'h']]) {
      $(id).addEventListener('change', (e) => {
        const l = this.lm.selected;
        if (!l) return;
        const g = { ...l.geometry, [key]: Number(e.target.value) };
        this.lm.setGeometry(l.id, g);
        this._scheduleSave();
      });
    }
    $('#lc-play').onclick = () => this.lm.selectedId && this.lm.play(this.lm.selectedId);
    $('#lc-pause').onclick = () => this.lm.selectedId && this.lm.pause(this.lm.selectedId);
    $('#lc-seek-back').onclick = () => { const l = this.lm.selected; if (l) this.lm.seek(l.id, (l.element?.currentTime || 0) - 5); };
    $('#lc-seek-fwd').onclick = () => { const l = this.lm.selected; if (l) this.lm.seek(l.id, (l.element?.currentTime || 0) + 5); };
    $('#lc-duplicate').onclick = () => { if (this.lm.selectedId) { this.lm.duplicate(this.lm.selectedId); this._scheduleSave(); } };
    $('#lc-delete').onclick = () => { if (this.lm.selectedId) { this.lm.remove(this.lm.selectedId); this._scheduleSave(); } };
    // source selector (relink / swap) (P3-25, P6-22)
    $('#lc-source').addEventListener('change', async (e) => {
      const layer = this.lm.selected;
      if (!layer || !e.target.value) return;
      if (e.target.value === '__relink__') {
        const files = await this.mediaLibrary.pick({ multiple: false, accept: ['video/*', 'image/*', 'audio/*'] });
        if (files[0]) await this._relinkLayer(layer, files[0]);
      }
    });
  }

  async _relinkLayer(layer, file) {
    try {
      const item = await this.mediaLibrary.ingest(file);
      const kind = item.metadata.has_video ? 'video' : item.metadata.has_audio ? 'audio' : 'image';
      const el = createMediaElement(kind === 'video' ? 'video' : kind === 'audio' ? 'audio' : 'img');
      if (kind !== 'image') { el.src = this.mediaLibrary.playbackUrl(item); el.muted = true; el.load(); }
      else el.src = this.mediaLibrary.playbackUrl(item);
      this.mixer.detach(layer.id);
      layer.element = el;
      layer.mediaId = item.id;
      layer.type = kind;
      layer.statusBadge = null;
      if (kind !== 'image') {
        el.addEventListener('loadedmetadata', () => {
          this.mixer.attachElement(layer.id, el);
          this.mixer.setVolume(layer.id, layer.volume);
          el.play().catch(() => {});
          this.perf.watch(layer.id, el);
          this._refresh();
        });
      }
      this.timeline.emit(layer.id, 'source_change', { payload: { mediaId: item.id, source: item.id } });
      this.toasts.ok(`Relinked "${layer.name}" to ${file.name}.`);
      this._refresh();
      this._scheduleSave();
    } catch (err) {
      this.toasts.err(`Relink failed: ${err.message}`);
    }
  }

  _renderLayerControls(layer) {
    const body = $('#layer-controls-body');
    const none = $('#no-selection');
    if (!layer) { body.hidden = true; none.hidden = false; return; }
    body.hidden = false; none.hidden = true;
    $('#lc-name').value = layer.name;
    $('#lc-fit').value = layer.fit || 'contain';
    $('#lc-aspect-lock').checked = !!layer.aspectLock;
    $('#lc-mirror').checked = !!layer.mirror;
    $('#lc-volume').value = String(layer.volume);
    $('#lc-mute').textContent = layer.muted ? '🔇' : '🔊';
    for (const [id, key] of [['#lc-x', 'x'], ['#lc-y', 'y'], ['#lc-w', 'w'], ['#lc-h', 'h']]) {
      $(id).value = String(layer.geometry[key]);
    }
    // source dropdown: registered media + relink option
    const sel = $('#lc-source');
    sel.innerHTML = '';
    if (layer.mediaId && this.mediaLibrary.get(layer.mediaId)) {
      const opt = document.createElement('option');
      opt.textContent = this.mediaLibrary.get(layer.mediaId).name;
      opt.selected = true;
      sel.appendChild(opt);
    } else {
      const opt = document.createElement('option');
      opt.textContent = layer.statusBadge === 'RELINK' ? '⚠ needs relink' : layer.type;
      opt.selected = true;
      sel.appendChild(opt);
    }
    const relink = document.createElement('option');
    relink.value = '__relink__';
    relink.textContent = '⟳ pick another file…';
    sel.appendChild(relink);
    $('#lc-media-card').innerHTML = this.mediaLibrary.card(this.mediaLibrary.get(layer.mediaId));
  }

  /* ------------------------------- shortcuts ------------------------------- */
  _bindShortcuts() {
    document.addEventListener('keydown', (e) => {
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) || e.target.isContentEditable) return;
      const layer = this.lm.selected;
      if (e.key === 'Delete' && layer) { this.lm.remove(layer.id); this._scheduleSave(); return; }
      if (e.key === ' ') {
        e.preventDefault();
        if (layer && layer.type !== 'image') {
          layer.playing ? this.lm.pause(layer.id) : this.lm.play(layer.id);
        }
        return;
      }
      if (layer) this.pip.key(e, layer);
    });
  }

  /* ------------------------------ record/export ---------------------------- */
  async _toggleRecord() {
    if (this.recorder.recording) {
      const t0 = this.timeline.t0;   // absolute performance.now() of record start
      const res = await this.recorder.stop();
      if (res) {
        this.lastTake = { takeId: res.takeId, durationSec: res.durationSec, t0 };
        this.takeDuration = res.durationSec;
        this.timelineUI.duration = res.durationSec;
        this._scheduleSave();
      }
      return;
    }
    if (!this.lm.layers.length) { this.toasts.warn('Add at least one layer before recording.'); return; }
    const started = await this.recorder.startCountdown({ seconds: 3, fps: 30 });
    if (!started) this.toasts.info('Countdown cancelled.');
  }

  async _openExport() {
    if (!this.lastTake) {
      this.toasts.warn('Record a take first — exports render the recorded timeline.');
      return;
    }
    await openExportDialog(this.dialogs, {
      project: this.project,
      defaults: { format: 'mp4', resolution: '1080p', fps: 30 },
      toast: (m, k, d) => this.toasts.show(m, k, d),
      onSubmit: (settings) => this._submitExport(settings),
    });
  }

  async _submitExport(settings) {
    this.lastExportSettings = settings;
    if (!this.lastTake?.t0) {
      this.toasts.warn('Take timing information is missing — record a new take first.', 'warn');
      return;
    }
    try {
      const { job_id } = await api.createExport({
        project_id: this.projectId,
        settings,
        project: this._projectJson(),
        timeline: this.timeline.serialize(),
        layers: this.lm.serialize(),
        take_start_ms: this.lastTake.t0,
        take_end_ms: this.lastTake.t0 + this.takeDuration * 1000,
        media_ids: Object.fromEntries(this.lm.layers.filter(l => l.mediaId).map(l => [l.id, l.mediaId])),
        take_id: this.lastTake.takeId,
        name: $('#project-name').value,
      });
      this.toasts.ok('Export queued — watch progress in the Jobs panel.', 6000);
      openJobsDialog(this.dialogs, { projectId: this.projectId, toast: (m, k, d) => this.toasts.show(m, k, d) });
    } catch (err) {
      this.toasts.err(err.message, 10000);
    }
  }

  /* --------------------------------- refresh ------------------------------- */
  _refresh() {
    this.compositor.setLayers(this.lm.layers);
    this.compositor.selectedId = this.lm.selectedId;
    this.timelineUI.setLayerNames(new Map(this.lm.layers.map(l => [l.id, l.name])));
    this.timelineUI.render();
    renderLayerPanel(this.lm, {
      listEl: $('#layer-list'), countEl: $('#layer-count'),
      onSelect: (id) => this.lm.select(id),
    }, {
      mediaLibrary: this.mediaLibrary,
      onRename: (id, name) => { this.lm.rename(id, name); this._scheduleSave(); },
      onReorder: (from, to) => { this.lm.reorder(from, to); this._scheduleSave(); },
      onToggle: (id, v) => { this.lm.setVisible(id, v); this._scheduleSave(); },
      onMute: (id, v) => this.lm.setMuted(id, v),
      onPlayPause: (id) => { const l = this.lm.layers.find(x => x.id === id); l?.playing ? this.lm.pause(id) : this.lm.play(id); },
      onVolume: (id, v) => this.lm.setVolume(id, v),
      onRemove: (id) => { this.lm.remove(id); this._scheduleSave(); },
      onDuplicate: (id) => { this.lm.duplicate(id); this._scheduleSave(); },
    });
    this._renderLayerControls(this.lm.selected);
    $('#layer-count').textContent = String(this.lm.layers.length);
    this._scheduleSave();
  }

  _uiTick() {
    // layer time readouts
    document.querySelectorAll('.layer-row').forEach((row) => {
      const layer = this.lm.layers.find(l => l.id === row.dataset.layerId);
      const timeEl = row.querySelector('[data-role="time"]');
      if (layer && timeEl && layer.element?.currentTime !== undefined && isFinite(layer.element.currentTime)) {
        const dur = isFinite(layer.element.duration) ? layer.element.duration.toFixed(1) : '∞';
        timeEl.textContent = `${layer.element.currentTime.toFixed(1)}/${dur}s`;
      }
    });
    const lcTime = $('#lc-time');
    const sel = this.lm.selected;
    if (sel && sel.element?.currentTime !== undefined && isFinite(sel.element.currentTime)) {
      lcTime.textContent = `${sel.element.currentTime.toFixed(1)}s`;
    }
  }

  _renderHealth(m) {
    $('#h-fps').textContent = m.fps?.toFixed?.(1) ?? m.fps;
    $('#h-target').textContent = m.target;
    $('#h-dropped').textContent = `${m.droppedWindow}/${m.dropped}`;
    $('#h-drift').textContent = `${m.driftMs?.toFixed?.(1) ?? m.driftMs} ms`;
    $('#h-render').textContent = `${(m.renderMs || 0).toFixed(1)} ms`;
    $('#h-mode').textContent = this.perf.mode;
    const strip = $('#health-strip');
    strip.className = `health-strip ${m.status}`;
    const label = { excellent: 'EXCELLENT', good: 'GOOD', degraded: 'DEGRADED', critical: 'CRITICAL' }[m.status] || m.status;
    this.status.set('health',
      m.status === HEALTH.EXCELLENT || m.status === HEALTH.GOOD ? 'ok' :
      m.status === HEALTH.DEGRADED ? 'warn' : 'err', label.toLowerCase());
  }

  _hideEmpty() {
    $('#canvas-empty').classList.toggle('hidden', this.lm.layers.length > 0);
  }

  _cleanup() {
    // release cameras so the light turns off (P4-12)
    this.cameraManager.closeAll();
    this.compositor.stop();
    this.perf.stop();
    clearTimeout(this._saveTimer);
  }
}

const studio = new Studio();
studio.init().catch(err => {
  console.error('boot failed', err);
  document.body.insertAdjacentHTML('afterbegin',
    `<div style="padding:12px;background:#ff5d73;color:#fff">Boot failed: ${err.message}</div>`);
});
window.studio = studio;   // debugging console access
