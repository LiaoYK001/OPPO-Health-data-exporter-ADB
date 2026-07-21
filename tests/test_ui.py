from __future__ import annotations

import datetime as dt
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from health_exporter.constants import UI_PROFILES
from health_exporter.ui import (
    UiCollector,
    UiTree,
    advance_checkpoint_date,
    canvas_previous_point,
)


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
    assert dump_calls == 1
    assert swipe_calls == 1


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


def test_partial_today_does_not_replace_older_resume_boundary() -> None:
    today = dt.date(2026, 7, 21)
    existing = dt.date(2026, 6, 1)

    assert advance_checkpoint_date(existing, today, today) == (existing, False)


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
