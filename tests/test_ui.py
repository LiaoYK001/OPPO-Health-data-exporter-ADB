from __future__ import annotations

import datetime as dt
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from health_exporter.constants import UI_PROFILES
from health_exporter.ui import (
    CalendarMonthView,
    UiCollector,
    UiTree,
    advance_checkpoint_date,
    calendar_day_point,
    calendar_month_views,
    calendar_semantic_day_point,
    calendar_semantic_target_point,
    canvas_previous_point,
    classify_home_cards,
    date_from_label,
)


def test_scrollable_prefers_vertical_scrollview_over_larger_viewpager() -> None:
    root = ET.fromstring(
        """
        <hierarchy>
          <node class="androidx.viewpager.widget.ViewPager" scrollable="true"
                bounds="[0,100][1440,2560]" resource-id="viewpager"/>
          <node class="android.widget.ScrollView" scrollable="true"
                bounds="[0,700][1440,2560]" resource-id="content_scroll"/>
          <node class="androidx.recyclerview.widget.RecyclerView" scrollable="true"
                bounds="[64,2000][1376,2560]" resource-id="details"/>
        </hierarchy>
        """
    )

    selected = UiTree(root).scrollable()

    assert selected is not None
    assert selected.attrib["resource-id"] == "content_scroll"


def test_vertical_swipe_uses_long_span_to_avoid_embedded_chart() -> None:
    node = ET.fromstring(
        '<node class="android.widget.ScrollView" scrollable="true" '
        'resource-id="content_scroll_view" bounds="[0,700][1440,2500]"/>'
    )

    class FakeAdb:
        calls: list[tuple[int, int, int, int, int]] = []

        def swipe(
            self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
        ) -> None:
            self.calls.append((x1, y1, x2, y2, duration_ms))

    collector = object.__new__(UiCollector)
    collector.adb = FakeAdb()  # type: ignore[assignment]

    assert collector._swipe_scrollable(node, down=False) is True
    assert collector._swipe_scrollable(node, down=True) is True
    assert collector.adb.calls == [
        (720, 2230, 720, 1060, 700),
        (720, 1996, 720, 2392, 500),
    ]


def test_ordinary_list_uses_long_reverse_swipe_to_return_top() -> None:
    node = ET.fromstring(
        '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
        'bounds="[0,100][1000,2100]"/>'
    )

    class FakeAdb:
        call: tuple[int, int, int, int, int] | None = None

        def swipe(
            self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
        ) -> None:
            self.call = (x1, y1, x2, y2, duration_ms)

    collector = object.__new__(UiCollector)
    collector.adb = FakeAdb()  # type: ignore[assignment]

    assert collector._swipe_scrollable(node, down=True) is True
    assert collector.adb.call == (500, 500, 500, 1800, 700)


def test_health_home_coordinator_uses_nested_card_reverse_swipe() -> None:
    node = ET.fromstring(
        '<node class="android.widget.ScrollView" scrollable="true" '
        'resource-id="coordinator_layout" bounds="[0,0][1440,2333]"/>'
    )

    class FakeAdb:
        call: tuple[int, int, int, int, int] | None = None

        def swipe(
            self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
        ) -> None:
            self.call = (x1, y1, x2, y2, duration_ms)

    collector = object.__new__(UiCollector)
    collector.adb = FakeAdb()  # type: ignore[assignment]

    assert collector._swipe_scrollable(node, down=True) is True
    assert collector.adb.call == (720, 1679, 720, 2193, 500)
    assert collector._swipe_scrollable(node, down=False) is True
    assert collector.adb.call == (720, 1516, 720, 816, 700)


def test_scroll_to_top_returns_exact_xml_for_first_viewport(tmp_path: Path) -> None:
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<hierarchy><node text="heart" resource-id="title" '
        b'scrollable="false" bounds="[0,0][100,100]"/></hierarchy>'
    )

    class FakeAdb:
        def shell(self, *_args: str, **_kwargs: object) -> str:
            return ""

    collector = object.__new__(UiCollector)
    collector.adb = FakeAdb()  # type: ignore[assignment]
    calls = 0

    def dump_tree(directory: Path, stem: str) -> tuple[UiTree, Path]:
        nonlocal calls
        calls += 1
        path = directory / f"{stem}.xml"
        path.write_bytes(xml)
        return UiTree(ET.fromstring(xml)), path

    collector.dump_tree = dump_tree  # type: ignore[method-assign]

    tree, returned_xml = collector._scroll_to_top(max_swipes=8) or (None, None)

    assert tree is not None
    assert tree.by_text("heart") is not None
    assert returned_xml == xml
    assert calls == 1


