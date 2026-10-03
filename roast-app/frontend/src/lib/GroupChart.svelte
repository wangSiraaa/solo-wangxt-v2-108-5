<script>
  // ECharts rendering for an anchor-aligned batch GROUP.
  //
  // Visual honesty rules (same spirit as RoastChart):
  //  - the median trend is drawn only at fully raw-supported grid points;
  //    unsupported points are null so the band breaks instead of bridging;
  //  - the IQR dispersion band is labelled an observational spread, never a
  //    confidence interval;
  //  - each member's original curve remains switchable: measured guide +
  //    flagged dashed interpolation + broken long gaps;
  //  - synthetic / deterministic-control members carry their provenance in
  //    the legend, and a banner marks the whole view a local synthetic demo
  //    when applicable — never a real stability conclusion.
  import { onMount, onDestroy } from 'svelte';
  import * as echarts from 'echarts';
  import { EXCLUSION_LABELS } from './api.js';

  export let result = null; // group_result_v1 payload
  export let showRaw = true;
  export let showRor = false;
  export let visibleMembers = {}; // batch_id -> bool

  let el;
  let chart;

  const PALETTE = ['#e07a3f', '#4aa3df', '#7ec47e', '#c678dd', '#e3d04a', '#56d9cd', '#e35d5d', '#b58f5a'];

  function bound(points, key) {
    // null stays null so the band breaks wherever support is incomplete.
    return points.map((p) => [p.t_rel_s, p[key]]);
  }

  function delta(points, loKey, hiKey) {
    // stacked upper-series values: hi - lo, null wherever the band breaks.
    return points.map((p) => [
      p.t_rel_s,
      p[loKey] === null || p[hiKey] === null ? null : p[hiKey] - p[loKey],
    ]);
  }

  function guideRuns(curve, isInterpRun) {
    const runs = [];
    let cur = [];
    let kind = null;
    const n = curve.t_rel_s.length;
    for (let i = 0; i < n; i++) {
      const v = curve.guide_bean_c[i];
      const interp = curve.point_flags[i].is_interpolated;
      if (v === null || v === undefined) {
        if (cur.length) runs.push({ kind, data: cur });
        cur = [];
        kind = null;
        continue;
      }
      if (kind === null) kind = interp;
      if (interp !== kind) {
        runs.push({ kind, data: cur });
        cur = [];
        kind = interp;
      }
      cur.push([curve.t_rel_s[i], v]);
    }
    if (cur.length) runs.push({ kind, data: cur });
    return runs.filter((r) => r.kind === isInterpRun);
  }

  function buildOption() {
    const series = [];
    const agg = result.aggregate;
    const members = result.members.filter((m) => !m.excluded);

    if (agg) {
      const pts = agg.bean_temp.points;
      // min-max envelope: transparent lower bound + stacked (max-min) area
      series.push({
        name: '__band_min_base',
        type: 'line',
        data: bound(pts, 'min'),
        stack: 'range-envelope',
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'transparent' },
        tooltip: { show: false },
        legendHoverLink: false,
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 1,
        silent: true,
      });
      series.push({
        name: '成员极差 min–max',
        type: 'line',
        data: delta(pts, 'min', 'max'),
        stack: 'range-envelope',
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'rgba(240,180,90,0.10)' },
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 1,
        silent: true,
      });
      // IQR band: transparent q25 base + stacked (q75-q25) area = Q25..Q75
      series.push({
        name: '__iqr_base',
        type: 'line',
        data: bound(pts, 'q25'),
        stack: 'iqr-band',
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'transparent' },
        tooltip: { show: false },
        legendHoverLink: false,
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 2,
        silent: true,
      });
      series.push({
        name: '离散带 Q25–Q75（观察散布，非置信区间）',
        type: 'line',
        data: delta(pts, 'q25', 'q75'),
        stack: 'iqr-band',
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'rgba(240,180,90,0.22)' },
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 2,
        silent: true,
      });
      // median line
      series.push({
        name: `中位豆温趋势（n=${agg.n_members}，全实测支持点）`,
        type: 'line',
        data: pts.map((p) => [p.t_rel_s, p.median]),
        connectNulls: false,
        showSymbol: false,
        lineStyle: { width: 3, color: '#f0b45a' },
        z: 5,
        xAxisIndex: 0,
        yAxisIndex: 0,
        markLine: {
          symbol: 'none',
          silent: true,
          data: [
            {
              xAxis: 0,
              lineStyle: { color: '#e3d04a', type: 'solid', width: 1.5 },
              label: {
                formatter: `锚点：${anchorName(agg.anchor_event)}`,
                color: '#e3d04a',
                fontSize: 10,
                position: 'insideStartTop',
              },
            },
          ],
        },
      });

      if (showRor) {
        series.push({
          name: '中位 RoR（全支持点）',
          type: 'line',
          showSymbol: false,
          connectNulls: false,
          data: agg.ror.points.map((p) => [p.t_rel_s, p.median]),
          lineStyle: { width: 2, color: '#d98fd0', type: 'solid' },
          xAxisIndex: 0,
          yAxisIndex: 1,
          z: 4,
        });
      }
    }

    if (showRaw) {
      members.forEach((m, i) => {
        if (!visibleMembers[m.batch_id]) return;
        const c = PALETTE[i % PALETTE.length];
        const tag = provenanceTag(m);
        const label = `${m.batch_name}${tag ? ' ' + tag : ''}`;
        guideRuns(m.curve, false).forEach((run) => {
          series.push({
            name: label,
            type: 'line',
            data: run.data,
            showSymbol: false,
            lineStyle: { width: 1.3, color: c, opacity: 0.85 },
            xAxisIndex: 0,
            yAxisIndex: 0,
            z: 3,
          });
        });
        guideRuns(m.curve, true).forEach((run) => {
          series.push({
            name: `${label} 插值段(非实测)`,
            type: 'line',
            data: run.data,
            showSymbol: true,
            symbol: 'diamond',
            symbolSize: 5,
            lineStyle: { width: 1, color: c, type: 'dashed', opacity: 0.85 },
            itemStyle: { color: 'transparent', borderColor: c, borderWidth: 1.2 },
            xAxisIndex: 0,
            yAxisIndex: 0,
            z: 3,
          });
        });
        if (showRor) {
          series.push({
            name: `${label} RoR`,
            type: 'line',
            showSymbol: false,
            connectNulls: false,
            data: m.curve.t_rel_s.map((t, j) => [t, m.curve.ror_display[j]]),
            lineStyle: { width: 1, color: c, type: 'dotted', opacity: 0.7 },
            xAxisIndex: 0,
            yAxisIndex: 1,
            z: 2,
          });
        }
      });
    }

    const legendNames = series
      .filter((s) => !s.name.startsWith('__'))
      .filter((s, idx, arr) => arr.findIndex((x) => x.name === s.name) === idx)
      .map((s) => s.name);

    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#2c2621',
        borderColor: '#3a322b',
        textStyle: { color: '#efe7dd' },
        valueFormatter: (v) => (v === null || v === undefined ? '断档' : Number(v).toFixed(1)),
      },
      legend: {
        data: legendNames,
        textStyle: { color: '#a89b8c', fontSize: 10 },
        top: 0,
        type: 'scroll',
      },
      grid: { left: 56, right: 56, top: 44, bottom: 40 },
      xAxis: {
        type: 'value',
        name: '相对锚点时间 τ (秒)',
        nameTextStyle: { color: '#a89b8c' },
        axisLabel: {
          color: '#a89b8c',
          formatter: (v) => `${v >= 0 ? '+' : ''}${Math.round(v)}s`,
        },
        splitLine: { lineStyle: { color: '#2a241f' } },
      },
      yAxis: [
        {
          type: 'value',
          name: '温度 °C',
          min: 80,
          max: 230,
          nameTextStyle: { color: '#a89b8c' },
          axisLabel: { color: '#a89b8c' },
          splitLine: { lineStyle: { color: '#2a241f' } },
        },
        {
          type: 'value',
          name: 'RoR °C/min',
          min: -20,
          max: 30,
          nameTextStyle: { color: '#a89b8c' },
          axisLabel: { color: '#a89b8c' },
          splitLine: { show: false },
        },
      ],
      series,
    };
  }

  function anchorName(t) {
    return {
      charge: '下豆',
      turning_point: '回温点',
      first_crack_start: '一爆开始',
      first_crack_end: '一爆结束',
      drop: '出锅',
    }[t] || t;
  }

  function provenanceTag(m) {
    const p = m.provenance || {};
    if (p.synthetic_kind === 'deterministic_control') return '[本地合成对照]';
    if (p.is_synthetic) return '[合成]';
    return '';
  }

  function render() {
    if (!chart || !result) return;
    chart.setOption(buildOption(), true);
  }

  onMount(() => {
    chart = echarts.init(el, null, { renderer: 'canvas' });
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    render();
  });

  onDestroy(() => chart && chart.dispose());

  $: { result; showRaw; showRor; visibleMembers; render(); }
</script>

<div>
  <div bind:this={el} style="width:100%;height:460px"></div>
  {#if result && result.n_members_excluded > 0}
    <div class="muted" style="font-size:11px;margin-top:4px">
      {#each result.exclusions as e}
        <span class="tag" style="margin-right:8px;color:#e3a0a0">
          排除 {e.batch_name}：{EXCLUSION_LABELS[e.reason] || e.reason}
        </span>
      {/each}
    </div>
  {/if}
</div>
