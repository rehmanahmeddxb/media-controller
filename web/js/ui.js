/**
 * ui.js — toasts, dialogs, export UI, jobs list, diagnostics panel,
 * status strip, layer panel rendering (P3-27, P3-46 … P3-50, P9-31 … P9-33).
 * Pure vanilla DOM — no frameworks (GR-05).
 */
import { api } from './api.js';

export function $(sel, root = document) { return root.querySelector(sel); }

/* --------------------------------- toasts -------------------------------- */
export class Toasts {
  constructor(container) { this.el = container; }
  show(message, kind = 'info', ms = 4500, title = null) {
    const t = document.createElement('div');
    t.className = `toast ${kind}`;
    t.innerHTML = `${title ? `<b>${escapeHtml(title)}</b>` : ''}<span>${escapeHtml(message)}</span>`;
    this.el.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 350); }, ms);
    return t;
  }
  info(m, d) { return this.show(m, 'info', d); }
  ok(m, d) { return this.show(m, 'ok', d); }
  warn(m, d) { return this.show(m, 'warn', d || 6500); }
  err(m, d) { return this.show(m, 'err', d || 8000); }
}

/* -------------------------------- dialogs --------------------------------- */
export class Dialogs {
  constructor(backdrop, dialogEl) { this.backdrop = backdrop; this.el = dialogEl; }
  open(html, { onClose = null } = {}) {
    this.el.innerHTML = html;
    this.backdrop.hidden = false;
    this._onClose = onClose;
    return this.el;
  }
  close() {
    this.backdrop.hidden = true;
    this.el.innerHTML = '';
    if (this._onClose) { const cb = this._onClose; this._onClose = null; cb(); }
  }
  bindClose() {
    this.backdrop.onclick = (e) => { if (e.target === this.backdrop) this.close(); };
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') this.close(); });
  }
}

/* ------------------------------ status strip ------------------------------ */
export class StatusStrip {
  constructor({ server, ffmpeg, storage, health, message, project }) {
    this.els = { server, ffmpeg, storage, health, message, project };
  }
  set(name, state, text) {
    const el = this.els[name];
    el.className = `status-chip ${state}`;
    el.textContent = `● ${text}`;
  }
  msg(text) { this.els.message.textContent = text; }
  project(text) { this.els.project.textContent = text; }
}

/* ----------------------------- layer panel rows --------------------------- */
const KIND_ICON = { video: '🎞', camera: '📷', image: '🖼', audio: '🎵' };

