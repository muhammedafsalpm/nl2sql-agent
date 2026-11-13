import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import type { ChartSpec } from "../types";
import { KpiCard } from "./KpiCard";

const PALETTE = [
  "#4f9cf9", "#2ec4b6", "#f4a261", "#e76f51", "#9b8cf7",
  "#57cc99", "#f6bd60", "#e5989b", "#84a59d", "#b8c0ff",
];

const AXIS = "#8b93a7";
const GRID_LINE = "rgba(139, 147, 167, 0.18)";

/**
 * Translates a backend ChartSpec into an ECharts option.
 *
 * Deliberately dumb: no type selection, no sorting, no capping, no aggregation. The server
 * already decided all of that, and its numbers are the executed query's numbers.
 */
function toOption(spec: ChartSpec): echarts.EChartsOption {
  const base: echarts.EChartsOption = {
    color: PALETTE,
    textStyle: { fontFamily: "Inter, system-ui, sans-serif", color: AXIS },
    tooltip: { trigger: spec.type === "donut" ? "item" : "axis" },
    animationDuration: 400,
  };

  if (spec.type === "donut") {
    const values = spec.series[0]?.data ?? [];
    return {
      ...base,
      legend: { bottom: 0, textStyle: { color: AXIS }, type: "scroll" },
      series: [
        {
          type: "pie",
          radius: ["45%", "72%"],
          center: ["50%", "45%"],
          avoidLabelOverlap: true,
          itemStyle: { borderWidth: 2, borderColor: "transparent" },
          label: { show: false },
          data: spec.labels.map((name, i) => ({ name, value: values[i] ?? 0 })),
        },
      ],
    };
  }

  const multi = spec.series.length > 1;
  return {
    ...base,
    grid: { left: 8, right: 16, top: multi ? 40 : 16, bottom: 8, containLabel: true },
    legend: multi ? { top: 0, textStyle: { color: AXIS } } : undefined,
    xAxis: {
      type: "category",
      data: spec.labels,
      name: spec.x_axis || undefined,
      nameLocation: "middle",
      nameGap: 34,
      nameTextStyle: { color: AXIS },
      axisLabel: { color: AXIS, rotate: spec.labels.some((l) => l.length > 8) ? 35 : 0 },
      axisLine: { lineStyle: { color: GRID_LINE } },
    },
    yAxis: {
      type: "value",
      name: spec.y_axis || undefined,
      nameTextStyle: { color: AXIS },
      axisLabel: { color: AXIS },
      splitLine: { lineStyle: { color: GRID_LINE } },
    },
    series: spec.series.map((s) => ({
      name: s.name,
      type: spec.type === "line" ? "line" : "bar",
      data: s.data,
      smooth: false,
      symbolSize: 6,
      barMaxWidth: 44,
      itemStyle: { borderRadius: spec.type === "bar" ? [4, 4, 0, 0] : 0 },
    })),
  };
}

export function Chart({ spec }: { spec: ChartSpec }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (spec.type === "kpi" || !host.current) return;
    const chart = echarts.init(host.current, undefined, { renderer: "canvas" });
    chart.setOption(toOption(spec));

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [spec]);

  return (
    <figure className="chart">
      {spec.title && <figcaption className="chart-title">{spec.title}</figcaption>}
      {spec.type === "kpi" ? (
        <KpiCard spec={spec} />
      ) : (
        <div ref={host} className="chart-canvas" />
      )}
      {spec.note && <p className="chart-note">{spec.note}</p>}
    </figure>
  );
}
