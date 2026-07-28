import { describe, expect, it } from "vitest";
import { primaryText, valueText } from "./App";
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
});
