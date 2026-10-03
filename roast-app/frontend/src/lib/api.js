// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster.
const qs = (p) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null));

export async function getBatches() {
  const r = await fetch('/api/batches');
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function seed() {
  const r = await fetch('/api/seed', { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function seedControl() {
  const r = await fetch('/api/seed-control', { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getSeries(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/series?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getCompare(a, b, params = {}) {
  const r = await fetch(`/api/compare?${qs({ a, b, ...params })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function addEvent(batchId, ev) {
  const r = await fetch(`/api/batches/${batchId}/events`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(ev),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listEvents(batchId, includeHistory = false) {
  const r = await fetch(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function exportBatch(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/export?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function recompute(body) {
  const r = await fetch('/api/recompute', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export const EVENT_LABELS = {
  charge: '下豆/开火',
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
  damper_change: '风门变化',
  custom: '自定义',
};

export const ANCHOR_LABELS = {
  charge: '下豆/开火',
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
};

export const EXCLUSION_LABELS = {
  missing_anchor: '缺少该锚点事件（不猜测、不补点）',
  anchor_in_long_gap: '锚点落在长断档内（超过桥接上限，插值不硬补）',
  anchor_in_interpolated_gap: '锚点落在插值段内（插值非实测，不可作锚）',
  anchor_without_raw_support: '锚点附近无非插值实测样本支持',
};

// ---------------------------------------------------------------------------
// batch groups
// ---------------------------------------------------------------------------

async function jsonFetch(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = await r.text();
    try {
      detail = JSON.parse(detail).detail || detail;
    } catch {
      /* keep text */
    }
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export function listGroups() {
  return jsonFetch('/api/groups');
}

export function getGroup(id) {
  return jsonFetch(`/api/groups/${id}`);
}

export function createGroup(body) {
  return jsonFetch('/api/groups', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function updateMembers(groupId, body) {
  return jsonFetch(`/api/groups/${groupId}/members`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function updateGroupParams(groupId, body) {
  return jsonFetch(`/api/groups/${groupId}/params`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function createSnapshot(groupId, body = {}) {
  return jsonFetch(`/api/groups/${groupId}/snapshots`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function getSnapshot(groupId, version) {
  return jsonFetch(`/api/groups/${groupId}/snapshots/${version}`);
}

export function exportSnapshotUrl(groupId, version) {
  return `/api/groups/${groupId}/snapshots/${version}/export`;
}

export async function exportSnapshot(groupId, version) {
  return jsonFetch(exportSnapshotUrl(groupId, version));
}

export function replayGroup(spec, resultSha256) {
  return jsonFetch('/api/group-replay', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ spec, result_sha256: resultSha256 }),
  });
}

export function fmtTime(s) {
  if (s === null || s === undefined) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
