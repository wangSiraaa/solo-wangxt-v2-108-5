// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster, and the deterministic control batch is generated locally only.
const qs = (p) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null));

async function jsonOrThrow(r) {
  if (!r.ok) {
    let detail;
    try {
      const body = await r.json();
      detail = body.detail;
      if (detail && typeof detail === 'object') {
        detail = detail.detail || JSON.stringify(detail);
      }
    } catch {
      detail = await r.text();
    }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export async function getBatches() {
  return jsonOrThrow(await fetch('/api/batches'));
}

export async function seed() {
  return jsonOrThrow(await fetch('/api/seed', { method: 'POST' }));
}

export async function seedControl() {
  return jsonOrThrow(await fetch('/api/seed/control', { method: 'POST' }));
}

export async function getSeries(batchId, params = {}) {
  return jsonOrThrow(await fetch(`/api/batches/${batchId}/series?${qs(params)}`));
}

export async function getCompare(a, b, params = {}) {
  return jsonOrThrow(await fetch(`/api/compare?${qs({ a, b, ...params })}`));
}

export async function addEvent(batchId, ev) {
  return jsonOrThrow(
    await fetch(`/api/batches/${batchId}/events`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(ev),
    })
  );
}

export async function listEvents(batchId, includeHistory = false) {
  return jsonOrThrow(
    await fetch(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`)
  );
}

export async function exportBatch(batchId, params = {}) {
  return jsonOrThrow(await fetch(`/api/batches/${batchId}/export?${qs(params)}`));
}

export async function recompute(body) {
  return jsonOrThrow(
    await fetch('/api/recompute', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

// ---------------------------------------------------------------------------
// batch groups
// ---------------------------------------------------------------------------

export async function listGroups() {
  return jsonOrThrow(await fetch('/api/groups'));
}

export async function createGroup(body) {
  return jsonOrThrow(
    await fetch('/api/groups', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function getGroup(id) {
  return jsonOrThrow(await fetch(`/api/groups/${id}`));
}

export async function patchGroup(id, body) {
  return jsonOrThrow(
    await fetch(`/api/groups/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function createSnapshot(groupId, body) {
  return jsonOrThrow(
    await fetch(`/api/groups/${groupId}/snapshots`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function listSnapshots(groupId) {
  return jsonOrThrow(await fetch(`/api/groups/${groupId}/snapshots`));
}

export async function replaySnapshot(groupId, version) {
  return jsonOrThrow(await fetch(`/api/groups/${groupId}/snapshots/${version}`));
}

export async function exportSnapshot(groupId, version) {
  return jsonOrThrow(
    await fetch(`/api/groups/${groupId}/snapshots/${version}/export`)
  );
}

export async function recomputeGroup(doc) {
  return jsonOrThrow(
    await fetch('/api/groups/recompute', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(doc),
    })
  );
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
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
};

export function fmtTime(s) {
  if (s === null || s === undefined) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

// Tau is seconds relative to the anchor and may be negative: show m:ss with a
// leading sign, e.g. -1:30 / +0:40.
export function fmtTau(s) {
  if (s === null || s === undefined) return '—';
  const sign = s < 0 ? '−' : '+';
  const a = Math.abs(s);
  const m = Math.floor(a / 60);
  const sec = Math.round(a % 60);
  return `${sign}${m}:${String(sec).padStart(2, '0')}`;
}

export function originTag(b) {
  if (!b) return '';
  if (b.data_origin === 'local_synthetic_control' || b.is_control_batch) {
    return '本地合成对照';
  }
  if (b.is_local_synthetic || (b.data_origin || '').startsWith('local_synthetic')) {
    return '本地合成';
  }
  return b.data_origin || '';
}
