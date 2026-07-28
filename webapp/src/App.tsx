import { BarChart, LineChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import {
  Activity,
  Brain,
  CalendarDays,
  ChevronRight,
  CircleAlert,
  Database,
  Droplets,
  Gauge,
  HeartPulse,
  LayoutDashboard,
  LockKeyhole,
  Menu,
  MoonStar,
  RefreshCw,
  Scale,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api, query } from "./api";
import { Link, NavLink, useLocation } from "./router";
import type {
  DayDetail,
  DayRecord,
  Meta,
  MetricDefinition,
  MetricInterval,
  Overview,
  SeriesPoint,
  SourceInfo,
} from "./types";

const metricIcons: Record<string, typeof Activity> = {
  activity: Activity,
  heart_rate: HeartPulse,
  sleep: MoonStar,
  spo2: Droplets,
  blood_pressure: Gauge,
  weight: Scale,
  stress: Brain,
};

use([
  AriaComponent,
  BarChart,
  CanvasRenderer,
  DataZoomComponent,
  GridComponent,
  LineChart,
  TooltipComponent,
]);

const fidelityLabels: Record<string, string> = {
  exact_source: "原始数据",
  exact_ui: "页面精确值",
  visual_only: "仅视觉证据",
  no_data: "无数据",
  failed: "采集失败",
};

const summaryFallbackLabels: Record<string, string> = {
  sleep_start_utc_ms: "入睡时间",
  sleep_end_utc_ms: "醒来时间",
  latest_measurement_utc_ms: "最近测量",
};

function useRemote<T>(path: string | null, refreshKey = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));

  useEffect(() => {
    let active = true;
    if (!path) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    api<T>(path)
      .then((value) => {
        if (active) setData(value);
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [path, refreshKey]);

  return { data, error, loading, setData };
}

function number(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function valueText(value: unknown, unit = "") {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = number(value);
  let output = String(value);
  if (numeric !== null) {
    if (unit === "kg") output = numeric.toFixed(1);
    else if (Math.abs(numeric) >= 1000)
      output = Math.round(numeric).toLocaleString("zh-CN");
    else output = Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(1);
  }
  return unit ? `${output} ${unit}` : output;
}

function primaryText(definition: MetricDefinition, day: DayRecord | null) {
  if (!day) return { value: "无记录", unit: "" };
  const primary = day.summary[definition.primary_field];
  if (primary === null || primary === undefined) return { value: "无记录", unit: "" };
  if (definition.key === "sleep") {
    const minutes = number(primary);
    if (minutes !== null) {
      return {
        value: `${Math.floor(minutes / 60)} 小时 ${Math.round(minutes % 60)} 分`,
        unit: "",
      };
    }
  }
  if (definition.key === "blood_pressure" && definition.secondary_field) {
    const secondary = day.summary[definition.secondary_field];
    if (secondary !== null && secondary !== undefined) {
      return {
        value: `${valueText(primary)} / ${valueText(secondary)}`,
        unit: definition.primary_unit,
      };
    }
  }
  return { value: valueText(primary), unit: definition.primary_unit };
}

function dateLabel(value?: string | null) {
  if (!value) return "暂无日期";
  const [year, month, day] = value.split("-");
  return `${year}年${Number(month)}月${Number(day)}日`;
}

function timeLabel(value?: string | number | null) {
  if (!value) return "—";
  const date =
    typeof value === "number" ? new Date(value) : new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function bytes(value: number) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function minusDays(value: string, days: number) {
  const date = new Date(`${value}T12:00:00`);
  date.setDate(date.getDate() - days);
  return date.toISOString().slice(0, 10);
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "good" | "warning" | "danger" | "neutral" | "info";
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function DataStateBadge({ fidelity, partial }: { fidelity: string; partial?: boolean }) {
  const tone =
    fidelity === "exact_source" || fidelity === "exact_ui"
      ? "good"
      : fidelity === "failed"
        ? "danger"
        : "warning";
  return (
    <span className="badge-cluster">
      <Badge tone={tone}>{fidelityLabels[fidelity] ?? fidelity}</Badge>
      {partial && <Badge tone="info">当日未完成</Badge>}
    </span>
  );
}

function LoadingBlock({ label = "正在读取健康档案…" }: { label?: string }) {
  return (
    <div className="loading-block" role="status">
      <span className="loading-dot" />
      <span>{label}</span>
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="error-block" role="alert">
      <CircleAlert size={20} />
      <div>
        <strong>数据暂时无法显示</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}

function EmptyBlock({
  title = "这段时间没有记录",
  copy = "这不是零值。当前数据库中没有可展示的数据。",
}: {
  title?: string;
  copy?: string;
}) {
  return (
    <div className="empty-block">
      <Database size={28} />
      <strong>{title}</strong>
      <p>{copy}</p>
    </div>
  );
}

function TrendChart({
  days,
  definition,
  height = 300,
}: {
  days: DayRecord[];
  definition: MetricDefinition;
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const points = useMemo(
    () =>
      days
        .map((day) => ({
          date: day.local_date,
          value: number(day.summary[definition.primary_field]),
        }))
        .filter((point) => point.value !== null),
    [days, definition.primary_field],
  );

  useEffect(() => {
    if (!ref.current || !points.length) return;
    const chart = init(ref.current, undefined, { renderer: "canvas" });
    chart.setOption({
      animationDuration: 450,
      aria: { enabled: true },
      color: [definition.color],
      grid: { left: 18, right: 16, top: 22, bottom: 28, containLabel: true },
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(20,26,24,.94)",
        borderColor: "rgba(255,255,255,.12)",
        textStyle: { color: "#f7fbf8" },
        valueFormatter: (value: unknown) => valueText(value, definition.primary_unit),
      },
      xAxis: {
        type: "category",
        data: points.map((point) => point.date.slice(5)),
        boundaryGap: definition.key === "activity",
        axisLine: { lineStyle: { color: "#d9e1dc" } },
        axisTick: { show: false },
        axisLabel: { color: "#708078", hideOverlap: true },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitNumber: 4,
        axisLabel: { color: "#708078" },
        splitLine: { lineStyle: { color: "rgba(96,119,106,.12)" } },
      },
      dataZoom:
        points.length > 120
          ? [
              { type: "inside", start: 70, end: 100 },
              {
                type: "slider",
                height: 14,
                bottom: 0,
                borderColor: "transparent",
                fillerColor: `${definition.color}33`,
              },
            ]
          : [],
      series: [
        {
          name: definition.name,
          type: definition.key === "activity" ? "bar" : "line",
          data: points.map((point) => point.value),
          symbol: points.length < 40 ? "circle" : "none",
          symbolSize: 6,
          smooth: definition.key !== "activity",
          lineStyle: { width: 2.4 },
          itemStyle: { borderRadius: definition.key === "activity" ? [4, 4, 0, 0] : 0 },
          areaStyle:
            definition.key === "activity"
              ? undefined
              : { color: `${definition.color}18`, opacity: 0.8 },
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [definition, points]);

  if (!points.length) {
    return <EmptyBlock title="没有可绘制的日级趋势" />;
  }
  return (
    <>
      <div
        ref={ref}
        className="chart"
        style={{ height }}
        role="img"
        aria-label={`${definition.name}日级趋势图`}
      />
      <details className="accessible-table">
        <summary>查看趋势数据表</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>日期</th>
                <th>{definition.name}</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.date}>
                  <td>{point.date}</td>
                  <td>{valueText(point.value, definition.primary_unit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

function IntradayChart({
  kind,
  points,
  color,
}: {
  kind: string;
  points: SeriesPoint[];
  color: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || !points.length) return;
    const chart = init(ref.current);
    chart.setOption({
      aria: { enabled: true },
      color: [color],
      grid: { left: 18, right: 14, top: 20, bottom: 32, containLabel: true },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value: unknown) => valueText(value, points[0]?.unit ?? ""),
      },
      xAxis: {
        type: "time",
        axisLabel: { color: "#708078", formatter: "{HH}:{mm}" },
        axisLine: { lineStyle: { color: "#d9e1dc" } },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: "#708078" },
        splitLine: { lineStyle: { color: "rgba(96,119,106,.12)" } },
      },
      dataZoom: [{ type: "inside" }],
      series: [
        {
          name: kind,
          type: kind === "steps" ? "bar" : "line",
          showSymbol: points.length < 50,
          smooth: kind !== "steps",
          data: points.map((point) => [point.timestamp_utc_ms, number(point.value)]),
          areaStyle: kind === "steps" ? undefined : { color: `${color}18` },
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [color, kind, points]);
  return (
    <div
      ref={ref}
      className="chart intraday-chart"
      role="img"
      aria-label={`${kind}日内采样曲线`}
    />
  );
}

function SleepTimeline({ intervals }: { intervals: MetricInterval[] }) {
  if (!intervals.length) return <EmptyBlock title="没有睡眠阶段区间" />;
  const start = Math.min(...intervals.map((item) => item.start_utc_ms));
  const end = Math.max(...intervals.map((item) => item.end_utc_ms));
  const duration = Math.max(end - start, 1);
  const stateClass = (state?: string | null) => {
    const key = String(state ?? "unknown").toLowerCase();
    if (["0", "awake", "wake"].includes(key)) return "awake";
    if (["2", "deep"].includes(key)) return "deep";
    if (["4", "light"].includes(key)) return "light";
    if (["3", "rem"].includes(key)) return "rem";
    return "unknown";
  };
  return (
    <div className="sleep-timeline-wrap">
      <div className="sleep-timeline" aria-label="睡眠阶段时间轴">
        {intervals.map((item, index) => (
          <span
            key={`${item.start_utc_ms}-${index}`}
            className={`sleep-segment sleep-${stateClass(item.state)}`}
            style={{
              left: `${((item.start_utc_ms - start) / duration) * 100}%`,
              width: `${Math.max(((item.end_utc_ms - item.start_utc_ms) / duration) * 100, 0.3)}%`,
            }}
            title={`${timeLabel(item.start_utc_ms)}–${timeLabel(item.end_utc_ms)} · ${item.state ?? "未知"}`}
          />
        ))}
      </div>
      <div className="timeline-axis">
        <span>{timeLabel(start)}</span>
        <span>{timeLabel(end)}</span>
      </div>
      <div className="timeline-legend">
        <span><i className="legend-deep" />深睡</span>
        <span><i className="legend-light" />浅睡</span>
        <span><i className="legend-rem" />快速眼动</span>
        <span><i className="legend-awake" />清醒</span>
      </div>
    </div>
  );
}

function MiniBars({
  days,
  definition,
}: {
  days: DayRecord[];
  definition: MetricDefinition;
}) {
  const values = days
    .slice(-18)
    .map((day) => number(day.summary[definition.primary_field]) ?? 0);
  const max = Math.max(...values, 1);
  return (
    <div className="mini-bars" aria-hidden="true">
      {values.map((value, index) => (
        <i
          key={index}
          style={{
            height: `${Math.max((value / max) * 100, value ? 8 : 2)}%`,
            background: definition.color,
          }}
        />
      ))}
    </div>
  );
}

function SourceSelect({
  value,
  sources,
  onChange,
}: {
  value: string;
  sources: SourceInfo[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="source-select">
      <span>数据来源</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="unified">统一视图</option>
        {sources.map((source) => (
          <option key={source.id} value={source.id}>
            {source.display_name || source.vendor.toUpperCase()}
          </option>
        ))}
      </select>
    </label>
  );
}

function HomePage({
  meta,
  source,
  sources,
  onSource,
}: {
  meta: Meta;
  source: string;
  sources: SourceInfo[];
  onSource: (value: string) => void;
}) {
  const [range, setRange] = useState(30);
  const latest = meta.latest_data_date;
  const path = latest
    ? query("/api/v1/overview", {
        from: minusDays(latest, range - 1),
        to: latest,
        source,
      })
    : null;
  const { data, error, loading } = useRemote<Overview>(path);
  const activityMetric = data?.metrics.activity;

  return (
    <div className="page">
      <header className="page-header home-header">
        <div>
          <p className="kicker">CONTINUOUS HEALTH ARCHIVE</p>
          <h1>你的健康记录，跨设备连续保存</h1>
          <p>
            当前档案从 {dateLabel(meta.earliest_data_date)} 延续到{" "}
            {dateLabel(meta.latest_data_date)}，所有结果都能回到原始来源。
          </p>
        </div>
        <SourceSelect value={source} sources={sources} onChange={onSource} />
      </header>

      <section className="data-freshness">
        <div className="freshness-icon"><ShieldCheck size={22} /></div>
        <div>
          <strong>本地只读档案</strong>
          <span>
            规范数据更新至 {dateLabel(meta.latest_data_date)}
            {meta.latest_ui_date && meta.latest_ui_date !== meta.latest_data_date
              ? `，页面证据更新至 ${dateLabel(meta.latest_ui_date)}`
              : ""}
          </span>
        </div>
        <Badge tone="good">{meta.source_count} 个数据源</Badge>
      </section>

      <div className="range-row">
        <div>
          <p className="section-label">健康速览</p>
          <h2>最近 {range} 天</h2>
        </div>
        <div className="segmented" aria-label="趋势范围">
          {[7, 30, 90].map((days) => (
            <button
              key={days}
              className={range === days ? "active" : ""}
              onClick={() => setRange(days)}
            >
              {days} 天
            </button>
          ))}
        </div>
      </div>

      {loading && <LoadingBlock />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <>
          <section className="metric-card-grid">
            {Object.values(data.metrics).map((metric) => {
              const definition = metric.definition;
              const Icon = metricIcons[definition.key] ?? Activity;
              const primary = primaryText(definition, metric.latest);
              return (
                <Link
                  to={`/metrics/${definition.key}`}
                  className="metric-card"
                  key={definition.key}
                  style={{ "--metric": definition.color } as React.CSSProperties}
                >
                  <div className="metric-card-head">
                    <span className="metric-icon"><Icon size={20} /></span>
                    <span>{definition.name}</span>
                    <ChevronRight size={18} />
                  </div>
                  <div className="metric-value">
                    <strong>{primary.value}</strong>
                    {primary.unit && <span>{primary.unit}</span>}
                  </div>
                  <div className="metric-date">
                    {metric.latest ? (
                      <>
                        {dateLabel(metric.latest.local_date)}
                        {metric.latest.partial && <i>未完成</i>}
                      </>
                    ) : (
                      "没有记录"
                    )}
                  </div>
                  <MiniBars days={metric.days} definition={definition} />
                </Link>
              );
            })}
          </section>

          <section className="content-grid">
            <article className="panel panel-wide">
              <div className="panel-heading">
                <div>
                  <p className="section-label">ACTIVITY RHYTHM</p>
                  <h2>活动趋势</h2>
                </div>
                <Link className="text-link" to="/metrics/activity">
                  查看完整活动 <ChevronRight size={16} />
                </Link>
              </div>
              {activityMetric ? (
                <TrendChart
                  days={activityMetric.days}
                  definition={activityMetric.definition}
                />
              ) : (
                <EmptyBlock />
              )}
            </article>

            <article className="panel continuity-card">
              <div className="panel-heading">
                <div>
                  <p className="section-label">DATA CONTINUITY</p>
                  <h2>档案连续性</h2>
                </div>
              </div>
              <div className="continuity-number">
                <strong>
                  {meta.earliest_data_date && meta.latest_data_date
                    ? Math.max(
                        1,
                        Math.round(
                          (new Date(meta.latest_data_date).getTime() -
                            new Date(meta.earliest_data_date).getTime()) /
                            86400000 /
                            365.25,
                        ),
                      )
                    : 0}
                </strong>
                <span>年健康跨度</span>
              </div>
              <p>
                更换手表不会切断时间线。发生来源重叠时，统一视图保留每份原始记录，并明确标出当前选择。
              </p>
              <Link className="button secondary" to="/sources">
                查看来源档案
              </Link>
            </article>
          </section>
        </>
      )}
    </div>
  );
}

function MetricPage({
  metricKey,
  metrics,
  source,
  sources,
  onSource,
}: {
  metricKey: string;
  metrics: MetricDefinition[];
  source: string;
  sources: SourceInfo[];
  onSource: (value: string) => void;
}) {
  const definition = metrics.find((item) => item.key === metricKey);
  const latestAvailable = definition?.availability?.latest_date ?? null;
  const [range, setRange] = useState(90);
  const [selectedDate, setSelectedDate] = useState("");
  useEffect(() => {
    if (latestAvailable) setSelectedDate(latestAvailable);
  }, [latestAvailable, metricKey]);
  const dailyPath =
    definition && latestAvailable
      ? query(`/api/v1/metrics/${metricKey}/daily`, {
          from:
            range === 0
              ? definition.availability?.earliest_date ?? latestAvailable
              : minusDays(latestAvailable, range - 1),
          to: latestAvailable,
          source,
        })
      : null;
  const detailPath =
    definition && selectedDate
      ? query(`/api/v1/metrics/${metricKey}/days/${selectedDate}`, { source })
      : null;
  const daily = useRemote<{
    metric: MetricDefinition;
    days: DayRecord[];
    latest_date: string | null;
  }>(dailyPath);
  const detail = useRemote<DayDetail>(detailPath);

  if (!definition) return <EmptyBlock title="未知健康指标" copy="请从左侧指标列表重新选择。" />;
  const Icon = metricIcons[definition.key] ?? Activity;
  const summary = detail.data?.data?.summary ?? {};
  const summaryFields = definition.summary_fields.filter(
    (field) => summary[field.key] !== null && summary[field.key] !== undefined,
  );
  const seriesEntries = Object.entries(detail.data?.data?.series ?? {})
    .filter(([, points]) => points.length)
    .sort(([kindA], [kindB]) => {
      const rank = (kind: string) => {
        const index = definition.series_kinds.indexOf(kind);
        return index === -1 ? Number.MAX_SAFE_INTEGER : index;
      };
      return rank(kindA) - rank(kindB);
    });

  return (
    <div
      className="page metric-page"
      style={{ "--metric": definition.color } as React.CSSProperties}
    >
      <header className="page-header metric-header">
        <div className="metric-title">
          <span className="metric-title-icon"><Icon size={28} /></span>
          <div>
            <p className="kicker">{definition.category.toUpperCase()}</p>
            <h1>{definition.name}</h1>
            <p>
              {definition.availability?.earliest_date
                ? `${dateLabel(definition.availability.earliest_date)} 至 ${dateLabel(definition.availability.latest_date)}`
                : "当前档案尚无该指标"}
            </p>
          </div>
        </div>
        <SourceSelect value={source} sources={sources} onChange={onSource} />
      </header>

      <section className="metric-toolbar">
        <label className="date-control">
          <CalendarDays size={18} />
          <span>查看日期</span>
          <input
            type="date"
            value={selectedDate}
            min={definition.availability?.earliest_date ?? undefined}
            max={definition.availability?.latest_date ?? undefined}
            onChange={(event) => setSelectedDate(event.target.value)}
          />
        </label>
        <div className="segmented">
          {[
            [30, "30 天"],
            [90, "90 天"],
            [365, "1 年"],
            [0, "全部"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={range === value ? "active" : ""}
              onClick={() => setRange(Number(value))}
            >
              {label}
            </button>
          ))}
        </div>
      </section>

      {detail.loading && <LoadingBlock label="正在读取该日完整记录…" />}
      {detail.error && <ErrorBlock message={detail.error} />}
      {detail.data && !detail.data.data && (
        <EmptyBlock
          title={`${dateLabel(selectedDate)}没有${definition.name}记录`}
          copy="数据库把“没有记录”和数值 0 明确区分开来。可以切换其他日期查看。"
        />
      )}
      {detail.data?.data && (
        <>
          <section className="day-hero">
            <div>
              <span>{dateLabel(selectedDate)}</span>
              <strong>{primaryText(definition, detail.data.data).value}</strong>
              <small>{primaryText(definition, detail.data.data).unit}</small>
            </div>
            <DataStateBadge
              fidelity={detail.data.data.fidelity}
              partial={detail.data.data.partial}
            />
          </section>

          {summaryFields.length > 0 && (
            <section className="summary-grid">
              {summaryFields.map((field) => (
                <article key={field.key} className="summary-item">
                  <span>{field.label}</span>
                  <strong>{valueText(summary[field.key], field.unit)}</strong>
                </article>
              ))}
            </section>
          )}

          {detail.data.completeness.issues.length > 0 && (
            <section className="quality-notice">
              <CircleAlert size={20} />
              <div>
                <strong>这一天的数据细节有缺口</strong>
                <ul>
                  {detail.data.completeness.issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              </div>
            </section>
          )}

          {definition.key === "sleep" && (
            <article className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-label">SLEEP STAGES</p>
                  <h2>睡眠阶段</h2>
                </div>
                <Badge tone={detail.data.completeness.intervals ? "good" : "warning"}>
                  {detail.data.data.intervals.length} 个区间
                </Badge>
              </div>
              <SleepTimeline intervals={detail.data.data.intervals} />
            </article>
          )}

          {seriesEntries.length > 0 && (
            <article className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-label">INTRADAY DETAIL</p>
                  <h2>日内精细序列</h2>
                </div>
                <Badge tone="good">{seriesEntries[0][1].length} 个采样点</Badge>
              </div>
              <IntradayChart
                kind={seriesEntries[0][0]}
                points={seriesEntries[0][1]}
                color={definition.color}
              />
              {seriesEntries.length > 1 && (
                <p className="panel-note">
                  该日还保存了 {seriesEntries.slice(1).map(([kind]) => kind).join("、")} 序列。
                </p>
              )}
            </article>
          )}

          {detail.data.candidates.length > 0 && (
            <article className="panel provenance-panel">
              <div className="panel-heading">
                <div>
                  <p className="section-label">PROVENANCE</p>
                  <h2>数据来源与选择</h2>
                </div>
              </div>
              <div className="candidate-list">
                {detail.data.candidates.map((candidate) => (
                  <div
                    className={`candidate ${candidate.selected ? "selected" : ""}`}
                    key={candidate.source_id}
                  >
                    <span className="vendor-mark">{candidate.vendor.slice(0, 2).toUpperCase()}</span>
                    <div>
                      <strong>{candidate.source_name}</strong>
                      <small>
                        revision {candidate.revision} · {fidelityLabels[candidate.fidelity]}
                      </small>
                    </div>
                    {candidate.selected && <Badge tone="good">当前采用</Badge>}
                  </div>
                ))}
              </div>
            </article>
          )}

          {detail.data.data.evidence.some((item) =>
            item.media_type.startsWith("image/"),
          ) && (
            <details className="panel evidence-panel">
              <summary>
                <span>
                  <p className="section-label">SOURCE EVIDENCE</p>
                  <strong>查看 APP 页面证据</strong>
                </span>
                <Badge tone="neutral">
                  {detail.data.data.evidence.filter((item) => item.media_type.startsWith("image/")).length} 张
                </Badge>
              </summary>
              <div className="evidence-grid">
                {detail.data.data.evidence
                  .filter(
                    (item, index, list) =>
                      item.media_type.startsWith("image/") &&
                      list.findIndex((other) => other.sha256 === item.sha256) === index,
                  )
                  .slice(0, 8)
                  .map((item) => (
                    <figure key={item.sha256}>
                      <img
                        src={`/api/v1/artifacts/${item.sha256}`}
                        alt={`${item.page_key} ${item.role}`}
                        loading="lazy"
                      />
                      <figcaption>
                        {item.page_key}
                        <span>{item.role}</span>
                      </figcaption>
                    </figure>
                  ))}
              </div>
            </details>
          )}
        </>
      )}

      <article className="panel trend-panel">
        <div className="panel-heading">
          <div>
            <p className="section-label">LONGITUDINAL VIEW</p>
            <h2>长期趋势</h2>
          </div>
          {daily.data && <Badge>{daily.data.days.length} 个有记录日</Badge>}
        </div>
        {daily.loading && <LoadingBlock />}
        {daily.error && <ErrorBlock message={daily.error} />}
        {daily.data && (
          <TrendChart days={daily.data.days} definition={definition} height={340} />
        )}
      </article>
    </div>
  );
}

function SourcesPage({ sources }: { sources: SourceInfo[] }) {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="kicker">SOURCE REGISTRY</p>
          <h1>数据来源</h1>
          <p>每个品牌的数据独立保存、统一表达，并始终保留可追溯的原始来源。</p>
        </div>
        <Badge tone="good">{sources.length} 个已连接来源</Badge>
      </header>

      <section className="source-grid">
        {sources.map((source) => (
          <article className="source-card" key={source.id}>
            <div className="source-card-head">
              <span className="source-logo">{source.vendor.slice(0, 2).toUpperCase()}</span>
              <div>
                <h2>{source.display_name}</h2>
                <p>{source.app_package}</p>
              </div>
              <Badge tone={source.enabled ? "good" : "warning"}>
                {source.enabled ? "已启用" : "已停用"}
              </Badge>
            </div>
            <dl className="source-facts">
              <div><dt>厂商</dt><dd>{source.vendor.toUpperCase()}</dd></div>
              <div><dt>APP 版本</dt><dd>{source.app_version}</dd></div>
              <div><dt>时区</dt><dd>{source.timezone}</dd></div>
              <div><dt>优先级</dt><dd>{source.priority}</dd></div>
            </dl>
            <div className="source-metric-list">
              {source.metrics.map((metric) => (
                <Link to={`/metrics/${metric.canonical_metric}`} key={metric.canonical_metric}>
                  <span>{metric.canonical_metric}</span>
                  <strong>{metric.day_count.toLocaleString()} 天</strong>
                  <small>{metric.earliest_date} — {metric.latest_date}</small>
                </Link>
              ))}
            </div>
            {source.devices.length > 0 && (
              <div className="device-strip">
                <Server size={17} />
                <span>
                  {source.devices.length} 个脱敏设备记录 ·{" "}
                  {String(
                    source.devices[0].metadata.model ??
                      source.devices[0].metadata.product ??
                      source.devices[0].device_hash,
                  )}
                </span>
              </div>
            )}
          </article>
        ))}
      </section>

      <section className="future-sources">
        <div>
          <SlidersHorizontal size={25} />
          <h2>为下一块手表留好位置</h2>
          <p>
            小米健康或华为健康适配器写入规范层后，会自动出现在这里。已有 OPPO 历史不会被迁移、覆盖或丢弃。
          </p>
        </div>
        <div className="future-brand">MI</div>
        <div className="future-brand">HW</div>
      </section>
    </div>
  );
}

interface QualityPayload {
  coverage: Array<{ canonical_metric: string; status: string; count: number }>;
  failures: Array<{
    canonical_metric: string | null;
    stage: string;
    error_type: string;
    count: number;
    earliest_date: string | null;
    latest_date: string | null;
  }>;
  sequences: Array<{
    canonical_metric: string;
    sample_kind: string;
    sample_count: number;
    day_count: number;
  }>;
  intervals_by_kind: Array<{
    canonical_metric: string;
    interval_kind: string;
    interval_count: number;
    day_count: number;
  }>;
  totals: {
    unresolved_failures: number;
    page_captures: number;
    artifacts: number;
    samples: number;
    intervals: number;
  };
  latest_verification: {
    name: string;
    passed: boolean | null;
    failure_count?: number;
    warning_count?: number;
  } | null;
}

function QualityPage({ metrics }: { metrics: MetricDefinition[] }) {
  const { data, error, loading } = useRemote<QualityPayload>("/api/v1/admin/quality");
  const coverageByMetric = useMemo(() => {
    const output: Record<string, Record<string, number>> = {};
    data?.coverage.forEach((row) => {
      output[row.canonical_metric] ??= {};
      output[row.canonical_metric][row.status] = row.count;
    });
    return output;
  }, [data]);

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="kicker">DATA TRUST</p>
          <h1>数据质量</h1>
          <p>这里展示真实覆盖和缺口，不把没有精细序列的数据包装成完整记录。</p>
        </div>
        {data?.latest_verification && (
          <Badge tone={data.latest_verification.passed ? "good" : "warning"}>
            最近验真{data.latest_verification.passed ? "通过" : "仍有缺口"}
          </Badge>
        )}
      </header>
      {loading && <LoadingBlock />}
      {error && <ErrorBlock message={error} />}
      {data && (
        <>
          <section className="quality-totals">
            <article><span>页面证据</span><strong>{data.totals.page_captures.toLocaleString()}</strong></article>
            <article><span>内容寻址素材</span><strong>{data.totals.artifacts.toLocaleString()}</strong></article>
            <article><span>精细采样点</span><strong>{data.totals.samples.toLocaleString()}</strong></article>
            <article className={data.totals.unresolved_failures ? "attention" : ""}>
              <span>未解决差异</span>
              <strong>{data.totals.unresolved_failures.toLocaleString()}</strong>
            </article>
          </section>

          <article className="panel">
            <div className="panel-heading">
              <div>
                <p className="section-label">COVERAGE MATRIX</p>
                <h2>指标覆盖状态</h2>
              </div>
            </div>
            <div className="coverage-list">
              {metrics.map((metric) => {
                const values = coverageByMetric[metric.key] ?? {};
                const total = Object.values(values).reduce((sum, value) => sum + value, 0);
                return (
                  <div className="coverage-row" key={metric.key}>
                    <div>
                      <span className="coverage-dot" style={{ background: metric.color }} />
                      <strong>{metric.name}</strong>
                    </div>
                    <div className="coverage-bar">
                      {Object.entries(values).map(([status, count]) => (
                        <i
                          key={status}
                          className={`coverage-${status}`}
                          style={{ width: `${total ? (count / total) * 100 : 0}%` }}
                          title={`${fidelityLabels[status] ?? status}: ${count}`}
                        />
                      ))}
                    </div>
                    <span>{total.toLocaleString()} 天</span>
                  </div>
                );
              })}
            </div>
            <div className="coverage-legend">
              {Object.entries(fidelityLabels).map(([key, label]) => (
                <span key={key}><i className={`coverage-${key}`} />{label}</span>
              ))}
            </div>
          </article>

          <section className="content-grid quality-columns">
            <article className="panel">
              <div className="panel-heading">
                <div><p className="section-label">FINE-GRAINED DATA</p><h2>精细序列现状</h2></div>
              </div>
              <div className="sequence-list">
                {data.sequences.map((row) => (
                  <div key={`${row.canonical_metric}-${row.sample_kind}`}>
                    <span>{row.sample_kind}</span>
                    <strong>{row.sample_count.toLocaleString()} 点</strong>
                    <small>{row.day_count.toLocaleString()} 个有序列日</small>
                  </div>
                ))}
                {data.intervals_by_kind.map((row) => (
                  <div key={`${row.canonical_metric}-${row.interval_kind}`}>
                    <span>{row.interval_kind}</span>
                    <strong>{row.interval_count.toLocaleString()} 段</strong>
                    <small>{row.day_count.toLocaleString()} 个有区间日</small>
                  </div>
                ))}
              </div>
            </article>
            <article className="panel">
              <div className="panel-heading">
                <div><p className="section-label">OPEN DIFFERENCES</p><h2>主要未解决差异</h2></div>
              </div>
              <div className="failure-summary">
                {data.failures.slice(0, 12).map((failure, index) => (
                  <div key={`${failure.stage}-${failure.error_type}-${index}`}>
                    <Badge tone="warning">{failure.count.toLocaleString()}</Badge>
                    <span>
                      <strong>{failure.canonical_metric ?? "全局"} · {failure.error_type}</strong>
                      <small>{failure.stage} · {failure.earliest_date ?? "—"} 至 {failure.latest_date ?? "—"}</small>
                    </span>
                  </div>
                ))}
              </div>
            </article>
          </section>
        </>
      )}
    </div>
  );
}

interface AdminStatus {
  phase: string;
  stop_marker: boolean;
  metrics: Array<{
    key: string;
    name: string;
    checkpoint: string | null;
    coverage: Record<string, number>;
    pages: { date_count?: number; page_count?: number };
    source: { earliest_source_date?: string; latest_source_date?: string };
  }>;
  totals: {
    artifact_count: number;
    artifact_bytes: number;
    page_capture_count: number;
    database_bytes: number;
  };
  logs: Array<{ name: string; bytes: number; modified_at: number }>;
  failures: Array<Record<string, unknown>>;
  system: {
    adb: boolean;
    root: boolean | null;
    foreground: string | null;
    app_pid: string | null;
    processes: Array<Record<string, unknown>>;
  };
}

interface EventPayload {
  events: Array<{
    timestamp: string;
    level: string;
    event: string;
    message: string;
    metric?: string;
    local_date?: string;
    page?: string;
    action?: string;
    status?: string;
  }>;
  bytes: number;
}

function AdminPage() {
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [events, setEvents] = useState<EventPayload["events"]>([]);
  const [error, setError] = useState("");
  const [paused, setPaused] = useState(false);
  const [logName, setLogName] = useState("");
  const [logText, setLogText] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextEvents] = await Promise.all([
        api<AdminStatus>("/api/v1/admin/status"),
        api<EventPayload>("/api/v1/admin/events?limit=300"),
      ]);
      setStatus(nextStatus);
      setEvents(nextEvents.events ?? []);
      setError("");
      if (!logName && nextStatus.logs.length) setLogName(nextStatus.logs[0].name);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }, [logName]);

  useEffect(() => {
    refresh();
    if (paused) return;
    const timer = window.setInterval(refresh, 1000);
    return () => window.clearInterval(timer);
  }, [paused, refresh]);

  useEffect(() => {
    if (!logName) return;
    api<{ text: string }>(
      query("/api/v1/admin/log", { name: logName, lines: 600 }),
    )
      .then((payload) => setLogText(payload.text))
      .catch((reason: Error) => setLogText(`日志读取失败：${reason.message}`));
  }, [logName, events.length]);

  return (
    <div className="page admin-page">
      <header className="page-header">
        <div>
          <p className="kicker">COLLECTOR OBSERVABILITY</p>
          <h1>采集管理</h1>
          <p>只读查看 ADB、checkpoint、页面证据和逐步操作日志，不会向模拟器发送操作。</p>
        </div>
        <div className="admin-actions">
          <Badge tone={paused ? "warning" : "good"}>{paused ? "刷新已暂停" : "每秒刷新"}</Badge>
          <button className="button secondary" onClick={() => setPaused((value) => !value)}>
            {paused ? <RefreshCw size={17} /> : <X size={17} />}
            {paused ? "恢复刷新" : "暂停刷新"}
          </button>
        </div>
      </header>
      {error && <ErrorBlock message={error} />}
      {!status && !error && <LoadingBlock label="正在读取采集器状态…" />}
      {status && (
        <>
          <section className="system-strip">
            <span className={status.system.adb ? "ok" : "bad"}>ADB {status.system.adb ? "已连接" : "断开"}</span>
            <span className={status.system.root === false ? "ok" : "warning"}>
              Root {status.system.root === false ? "关闭" : status.system.root ? "开启" : "未知"}
            </span>
            <span className={status.system.app_pid ? "ok" : "warning"}>
              健康 APP {status.system.app_pid ? `PID ${status.system.app_pid}` : "未运行"}
            </span>
            <span className={status.phase === "collecting" ? "ok" : "neutral"}>采集状态 {status.phase}</span>
            <span className={status.stop_marker ? "warning" : "ok"}>暂停标志 {status.stop_marker ? "存在" : "无"}</span>
          </section>

          <section className="quality-totals">
            <article><span>页面证据</span><strong>{status.totals.page_capture_count.toLocaleString()}</strong></article>
            <article><span>素材</span><strong>{status.totals.artifact_count.toLocaleString()}</strong></article>
            <article><span>素材容量</span><strong>{bytes(status.totals.artifact_bytes)}</strong></article>
            <article className={status.failures.length ? "attention" : ""}><span>近期失败</span><strong>{status.failures.length}</strong></article>
          </section>

          <article className="panel">
            <div className="panel-heading">
              <div><p className="section-label">CHECKPOINTS</p><h2>指标采集边界</h2></div>
            </div>
            <div className="table-scroll">
              <table>
                <thead><tr><th>指标</th><th>checkpoint</th><th>来源范围</th><th>页面证据</th><th>覆盖状态</th></tr></thead>
                <tbody>
                  {status.metrics.map((metric) => (
                    <tr key={metric.key}>
                      <td><strong>{metric.name}</strong><small>{metric.key}</small></td>
                      <td>{metric.checkpoint ?? "—"}</td>
                      <td>{metric.source.earliest_source_date ?? "—"}<small>至 {metric.source.latest_source_date ?? "—"}</small></td>
                      <td>{metric.pages.date_count ?? 0} 天<small>{metric.pages.page_count ?? 0} 个页面</small></td>
                      <td><div className="badge-cluster">{Object.entries(metric.coverage).map(([key, count]) => <Badge key={key}>{key} {count}</Badge>)}</div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>

          <article className="panel operation-panel">
            <div className="panel-heading">
              <div><p className="section-label">LIVE OPERATIONS</p><h2>实时操作流</h2></div>
              <Badge tone="good">{events.length} 条</Badge>
            </div>
            <div className="operation-stream">
              {events.map((event, index) => (
                <div className={`operation-row level-${event.level?.toLowerCase()}`} key={`${event.timestamp}-${index}`}>
                  <time>{timeLabel(event.timestamp)}</time>
                  <Badge tone={event.level === "ERROR" ? "danger" : event.level === "WARNING" ? "warning" : "info"}>{event.level ?? "INFO"}</Badge>
                  <span><strong>{[event.metric, event.local_date].filter(Boolean).join(" · ") || "全局"}</strong><small>{event.page ?? event.action ?? event.event}</small></span>
                  <span><strong>{event.message ?? event.event}</strong><small>{event.status ?? "—"}</small></span>
                </div>
              ))}
              {!events.length && <EmptyBlock title="等待采集操作事件" />}
            </div>
          </article>

          <article className="panel">
            <div className="panel-heading log-heading">
              <div><p className="section-label">RAW LOG TAIL</p><h2>详细日志</h2></div>
              <select value={logName} onChange={(event) => setLogName(event.target.value)}>
                {status.logs.map((log) => <option value={log.name} key={log.name}>{log.name} · {bytes(log.bytes)}</option>)}
              </select>
            </div>
            <pre className="log-output">{logText || "日志为空"}</pre>
          </article>
        </>
      )}
    </div>
  );
}

function Login({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      onAuthenticated();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login-page">
      <section className="login-card">
        <div className="login-mark"><LockKeyhole size={30} /></div>
        <p className="kicker">PRIVATE HEALTH ARCHIVE</p>
        <h1>健康数据中枢</h1>
        <p>这是局域网私有档案。请输入在启动服务时设置的访问密码。</p>
        <form onSubmit={submit}>
          <label>
            <span>访问密码</span>
            <input
              type="password"
              autoFocus
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {error && <span className="login-error">{error}</span>}
          <button className="button primary" disabled={busy || password.length < 1}>
            {busy ? "正在验证…" : "进入健康档案"}
          </button>
        </form>
      </section>
    </main>
  );
}

function Shell({
  metrics,
  meta,
  sources,
  source,
  onSource,
}: {
  metrics: MetricDefinition[];
  meta: Meta;
  sources: SourceInfo[];
  source: string;
  onSource: (value: string) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMenuOpen(false), [location.pathname]);
  const nav = [
    { to: "/", label: "健康总览", icon: LayoutDashboard, end: true },
    { to: "/sources", label: "数据来源", icon: Database },
    { to: "/quality", label: "数据质量", icon: ShieldCheck },
    { to: "/admin/collector", label: "采集管理", icon: SlidersHorizontal },
  ];
  const metricMatch = location.pathname.match(/^\/metrics\/([^/]+)$/);
  let pageContent: ReactNode;
  if (location.pathname === "/") {
    pageContent = (
      <HomePage meta={meta} source={source} sources={sources} onSource={onSource} />
    );
  } else if (metricMatch) {
    pageContent = (
      <MetricPage
        metricKey={decodeURIComponent(metricMatch[1])}
        metrics={metrics}
        source={source}
        sources={sources}
        onSource={onSource}
      />
    );
  } else if (location.pathname === "/sources") {
    pageContent = <SourcesPage sources={sources} />;
  } else if (location.pathname === "/quality") {
    pageContent = <QualityPage metrics={metrics} />;
  } else if (location.pathname === "/admin/collector") {
    pageContent = <AdminPage />;
  } else {
    pageContent = (
      <HomePage meta={meta} source={source} sources={sources} onSource={onSource} />
    );
  }
  return (
    <div className="app-shell">
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <div className="brand">
          <span className="brand-mark"><Activity size={23} /></span>
          <div><strong>健康数据中枢</strong><small>Health Archive</small></div>
        </div>
        <nav className="main-nav">
          {nav.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink key={item.to} to={item.to} end={item.end}>
                <Icon size={19} />{item.label}
              </NavLink>
            );
          })}
        </nav>
        <p className="nav-section">健康指标</p>
        <nav className="metric-nav">
          {metrics.map((metric) => {
            const Icon = metricIcons[metric.key] ?? Activity;
            return (
              <NavLink key={metric.key} to={`/metrics/${metric.key}`}>
                <Icon size={18} />
                <span>{metric.short_name}</span>
                <i style={{ background: metric.color }} />
              </NavLink>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <span className="local-dot" />
          <div><strong>本地私有模式</strong><small>{bytes(meta.database_bytes)} 数据库</small></div>
        </div>
      </aside>
      {menuOpen && <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
      <div className="app-main">
        <header className="mobile-topbar">
          <button aria-label="打开导航" onClick={() => setMenuOpen(true)}><Menu /></button>
          <strong>健康数据中枢</strong>
          <span className="local-dot" />
        </header>
        {pageContent}
      </div>
      <nav className="bottom-nav">
        {nav.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink key={item.to} to={item.to} end={item.end}>
              <Icon size={20} /><span>{item.label.replace("健康", "")}</span>
            </NavLink>
          );
        })}
      </nav>
    </div>
  );
}

export default function App() {
  const [authState, setAuthState] = useState<{
    required: boolean;
    authenticated: boolean;
  } | null>(null);
  const [authError, setAuthError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [source, setSource] = useState(
    () => window.localStorage.getItem("health-source") ?? "unified",
  );

  useEffect(() => {
    api<{ required: boolean; authenticated: boolean }>("/api/v1/auth/status")
      .then(setAuthState)
      .catch((reason: Error) => setAuthError(reason.message));
  }, [refreshKey]);

  const meta = useRemote<Meta>(
    authState?.authenticated ? "/api/v1/meta" : null,
    refreshKey,
  );
  const metrics = useRemote<MetricDefinition[]>(
    authState?.authenticated ? "/api/v1/metrics" : null,
    refreshKey,
  );
  const sources = useRemote<SourceInfo[]>(
    authState?.authenticated ? "/api/v1/sources" : null,
    refreshKey,
  );

  const updateSource = (value: string) => {
    setSource(value);
    window.localStorage.setItem("health-source", value);
  };

  if (authError) return <main className="fatal-page"><ErrorBlock message={authError} /></main>;
  if (!authState) return <main className="fatal-page"><LoadingBlock label="正在打开本地健康档案…" /></main>;
  if (!authState.authenticated) {
    return <Login onAuthenticated={() => setRefreshKey((value) => value + 1)} />;
  }
  if (meta.error || metrics.error || sources.error) {
    return <main className="fatal-page"><ErrorBlock message={meta.error || metrics.error || sources.error} /></main>;
  }
  if (!meta.data || !metrics.data || !sources.data) {
    return <main className="fatal-page"><LoadingBlock label="正在整理跨品牌健康时间线…" /></main>;
  }
  return (
    <Shell
      metrics={metrics.data}
      meta={meta.data}
      sources={sources.data}
      source={source}
      onSource={updateSource}
    />
  );
}

export { primaryText, valueText };
