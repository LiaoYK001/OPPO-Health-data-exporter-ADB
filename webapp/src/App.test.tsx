import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import {
  EvidenceGallery,
  LongTermStats,
  primaryText,
  rangeMaxPoints,
  rangeOptions,
  statisticText,
  valueText,
} from "./App";
import { apiErrorMessage } from "./api";
import type { DayRecord, Evidence, MetricDefinition } from "./types";

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
    const statistics = {
      field: "steps",
      unit: "千卡",
      recorded_days: 4,
      total: 1000,
      average: 250,
      minimum: 100,
      median: 225,
      maximum: 400,
      first_date: "2025-12-30",
      last_date: "2026-01-02",
    };
    render(
      <LongTermStats
        definition={{ ...definition, key: "active_energy", primary_unit: "千卡" }}
        statistics={statistics}
        days={[
          { ...day, local_date: "2026-01-01", summary: { steps: 100 } },
          { ...day, local_date: "2026-01-02", summary: { steps: 300 } },
        ]}
      />,
    );

    expect(screen.getByText("所选范围总计")).toBeInTheDocument();
    expect(screen.getByText("1,000 千卡")).toBeInTheDocument();
    expect(screen.getByText("250 千卡")).toBeInTheDocument();
    expect(screen.getByText("400 千卡")).toBeInTheDocument();
    expect(screen.getByText("4 天")).toBeInTheDocument();
    expect(
      statisticText(
        { ...definition, key: "active_energy", primary_unit: "千卡" },
        statistics,
        "median",
      ),
    ).toEqual({ value: "225", unit: "千卡" });
  });

  it("opens complete evidence images and supports keyboard navigation", () => {
    const evidence: Evidence[] = [
      {
        page_capture_id: 1,
        page_key: "activity",
        fidelity: "exact_ui",
        created_at: "2026-01-01T00:00:00Z",
        role: "viewport_png",
        viewport_index: 0,
        sha256: "a".repeat(64),
        media_type: "image/png",
        byte_size: 100,
        width: 1440,
        height: 2560,
      },
      {
        page_capture_id: 1,
        page_key: "activity",
        fidelity: "exact_ui",
        created_at: "2026-01-01T00:00:00Z",
        role: "page_mosaic",
        viewport_index: null,
        sha256: "b".repeat(64),
        media_type: "image/png",
        byte_size: 200,
        width: 1440,
        height: 5120,
      },
    ];
    render(<EvidenceGallery items={evidence} />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "查看完整图像：activity viewport_png",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "APP 页面证据完整图像",
    });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText(/viewport_png/)).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(within(dialog).getByText(/page_mosaic/)).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(
      screen.queryByRole("dialog", { name: "APP 页面证据完整图像" }),
    ).not.toBeInTheDocument();
  });

  it("offers one, two, three year and optimized life span ranges", () => {
    expect(rangeOptions.map((item) => item.label)).toEqual([
      "7 天",
      "30 天",
      "90 天",
      "1 年",
      "2 年",
      "3 年",
      "Life span",
    ]);
    expect(rangeMaxPoints(365, "overview")).toBe(365);
    expect(rangeMaxPoints(730, "overview")).toBe(420);
    expect(rangeMaxPoints(0, "overview")).toBe(480);
    expect(rangeMaxPoints(0, "detail")).toBe(720);
  });

  it("renders structured API validation errors as readable text", () => {
    expect(
      apiErrorMessage(
        {
          detail: [
            {
              loc: ["query", "max_points"],
              msg: "Input should be greater than or equal to 3",
            },
          ],
        },
        422,
      ),
    ).toBe("max_points：Input should be greater than or equal to 3");
  });
});
