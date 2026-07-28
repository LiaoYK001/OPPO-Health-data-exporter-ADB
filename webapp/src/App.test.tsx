import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { LongTermStats, primaryText, valueText } from "./App";
import type { DayRecord, MetricDefinition } from "./types";

const definition: MetricDefinition = {
  key: "activity",
  name: "每日活动",
  short_name: "活动",
  category: "activity",
  color: "#36d98b",
  accent: "#13b96f",
  primary_field: "steps",
  primary_unit: "步",
  series_kinds: ["steps"],
  summary_fields: [],
};

const day: DayRecord = {
  source_id: 1,
  source_name: "OPPO 健康",
  vendor: "oppo",
  canonical_metric: "activity",
  local_date: "2026-01-01",
  revision: 1,
  summary: { steps: 12345 },
  fidelity: "exact_source",
  partial: false,
  created_at: "2026-01-02T00:00:00Z",
};

describe("health value presentation", () => {
  it("does not turn missing health data into zero", () => {
    expect(primaryText(definition, null)).toEqual({ value: "无记录", unit: "" });
    expect(valueText(null, "步")).toBe("—");
  });

  it("formats exact daily values without losing the source unit", () => {
    expect(primaryText(definition, day)).toEqual({
      value: "12,345",
      unit: "步",
    });
  });

  it("shows total, daily average, maximum and recorded days for activity submetrics", () => {
    render(
      <LongTermStats
        definition={{ ...definition, key: "active_energy", primary_unit: "千卡" }}
        days={[
          { ...day, local_date: "2026-01-01", summary: { steps: 100 } },
          { ...day, local_date: "2026-01-02", summary: { steps: 300 } },
        ]}
      />,
    );

    expect(screen.getByText("所选范围总计")).toBeInTheDocument();
    expect(screen.getByText("400 千卡")).toBeInTheDocument();
    expect(screen.getByText("200 千卡")).toBeInTheDocument();
    expect(screen.getByText("300 千卡")).toBeInTheDocument();
    expect(screen.getByText("2 天")).toBeInTheDocument();
  });
});
