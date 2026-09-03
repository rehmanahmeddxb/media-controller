/**
 * api.js — typed-structure client for all studio endpoints (P2-30, P6-23).
 * Timeout + bounded retry; offline-friendly errors. No external libraries.
 */

export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

const DEFAULT_TIMEOUT = 15000;
const RETRY_DELAYS = [300, 900]; // bounded retry (GR-13)

async function request(path, { method = 'GET', body = null, form = null,
                               timeout = DEFAULT_TIMEOUT, retries = RETRY_DELAYS.length,
                               parseJson = true } = {}) {
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const opts = { method, signal: controller.signal, headers: {} };
      if (body !== null) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      if (form !== null) opts.body = form; // FormData (uploads)
      const res = await fetch(path, opts);
      clearTimeout(timer);
      if (!res.ok) {
        let detail = null;
        try { detail = await res.json(); } catch { /* non-JSON error */ }
        const msg = (detail && (detail.detail || detail.error)) || `${res.status} ${res.statusText}`;
        // 4xx are deterministic — do not retry
        if (res.status >= 400 && res.status < 500) throw new ApiError(String(msg), res.status, detail);
        throw new ApiError(String(msg), res.status, detail); // retried below if attempts remain
      }
      if (res.status === 204 || !parseJson) return await res.blob();
      return await res.json();
    } catch (err) {
      clearTimeout(timer);
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) throw err;
      lastErr = err;
      if (attempt < retries) {
        await new Promise(r => setTimeout(r, RETRY_DELAYS[Math.min(attempt, RETRY_DELAYS.length - 1)]));
        continue;
      }
    }
  }
  const offline = (lastErr && lastErr.name === 'AbortError')
    ? 'Server did not respond (timeout). Is the studio still running?'
    : 'Cannot reach the local server. Restart it with scripts/start_windows.bat (or start_termux.sh).';
  throw new ApiError(offline, 0, null);
}

export const api = {
  // ---- health / system (P1-25/26) ----
  health: () => request('/api/health'),
  system: () => request('/api/system'),

  // ---- media (P2-07..P2-12) ----
  probeUpload: (file, onProgress) => uploadWithProgress('/api/media/probe', file, 'file', onProgress),
  registerMedia: (path) => request('/api/media/register', { method: 'POST', body: { path } }),
  listMedia: () => request('/api/media'),
  getMedia: (id) => request(`/api/media/${id}`),
  deleteMedia: (id) => request(`/api/media/${id}`, { method: 'DELETE' }),
  makeProxy: (id) => request(`/api/media/${id}/proxy`, { method: 'POST', body: {} }),
  mediaUrl: (id, variant = 'original') => `/api/media/${id}/file?variant=${variant}`,

  // ---- projects (P6-16..) ----
  createProject: (name) => request('/api/projects', { method: 'POST', body: { name } }),
  listProjects: () => request('/api/projects'),
  loadProject: (id) => request(`/api/projects/${id}`),
  saveProject: (id, project) => request(`/api/projects/${id}`, { method: 'PUT', body: { project } }),
  deleteProject: (id) => request(`/api/projects/${id}`, { method: 'DELETE' }),
  markDirty: (id) => request(`/api/projects/${id}/dirty`, { method: 'POST', body: {} }),

  // ---- recordings (P7-11/12) ----
  uploadRecording: (projectId, form) =>
    request(`/api/recording/${encodeURIComponent(projectId)}`, { method: 'POST', form, timeout: 120000, retries: 0 }),
  attachTakeMeta: (projectId, takeId, body) =>
    request(`/api/recording/${encodeURIComponent(projectId)}/${encodeURIComponent(takeId)}/meta`,
            { method: 'POST', body }),
  listRecordings: (projectId) => request(`/api/recording/${encodeURIComponent(projectId)}`),
  deleteTake: (projectId, takeId) =>
    request(`/api/recording/${encodeURIComponent(projectId)}/${encodeURIComponent(takeId)}`, { method: 'DELETE' }),

  // ---- export jobs (P9-10..) ----
  exportFormats: () => request('/api/export/formats'),
  createExport: (payload) => request('/api/export', { method: 'POST', body: payload, timeout: 30000 }),
  listJobs: (projectId) => request('/api/export' + (projectId ? `?project_id=${encodeURIComponent(projectId)}` : '')),
  jobStatus: (id) => request(`/api/export/${id}`),
  cancelJob: (id) => request(`/api/export/${id}/cancel`, { method: 'POST', body: {} }),
  jobLog: (id) => request(`/api/export/${id}/log`),
};

/** XHR-based upload with progress callback (fetch cannot report upload progress). */
export function uploadWithProgress(path, file, fieldName = 'file', onProgress = null, extraFields = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.timeout = 600000;
    xhr.upload.onprogress = (e) => {
      if (onProgress && e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      try {
        const data = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new ApiError((data && (data.detail || data.error)) || `upload failed (${xhr.status})`, xhr.status, data));
      } catch {
        reject(new ApiError(`upload failed (${xhr.status})`, xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError('network error during upload', 0));
    xhr.ontimeout = () => reject(new ApiError('upload timed out', 0));
    const form = new FormData();
    form.append(fieldName, file, file.name);
    for (const [k, v] of Object.entries(extraFields)) {
      if (v !== undefined && v !== null) form.append(k, v);
    }
    xhr.send(form);
  });
}