export function renderLayerPanel(layerManager, { listEl, countEl, onSelect }, { mediaLibrary, onRename, onReorder, onToggle, onMute, onPlayPause, onVolume, onRemove, onDuplicate }) {
  countEl.textContent = String(layerManager.layers.length);
  listEl.innerHTML = '';
  // topmost layer first in the panel
  const ordered = [...layerManager.layers].reverse();
  ordered.forEach((layer) => {
    const row = document.createElement('div');
    row.className = `layer-row${layer.id === layerManager.selectedId ? ' selected' : ''}`;
    row.dataset.layerId = layer.id;
    row.draggable = true;

    const vis = document.createElement('span');
    vis.className = 'lr-visibility';
    vis.textContent = layer.visible ? '👁' : '🚫';
    vis.title = 'Show/hide (independent of play/pause)';
    vis.onclick = (e) => { e.stopPropagation(); onToggle(layer.id, !layer.visible); };

    const lock = document.createElement('span');
    lock.className = 'lr-lock';
    lock.textContent = layer.locked ? '🔒' : '🔓';
    lock.title = 'Lock geometry & settings (layer keeps rendering)';
    lock.onclick = (e) => { e.stopPropagation(); layerManager.setLocked(layer.id, !layer.locked); };

    const main = document.createElement('div');
    main.className = 'lr-main';
    const name = document.createElement('span');
    name.className = 'lr-name';
    name.textContent = layer.name;
    name.title = 'Double-click to rename';
    name.ondblclick = (e) => {
      e.stopPropagation();
      name.contentEditable = 'true';
      name.focus();
      document.getSelection().selectAllChildren(name);
      const commit = () => { name.contentEditable = 'false'; onRename(layer.id, name.textContent); };
      name.onblur = commit;
      name.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); commit(); } };
    };
    const sub = document.createElement('div');
    sub.className = 'lr-sub';
    const kind = document.createElement('span');
    kind.className = 'lr-kind';
    kind.textContent = `${KIND_ICON[layer.type] || '•'} ${layer.type}`;
    sub.appendChild(kind);
    if (layer.element && (layer.element.duration || layer.element.currentTime)) {
      const time = document.createElement('span');
      const dur = isFinite(layer.element.duration) ? layer.element.duration.toFixed(1) : '?';
      time.textContent = `${layer.element.currentTime.toFixed(1)}/${dur}s`;
      time.dataset.role = 'time';
      sub.appendChild(time);
    }
    if (layer.statusBadge === 'SOURCE_LOST') {
      const b = document.createElement('span');
      b.className = 'lr-badge source-lost';
      b.textContent = 'SOURCE_LOST';
      sub.appendChild(b);
    } else if (layer.mediaId && mediaLibrary?.get(layer.mediaId)?.proxies &&
               Object.keys(mediaLibrary.get(layer.mediaId).proxies).length) {
      const b = document.createElement('span');
      b.className = 'lr-badge proxy';
      const rungs = Object.keys(mediaLibrary.get(layer.mediaId).proxies);
      b.textContent = `proxy ${rungs.join('/')}`;
      sub.appendChild(b);
    }
    const vol = document.createElement('input');
    vol.type = 'range'; vol.min = '0'; vol.max = '1'; vol.step = '0.01';
    vol.value = String(layer.volume);
    vol.style.width = '54px';
    vol.title = `Volume ${(layer.volume * 100) | 0}%`;
    vol.onclick = (e) => e.stopPropagation();
    vol.oninput = () => onVolume(layer.id, Number(vol.value));
    const mute = document.createElement('span');
    mute.textContent = layer.muted ? '🔇' : '🔊';
    mute.style.cursor = 'pointer';
    mute.title = 'Mute/unmute (independent)';
    mute.onclick = (e) => { e.stopPropagation(); onMute(layer.id, !layer.muted); };
    sub.append(vol, mute);
    main.append(name, sub);

    const actions = document.createElement('div');
    actions.className = 'lr-actions';
    if (layer.type !== 'image' && layer.type !== 'camera') {
      const play = document.createElement('button');
      play.className = 'lr-play-btn';
      play.textContent = layer.playing ? '⏸' : '▶';
      play.title = 'Play/pause (this layer only)';
      play.onclick = (e) => { e.stopPropagation(); onPlayPause(layer.id); };
      actions.appendChild(play);
    }
    const dup = document.createElement('button');
    dup.textContent = '⧉'; dup.title = 'Duplicate layer';
    dup.onclick = (e) => { e.stopPropagation(); onDuplicate(layer.id); };
    const del = document.createElement('button');
    del.textContent = '🗑'; del.title = 'Remove layer';
    del.onclick = (e) => { e.stopPropagation(); onRemove(layer.id); };
    actions.append(dup, del);

    row.append(vis, lock, main, actions);
    row.onclick = () => onSelect(layer.id);

    // drag reorder (P3-20)
    row.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/layer-id', layer.id);
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));
    row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', (e) => {
      e.preventDefault();
      row.classList.remove('drag-over');
      const draggedId = e.dataTransfer.getData('text/layer-id');
      if (draggedId && draggedId !== layer.id) {
        const from = layerManager.layers.findIndex(l => l.id === draggedId);
        const to = layerManager.layers.findIndex(l => l.id === layer.id);
        onReorder(from, to);
      }
    });
    listEl.appendChild(row);
  });
}

