<script>
  // One member's original single-batch bean curve in ABSOLUTE charge time —
  // the same honest styling as the single-batch view: measured dots, flagged
  // dashed interpolation, broken across wide gaps, event marks.
  import { onMount, onDestroy } from 'svelte';
  import * as echarts from 'echarts';
  import { fmtTime, originTag } from './api.js';

  export let m;

  let el;
  let chart;

  function buildOption() {
    const pts = m.series.raw_points;
    const measured = pts
      .filter((p) => !p.is_interpolated && p.bean_temp_c !== null)
      .map((p) => [p.t_s, p.bean_temp_c]);

    // measured solid runs
    const mRuns = [];
    let cur = [];
    let prev = null;
    for (const p of pts) {
      const v = p.bean_temp_c;
      if (v === null || p.is_interpolated) {
        if (cur.length) mRuns.push(cur);
        cur = [];
        prev = null;
        continue;
      }
      cur.push([p.t_s, v]);
      prev = v;
    }
    if (cur.length) mRuns.push(cur);

    const iRuns = [];
    cur = [];
    for (const p of pts) {
      const v = p.bean_temp_c;
      if (v === null || !p.is_interpolated) {
        if (cur.length) iRuns.push(cur);
        cur = [];
        continue;
      }
      cur.push([p.t_s, v]);
    }
    if (cur.length) iRuns.push(cur);

    const series = [];
    mRuns.forEach((data, i) =>
      series.push({
        name: i === 0 ? '豆温实测引导线' : '引导线',
        type: 'line',
        data,
        showSymbol: false,
        lineStyle: { width: 2, color: '#4aa3df' },
        z: 3,
        tooltip: { show: false },
        legendHoverLink: false,
      })
    );
    series.push({
      name: '实测点',
      type: 'scatter',
      data: measured,
      symbolSize: 3,
      itemStyle: { color: '#4aa3df' },
      z: 4,
    });
    iRuns.forEach((data) =>
      series.push({
        name: '插值段(非实测)',
        type: 'line',
        data,
        showSymbol: true,
        symbol: 'diamond',
        symbolSize: 6,
        lineStyle: { width: 1.5, type: 'dashed', color: '#4aa3df' },
        itemStyle: { color: 'transparent', borderColor: '#4aa3df' },
        z: 3,
      })
    );
    // env dotted
    series.push({
      name: '环境温度',
      type: 'line',
      showSymbol: false,
      data: pts.map((p) => [p.t_s, p.env_temp_c]),
      lineStyle: { width: 1, opacity: 0.4, type: 'dotted', color: '#8ab4cf' },
      z: 1,
    });

    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#2c2621',
        textStyle: { color: '#efe7dd' },
        valueFormatter: (v) => (v == null ? '缺测' : Number(v).toFixed(1)),
      },
      legend: { data: ['实测点', '插值段(非实测)', '环境温度'], textStyle: { color: '#a89b8c', fontSize: 10 }, top: 0 },
      grid: { left: 56, right: 24, top: 30, bottom: 38 },
      title: {
        text: `${m.batch_name} · ${originTag(m)}`,
        textStyle: { color: '#cfc2b2', fontSize: 12, fontWeight: 'normal' },
        left: 60,
        top: 22,
      },
      xAxis: {
        type: 'value',
        name: '自下豆 (秒)',
        nameTextStyle: { color: '#a89b8c' },
        axisLabel: { color: '#a89b8c', formatter: (v) => fmtTime(v) },
        splitLine: { lineStyle: { color: '#2a241f' } },
      },
      yAxis: {
        type: 'value',
        min: 80,
        max: 230,
        nameTextStyle: { color: '#a89b8c' },
        axisLabel: { color: '#a89b8c' },
        splitLine: { lineStyle: { color: '#2a241f' } },
      },
      series,
    };
  }

  onMount(() => {
    chart = echarts.init(el, null, { renderer: 'canvas' });
    chart.setOption(buildOption(), true);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
  });
  onDestroy(() => chart && chart.dispose());
  $: { m; if (chart) chart.setOption(buildOption(), true); }
</script>

<div bind:this={el} style="width:100%;height:300px"></div>
