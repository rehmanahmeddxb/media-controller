/**
 * media.js — local file handling for the browser (P2-23 … P2-29).
 *
 * File picker + File System Access API where available. Object-URL lifecycle
 * (revoke on removal/unload). Source File references retained for the session.
 * Proxy-vs-original selection with manual override. Large-file guard.
 */
import { api } from './api.js';

const LARGE_FILE_BYTES = 1024 * 1024 * 1024;        // 1 GiB (P2-29)
const LARGE_PIXELS = 2560 * 1440;

export class MediaLibrary {
  constructor({ toast, onRegistered } = {}) {
    this.toast = toast || (() => {});
    this.onRegistered = onRegistered || (() => {});
    this.items = new Map();      // mediaId -> { id, file, objectUrl, metadata, proxies, useProxy, name }
    this.sessionFiles = new Set(); // retained File references (P2-25)
  }

  /** Pick files: File System Access API when available, <input> otherwise (P2-23). */
  async pick({ multiple = true, accept = ['video/*', 'audio/*', 'image/*'] } = {}) {
    if (window.showOpenFilePicker) {
      try {
        const handles = await window.showOpenFilePicker({ multiple, types: [{ accept: { '*/*': accept } }] });
        return await Promise.all(handles.map(h => h.getFile()));
      } catch (err) {
        if (err.name === 'AbortError') return [];
        // fall through to <input>
      }
    }
    return new Promise(resolve => {
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = multiple;
      input.accept = accept.join(',');
      input.onchange = () => resolve([...input.files]);
      // cancel -> empty
      window.addEventListener('focus', () => setTimeout(() => resolve([]), 800), { once: true });
      input.click();
    });
  }

  /** Object-URL creation tracked for later revoke (P2-24). */
  objectUrl(file) {
    const url = URL.createObjectURL(file);
    this.sessionFiles.add(file);
    return url;
  }

  revoke(url) { try { URL.revokeObjectURL(url); } catch { /* already revoked */ } }

  /**
   * Ingest a file: upload a copy to the backend (chunk-streamed), probe it,
   * validate, warn on heavy sources (P2-29), auto-proxy when needed.
   */
  async ingest(file, { onProgress } = {}) {
    if (file.size > LARGE_FILE_BYTES) {
      this.toast(`"${file.name}" is very large (${(file.size / 1e9).toFixed(1)} GB). ` +
                 'A preview proxy will be generated to keep playback smooth.', 'warn', 8000);
    }
    const res = await api.probeUpload(file, onProgress);
    const { media_id: id, metadata, valid, issues = [] } = res;
    if (!valid) {
      const errs = issues.filter(i => i.severity === 'error').map(i => i.message);
      throw new Error(errs[0] || 'Media failed validation');
    }
    const item = {
      id, file, name: file.name,
      objectUrl: this.objectUrl(file),
      metadata, proxies: res.proxies || {},
      useProxy: null,   // null = auto (P2-27)
      variantPath: variant => this.variantUrl(id, variant),
    };
    this.items.set(id, item);
    const px = metadata.video?.display_width || metadata.video?.width || 0;
    const py = metadata.video?.display_height || metadata.video?.height || 0;
    if (px * py > LARGE_PIXELS) {
      this.toast(`"${file.name}" is ${px}×${py} — generating a preview proxy…`, 'info', 6000);
      this.autoProxy(item).catch(() => {});
    }
    for (const issue of issues.filter(i => i.severity === 'warning')) {
      this.toast(issue.message, 'warn', 6000);
    }
    this.onRegistered(item);
    return item;
  }

  /** Ask the backend to generate a proxy (decision engine server-side). */
  async autoProxy(item) {
    try {
      const res = await api.makeProxy(item.id);
      if (res.status === 'ok') {
        item.proxies = { ...(item.proxies || {}), [res.rung]: res };
        this.toast(`Proxy ready (${res.rung}p) for "${item.name}"`, 'ok');
      } else if (res.status === 'not_needed') {
        /* original is fine — no unnecessary transcodes (P2-15) */
      } else if (res.status === 'failed') {
        this.toast(`Proxy failed for "${item.name}" — using the original.`, 'warn', 7000);
      }
    } catch (err) {
      this.toast(`Proxy error: ${err.message}`, 'warn');
    }
    return item;
  }

  /** Which URL should the layer play? Original vs best proxy (P2-27). */
  playbackUrl(item) {
    if (!item) return null;
    if (item.useProxy === 'original') return item.objectUrl;
    if (item.useProxy && item.proxies?.[item.useProxy]) {
      return item.variantPath?.(item.useProxy) || item.proxies[item.useProxy].path;
    }
    // auto: proxy if one exists
    const rungs = Object.keys(item.proxies || {}).sort((a, b) => Number(b) - Number(a));
    if (rungs.length && item.proxies[rungs[0]]?.path) {
      return item.variantPath?.(rungs[0]);
    }
    return item.objectUrl;
  }

  variantUrl(id, variant) { return api.mediaUrl(id, variant === 'original' ? 'original' : `proxy_${variant}p`); }

  get(id) { return this.items.get(id); }

  /** Remove an item: revoke object URLs; backend delete removes proxies only (P2-10). */
  async remove(id) {
    const item = this.items.get(id);
    if (!item) return;
    this.revoke(item.objectUrl);
    this.sessionFiles.delete(item.file);
    this.items.delete(id);
    try { await api.deleteMedia(id); } catch { /* server may already be gone */ }
  }

  /** Metadata card text for the UI (P2-28). */
  card(item) {
    if (!item || !item.metadata) return '—';
    const v = item.metadata.video || {};
    const a = (item.metadata.audio_streams || [])[0];
    const bits = [
      `<b>${escapeHtml(item.name)}</b>`,
      `${v.codec || '?'}${v.profile ? ' (' + escapeHtml(v.profile) + ')' : ''} · ${v.display_width || v.width || '?'}×${v.display_height || v.height || '?'}`,
      `${(v.fps || 0).toFixed(v.fps % 1 ? 2 : 0)} fps${item.metadata.vfr ? ' · <span title="Variable frame rate">VFR</span>' : ' CFR'}`,
    ];
    if (item.metadata.duration) bits.push(`${item.metadata.duration.toFixed(1)}s · ${(item.metadata.size / 1e6).toFixed(1)} MB`);
    if (a) bits.push(`${a.codec} ${a.channels}ch ${(a.sample_rate / 1000).toFixed(1)}kHz`);
    const rungs = Object.keys(item.proxies || {});
    if (rungs.length) bits.push(`<span class="lr-badge proxy">proxy ${rungs.join('/')}</span>`);
    if (v.hdr) bits.push(`<span class="lr-badge">HDR ${escapeHtml(String(v.hdr))}</span>`);
    if (v.rotation) bits.push(`<span class="lr-badge">${v.rotation}°</span>`);
    return bits.join('<br>');
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