/* ----------------------------- export dialog ------------------------------ */
export async function openExportDialog(dialogs, { project, defaults, toast, onSubmit }) {
  let formats;
  try {
    formats = await api.exportFormats();
  } catch (err) {
    toast(`Cannot read encoder capabilities: ${err.message}`, 'err');
    return;
  }
  const fmtOpts = Object.entries(formats.formats)
    .map(([k, v]) => `<option value="${k}" ${v.available ? '' : 'disabled'}>${k.toUpperCase()} — ${v.label}${v.available ? '' : ' (unavailable)'}</option>`)
    .join('');
  const resOpts = formats.resolutions.map(r => `<option value="${r}" ${r === defaults.resolution ? 'selected' : ''}>${r}</option>`).join('') +
    '<option value="custom">custom</option>';
  const fpsOpts = formats.fps.map(f => `<option ${f === defaults.fps ? 'selected' : ''}>${f}</option>`).join('');
  const hw = formats.hw_encoders && Object.keys(formats.hw_encoders).length
    ? Object.keys(formats.hw_encoders).join(', ') : 'none detected';
  const el = dialogs.open(`
    <h2>Export final video</h2>
    <p>FFmpeg re-renders the take from the <b>original sources</b> using the recorded timeline — pauses, hides, seeks and volume changes are all reconstructed.</p>
    <h3>Settings</h3>
    <div class="field-grid">
      <label class="field"><span>Format</span><select id="ex-format">${fmtOpts}</select></label>
      <label class="field"><span>Resolution</span><select id="ex-resolution">${resOpts}</select></label>
      <label class="field"><span>FPS</span><select id="ex-fps">${fpsOpts}</select></label>
      <label class="field"><span>Quality (CRF lower=better)</span><input id="ex-crf" type="number" min="14" max="34" value="20"></label>
    </div>
    <div class="field-grid" id="ex-custom-res" hidden>
      <label class="field"><span>Custom width</span><input id="ex-cw" type="number" value="1920" min="16" step="2"></label>
      <label class="field"><span>Custom height</span><input id="ex-ch" type="number" value="1080" min="16" step="2"></label>
    </div>
    <label class="check"><input type="checkbox" id="ex-loudnorm"> Loudness normalization (EBU R128)</label>
    <p class="muted">Hardware encoders detected: ${hw}. The studio verifies them before use and falls back to software automatically.</p>
    <div class="dlg-actions">
      <button class="btn" id="ex-cancel">Cancel</button>
      <button class="btn btn-primary" id="ex-start">Start export</button>
    </div>`);
  $('#ex-cancel', el).onclick = () => dialogs.close();
  $('#ex-resolution', el).onchange = (e) => { $('#ex-custom-res', el).hidden = e.target.value !== 'custom'; };
  $('#ex-start', el).onclick = () => {
    const settings = {
      format: $('#ex-format', el).value,
      resolution: $('#ex-resolution', el).value,
      fps: Number($('#ex-fps', el).value),
      crf: Number($('#ex-crf', el).value),
      loudnorm: $('#ex-loudnorm', el).checked,
    };
    if (settings.resolution === 'custom') {
      settings.custom_resolution = [Number($('#ex-cw', el).value), Number($('#ex-ch', el).value)];
      settings.resolution = 'custom';
    }
    dialogs.close();
    onSubmit(settings);
  };
}

