<script>
  import { onMount } from 'svelte';
  import {
    listGroups,
    createGroup,
    getGroup,
    patchGroup,
    createSnapshot,
    listSnapshots,
    replaySnapshot,
    exportSnapshot,
    recomputeGroup,
    seedControl,
    ANCHOR_LABELS,
    fmtTime,
    fmtTau,
    originTag,
  } from './api.js';
  import GroupChart from './GroupChart.svelte';
  import RawMemberCurve from './RawMemberCurve.svelte';

  export let batches = [];
  export let notify = () => {};
  export let reloadBatches = async () => {};

  let groups = [];
  let selectedGroupId = null;
  let groupDetail = null; // GET /api/groups/{id}
  let snapshots = [];
  let replay = null; // {version, payload} frozen snapshot being reviewed

  // create form
  let chosen = [];
  let anchorType = 'first_crack_start';
  let gridStep = 10;
  let anchorTol = 5;
  let groupName = '';
  let showCreate = false;

  // review controls
  let hiddenMembers = [];
  let showMembers = true;
  let showInterpolation = true;
  let showRawMember = null; // batch id for the single-batch raw curve switch

  // snapshot / recompute
  let publishing = false;
  let snapshotNote = '';
  let recomputeCheck = null;

  $: viewResult = replay ? replay.payload.document.result : groupDetail?.current_analysis || null;
  $: viewParams = replay ? replay.payload.document.params : groupDetail?.current_analysis?.params || null;

  export async function refresh() {
    groups = await listGroups();
    if (selectedGroupId !== null) {
      await loadGroup(selectedGroupId);
    } else if (groups.length) {
      await selectGroup(groups[0].id);
    }
  }

  async function ensureControl() {
    await seedControl();
    await reloadBatches();
    notify('已在本地生成确定性对照批次 C（固定种子，可重复生成；非真实烘焙机数据）。');
  }

  function toggleChosen(id) {
    id = Number(id);
    if (chosen.includes(id)) chosen = chosen.filter((x) => x !== id);
    else if (chosen.length < 8) chosen = [...chosen, id];
  }

  async function submitCreate() {
    if (chosen.length < 3 || chosen.length > 8) {
      notify(`组需要 3–8 个批次（当前 ${chosen.length} 个）。`, true);
      return;
    }
    const body = {
      name: groupName || `批次组 ${groups.length + 1}`,
      anchor_event_type: anchorType,
      batch_ids: chosen.slice().sort((a, b) => a - b),
      grid_step_s: gridStep,
      anchor_support_tolerance_s: anchorTol,
    };
    try {
      const g = await createGroup(body);
      showCreate = false;
      groupName = '';
      await refresh();
      await selectGroup(g.id);
    } catch (e) {
      notify(e.message, true);
    }
  }

  async function selectGroup(id) {
    selectedGroupId = id;
    replay = null;
    recomputeCheck = null;
    hiddenMembers = [];
    snapshots = [];
    await loadGroup(id);
    snapshots = await listSnapshots(id);
  }

  async function loadGroup(id) {
    groupDetail = await getGroup(id);
  }

  async function saveMembers(nextIds) {
    const ids = Array.from(new Set(nextIds));
    if (ids.length < 3 || ids.length > 8) {
      notify(`组必须保持 3–8 个成员（当前 ${ids.length} 个）。`, true);
      await loadGroup(selectedGroupId);
      return;
    }
    try {
      await patchGroup(selectedGroupId, { base_revision: groupDetail.revision, batch_ids: ids });
      await refresh();
      notify('成员已更新（草稿修订号已增加；既有快照不变）。');
    } catch (e) {
      notify(e.status === 409 ? `并发冲突：${e.message}` : e.message, true);
      await loadGroup(selectedGroupId);
    }
  }

  function toggleMemberInGroup(id) {
    id = Number(id);
    const cur = groupDetail.batch_ids.slice();
    if (cur.includes(id)) saveMembers(cur.filter((x) => x !== id));
    else saveMembers([...cur, id]);
  }

  async function publishSnapshot() {
    publishing = true;
    try {
      const r = await createSnapshot(selectedGroupId, {
        base_revision: groupDetail.revision,
        note: snapshotNote,
        created_by: '负责人(界面)',
      });
      snapshotNote = '';
      await refresh();
      snapshots = await listSnapshots(selectedGroupId);
      await viewSnapshot(r.document.version);
      notify(`已发布不可变快照 v${r.document.version}（成员、锚点版本、排除与参数已固化）。`);
    } catch (e) {
      notify(e.status === 409 ? `快照发布被拒：${e.message}` : e.message, true);
    } finally {
      publishing = false;
    }
  }

  async function viewSnapshot(version) {
    const payload = await replaySnapshot(selectedGroupId, version);
    replay = { version, payload };
    recomputeCheck = null;
    hiddenMembers = [];
    showRawMember = null;
  }

  function backToCurrent() {
    replay = null;
    recomputeCheck = null;
    hiddenMembers = [];
    showRawMember = null;
  }

  async function verifyRecompute() {
    if (!replay) return;
    const doc = await exportSnapshot(selectedGroupId, replay.version);
    const rc = await recomputeGroup(doc);
    recomputeCheck = { identical: rc.identical_to_frozen, nPoints: rc.result.aggregate.points.length };
  }

  function downloadExport() {
    if (!replay) return;
    exportSnapshot(selectedGroupId, replay.version).then((doc) => {
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `group-${selectedGroupId}-v${replay.version}-snapshot.json`;
      a.click();
    });
  }

  function toggleHidden(id) {
    id = Number(id);
    hiddenMembers = hiddenMembers.includes(id)
      ? hiddenMembers.filter((x) => x !== id)
      : [...hiddenMembers, id];
  }

  const staleReasonsText = (reasons) =>
    (reasons || [])
      .map((r) =>
        r.code === 'anchor_event_corrected'
          ? `批次 #${r.detail.batch_id} 的锚点事件 (#${r.detail.event_id}) 已被人工修正`
          : r.code === 'members_changed'
            ? `成员已变化（移除 ${r.detail.removed.join(',') || '无'}，新增 ${r.detail.added.join(',') || '无'}）`
            : r.code === 'params_changed'
              ? '分析参数已变化'
              : r.code === 'anchor_type_changed'
                ? '锚点类型已变化'
                : r.code === 'anchor_event_missing'
                  ? `批次 #${r.detail.batch_id} 的锚点事件已不存在`
                  : r.code
      )
      .join('；');

  function memberRow(id) {
    return viewResult?.members.find((m) => m.batch_id === id);
  }

  onMount(refresh);
