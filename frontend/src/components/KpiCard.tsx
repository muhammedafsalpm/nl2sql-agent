import type { ChartSpec } from "../types";

/** A single figure is a stat card, not a chart. Rendered without ECharts. */
export function KpiCard({ spec }: { spec: ChartSpec }) {
  const value = spec.series[0]?.data[0] ?? 0;
  const label = spec.labels[0] ?? spec.series[0]?.name ?? "";
  const formatted = Number.isInteger(value)
    ? value.toLocaleString()
    : value.toLocaleString(undefined, { maximumFractionDigits: 2 });

  return (
    <div className="kpi">
      <span className="kpi-value">{formatted}</span>
      <span className="kpi-label">{label.replace(/_/g, " ")}</span>
    </div>
  );
}