/* ------------------------------ jobs dialog ------------------------------- */
export async function openJobsDialog(dialogs, { projectId, toast }) {
  const el = dialogs.open(`<h2>Export jobs</h2><div id="jobs-body"><p class="muted">Loading…</p></div>
    <h3>Recorded takes</h3><div id="takes-body"><p class="muted">Loading…</p></div>
    <div class="dlg-actions"><button class="btn" id="jobs-close">Close</button></div>`);
  $('#jobs-close', el).onclick = () => dialogs.close();
  const body = $('#jobs-body', el);
  let timer = null;
  const refresh = async () => {
    try {
      const { jobs } = await api.listJobs(projectId);
      if (!jobs.length) { body.innerHTML = '<p class="muted">No export jobs yet.</p>'; return; }
      body.innerHTML = '';
      for (const job of jobs.slice(0, 12)) {
        const row = document.createElement('div');
        row.className = 'job-row';
        const pct = Math.round(job.progress?.pct ?? 0);
        row.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center">
            <b>${escapeHtml(job.id)}</b>
            <span class="job-state ${job.state}">${job.state}</span>
          </div>
          ${job.state === 'RUNNING' || job.state === 'PREPARING' || job.state === 'VALIDATING' ? `
            <div class="progress-outer"><div class="progress-inner" style="width:${pct}%"></div></div>
            <div class="muted">${escapeHtml(job.progress?.stage || '')} — ${pct}%${job.progress?.eta_s ? ` · ETA ${job.progress.eta_s}s` : ''}</div>` : ''}
          ${job.output_path ? `<div class="muted">→ ${escapeHtml(job.output_path)}</div>` : ''}
          ${job.error ? `<div style="color:var(--err)">${escapeHtml(job.error.slice(0, 200))}</div>` : ''}
          ${(job.log_tail || []).length ? `<div class="job-log">${(job.log_tail.slice(-8)).map(escapeHtml).join('\n')}</div>` : ''}`;
        if (['QUEUED', 'RUNNING', 'PREPARING', 'VALIDATING', 'RECOVERING', 'CANCELLING'].includes(job.state)) {
          const cancel = document.createElement('button');
          cancel.className = 'btn tiny';
          cancel.textContent = 'Cancel';
          cancel.style.marginTop = '6px';
          cancel.onclick = async () => {
            try { await api.cancelJob(job.id); toast('Cancellation requested — FFmpeg will stop and temp files will be cleaned.', 'info'); }
            catch (err) { toast(err.message, 'warn'); }
            refresh();
          };
          row.appendChild(cancel);
        }
        body.appendChild(row);
      }
    } catch (err) {
      body.innerHTML = `<p style="color:var(--err)">${escapeHtml(err.message)}</p>`;
    }
  };
  await refresh();
  timer = setInterval(() => {
    if (dialogs.backdrop.hidden) { clearInterval(timer); return; }
    refresh();
  }, 1500);

  // recorded takes list — incomplete takes offer discard/keep (P7-15)
  const takesBody = $('#takes-body', el);
  try {
    const { takes } = await api.listRecordings(projectId);
    if (!takes.length) {
      takesBody.innerHTML = '<p class="muted">No takes recorded yet.</p>';
    } else {
      takesBody.innerHTML = '';
      for (const take of takes.slice(0, 10)) {
        const row = document.createElement('div');
        row.className = 'job-row';
        const meta = take.meta || {};
        row.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center">
            <b>${escapeHtml(take.take_id)}</b>
            <span class="job-state ${take.status === 'COMPLETE' ? 'COMPLETED' : 'FAILED'}">${take.status}</span>
          </div>
          <div class="muted">${meta.duration_s ? `${meta.duration_s.toFixed(1)}s · ` : ''}` +
            `${(take.total_size / 1e6).toFixed(1)} MB · ${take.files.length} file(s)` +
            `${meta.event_count ? ` · ${meta.event_count} events` : ''}</div>`;
        const del = document.createElement('button');
        del.className = 'btn tiny btn-danger';
        del.textContent = '🗑 Discard take';
        del.style.marginTop = '6px';
        del.onclick = async () => {
          if (!window.confirm('Discard this take? Recordings only — sources are never touched.')) return;
          try {
            await api.deleteTake(projectId, take.take_id);
            toast('Take discarded (generated recordings only).', 'ok');
            row.remove();
          } catch (err) { toast(err.message, 'err'); }
        };
        row.appendChild(del);
        takesBody.appendChild(row);
      }
    }
  } catch (err) {
    takesBody.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

/* --------------------------- diagnostics dialog --------------------------- */
export async function openDiagnosticsDialog(dialogs, { health, toast }) {
  let sys, hl;
  try { sys = await api.system(); hl = await api.health(); }
  catch (err) { toast(`Diagnostics failed: ${err.message}`, 'err'); return; }
  const f = sys.ffmpeg;
  const p = sys.platform;
  const diskGB = (n) => (n / 1e9).toFixed(2) + ' GB';
  dialogs.open(`
    <h2>🩺 Diagnostics</h2>
    <h3>Server</h3>
    <table class="kv-table">
      <tr><td>Status</td><td>${hl.status} · uptime ${hl.uptime_s}s</td></tr>
      <tr><td>Platform</td><td>${escapeHtml(p.system)} ${escapeHtml(p.release)} ${escapeHtml(p.machine)}${p.termux ? ' (Termux)' : ''}</td></tr>
      <tr><td>Python / CPU / RAM</td><td>${escapeHtml(p.python)} · ${p.cpu_count} cores · ${escapeHtml(p.ram_total)}</td></tr>
      <tr><td>Recovery</td><td>${escapeHtml(hl.recovery?.message || '—')}</td></tr>
    </table>
    <h3>FFmpeg</h3>
    <table class="kv-table">
      <tr><td>Found</td><td>${f.available ? `✅ ${escapeHtml(f.version || '')}` : '❌ missing'}</td></tr>
      <tr><td>Path</td><td>${escapeHtml(f.path || '—')}</td></tr>
      <tr><td>Codecs</td><td>x264:${f.has_libx264 ? '✅' : '❌'} · VP9:${f.has_libvpx_vp9 ? '✅' : '❌'} · x265:${f.has_libx265 ? '✅' : '❌'} · AAC:${f.has_aac ? '✅' : '❌'} · Opus:${f.has_libopus ? '✅' : '❌'}</td></tr>
      <tr><td>Hardware encoders</td><td>${Object.keys(f.hw_encoders || {}).join(', ') || 'none (software only — fine)'}</td></tr>
      <tr><td>FFprobe</td><td>${sys.ffprobe.available ? '✅ ' + escapeHtml(sys.ffprobe.version || '') : '❌ missing'}</td></tr>
      ${!f.available || !sys.ffprobe.available ? `<tr><td colspan="2" style="color:var(--warn)">${escapeHtml(f.remediation || sys.ffprobe.remediation || '')}</td></tr>` : ''}
    </table>
    <h3>Storage</h3>
    <table class="kv-table">
      ${Object.entries(sys.storage.free_bytes).map(([k, v]) => `<tr><td>${k} free</td><td>${diskGB(v)}</td></tr>`).join('')}
      <tr><td>Writable</td><td>${sys.storage.writable ? '✅' : '❌'}</td></tr>
    </table>
    <h3>Cameras</h3>
    <p class="muted">${escapeHtml(sys.cameras_hint)}</p>
    <div class="dlg-actions"><button class="btn" id="diag-close">Close</button></div>
  `);
  $('#diag-close').onclick = () => dialogs.close();
}

/* ------------------------------- help dialog ------------------------------ */
export function openHelpDialog(dialogs) {
  dialogs.open(`
    <h2>Ahmed Reaction Studio — guide</h2>
    <h3>Quick start</h3>
    <p>1. <b>Add media</b> (local video / image / audio) or a <b>camera</b>.<br>
       2. Drag PiPs on the canvas; use the 8 handles to resize; presets place common layouts.<br>
       3. Every layer has <b>independent</b> play/pause/seek/volume/mute/visibility.<br>
       4. Press <b>● Record</b> — countdown, then the composite + all cameras + the event timeline are captured.<br>
       5. Press <b>⬇ Export</b> — FFmpeg rebuilds the take from the originals with every pause, hide, seek and volume change.</p>
    <h3>Key ideas</h3>
    <p>• <b>Hide ≠ pause</b>: a hidden layer keeps playing (its audio continues); pause freezes the frame.<br>
       • Everything is <b>100% local</b>. Nothing is uploaded. Originals are never modified.<br>
       • Heavy sources automatically get preview proxies; exports always use originals.</p>
    <h3>Troubleshooting</h3>
    <p>• <b>FFmpeg missing</b> → Windows: <code>winget install Gyan.FFmpeg</code>; Termux: <code>pkg install ffmpeg</code>.<br>
       • <b>Camera busy</b> → close other apps using the camera.<br>
       • <b>Preview stutters</b> → the health panel auto-degrades preview; switch a layer to its proxy.<br>
       • <b>Disk full</b> → exports are refused before starting when space is insufficient.<br>
       • <b>Permission denied (camera)</b> → allow it via the padlock icon in the address bar.</p>
    <h3>Known limits (honest)</h3>
    <p>• Android: at most <b>2 simultaneous cameras</b> — a hard policy; if the device/browser exposes fewer, the studio says so instead of faking it.<br>
       • Windows multi-camera limits come from your hardware (USB bandwidth, drivers, CPU/RAM).<br>
       • Absolute zero-stutter cannot be guaranteed on every device/codec — the studio adapts instead (§27).<br>
       • After a page reload, file-backed layers ask you to re-pick their file (browsers cannot reopen local files without you).<br>
       • Camera layers export from their per-camera recordings; if those are missing, the composited take is used as fallback.<br>
       • Two tabs editing one project at once: saves are atomic and snapshotted, but the last writer wins.</p>
    <div class="dlg-actions"><button class="btn" id="help-close">Close</button></div>
  `);
  $('#help-close').onclick = () => dialogs.close();
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