</script>

<section class="panel">
  <div class="row" style="align-items:flex-end;justify-content:space-between">
    <div>
      <h2 style="margin:0">批次组比较（3–8 批 · 明确事件锚点 · 可版本化快照）</h2>
      <div class="muted" style="font-size:12px;margin-top:2px">
        中位趋势/离散带只在所有纳入成员都有<b>非插值原始实测样本</b>支持的区间计算；
        组内对比是观察，不是统计检验，<b>不构成因果结论</b>。
      </div>
    </div>
    <div class="row" style="gap:8px">
      <button class="ghost" on:click={ensureControl}>＋ 生成本地确定性对照批次 C</button>
      <button on:click={() => (showCreate = !showCreate)}>{showCreate ? '收起' : '新建批次组'}</button>
    </div>
  </div>

  {#if showCreate}
    <div style="margin-top:12px;border-top:1px solid var(--line);padding-top:10px">
      <div class="row" style="gap:10px;align-items:flex-end;flex-wrap:wrap">
        <label class="inline">组名 <input bind:value={groupName} placeholder="例如：一爆稳定性观察组" style="width:200px" /></label>
        <label class="inline">事件锚点
          <select bind:value={anchorType}>
            {#each Object.entries(ANCHOR_LABELS) as [k, v]}<option value={k}>{v}</option>{/each}
          </select>
        </label>
        <label class="inline">网格步长
          <input type="number" min="5" max="120" step="5" bind:value={gridStep} style="width:64px" /> s
        </label>
        <label class="inline">锚点实测容差
          <input type="number" min="1" max="120" step="1" bind:value={anchorTol} style="width:64px" /> s
        </label>
      </div>
      <div class="muted" style="font-size:11px;margin:6px 0">
        锚点 ±{anchorTol}s 内没有原始实测豆温（落在长断档内）的成员会被排除并写明原因，不用插值硬补。
      </div>
      <table>
        <tr><th>加入</th><th>批次</th><th>豆种</th><th>来源</th></tr>
        {#each batches as b}
          <tr>
            <td><input type="checkbox" checked={chosen.includes(b.id)} on:change={() => toggleChosen(b.id)} /></td>
            <td>{b.name}</td>
            <td>{b.bean}</td>
            <td><span class="tag {b.data_origin}">{originTag(b)}</span></td>
          </tr>
        {/each}
      </table>
      <div class="row" style="margin-top:8px;gap:10px;align-items:center">
        <button on:click={submitCreate}>创建组（{chosen.length} 个成员）</button>
        <span class="muted">同一批次重复勾选不会产生重复成员。</span>
      </div>
    </div>
  {/if}

  {#if groups.length}
    <div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">
      {#each groups as g}
        <button class="ghost" class:active={g.id === selectedGroupId} on:click={() => selectGroup(g.id)}>
          {g.name} · v{g.latest_snapshot_version ?? '—'}
          {#if g.latest_snapshot?.staleness?.stale}<span style="color:#e3c84a"> ⚠过期</span>{/if}
        </button>
      {/each}
    </div>
  {/if}
</section>

{#if groupDetail}
  <section class="panel">
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <h3 style="margin:0">{groupDetail.name}</h3>
        <div class="muted" style="font-size:12px">
          锚点：{ANCHOR_LABELS[groupDetail.anchor_event_type] || groupDetail.anchor_event_type} ·
          网格 {groupDetail.params.grid_step_s}s · 容差 ±{groupDetail.params.anchor_support_tolerance_s}s ·
          修订号 {groupDetail.revision}
        </div>
      </div>
      <div class="row" style="gap:8px;align-items:center">
        <input bind:value={snapshotNote} placeholder="快照备注（可选）" style="width:180px" />
        <button on:click={publishSnapshot} disabled={publishing}>{publishing ? '发布中…' : '发布新快照版本'}</button>
      </div>
    </div>

    {#if snapshots.length}
      <div class="row" style="margin-top:8px;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="muted">版本：</span>
        <button class="ghost" class:active={!replay} on:click={backToCurrent}>当前草稿（未固化）</button>
        {#each snapshots as s}
          <button class="ghost" class:active={replay?.version === s.version} on:click={() => viewSnapshot(s.version)}>
            v{s.version}{s.staleness.stale ? ' ⚠过期' : ' ✓'}
          </button>
        {/each}
      </div>
    {/if}

    {#if replay}
      {@const st = replay.payload.replay.staleness_vs_current_group}
      <div class="warn" style="margin-top:8px">
        正在回放不可变快照 <b>v{replay.version}</b>（基线修订号 {replay.payload.document.base_revision}，
        {replay.payload.document.created_at}）。
        {#if st.stale}
          <b style="color:#e3c84a">该快照相对当前组已过期：{staleReasonsText(st.reasons)}</b>；
          旧报告仍可完整回放，请审阅后发布新快照。
        {:else}
          与当前组状态一致。
        {/if}
        重算一致性：{replay.payload.replay.result_identical ? '✅ 字节级一致' : '❌ 不一致'}
      </div>
    {:else}
      {#if groupDetail.latest_snapshot?.staleness?.stale}
        <div class="warn" style="margin-top:8px">
          当前草稿相对最新快照 v{groupDetail.latest_snapshot_version} 已过期：
          {staleReasonsText(groupDetail.latest_snapshot.staleness.reasons)}。
          既有快照不变；确认当前成员/锚点后可发布新快照。
        </div>
      {/if}
    {/if}

    {#if viewResult}
      <!-- provenance / boundary -->
      {#if viewResult.provenance.contains_local_synthetic}
        <div class="warn" style="margin-top:8px;border-color:#7a5a2a">
          🧪 {viewResult.provenance.disclaimer}
          {#if viewResult.provenance.contains_local_synthetic_control}
            本组成员含<b>本地合成对照批次</b>（确定性生成器，未上传）。
          {/if}
          结果不得表述为真实稳定性结论，也不得伪装为来自真实烘焙机的数据。
        </div>
      {/if}
      <div class="muted" style="margin-top:6px;font-size:12px">{viewResult.interpretation}</div>

      <!-- chart controls -->
      <div class="row" style="margin-top:8px;gap:14px;align-items:center;flex-wrap:wrap">
        <label class="inline"><input type="checkbox" bind:checked={showMembers} /> 显示成员原始曲线</label>
        <label class="inline"><input type="checkbox" bind:checked={showInterpolation} /> 显示插值段（虚线，非实测）</label>
        <span class="muted">点击成员可临时隐藏：</span>
        {#each viewResult.members.filter((m) => m.included) as m}
          <label class="inline" style="font-size:12px">
            <input type="checkbox" checked={!hiddenMembers.includes(m.batch_id)} on:change={() => toggleHidden(m.batch_id)} />
            {m.batch_name}{m.is_control_batch ? '〔对照〕' : ''}
          </label>
        {/each}
      </div>

      <GroupChart
        result={viewResult}
        params={viewParams}
        {showMembers}
        {hiddenMembers}
        {showInterpolation}
        frozenVersion={replay?.version ?? null}
      />

      <div class="row" style="margin-top:6px;font-size:12px;gap:10px;flex-wrap:wrap">
        <span class="tag">橙色实线圆点＝成员实测豆温</span>
        <span class="tag">虚线＝插值（非实测）</span>
        <span class="tag">粗黄线＝中位趋势</span>
        <span class="tag">橙色带＝q1–q3 成员横截面（不是置信区间）</span>
        <span class="tag">带中断＝至少一个成员在该 τ 缺非插值实测，不输出聚合</span>
      </div>

      <!-- member review table -->
      <h4 style="margin:14px 0 6px">成员审阅（锚点版本 / 支持 / 组内差异）</h4>
      <table>
        <tr>
          <th>批次</th><th>来源</th><th>状态</th><th>锚点事件版本</th><th>锚点 t</th>
          <th>最近实测点</th><th>实测 τ 区间</th><th>原始曲线</th>
        </tr>
        {#each viewResult.members as m}
          <tr style={m.included ? '' : 'opacity:.65'}>
            <td>{m.batch_name}<div class="muted" style="font-size:11px">{m.bean}</div></td>
            <td>
              <span class="tag {m.data_origin}">{m.is_control_batch ? '本地合成对照' : m.is_local_synthetic ? '本地合成' : '其他'}</span>
              {#if m.generator.seed !== null}<div class="muted" style="font-size:10px">seed {m.generator.seed}</div>{/if}
            </td>
            <td>
              {#if m.included}<span style="color:#7fd187">纳入聚合</span>
              {:else}<span style="color:#e38b6b">排除 · {m.exclusion_reason_code}</span>{/if}
            </td>
            <td>
              {#if m.anchor}
                #{m.anchor.event_id}
                <span class="tag {m.anchor.source}">{m.anchor.source === 'manual' ? '人工' : '自动'}</span>
                {#if m.anchor.superseded}<span class="tag" style="color:#e3c84a">该版本后续被取代</span>{/if}
                <div class="muted" style="font-size:10px">{m.anchor.selection === 'frozen_event_version' ? '冻结版本回放' : '当前版本'}</div>
              {:else}—{/if}
            </td>
            <td>{m.anchor ? fmtTime(m.anchor.t_s) : '—'}</td>
            <td>
              {#if m.anchor_support && m.anchor_support.nearest_measured_delta_s !== null}
                {m.anchor_support.nearest_measured_delta_s}s
                <div class="muted" style="font-size:10px">跨接缺口 {m.anchor_support.bracketing_gap_s ?? '—'}s</div>
              {:else}—{/if}
            </td>
            <td>
              {#if m.included}{fmtTau(m.support_window_tau.lo_tau_s)} … {fmtTau(m.support_window_tau.hi_tau_s)}{:else}—{/if}
            </td>
            <td>
              <button class="ghost" on:click={() => (showRawMember = showRawMember === m.batch_id ? null : m.batch_id)}>
                {showRawMember === m.batch_id ? '收起原始曲线' : '切换查看原始单批次曲线'}
              </button>
            </td>
          </tr>
          {#if !m.included}
            <tr>
              <td colspan="8" style="color:#e3a88b;font-size:12px">排除原因：{m.exclusion_reason}</td>
            </tr>
          {/if}
        {/each}
      </table>

      <!-- raw single-batch curve switch (acceptance: raw curves remain viewable) -->
      {#if showRawMember}
        {@const m = memberRow(showRawMember)}
        <div class="panel col" style="margin-top:8px;background:#241f1a">
          <h4 style="margin:0 0 4px">{m.batch_name} · 原始单批次曲线（自下豆时间，非对齐）</h4>
          <RawMemberCurve {m} />
        </div>
      {/if}

      <!-- within-group dispersion at selected taus -->
      <h4 style="margin:14px 0 6px">组内差异明细（公共支持区间上的横截面）</h4>
      <table>
        <tr><th>τ（相对锚点）</th><th>中位 °C</th><th>q1</th><th>q3</th><th>IQR</th><th>各成员实测 °C</th></tr>
        {#each viewResult.aggregate.points.filter((_, i) => i % 3 === 0) as p}
          <tr>
            <td>{fmtTau(p.tau_s)}</td>
            <td>{p.median_c}</td>
            <td>{p.q1_c}</td>
            <td>{p.q3_c}</td>
            <td>{p.iqr_c}</td>
            <td style="font-size:11px">
              {viewResult.aggregate.member_values
                .filter((mv) => mv.tau_s === p.tau_s)
                .map((mv) => `#${mv.batch_id}:${mv.bean_temp_c}`)
                .join('  ')}
            </td>
          </tr>
        {/each}
      </table>
      <div class="muted" style="font-size:11px;margin-top:4px">
        公共支持窗口：τ {fmtTau(viewResult.aggregate.common_support_window_tau.lo_tau_s)}
        ～ {fmtTau(viewResult.aggregate.common_support_window_tau.hi_tau_s)}；
        {viewResult.aggregate.points.length} 个网格点，规则：{viewResult.aggregate.method.support_rule}
      </div>

      <!-- export / recompute -->
      <div class="row" style="margin-top:12px;gap:10px;align-items:center">
        {#if replay}
          <button on:click={verifyRecompute}>③ 用导出文档独立重算并比对</button>
          <button class="ghost" on:click={downloadExport}>下载快照导出 JSON（含全部 samples 与关键事件）</button>
          {#if recomputeCheck}
            <span>
              {recomputeCheck.identical ? '✅ 重算与冻结结果完全一致' : '❌ 不一致'}
              （{recomputeCheck.nPoints} 个聚合点）
            </span>
          {/if}
        {:else}
          <span class="muted">发布并选择一个快照版本后，可导出含完整 samples/事件的自包含文档并独立重算。</span>
        {/if}
      </div>
    {/if}
  </section>
{/if}

{#if groups.length === 0 && !showCreate}
  <section class="panel muted">
    尚无批次组。先点「＋ 生成本地确定性对照批次 C」，再「新建批次组」（两个既有示例批次 + C 即可组成三条完整批次）。
  </section>
{/if}