def test_reuse_current_metric_requires_daily_page_with_date() -> None:
    xml = ET.fromstring(
        '<hierarchy><node text="心率"/><node text="日" selected="true"/>'
        '<node text="7月10日" resource-id="com.heytap.health:id/tv_date"/>'
        '</hierarchy>'
    )

    class FakeAdb:
        def foreground(self) -> str:
            return "com.heytap.health/.heartrate.ui.HeartRateHistoryActivity"

    collector = object.__new__(UiCollector)
    collector.adb = FakeAdb()  # type: ignore[assignment]
    collector.dump_tree = (  # type: ignore[method-assign]
        lambda _directory, _stem: (UiTree(xml), Path("unused.xml"))
    )

    reused = collector._reuse_current_metric_page(
        "heart_rate", UI_PROFILES["heart_rate"]
    )

    assert reused is not None
    assert reused.date_label(UI_PROFILES["heart_rate"]) == "7月10日"


def test_subpage_discovery_only_returns_entries_visible_in_main_capture() -> None:
    class FakeConnection:
        def execute(self, _query: str, _params: tuple[int]) -> object:
            class Cursor:
                @staticmethod
                def fetchone() -> dict[str, str]:
                    return {
                        "display_payload_json": json.dumps(
                            {
                                "viewports": [
                                    {
                                        "nodes": [
                                            {"text": "静息心率"},
                                            {"text": "步行平均心率"},
                                        ]
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }

            return Cursor()

    class FakeDatabase:
        connection = FakeConnection()

    collector = object.__new__(UiCollector)
    collector.db = FakeDatabase()  # type: ignore[assignment]

    assert collector._visible_subpage_titles(
        7,
        UI_PROFILES["heart_rate"],
    ) == {"静息心率", "步行平均心率"}


def test_subpage_search_stops_when_scroll_does_not_change_ui() -> None:
    root = ET.fromstring(
        '<hierarchy><node text="心率" scrollable="true" '
        'bounds="[0,0][100,100]"/></hierarchy>'
    )
    tree = UiTree(root)
    collector = object.__new__(UiCollector)
    collector._scroll_to_top = lambda max_swipes=8: (tree, b"xml")  # type: ignore[method-assign]
    dump_calls = 0
    swipe_calls = 0

    def dump_tree(_directory: Path, _stem: str) -> tuple[UiTree, Path]:
        nonlocal dump_calls
        dump_calls += 1
        return tree, Path("unused.xml")

    def swipe(_node: ET.Element, *, down: bool) -> bool:
        nonlocal swipe_calls
        swipe_calls += 1
        assert down is False
        return True

    collector.dump_tree = dump_tree  # type: ignore[method-assign]
    collector._swipe_scrollable = swipe  # type: ignore[method-assign]

    assert collector._open_text_target("睡眠基准心率") is False
    assert dump_calls == 2
    assert swipe_calls == 1


def test_subpage_search_taps_current_visible_state_before_scrolling() -> None:
    root = ET.fromstring(
        '<hierarchy><node text="target" clickable="true" '
        'bounds="[10,20][30,40]"/></hierarchy>'
    )
    tree = UiTree(root)

    class FakeAdb:
        tapped: tuple[int, int] | None = None

        def tap(self, x: int, y: int) -> None:
            self.tapped = (x, y)

    collector = object.__new__(UiCollector)
    collector.adb = FakeAdb()  # type: ignore[assignment]
    collector.dump_tree = lambda _directory, _stem: (tree, Path("unused.xml"))  # type: ignore[method-assign]
    collector._scroll_to_top = lambda max_swipes=8: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("visible fast path must not scroll")
    )

    assert collector._open_text_target("target") is True
    assert collector.adb.tapped == (20, 30)


def test_resume_navigation_batches_clicks_and_verifies_actual_date(tmp_path: Path) -> None:
    class FakeAdb:
        def __init__(self) -> None:
            self.current = dt.date(2026, 7, 10)
            self.taps = 0

        def tap(self, _x: int, _y: int) -> None:
            self.current -= dt.timedelta(days=1)
            self.taps += 1

    fake_adb = FakeAdb()

    def make_tree() -> UiTree:
        label = f"{fake_adb.current.month}月{fake_adb.current.day:02d}日"
        root = ET.fromstring(
            f'<hierarchy><node text="{label}" resource-id="tv_date" '
            'bounds="[400,100][900,200]"/>'
            '<node text="" resource-id="iv_pre" clickable="true" '
            'bounds="[100,100][300,200]"/></hierarchy>'
        )
        return UiTree(root)

    collector = object.__new__(UiCollector)
    collector.adb = fake_adb  # type: ignore[assignment]
    collector._scroll_to_top = lambda max_swipes=8: (make_tree(), b"xml")  # type: ignore[method-assign]

    def dump_tree(_directory: Path, stem: str) -> tuple[UiTree, Path]:
        path = tmp_path / f"{stem}.xml"
        path.write_text("fixture", encoding="utf-8")
        return make_tree(), path

    collector.dump_tree = dump_tree  # type: ignore[method-assign]

    reached = collector._resume_to_checkpoint(
        UI_PROFILES["heart_rate"],
        dt.date(2026, 7, 10),
        dt.date(2026, 7, 1),
        batch_size=5,
        tap_delay=0,
    )

    assert reached == dt.date(2026, 7, 1)
    assert fake_adb.taps == 9


def test_calendar_month_views_reconstructs_year_across_december() -> None:
    root = ET.fromstring(
        """
        <hierarchy>
          <node text="2024年" resource-id="calendar_year_value"
                bounds="[600,260][840,366]"/>
          <node resource-id="recycler_view" scrollable="true"
                bounds="[0,480][1440,2560]">
            <node bounds="[0,480][1440,2172]">
              <node text="12月" resource-id="month_value"
                    bounds="[96,480][300,620]"/>
              <node class="android.view.View" bounds="[76,620][1364,2132]"/>
            </node>
            <node bounds="[0,2172][1440,2560]">
              <node text="1月" resource-id="month_value"
                    bounds="[96,2172][246,2312]"/>
              <node class="android.view.View" bounds="[76,2312][1364,2560]"/>
            </node>
          </node>
        </hierarchy>
        """
    )

    months = calendar_month_views(UiTree(root))

    assert [(item.year, item.month) for item in months] == [(2024, 12), (2025, 1)]


def test_calendar_month_views_assigns_clipped_december_year_to_visible_january() -> None:
    root = ET.fromstring(
        """
        <hierarchy>
          <node text="2021年" resource-id="calendar_year_value"
                bounds="[602,261][838,366]"/>
          <node resource-id="recycler_view" scrollable="true"
                bounds="[0,480][1440,2560]">
            <node bounds="[0,480][1440,756]">
              <node class="android.view.View" bounds="[76,480][1364,716]"/>
            </node>
            <node bounds="[0,756][1440,2560]">
              <node text="1月" resource-id="month_value"
                    bounds="[96,756][246,896]"/>
              <node class="android.view.View" bounds="[76,985][1364,2560]"/>
            </node>
          </node>
        </hierarchy>
        """
    )

    months = calendar_month_views(UiTree(root))

    assert [(item.year, item.month) for item in months] == [(2022, 1)]


def test_calendar_day_point_maps_custom_canvas_cell() -> None:
    april = CalendarMonthView(
        year=2024,
        month=4,
        label_bounds=(96, 480, 246, 620),
        grid_bounds=(76, 620, 1364, 2132),
    )

    assert calendar_day_point(april, 9, 1440) == (540, 1074)
    assert calendar_day_point(april, 31, 1440) is None


def test_calendar_month_view_uses_largest_grid_after_summary_label() -> None:
    root = ET.fromstring(
        """
        <hierarchy>
          <node text="2026年" resource-id="calendar_year_value"
                bounds="[602,261][838,366]"/>
          <node resource-id="recycler_view" scrollable="true"
                bounds="[0,480][1440,2560]">
            <node bounds="[0,779][1440,2560]">
              <node text="7月" resource-id="month_value"
                    bounds="[96,779][246,919]"/>
              <node text="" resource-id="congratulation"
                    bounds="[76,919][96,1008]"/>
              <node class="android.view.View"
                    bounds="[76,1008][1364,2520]"/>
            </node>
          </node>
        </hierarchy>
        """
    )

    months = calendar_month_views(UiTree(root))

    assert len(months) == 1
    assert months[0].grid_bounds == (76, 1008, 1364, 2520)


def test_activity_calendar_uses_exact_semantic_date_node() -> None:
    root = ET.fromstring(
        """
        <hierarchy>
          <node text="2025年" resource-id="calendar_year_value"
                bounds="[602,168][838,273]"/>
          <node resource-id="recycler_view" scrollable="true"
                bounds="[0,455][1440,2560]">
            <node bounds="[0,455][1440,2560]">
              <node bounds="[76,535][1364,1009]">
                <node text="8月" resource-id="month_value"
                      bounds="[96,535][222,652]"/>
                <node text="已达标0/31天" resource-id="congratulation"
                      bounds="[238,566][578,648]"/>
              </node>
              <node bounds="[76,2321][1364,2560]">
                <node bounds="[444,2321][628,2560]">
                  <node text="26" resource-id="health_tv_calendar_date"
                        bounds="[492,2321][580,2409]"/>
                </node>
                <node bounds="[628,2321][812,2560]">
                  <node text="27" resource-id="health_tv_calendar_date"
                        bounds="[676,2321][764,2409]"/>
                </node>
              </node>
            </node>
          </node>
        </hierarchy>
        """
    )
    tree = UiTree(root)
    month = calendar_month_views(tree)[0]

    assert calendar_semantic_day_point(tree, month, 27) == (720, 2365)
    assert calendar_semantic_day_point(tree, month, 31) is None


def test_activity_calendar_maps_clipped_previous_month_dates() -> None:
    root = ET.fromstring(
        """
        <hierarchy>
          <node text="2024年" resource-id="calendar_year_value"
                bounds="[602,168][838,273]"/>
          <node resource-id="recycler_view" scrollable="true"
                bounds="[0,455][1440,2560]">
            <node bounds="[0,455][1440,2560]">
              <node bounds="[260,819][444,1123]">
                <node text="24" resource-id="health_tv_calendar_date"
                      bounds="[308,863][396,951]"/>
              </node>
              <node bounds="[76,1735][1364,2209]">
                <node text="7月" resource-id="month_value"
                      bounds="[96,1735][222,1852]"/>
                <node text="已达标0/31天" resource-id="congratulation"
                      bounds="[238,1766][578,1848]"/>
              </node>
              <node bounds="[260,2261][444,2560]">
                <node text="1" resource-id="health_tv_calendar_date"
                      bounds="[308,2305][396,2393]"/>
              </node>
            </node>
          </node>
        </hierarchy>
        """
    )
    tree = UiTree(root)
    months = calendar_month_views(tree)

    assert [(item.year, item.month) for item in months] == [(2024, 7)]
    assert calendar_semantic_target_point(
        tree,
        months,
        dt.date(2024, 6, 24),
    ) == (352, 907)


def test_known_heart_detail_links_are_not_unknown_home_cards() -> None:
    result = classify_home_cards(
        {"心率", "静息心率", "步行平均心率", "睡眠基准心率", "意外的新指标"}
    )

    assert result["included"] == ["心率"]
    assert result["known_details"] == ["步行平均心率", "睡眠基准心率", "静息心率"]
    assert result["unknown"] == ["意外的新指标"]


def test_partial_today_does_not_replace_older_resume_boundary() -> None:
    today = dt.date(2026, 7, 21)
    existing = dt.date(2026, 6, 1)

    assert advance_checkpoint_date(existing, today, today) == (existing, False)


def test_relative_date_labels_use_reference_day() -> None:
    reference = dt.date(2026, 7, 26)

    assert date_from_label("今天", reference) == reference
    assert date_from_label("昨日", reference) == dt.date(2026, 7, 25)
    assert date_from_label("前天，周五", reference) == dt.date(2026, 7, 24)


def test_checkpoint_only_moves_toward_older_dates() -> None:
    today = dt.date(2026, 7, 21)
    existing = dt.date(2026, 6, 1)

    assert advance_checkpoint_date(None, today, today) == (today, True)
    assert advance_checkpoint_date(existing, dt.date(2026, 5, 31), today) == (
        dt.date(2026, 5, 31),
        True,
    )
    assert advance_checkpoint_date(existing, dt.date(2026, 6, 2), today) == (
        existing,
        False,
    )


def test_weight_canvas_arrow_uses_date_evidence_and_screen_width() -> None:
    assert canvas_previous_point((380, 546, 964, 622), 1440, 1 / 12) == (120, 622)


def test_max_days_counts_new_attempts_not_checkpoint_skips() -> None:
    class FakeConnection:
        def commit(self) -> None:
            pass

    class FakeDatabase:
        connection = FakeConnection()

        def __init__(self) -> None:
            self.last_date = "2026-07-08"

        def get_checkpoint(self, _source_id: int, _metric: str) -> dict[str, str]:
            return {"last_date": self.last_date}

        def metric_ui_evidence_dates(
            self,
            _source_id: int,
            _metric: str,
        ) -> list[str]:
            return ["2026-07-20", "2026-07-15"]

        def set_coverage(self, *_args: object, **_kwargs: object) -> None:
            pass

        def set_checkpoint(
            self,
            _source_id: int,
            _metric: str,
            day: str,
            _state: dict[str, str],
        ) -> None:
            self.last_date = day

        def record_failure(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unexpected capture failure")

    class FakeTree:
        def date_label(self, _profile: object) -> str:
            return "2026年7月10日"

    collector = UiCollector.__new__(UiCollector)
    collector.db = FakeDatabase()  # type: ignore[assignment]
    collector.source_id = 1
    collector.run_id = "test"
    captured: list[tuple[str, int]] = []
    collector.enter_metric = lambda _metric: FakeTree()  # type: ignore[method-assign]
    collector.capture_page = (  # type: ignore[method-assign]
        lambda _metric, day, _page_key, max_scrolls=12: captured.append(
            (day, max_scrolls)
        )
    )
    collector._capture_subpages = lambda *_args: None  # type: ignore[method-assign]
    collector._has_source_day = lambda *_args: True  # type: ignore[method-assign]
    collector._go_previous_day = (  # type: ignore[method-assign]
        lambda _profile, current: current - dt.timedelta(days=1)
    )

    result = collector.capture_history(
        "heart_rate",
        earliest_date=dt.date(2026, 7, 1),
        max_days=2,
        resume=True,
        capture_subpages=False,
        max_scrolls=20,
    )

    assert captured == [("2026-07-07", 20), ("2026-07-06", 20)]
    assert result == {
        "metric": "heart_rate",
        "captured": 2,
        "skipped": 3,
        "failures": 0,
        "attempted": 2,
        "visited": 5,
    }


def test_sparse_history_uses_calendar_only_for_pending_source_dates() -> None:
    class FakeConnection:
        def commit(self) -> None:
            pass

    class FakeDatabase:
        connection = FakeConnection()

        def __init__(self) -> None:
            self.last_date = "2026-07-15"

        def get_checkpoint(self, _source_id: int, _metric: str) -> dict[str, str]:
            return {"last_date": self.last_date}

        def set_coverage(self, *_args: object, **_kwargs: object) -> None:
            pass

        def set_checkpoint(
            self,
            _source_id: int,
            _metric: str,
            day: str,
            _state: dict[str, str],
        ) -> None:
            self.last_date = day

        def record_failure(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unexpected sparse capture failure")

    class FakeTree:
        def date_label(self, _profile: object) -> str:
            return "2026年7月20日"

    collector = UiCollector.__new__(UiCollector)
    collector.db = FakeDatabase()  # type: ignore[assignment]
    collector.source_id = 1
    collector.run_id = "sparse-test"
    captured: list[str] = []
    jumps: list[dt.date] = []
    collector.enter_metric = lambda _metric: FakeTree()  # type: ignore[method-assign]
    collector._resume_via_calendar = (  # type: ignore[method-assign]
        lambda _profile, target: jumps.append(target) or target
    )
    collector.capture_page = (  # type: ignore[method-assign]
        lambda _metric, day, _page_key, max_scrolls=12: captured.append(day) or 7
    )
    collector._capture_subpages = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    collector._has_source_day = lambda *_args: True  # type: ignore[method-assign]

    result = collector.capture_history(
        "spo2",
        earliest_date=dt.date(2026, 7, 1),
        target_dates=(
            dt.date(2026, 7, 20),
            dt.date(2026, 7, 15),
            dt.date(2026, 7, 1),
        ),
        resume=True,
        capture_subpages=False,
        max_scrolls=20,
    )

    assert jumps == [dt.date(2026, 7, 1)]
    assert captured == ["2026-07-01"]
    assert result == {
        "metric": "spo2",
        "captured": 1,
        "skipped": 2,
        "failures": 0,
        "attempted": 1,
        "visited": 1,
        "mode": "calendar_source_dates",
        "target_dates": 3,
    }


def test_sparse_history_retries_failed_date_newer_than_checkpoint() -> None:
    class FakeConnection:
        def commit(self) -> None:
            pass

    class FakeDatabase:
        connection = FakeConnection()

        def metric_ui_evidence_dates(
            self,
            _source_id: int,
            _metric: str,
        ) -> list[str]:
            return ["2025-12-07", "2023-08-06"]

        def set_coverage(self, *_args: object, **_kwargs: object) -> None:
            pass

        def set_checkpoint(self, *_args: object, **_kwargs: object) -> None:
            pass

        def record_failure(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unexpected sparse retry failure")

    collector = UiCollector.__new__(UiCollector)
    collector.db = FakeDatabase()  # type: ignore[assignment]
    collector.source_id = 1
    collector.run_id = "sparse-retry-test"
    captured: list[str] = []
    collector._resume_via_calendar = (  # type: ignore[method-assign]
        lambda _profile, target: target
    )
    collector.capture_page = (  # type: ignore[method-assign]
        lambda _metric, day, _page_key, max_scrolls=12: captured.append(day) or 8
    )
    collector._capture_subpages = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    collector._has_source_day = lambda *_args: True  # type: ignore[method-assign]

    result = collector._capture_sparse_dates(
        "weight",
        UI_PROFILES["weight"],
        current_date=dt.date(2025, 12, 7),
        earliest_date=dt.date(2023, 8, 6),
        target_dates=(
            dt.date(2025, 12, 7),
            dt.date(2024, 11, 21),
            dt.date(2023, 8, 6),
        ),
        checkpoint_date=dt.date(2023, 8, 6),
        max_days=None,
        capture_subpages=False,
        max_scrolls=20,
    )

    assert captured == ["2024-11-21"]
    assert result["captured"] == 1
    assert result["skipped"] == 2


def test_stalled_previous_day_recovers_through_calendar() -> None:
    class FakeConnection:
        def commit(self) -> None:
            pass

    class FakeDatabase:
        connection = FakeConnection()

        def get_checkpoint(self, _source_id: int, _metric: str) -> None:
            return None

        def set_coverage(self, *_args: object, **_kwargs: object) -> None:
            pass

        def set_checkpoint(self, *_args: object, **_kwargs: object) -> None:
            pass

        def record_failure(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("calendar recovery should avoid a failure")

    class FakeTree:
        def date_label(self, _profile: object) -> str:
            return "2026年7月2日"

    collector = UiCollector.__new__(UiCollector)
    collector.db = FakeDatabase()  # type: ignore[assignment]
    collector.source_id = 1
    collector.run_id = "recovery-test"
    captured: list[str] = []
    recovered: list[dt.date] = []
    collector.enter_metric = lambda _metric: FakeTree()  # type: ignore[method-assign]
    collector.capture_page = (  # type: ignore[method-assign]
        lambda _metric, day, _page_key, max_scrolls=12: captured.append(day) or 9
    )
    collector._capture_subpages = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    collector._has_source_day = lambda *_args: True  # type: ignore[method-assign]
    collector._go_previous_day = lambda *_args: None  # type: ignore[method-assign]
    collector._resume_via_calendar = (  # type: ignore[method-assign]
        lambda _profile, target: recovered.append(target) or target
    )

    result = collector.capture_history(
        "activity",
        earliest_date=dt.date(2026, 7, 1),
        resume=False,
        capture_subpages=False,
    )

    assert recovered == [dt.date(2026, 7, 1)]
    assert captured == ["2026-07-02", "2026-07-01"]
    assert result["failures"] == 0
