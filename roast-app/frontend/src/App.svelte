<script>
  import { onMount } from 'svelte';
  import RoastChart from './lib/RoastChart.svelte';
  import GroupChart from './lib/GroupChart.svelte';
  import {
    getBatches,
    seed,
    seedControl,
    getSeries,
    getCompare,
    addEvent,
    listEvents,
    exportBatch,
    recompute,
    listGroups,
    getGroup,
    createGroup,
    updateMembers,
    updateGroupParams,
    createSnapshot,
    getSnapshot,
    exportSnapshot,
    exportSnapshotUrl,
    replayGroup,
    ANCHOR_LABELS,
    EXCLUSION_LABELS,
    EVENT_LABELS,
    fmtTime,
  } from './lib/api.js';

  let batches = [];
  let view = 'single'; // single | compare | group
  let selA = null;
  let selB = null;
  let dataA = null;
  let dataB = null;
  let comparePayload = null;
  let eventHistory = [];
  let loading = '';
  let error = '';

  // Analysis parameters — affect DERIVED traces only, never stored samples.
  let windowS = 30;
  let smoothS = 12;
  let maxGapFillS = 45;

  // event correction form
  let newEventType = 'turning_point';
  let newEventTime = '1:00';
  let newEventDamper = '';
  let showHistory = false;

  // export verification
  let verifyResult = null;

  const phaseKeys = [
    ['drying_s', '脱水期', '下豆 → 回温点'],
    ['maillard_s', '梅纳/反应期', '回温点 → 一爆开始'],
    ['development_s', '发展期', '一爆开始 → 出锅'],
    ['first_crack_window_s', '一爆持续', '一爆开始 → 一爆结束'],
    ['total_s', '总时长', '下豆 → 出锅'],
  ];

  onMount(loadBatches);

  async function loadBatches() {
    error = '';
    try {
      batches = await getBatches();
      if (batches.length) {
        selA = batches[0].id;
        selB = batches[batches.length - 1].id;
        await refresh();
      }
      await loadGroups(false);
    } catch (e) {
      error = `无法连接后端：${e.message}`;
    }
  }

  async function doSeed() {
    loading = '正在生成合成批次…';
    error = '';
    try {
      await seed();
      await loadBatches();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function refresh() {
    if (!selA) return;
    loading = '加载曲线…';
    error = '';
    verifyResult = null;
    const params = {
      window_s: windowS,
      display_smooth_s: smoothS,
      max_gap_fill_s: maxGapFillS,
    };
    try {
      dataA = await getSeries(selA, { ...params, include_history: showHistory });
      eventHistory = await listEvents(selA, showHistory);
      if (view === 'compare' && selB && selB !== selA) {
        comparePayload = await getCompare(selA, selB, params);
        dataB = comparePayload.batches[1];
      } else {
        comparePayload = null;
        dataB = null;
      }
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function parseMMSS(str) {
    const m = /^(\d+):([0-5]?\d)$/.exec(str.trim());
    if (!m) return null;
    return Number(m[1]) * 60 + Number(m[2]);
  }

  async function submitEvent() {
    const t = parseMMSS(newEventTime);
    if (t === null) {
      error = '时间格式应为 m:ss，例如 1:05';
      return;
    }
    const body = {
      event_type: newEventType,
      t_s: t,
      source: 'manual',
      created_by: '操作员(界面)',
      label: `${EVENT_LABELS[newEventType] || newEventType} 人工修正`,
    };
    if (newEventType === 'damper_change') {
      const v = Number(newEventDamper);
      if (!Number.isFinite(v)) {
        error = '风门变化需要填写新风门开度 (%)';
        return;
      }
      body.value_num = v;
      body.label = `风门 → ${v}% 人工标记`;
    }
    loading = '保存修正…';
    error = '';
    try {
      await addEvent(selA, body);
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function verifyExport() {
    loading = '导出并重算校验…';
    error = '';
    verifyResult = null;
    try {
      const ex = await exportBatch(selA, {
        window_s: windowS,
        display_smooth_s: smoothS,
      });
      const rc = await recompute({
        samples: ex.series.raw_points.map((p) => ({
          t_s: p.t_s,
          bean_temp_c: p.bean_temp_c,
          env_temp_c: p.env_temp_c,
        })),
        events: ex.events,
        params: ex.params,
      });
      const keys = Object.keys(ex.metrics).filter(
        (k) => k.endsWith('_s') || k === 'development_ratio'
      );
      const rows = keys.map((k) => ({
        key: k,
        exported: ex.metrics[k],
        recomputed: rc.metrics[k],
        match: ex.metrics[k] === rc.metrics[k],
      }));
      // Changing window/smoothing must leave every stored sample untouched.
      const alt = await getSeries(selA, {
        window_s: windowS * 2,
        display_smooth_s: smoothS === 0 ? 30 : 0,
        max_gap_fill_s: maxGapFillS,
      });
      const sig = (arr) =>
        JSON.stringify(arr.map((p) => [p.t_s, p.bean_temp_c, p.env_temp_c]));
      const rawSame = sig(ex.series.raw_points) === sig(alt.series.raw_points);
      verifyResult = { rows, rawSame, exportObj: ex };
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function downloadExport() {
    if (!verifyResult?.exportObj) return;
    const blob = new Blob([JSON.stringify(verifyResult.exportObj, null, 2)], {
      type: 'application/json',
    });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${verifyResult.exportObj.batch.name}-export.json`;
    a.click();
  }

  $: chartPayloads =
    view === 'compare' && comparePayload
      ? comparePayload.batches
      : dataA
        ? [dataA]
        : [];

  let refreshTimer;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }

  // -------------------------------------------------------------------------
  // batch-group workspace (3-8 members, explicit anchor, versioned snapshots)
  // -------------------------------------------------------------------------

  let groups = [];
  let selGroupId = null;
  let groupData = null; // live editable group payload
  let selectedBatchIds = [];
  let groupForm = {
    name: '',
    anchor_event: 'first_crack_start',
    grid_step_s: 5,
    support_tolerance_s: 3,
    max_gap_fill_s: 45,
  };
  let snapshotView = null; // full snapshot payload currently being reviewed
  let showRawCurves = true;
  let showGroupRor = false;
  let memberVisibility = {};
  let replayVerify = null; // {matches, sha, staleCheck}

  async function doSeedControl() {
    loading = '正在本地生成确定性对照批次…';
    error = '';
    try {
      await seedControl();
      await loadBatches();
      await loadGroups();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function loadGroups(preserveSelection = true) {
    try {
      groups = await listGroups();
      if (selGroupId && preserveSelection && groups.some((g) => g.id === selGroupId)) {
        await openGroup(selGroupId);
      }
    } catch (e) {
      error = e.message;
    }
  }

  async function openGroup(id) {
    selGroupId = id;
    error = '';
    snapshotView = null;
    replayVerify = null;
    try {
      groupData = await getGroup(id);
      selectedBatchIds = groupData.members.map((m) => m.batch_id);
      groupForm.name = groupData.name;
      groupForm.anchor_event = groupData.anchor_event;
      groupForm.grid_step_s = groupData.params.grid_step_s;
      groupForm.support_tolerance_s = groupData.params.support_tolerance_s;
      groupForm.max_gap_fill_s = groupData.params.max_gap_fill_s;
      groupData.members.forEach((m) => {
        if (!(m.batch_id in memberVisibility)) memberVisibility[m.batch_id] = true;
      });
      memberVisibility = memberVisibility; // invalidate for member-curve toggles
    } catch (e) {
      error = e.message;
    }
  }

  function toggleBatchInForm(bid) {
    const i = selectedBatchIds.indexOf(bid);
    if (i >= 0) selectedBatchIds.splice(i, 1);
    else if (selectedBatchIds.length < 8) selectedBatchIds.push(bid);
    selectedBatchIds = selectedBatchIds;
  }

  function startNewGroup() {
    groupData = null;
    snapshotView = null;
    replayVerify = null;
    selectedBatchIds = [];
    groupForm.name = '';
  }

  async function submitNewGroup() {
    error = '';
    if (selectedBatchIds.length < 3 || selectedBatchIds.length > 8) {
      error = '批次组必须包含 3–8 个批次。';
      return;
    }
    loading = '正在建立批次组…';
    try {
      const g = await createGroup({
        name: groupForm.name.trim() || `批次组 ${new Date().toLocaleString()}`,
        anchor_event: groupForm.anchor_event,
        window_s: windowS,
        display_smooth_s: smoothS,
        max_gap_fill_s: groupForm.max_gap_fill_s,
        grid_step_s: groupForm.grid_step_s,
        support_tolerance_s: groupForm.support_tolerance_s,
        batch_ids: selectedBatchIds,
      });
      await loadGroups(false);
      await openGroup(g.id);
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function saveGroupParams() {
    error = '';
    loading = '正在保存锚点/分析参数（版本检查）…';
    try {
      groupData = await updateGroupParams(groupData.id, {
        expected_revision: groupData.revision,
        anchor_event: groupForm.anchor_event,
        max_gap_fill_s: Number(groupForm.max_gap_fill_s),
        grid_step_s: Number(groupForm.grid_step_s),
        support_tolerance_s: Number(groupForm.support_tolerance_s),
        window_s: windowS,
        display_smooth_s: smoothS,
      });
      await openGroup(groupData.id);
    } catch (e) {
      error = `${e.status === 409 ? '并发冲突：' : ''}${e.message}`;
      await openGroup(groupData.id);
    } finally {
      loading = '';
    }
  }

  async function saveMembership() {
    error = '';
    if (selectedBatchIds.length < 3 || selectedBatchIds.length > 8) {
      error = '批次组必须包含 3–8 个批次。';
      return;
    }
    loading = '正在保存成员（乐观版本检查）…';
    try {
      groupData = await updateMembers(groupData.id, {
        expected_revision: groupData.revision,
        batch_ids: selectedBatchIds,
      });
      selectedBatchIds = groupData.members.map((m) => m.batch_id);
      await loadGroups();
    } catch (e) {
      error = `${e.status === 409 ? '并发冲突：' : ''}${e.message}`;
      await openGroup(groupData.id); // refresh; stale editor re-reads state
    } finally {
      loading = '';
    }
  }

  async function cutSnapshot() {
    error = '';
    loading = '正在固化组快照…';
    try {
      const snap = await createSnapshot(groupData.id, { created_by: '负责人(界面)' });
      await openGroup(groupData.id);
      await openSnapshot(snap.snapshot.version);
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function openSnapshot(version) {
    error = '';
    replayVerify = null;
    try {
      snapshotView = await getSnapshot(groupData.id, version);
    } catch (e) {
      error = e.message;
    }
  }

  async function verifySnapshotReplay() {
    if (!snapshotView) return;
    error = '';
    loading = '正在用导出快照独立重算…';
    try {
      const ex = await exportSnapshot(groupData.id, snapshotView.snapshot.version);
      const rp = await replayGroup(ex.spec, ex.snapshot.result_sha256);
      replayVerify = {
        matches: rp.matches_exported_result,
        resultSha: rp.result_sha256,
        nIncluded: rp.result.n_members_included,
        nExcluded: rp.result.n_members_excluded,
        demoWarning: rp.demo_warning,
        exportObj: ex,
      };
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function downloadSnapshotExport() {
    if (!snapshotView) return;
    const url = exportSnapshotUrl(groupData.id, snapshotView.snapshot.version);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${groupData.name}-snapshot-v${snapshotView.snapshot.version}.json`;
    a.click();
  }

  function provenanceBadge(b) {
    if (b.synthetic_kind === 'deterministic_control') return '[本地合成对照]';
    if (b.is_synthetic) return '[合成]';
    return '';
  }

  function memberProvenanceTag(m) {
    const p = (m && m.provenance) || {};
    if (p.synthetic_kind === 'deterministic_control') return '[本地合成对照]';
    if (p.is_synthetic) return '[合成]';
    return '';
  }

  $: snapshotResult = snapshotView ? snapshotView.result : null;
  $: livePreview = groupData ? groupData.live_preview : null;

  // Per-member spread at a chosen anchor-relative time (read from the frozen
  // snapshot result), so the lead can review within-group differences without
  // mistaking them for a causal or stability conclusion.
  let diffTau = 0;
  $: diffPoint = (function () {
    if (!snapshotResult || !snapshotResult.aggregate) return null;
    const pts = snapshotResult.aggregate.bean_temp.points;
    const target = Number(diffTau) || 0;
    return pts.reduce(
      (best, p) =>
        best === null || Math.abs(p.t_rel_s - target) < Math.abs(best.t_rel_s - target)
          ? p
          : best,
      null
    );
  })();
</script>

<header style="padding:14px 20px;border-bottom:1px solid var(--line)">
  <h1>咖啡烘焙批次曲线 · 过程记录对比</h1>
  <div class="muted" style="margin-top:2px">
    豆温 / 环境温度 / 操作事件的过程视角 · 合成数据离线运行，<b>未连接真实烘焙机</b>
  </div>
</header>

<main style="padding:16px 20px;display:flex;flex-direction:column;gap:14px">
  {#if error}
    <div class="warn">⚠ {error}</div>
  {/if}

  <section class="panel">
    <div class="row" style="align-items:flex-end">
      <div>
        <div class="muted">数据</div>
        {#if batches.length === 0}
          <button on:click={doSeed}>① 生成两个合成批次（含噪声/不均采样/探针缺测）</button>
        {:else}
          <button class="ghost" on:click={doSeed}>重新生成合成批次</button>
        {/if}
        <button class="ghost" style="margin-left:8px" on:click={doSeedControl}>
          ② 生成本地确定性对照批次（完整 samples + 全关键事件，仅本地）
        </button>
      </div>
      <div>
        <div class="muted">视图</div>
        <label class="inline">
          <input type="radio" bind:group={view} value="single" on:change={refresh} />单批次
        </label>
        <label class="inline">
          <input type="radio" bind:group={view} value="compare" on:change={refresh} />双批次对比
        </label>
        <label class="inline">
          <input type="radio" bind:group={view} value="group" on:change={() => loadGroups(false)} />批次组比较（3–8）
        </label>
      </div>
      <div>
        <div class="muted">批次 A</div>
        <select bind:value={selA} on:change={refresh}>
          {#each batches as b}
            <option value={b.id}>{provenanceBadge(b)} {b.name} · {b.bean}</option>
          {/each}
        </select>
      </div>
      {#if view === 'compare'}
        <div>
          <div class="muted">批次 B</div>
          <select bind:value={selB} on:change={refresh}>
            {#each batches as b}
              <option value={b.id}>{provenanceBadge(b)} {b.name} · {b.bean}</option>
            {/each}
          </select>
        </div>
      {/if}
    </div>

    <div class="row" style="margin-top:12px;align-items:flex-end">
      <label class="inline">
        RoR 回归窗口
        <input
          type="number"
          min="5"
          max="300"
          step="5"
          bind:value={windowS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        显示平滑（仅 RoR 曲线）
        <input
          type="number"
          min="0"
          max="180"
          step="3"
          bind:value={smoothS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        最大插值桥接
        <input
          type="number"
          min="5"
          max="600"
          step="5"
          bind:value={maxGapFillS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      {#if loading}<span class="muted">{loading}</span>{/if}
    </div>
    <div class="muted" style="margin-top:6px;font-size:12px">
      RoR 口径：在每个实测时刻，对居中 ±{(windowS / 2).toFixed(0)}s 时间窗内的<b>实测</b>豆温点做最小二乘直线拟合取斜率（°C/min），
      至少 4 个点且跨度 ≥10s 才出值；插值点不参与拟合，缺测宽缺口处 RoR 断档。调整窗口/平滑<b>只改变派生曲线，不改原始温度</b>。
    </div>
  </section>

  {#if dataA && view !== 'group'}
    <section class="panel">
      <RoastChart {chartPayloads} {windowS} {smoothS} />
      <div class="row" style="margin-top:6px;font-size:12px">
        <span class="tag">圆点＝实测豆温</span>
        <span class="tag">虚线菱形＝线性插值（非实测）</span>
        <span class="tag">细点线＝环境温度</span>
        <span class="tag">金色竖虚线＝风门变化</span>
        <span class="tag">曲线断档＝缺测未桥接</span>
      </div>
      {#if comparePayload}
        <div class="warn" style="margin-top:8px">{comparePayload.interpretation}</div>
      {/if}
    </section>

    <section class="row">
      <div class="panel col">
        <h2>阶段指标（明确区间）</h2>
        <div class="row" style="gap:8px">
          {#each view === 'compare' && dataB ? [dataA, dataB] : [dataA] as pl, i}
            <div style="flex:1;min-width:260px">
              <div class="muted" style="margin-bottom:4px">
                {i === 0 ? 'A' : 'B'} · {pl.batch.name}
              </div>
              <table>
                <tr><th>阶段</th><th>区间定义</th><th>时长</th><th>来源</th></tr>
                {#each phaseKeys as [key, label, def]}
                  <tr>
                    <td>{label}</td>
                    <td class="muted" style="font-size:11px">{def}</td>
                    <td>{fmtTime(pl.metrics[key])}</td>
                    <td style="font-size:11px">
                      {#if key === 'drying_s'}
                        <span class="tag {pl.metrics.anchors.turning_point?.source}">
                          {pl.metrics.anchors.turning_point?.source || '—'}
                        </span>
                      {:else if key === 'maillard_s'}
                        <span class="tag {pl.metrics.anchors.first_crack_start?.source}">
                          {pl.metrics.anchors.first_crack_start?.source || '—'}
                        </span>
                      {:else if key === 'development_s' || key === 'first_crack_window_s'}
                        <span class="tag {pl.metrics.anchors.first_crack_start?.source}">
                          FC {pl.metrics.anchors.first_crack_start?.source || '—'}
                        </span>
                      {/if}
                    </td>
                  </tr>
                {/each}
                <tr>
                  <td><b>发展时间比 DTR</b></td>
                  <td class="muted" style="font-size:11px">发展期 / 总时长</td>
                  <td>
                    <b>
                      {pl.metrics.development_ratio !== null
                        ? (pl.metrics.development_ratio * 100).toFixed(1) + '%'
                        : '—'}
                    </b>
                  </td>
                  <td></td>
                </tr>
              </table>
            </div>
          {/each}
        </div>
      </div>

      <div class="panel col">
        <h2>人工修正事件（批次 A）· 保留来源</h2>
        <div class="row" style="gap:8px;align-items:flex-end">
          <div>
            <div class="muted">类型</div>
            <select bind:value={newEventType}>
              {#each Object.entries(EVENT_LABELS) as [k, v]}
                {#if k !== 'charge'}<option value={k}>{v}</option>{/if}
              {/each}
            </select>
          </div>
          <div>
            <div class="muted">时间 m:ss</div>
            <input bind:value={newEventTime} placeholder="1:05" style="width:80px" />
          </div>
          {#if newEventType === 'damper_change'}
            <div>
              <div class="muted">新风门 %</div>
              <input bind:value={newEventDamper} type="number" min="0" max="100" style="width:80px" />
            </div>
          {/if}
          <button on:click={submitEvent}>提交修正</button>
          <label class="inline" style="align-self:center">
            <input type="checkbox" bind:checked={showHistory} on:change={refresh} />
            显示已被取代的旧值
          </label>
        </div>

        <table style="margin-top:10px">
          <tr><th>事件</th><th>时间</th><th>来源</th><th>备注</th><th>状态</th></tr>
          {#each eventHistory as e}
            <tr style={e.superseded ? 'opacity:.45' : ''}>
              <td>
                {EVENT_LABELS[e.event_type] || e.event_type}
                {e.value_num !== null && e.value_num !== undefined ? ` → ${e.value_num}%` : ''}
              </td>
              <td>{fmtTime(e.t_s)}</td>
              <td>
                <span class="tag {e.source}">{e.source === 'manual' ? '人工' : '自动建议'}</span>
                {e.created_by}
              </td>
              <td class="muted" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis">
                {e.label}
              </td>
              <td>{e.superseded ? '已被修正取代（保留）' : '当前'}</td>
            </tr>
          {/each}
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>缺测与插值审计 · 导出可复现</h2>
      <div class="row">
        <div style="flex:1;min-width:280px">
          <table>
            <tr><th>通道</th><th>起(s)</th><th>止(s)</th><th>缺测点</th><th>处理</th></tr>
            {#each dataA.series.missing_segments as g}
              <tr>
                <td>{g.channel === 'bean' ? '豆温' : '环境'}</td>
                <td>{g.t_start_s.toFixed(1)}</td>
                <td>{g.t_end_s.toFixed(1)}</td>
                <td>{g.n_missing}</td>
                <td>
                  {#if g.status === 'interpolated'}
                    <span style="color:#f3c98b">线性插值并标记（非实测）</span>
                  {:else if g.status === 'wide_unfilled'}
                    <span style="color:#e35d5d">缺口超 {maxGapFillS}s，不桥接（曲线断档）</span>
                  {:else}
                    端点缺测，不填充
                  {/if}
                </td>
              </tr>
            {/each}
          </table>
          <div class="muted" style="font-size:12px;margin-top:6px">
            实测豆温 {dataA.series.raw_points.filter((p) => p.bean_temp_c !== null).length} /
            总点 {dataA.series.raw_points.length}；
            插值点 {dataA.series.interpolated_t_s.length} 个，仅用于引导线，不写回原始采样表。
          </div>
        </div>
        <div style="flex:1;min-width:280px">
          <button on:click={verifyExport}>
            ② 导出 JSON 并用 /api/recompute 重算全部阶段指标
          </button>
          {#if verifyResult}
            <table style="margin-top:10px">
              <tr><th>指标</th><th>导出值</th><th>独立重算</th><th>一致</th></tr>
              {#each verifyResult.rows as r}
                <tr>
                  <td>{r.key}</td>
                  <td>{r.exported ?? '—'}</td>
                  <td>{r.recomputed ?? '—'}</td>
                  <td>{r.match ? '✅' : '❌'}</td>
                </tr>
              {/each}
            </table>
            <div style="margin-top:8px">
              <span class="{verifyResult.rawSame ? '' : 'warn'}">
                改变窗口/平滑后原始豆温/环温逐点比对：
                {verifyResult.rawSame ? '✅ 完全不变' : '❌ 被修改'}
              </span>
              <button class="ghost" style="margin-left:10px" on:click={downloadExport}>
                下载导出 JSON
              </button>
            </div>
          {/if}
        </div>
      </div>
    </section>
  {/if}

  {#if view === 'group'}
    <section class="panel">
      <h2>批次组比较 · 3–8 个批次 · 事件锚点对齐</h2>
      <div class="warn" style="margin-bottom:10px">
        中位趋势与离散带只在<b>每个入组成员都有非插值实测样本</b>支持的网格点计算；
        聚合曲线不是任何一次真实测量，样本量 3–8、无随机对照、无统计检验，<b>不构成稳定性或因果结论</b>。
        含 [本地合成对照] 的结果只能表述为本地合成演示，不来自真实烘焙机，也不是上传数据。
      </div>

      <div class="row" style="align-items:flex-end;gap:12px;flex-wrap:wrap">
        <div>
          <div class="muted">已有组</div>
          <select on:change={(e) => (e.target.value ? openGroup(Number(e.target.value)) : startNewGroup())}>
            <option value="">— 新建组 —</option>
            {#each groups as g}
              <option value={g.id} selected={groupData && groupData.id === g.id}>
                {g.name}（rev {g.revision}，{g.n_members} 成员{g.n_snapshots ? `，快照 v${g.latest_snapshot_version}` : ''}）
              </option>
            {/each}
          </select>
        </div>
        <div>
          <div class="muted">新组名称</div>
          <input bind:value={groupForm.name} placeholder="例如：一爆附近稳定性观察组" style="width:220px" />
        </div>
        <div>
          <div class="muted">事件锚点</div>
          <select bind:value={groupForm.anchor_event}>
            {#each Object.entries(ANCHOR_LABELS) as [k, v]}
              <option value={k}>{v}</option>
            {/each}
          </select>
        </div>
        <div>
          <div class="muted">网格步长 s</div>
          <input type="number" min="1" max="60" bind:value={groupForm.grid_step_s} style="width:64px" />
        </div>
        <div>
          <div class="muted">实测支持半径 s</div>
          <input type="number" min="0.5" max="30" step="0.5" bind:value={groupForm.support_tolerance_s} style="width:64px" />
        </div>
        <div>
          <div class="muted">长断档桥接上限 s</div>
          <input type="number" min="5" max="600" bind:value={groupForm.max_gap_fill_s} style="width:64px" />
        </div>
      </div>

      <div style="margin-top:10px">
        <div class="muted">成员（勾选 3–8 个；重复批次无法加入）</div>
        <div class="row" style="gap:6px;flex-wrap:wrap;margin-top:4px">
          {#each batches as b}
            <label class="inline" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px">
              <input
                type="checkbox"
                checked={selectedBatchIds.includes(b.id)}
                on:change={() => toggleBatchInForm(b.id)}
              />
              {provenanceBadge(b)} {b.name}
            </label>
          {/each}
        </div>
        <div class="muted" style="margin-top:4px;font-size:12px">
          已选 {selectedBatchIds.length} 个（允许 3–8）。RoR 回归窗口沿用上方参数（{windowS}s 窗口 / {smoothS}s 显示平滑）。
        </div>
      </div>

      <div class="row" style="margin-top:10px">
        {#if !groupData}
          <button on:click={submitNewGroup}>建立批次组并冻结为快照</button>
        {:else}
          <button on:click={saveMembership}>保存成员修改（版本 {groupData.revision}）</button>
          <button class="ghost" on:click={saveGroupParams}>保存锚点/参数修改（版本 {groupData.revision}）</button>
          <button on:click={cutSnapshot}>固化新组快照</button>
        {/if}
        {#if loading}<span class="muted">{loading}</span>{/if}
      </div>
    </section>

    {#if groupData}
      <section class="panel">
        <h2>
          组 {groupData.name}
          <span class="muted" style="font-size:13px;font-weight:normal">
            · revision {groupData.revision} · 锚点：{ANCHOR_LABELS[groupData.anchor_event]}
          </span>
        </h2>

        {#if livePreview}
          <div class="row" style="gap:10px;align-items:center;margin-bottom:8px">
            <span class="tag">入组 {livePreview.n_included}</span>
            <span class="tag" style={livePreview.n_excluded ? 'color:#e3a0a0' : ''}>
              排除 {livePreview.n_excluded}
            </span>
            {#if livePreview.aggregate_summary}
              <span class="tag">全支持网格点 {livePreview.aggregate_summary.fully_supported_points}</span>
            {/if}
            {#if livePreview.provenance.contains_deterministic_control}
              <span class="tag" style="color:#f3c98b">含本地合成对照批次 · 仅演示</span>
            {/if}
          </div>
          {#if livePreview.demo_warning}
            <div class="warn" style="margin-bottom:8px">{livePreview.demo_warning}</div>
          {/if}
        {/if}

        <table>
          <tr>
            <th>成员</th><th>来源</th><th>锚点版本</th><th>锚点时间</th>
            <th>状态 / 排除原因</th><th>曲线</th>
          </tr>
          {#each groupData.members as m}
            <tr style={m.excluded ? 'opacity:.7' : ''}>
              <td>{provenanceBadge(m.batch)} {m.batch.name}</td>
              <td style="font-size:11px">
                {#if m.batch.synthetic_kind === 'deterministic_control'}
                  deterministic_control
                {:else if m.batch.is_synthetic}
                  合成 {m.batch.synthetic_kind || ''}
                {:else}
                  非合成
                {/if}
              </td>
              <td>
                {#if m.anchor}
                  <span class="tag {m.anchor.source}">
                    #{m.anchor.event_id} · {m.anchor.source === 'manual' ? '人工' : '自动'}
                  </span>
                {:else}
                  —
                {/if}
              </td>
              <td>{m.anchor ? fmtTime(m.anchor.t_s) : '—'}</td>
              <td>
                {#if m.excluded}
                  <span style="color:#e3a0a0">
                    排除：{EXCLUSION_LABELS[m.exclusion_reason] || m.exclusion_reason}
                  </span>
                  {#if m.exclusion_detail && m.exclusion_detail.neighbour_span_s}
                    <div class="muted" style="font-size:11px">
                      缺口跨 {m.exclusion_detail.neighbour_span_s}s（桥接上限
                      {m.exclusion_detail.max_gap_fill_s}s），插值不硬补
                    </div>
                  {/if}
                {:else}
                  纳入
                {/if}
              </td>
              <td>
                {#if !m.excluded}
                  <label class="inline">
                    <input type="checkbox" bind:checked={memberVisibility[m.batch_id]} />
                    显示原始曲线
                  </label>
                {/if}
              </td>
            </tr>
          {/each}
        </table>

        <div class="row" style="margin-top:8px;align-items:center;gap:10px">
          <label class="inline">
            <input type="checkbox" bind:checked={showRawCurves} />叠加成员原始曲线（含插值/断档身份）
          </label>
          <label class="inline">
            <input type="checkbox" bind:checked={showGroupRor} />显示中位 RoR
          </label>
        </div>

        <div class="row" style="margin-top:10px;gap:8px;align-items:flex-start">
          <div style="flex:0 0 260px">
            <div class="muted">组快照（不可变版本）</div>
            {#each groupData.snapshots as sn}
              {@const stale = groupData.snapshot_staleness[String(sn.version)]}
              <div style="border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin-top:6px">
                <button class="ghost" on:click={() => openSnapshot(sn.version)}>
                  v{sn.version}（rev {sn.group_revision} · 入{sn.n_included}/排{sn.n_excluded}）
                </button>
                {#if stale && stale.is_stale}
                  <div style="color:#e3a0a0;font-size:11px;margin-top:2px">
                    ⚠ 当前组已过期于该快照（成员/锚点/参数变化）；旧快照仍可回放，可固化新快照
                  </div>
                {:else}
                  <div class="muted" style="font-size:11px;margin-top:2px">与当前组一致</div>
                {/if}
              </div>
            {/each}
            {#if groupData.snapshots.length === 0}
              <div class="muted" style="font-size:12px;margin-top:4px">尚无快照。</div>
            {/if}
          </div>

          {#if snapshotResult}
            <div style="flex:1;min-width:320px">
              {#if snapshotView.staleness.is_stale}
                <div class="warn">
                  ⚠ 快照 v{snapshotView.snapshot.version} 对当前组已过期：
                  {#each snapshotView.staleness.reasons as r}<div style="font-size:11px">· {r}</div>{/each}
                  旧结果仍可完整回放；需要时请固化新快照。
                </div>
              {/if}
              <GroupChart
                result={snapshotResult}
                showRaw={showRawCurves}
                showRor={showGroupRor}
                visibleMembers={memberVisibility}
              />
              <div style="font-size:12px;margin-top:6px">
                <span class="tag">spec_sha {snapshotView.snapshot.spec_sha256.slice(0, 12)}</span>
                <span class="tag">result_sha {snapshotView.snapshot.result_sha256.slice(0, 12)}</span>
                {#if snapshotView.replay_check.matches}
                  <span style="color:#8fd08a">✓ 从快照重算哈希一致</span>
                {:else}
                  <span class="warn">✗ 重算不一致</span>
                {/if}
              </div>
              <div class="muted" style="margin-top:6px;font-size:12px">{snapshotResult.interpretation}</div>
              {#if snapshotResult.demo_warning}
                <div class="warn" style="margin-top:6px">{snapshotResult.demo_warning}</div>
              {/if}

              {#if snapshotResult.aggregate}
                <div style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px">
                  <h3 style="margin:0 0 4px;font-size:14px">组内差异审阅（观察值，非因果/稳定性结论）</h3>
                  <label class="inline" style="font-size:12px">
                    锚点相对时间 τ
                    <input
                      type="number"
                      bind:value={diffTau}
                      step={snapshotResult.aggregate.grid_step_s}
                      style="width:70px;margin-left:6px"
                    />
                    s
                  </label>
                  {#if diffPoint}
                    <table style="margin-top:6px">
                      <tr>
                        <th>成员</th><th>来源</th><th>实测 τ(s)</th><th>豆温 °C</th>
                        <th>与中位差 °C</th><th>支持</th>
                      </tr>
                      {#each snapshotResult.aggregate.member_order as mo, i}
                        {@const pm = diffPoint.per_member[i]}
                        <tr>
                          <td>{mo.batch_name}</td>
                          <td style="font-size:11px">
                            {memberProvenanceTag(
                              snapshotResult.members.find((x) => x.batch_id === mo.batch_id)
                            )}
                          </td>
                          <td>{pm.supported ? pm.t_rel_s : '缺测/无实测'}</td>
                          <td>{pm.supported ? pm.value_c : '—'}</td>
                          <td>
                            {pm.supported && diffPoint.fully_supported
                              ? (pm.value_c - diffPoint.median).toFixed(2)
                              : '—'}
                          </td>
                          <td>{pm.supported ? '✓' : '✗ 该点断档'}</td>
                        </tr>
                      {/each}
                    </table>
                    <div class="muted" style="font-size:11px;margin-top:4px">
                      网格点 τ={diffPoint.t_rel_s}s：
                      {#if diffPoint.fully_supported}
                        全部 {diffPoint.n_supporting} 个成员在 ±{snapshotResult.aggregate.support_tolerance_s}s
                        内各有非插值实测样本；中位 {diffPoint.median}°C，IQR {diffPoint.q25}–{diffPoint.q75}°C，
                        极差 {diffPoint.min}–{diffPoint.max}°C（观察散布，非置信区间）。
                      {:else}
                        仅 {diffPoint.n_supporting}/{snapshotResult.aggregate.member_order.length}
                        个成员有实测支持，聚合在该点断档（不插值硬补）。
                      {/if}
                    </div>
                  {/if}
                </div>
              {/if}

              <div class="row" style="margin-top:8px">
                <button on:click={verifySnapshotReplay}>
                  ③ 导出该快照并 POST /api/group-replay 独立重算比对
                </button>
                <button class="ghost" on:click={downloadSnapshotExport}>下载快照导出 JSON</button>
              </div>
              {#if replayVerify}
                <div style="margin-top:8px;font-size:13px">
                  {#if replayVerify.matches}
                    <span style="color:#8fd08a">
                      ✅ 独立重算复现同一成员集合、参数、排除记录与聚合结果（入{replayVerify.nIncluded}/排{replayVerify.nExcluded}）
                    </span>
                  {:else}
                    <span class="warn">❌ 重算结果与快照不一致</span>
                  {/if}
                  <div class="muted" style="font-size:11px;margin-top:2px">{replayVerify.resultSha.slice(0, 20)}…</div>
                  {#if replayVerify.demoWarning}
                    <div class="warn" style="margin-top:4px">{replayVerify.demoWarning}</div>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        </div>
      </section>
    {/if}
  {/if}
</main>
