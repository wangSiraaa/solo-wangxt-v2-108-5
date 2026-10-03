<script>
  // Group chart: anchor-aligned median trend + q1–q3 dispersion band.
  //
  // Honesty rules:
  //  - the aggregate exists only where ALL members have raw measured support;
  //    elsewhere the band is simply absent (no bridging);
  //  - each member's raw curve can be toggled, plotted as measured dots plus
  //    the flagged dashed interpolation segments and broken across gaps;
  //  - the synthetic control member is labelled in every legend entry;
  //  - the title states anchor, grid basis and the non-causal boundary.
  import { onMount, onDestroy } from 'svelte';
  import * as echarts from 'echarts';
  import { fmtTau } from './api.js';

  export let result = null; // group analysis result (live or frozen replay)
  export let params = { grid_step_s: 10 };
  export let showMembers = true;
  export let hiddenMembers = []; // batch ids toggled off
  export let showInterpolation = true;
  export let frozenVersion = null; // when showing an immutable snapshot

  let el;
  let chart;

  const MEMBER_COLORS = ['#e07a3f', '#4aa3df', '#7fd187', '#c98be0', '#e3d04a', '#e38bc0', '#5fd0d0', '#d4af37'];

  function memberColor(id, members) {
    const ids = members.map((m) => m.batch_id);
    return MEMBER_COLORS[ids.indexOf(id) % MEMBER_COLORS.length];
  }

  function memberLabel(m) {
    const tag = m.is_control_batch ? '〔本地合成对照·演示〕' : m.is_local_synthetic ? '〔本地合成〕' : '';
    return `${m.batch_name}${tag}`;
  }

  function rawRuns(points, key) {
    // split raw points into measured vs interpolated runs in tau space
    const measured = [];
    const interp = [];
    let curM = [];
    let curI = [];
    let prevInterp = null;
    const push = () => {
      if (curM.length) measured.push(curM);
      if (curI.length) interp.push(curI);
      curM = [];
      curI = [];
    };
    for (const p of points) {
      const tau = p.tau_s;
      const v = p[key];
      const isI = p.is_interpolated;
      if (v === null || v === undefined) {
        push();
        prevInterp = null;
        continue;
      }
      if (prevInterp !== null && isI !== prevInterp) push();
      prevInterp = isI;
      (isI ? curI : curM).push([tau, v]);
    }
    push();
    return { measured, interp };
  }

  function buildOption() {
    if (!result) return {};
    const members = result.members;
    const anchorT = (m) => (m.anchor ? m.anchor.t_s : null);
    const series = [];

    // member raw curves first (lower z), so the band reads on top
    if (showMembers) {
      members
        .filter((m) => m.included && !hiddenMembers.includes(m.batch_id))
        .forEach((m) => {
          const c = memberColor(m.batch_id, members);
          const a = anchorT(m);
          const pts = m.series.raw_points.map((p) => ({ ...p, tau_s: a === null ? null : p.t_s - a }));
          const { measured, interp } = rawRuns(pts, 'bean_temp_c');
          measured.forEach((run, i) => {
            series.push({
              name: i === 0 ? memberLabel(m) : `${m.batch_name} raw ${i}`,
              type: 'line',
              data: run,
              showSymbol: false,
              lineStyle: { width: 1.2, color: c, opacity: 0.85 },
              itemStyle: { color: c },
              z: 2,
              tooltip: { show: false },
              legendHoverLink: false,
            });
          });
          series.push({
            name: `${m.batch_name} 实测点`,
            type: 'scatter',
            data: pts.filter((p) => !p.is_interpolated && p.bean_temp_c !== null).map((p) => [p.tau_s, p.bean_temp_c]),
            symbolSize: 3,
            itemStyle: { color: c },
            z: 3,
          });
          if (showInterpolation) {
            interp.forEach((run) => {
              series.push({
                name: `${m.batch_name} 插值段(非实测)`,
                type: 'line',
                data: run,
                showSymbol: false,
                lineStyle: { width: 1, color: c, type: 'dashed', opacity: 0.7 },
                z: 2,
                tooltip: { show: false },
                legendHoverLink: false,
              });
            });
          }
        });
    }

    // q1–q3 dispersion band
    const nInc = result.aggregate.n_included_members;
    const medianName = `中位趋势（每点 ${nInc} 成员均有非插值实测）`;
    const bandName = '离散带 q1–q3（成员横截面，非置信区间）';
    const pts = result.aggregate.points;
    const band = pts.map((p) => [p.tau_s, p.q1_c, p.q3_c]);
    series.push({
      name: bandName,
      type: 'line',
      data: band.map(([t, lo]) => [t, lo]),
      lineStyle: { opacity: 0 },
      stack: 'band',
      symbol: 'none',
      z: 4,
      tooltip: { show: false },
      silent: true,
    });
    series.push({
      name: bandName,
      type: 'line',
      data: band.map(([t, lo, hi]) => [t, +(hi - lo).toFixed(3)]),
      lineStyle: { opacity: 0 },
      areaStyle: { color: 'rgba(224,122,63,0.22)' },
      stack: 'band',
      symbol: 'none',
      z: 4,
      silent: true,
    });
    series.push({
      name: medianName,
      type: 'line',
      data: pts.map((p) => [p.tau_s, p.median_c]),
      showSymbol: true,
      symbolSize: 5,
      lineStyle: { width: 2.6, color: '#ffb36b' },
      itemStyle: { color: '#ffb36b' },
      connectNulls: false,
      z: 5,
    });

    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#2c2621',
        borderColor: '#3a322b',
        textStyle: { color: '#efe7dd' },
        valueFormatter: (v) => (v === null || v === undefined ? '缺测/不支持' : Number(v).toFixed(1)),
      },
      legend: {
        type: 'scroll',
        top: 0,
        textStyle: { color: '#a89b8c', fontSize: 10 },
        data: [medianName, bandName],
      },
      grid: { left: 56, right: 30, top: 44, bottom: 42 },
      xAxis: {
        type: 'value',
        name: `相对锚点「${result.anchor_event_type}」τ (秒)`,
        nameTextStyle: { color: '#a89b8c' },
        axisLabel: {
          color: '#a89b8c',
          formatter: (v) => fmtTau(v),
        },
        splitLine: { lineStyle: { color: '#2a241f' } },
      },
      yAxis: {
        type: 'value',
        name: '豆温 °C',
        min: 80,
        max: 230,
        nameTextStyle: { color: '#a89b8c' },
        axisLabel: { color: '#a89b8c' },
        splitLine: { lineStyle: { color: '#2a241f' } },
      },
      series,
    };
  }

  function render() {
    if (!chart || !result) return;
    chart.setOption(buildOption(), true);
  }

  onMount(() => {
    chart = echarts.init(el, null, { renderer: 'canvas' });
    render();
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
  });

  onDestroy(() => chart && chart.dispose());

  $: { result; params; showMembers; hiddenMembers; showInterpolation; frozenVersion; render(); }
</script>

<div bind:this={el} style="width:100%;height:440px"></div>
