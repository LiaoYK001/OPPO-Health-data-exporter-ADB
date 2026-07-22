from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .adb import AdbClient
from .artifacts import Artifact, ArtifactStore
from .constants import (
    EXCLUDED_HOME_CARDS,
    MAIN_ACTIVITY,
    PACKAGE,
    UI_PROFILES,
    MetricUiProfile,
)
from .db import Database, canonical_json
from .errors import ExporterError
from .operation_log import EVENT_LOG_NAME, OperationLogger


REMOTE_XML = "/sdcard/ohealth-exporter-window.xml"
REMOTE_PNG = "/sdcard/ohealth-exporter-screen.png"
DATE_PATTERN = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def local_id(node: ET.Element) -> str:
    return node.attrib.get("resource-id", "").split(":id/")[-1]


def node_label(node: ET.Element) -> str:
    return (
        node.attrib.get("text", "").strip()
        or node.attrib.get("content-desc", "").strip()
    )


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = BOUNDS_PATTERN.fullmatch(value or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def canvas_previous_point(
    date_bounds: tuple[int, int, int, int],
    screen_width: int,
    x_ratio: float,
) -> tuple[int, int]:
    """Return the evidence-derived center of a custom-drawn previous arrow."""

    return round(screen_width * x_ratio), date_bounds[3]


def date_from_label(label: str, reference: dt.date) -> dt.date | None:
    match = DATE_PATTERN.search(label)
    if not match:
        return None
    explicit_year, month, day = match.groups()
    if explicit_year:
        try:
            return dt.date(int(explicit_year), int(month), int(day))
        except ValueError:
            return None
    candidates: list[dt.date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(dt.date(year, int(month), int(day)))
        except ValueError:
            pass
    return min(candidates, key=lambda item: abs((item - reference).days)) if candidates else None


def advance_checkpoint_date(
    existing: dt.date | None,
    captured: dt.date,
    today: dt.date,
) -> tuple[dt.date, bool]:
    """Keep a descending-history checkpoint monotonic toward older dates.

    The current day is intentionally re-captured because it is partial, but it
    must not replace an older resume boundary and cause completed history to be
    traversed again after a restart.
    """

    if existing is not None and captured == today:
        return existing, False
    candidate = captured if existing is None else min(existing, captured)
    return candidate, candidate != existing


@dataclass
class UiTree:
    root: ET.Element

    def __post_init__(self) -> None:
        self.parent = {
            child: parent for parent in self.root.iter() for child in parent
        }

    @property
    def nodes(self) -> list[ET.Element]:
        return list(self.root.iter("node"))

    def by_id(self, *resource_names: str) -> ET.Element | None:
        names = set(resource_names)
        for node in self.nodes:
            if local_id(node) in names:
                return node
        return None

    def by_text(self, text: str) -> ET.Element | None:
        for node in self.nodes:
            if node_label(node) == text:
                return node
        return None

    def clickable_ancestor(self, node: ET.Element) -> ET.Element | None:
        current: ET.Element | None = node
        while current is not None:
            if current.attrib.get("clickable") == "true" and parse_bounds(
                current.attrib.get("bounds", "")
            ):
                return current
            current = self.parent.get(current)
        return None

    def scrollable(self) -> ET.Element | None:
        candidates = [
            node
            for node in self.nodes
            if node.attrib.get("scrollable") == "true"
            and parse_bounds(node.attrib.get("bounds", ""))
        ]
        if not candidates:
            return None
        # Detail screens frequently expose a full-screen horizontal ViewPager
        # as well as a slightly smaller vertical ScrollView. Selecting only by
        # area picks the pager and a vertical swipe leaves the page unchanged,
        # silently truncating evidence to the first viewport. Prefer widgets
        # whose Android class is explicitly vertical-scroll oriented.
        scroll_views = [
            node
            for node in candidates
            if "ScrollView" in node.attrib.get("class", "")
        ]
        vertical_collections = [
            node
            for node in candidates
            if any(
                name in node.attrib.get("class", "")
                for name in ("RecyclerView", "ListView")
            )
        ]
        return max(
            scroll_views or vertical_collections or candidates,
            key=lambda node: (
                (parse_bounds(node.attrib["bounds"])[2] - parse_bounds(node.attrib["bounds"])[0])
                * (parse_bounds(node.attrib["bounds"])[3] - parse_bounds(node.attrib["bounds"])[1])
            ),
        )

    def visible_payload(self) -> list[dict[str, Any]]:
        payload = []
        for node in self.nodes:
            label = node_label(node)
            resource_name = local_id(node)
            if not label and not resource_name:
                continue
            payload.append(
                {
                    "text": node.attrib.get("text", ""),
                    "content_desc": node.attrib.get("content-desc", ""),
                    "resource_id": resource_name,
                    "class": node.attrib.get("class", ""),
                    "bounds": node.attrib.get("bounds", ""),
                    "clickable": node.attrib.get("clickable") == "true",
                    "scrollable": node.attrib.get("scrollable") == "true",
                    "enabled": node.attrib.get("enabled") == "true",
                }
            )
        return payload

    def date_node(self, profile: MetricUiProfile | None = None) -> ET.Element | None:
        if profile:
            node = self.by_id(*profile.date_ids)
            if node is not None and node_label(node):
                return node
        for node in self.nodes:
            label = node_label(node)
            if DATE_PATTERN.search(label):
                return node
        return None

    def date_label(self, profile: MetricUiProfile | None = None) -> str | None:
        node = self.date_node(profile)
        return node_label(node) if node is not None else None


@dataclass(frozen=True)
class ViewportEvidence:
    index: int
    screenshot: Artifact
    xml: Artifact
    payload: list[dict[str, Any]]
    scroll_bounds: tuple[int, int, int, int] | None
    chart_artifacts: tuple[tuple[str, Artifact, tuple[int, int, int, int]], ...]


@dataclass(frozen=True)
class CalendarMonthView:
    year: int
    month: int
    label_bounds: tuple[int, int, int, int]
    grid_bounds: tuple[int, int, int, int]


def calendar_month_views(tree: UiTree) -> list[CalendarMonthView]:
    """Return visible calendar months with years reconstructed across December."""

    year_node = tree.by_id("calendar_year_value")
    if year_node is None:
        return []
    year_match = re.search(r"(\d{4})年", node_label(year_node))
    if year_match is None:
        return []
    year = int(year_match.group(1))
    month_nodes = sorted(
        (node for node in tree.nodes if local_id(node) == "month_value"),
        key=lambda node: (parse_bounds(node.attrib.get("bounds", "")) or (0, 0, 0, 0))[1],
    )
    result: list[CalendarMonthView] = []
    previous_month: int | None = None
    for node in month_nodes:
        month_match = re.fullmatch(r"(\d{1,2})月", node_label(node))
        label_bounds = parse_bounds(node.attrib.get("bounds", ""))
        parent = tree.parent.get(node)
        if month_match is None or label_bounds is None or parent is None:
            continue
        month = int(month_match.group(1))
        if not 1 <= month <= 12:
            continue
        if previous_month is not None and month < previous_month:
            year += 1
        previous_month = month
        grid_bounds = None
        children = list(parent)
        try:
            node_index = children.index(node)
        except ValueError:
            continue
        for child in children[node_index + 1 :]:
            bounds = parse_bounds(child.attrib.get("bounds", ""))
            if bounds is not None:
                grid_bounds = bounds
                break
        if grid_bounds is None:
            continue
        result.append(CalendarMonthView(year, month, label_bounds, grid_bounds))
    return result


def calendar_day_point(
    month_view: CalendarMonthView,
    day: int,
    screen_width: int,
) -> tuple[int, int] | None:
    """Map an OPPO custom-drawn calendar cell to its screen coordinate."""

    try:
        days_in_month = (dt.date(month_view.year + (month_view.month == 12), month_view.month % 12 + 1, 1) - dt.timedelta(days=1)).day
        first_weekday = (dt.date(month_view.year, month_view.month, 1).weekday() + 1) % 7
    except ValueError:
        return None
    if not 1 <= day <= days_in_month or screen_width <= 0:
        return None
    cell_index = first_weekday + day - 1
    row, column = divmod(cell_index, 7)
    week_rows = math.ceil((first_weekday + days_in_month) / 7)
    x = round(screen_width * (column + 1) / 8)
    grid_top = month_view.grid_bounds[1]
    grid_height = month_view.grid_bounds[3] - grid_top
    if grid_height <= 0:
        return None
    y = round(grid_top + grid_height * (row + 0.5) / week_rows)
    return x, y


class UiCollector:
    def __init__(
        self,
        adb: AdbClient,
        db: Database,
        artifacts: ArtifactStore,
        source_id: int,
        run_id: str,
        app_version: str,
        display_profile: str,
    ):
        self.adb = adb
        self.db = db
        self.artifacts = artifacts
        self.source_id = source_id
        self.run_id = run_id
        self.app_version = app_version
        self.display_profile = display_profile
        self.operation_logger = OperationLogger(
            artifacts.data_dir / "logs" / EVENT_LOG_NAME,
            run_id,
        )

    def _event(
        self,
        level: str,
        event: str,
        message: str,
        **context: Any,
    ) -> None:
        logger = getattr(self, "operation_logger", None)
        if logger is not None:
            logger.log(level, event, message, **context)

    def dump_tree(self, directory: Path, stem: str) -> tuple[UiTree, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        xml_path = directory / f"{stem}.xml"
        output = self.adb.shell("uiautomator", "dump", REMOTE_XML, check=False)
        if "ERROR" in output.upper():
            raise ExporterError(f"UI 层级读取失败：{output}")
        self.adb.pull(REMOTE_XML, xml_path)
        try:
            return UiTree(ET.parse(xml_path).getroot()), xml_path
        except ET.ParseError as exc:
            raise ExporterError(f"UI XML 无法解析：{xml_path}") from exc

    def screenshot(self, directory: Path, stem: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem}.png"
        self.adb.shell("screencap", "-p", REMOTE_PNG)
        self.adb.pull(REMOTE_PNG, path)
        return path

    def inventory_home(self, max_scrolls: int = 12) -> dict[str, Any]:
        self._ensure_home()
        self.dismiss_known_popups()
        self._scroll_to_top()
        cards: set[str] = set()
        views: list[dict[str, Any]] = []
        fingerprints: set[str] = set()
        with tempfile.TemporaryDirectory(prefix="ohealth-home-inventory-") as temp:
            temp_root = Path(temp)
            for index in range(max_scrolls):
                tree, xml_path = self.dump_tree(temp_root, f"home-{index:02d}")
                payload = tree.visible_payload()
                fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                if fingerprint in fingerprints:
                    break
                fingerprints.add(fingerprint)
                titles = {
                    node_label(node)
                    for node in tree.nodes
                    if local_id(node) == "tv_title" and node_label(node)
                }
                composite = {
                    label
                    for label in ("步数", "活动消耗", "锻炼时长", "活动次数")
                    if any(node_label(node) == label for node in tree.nodes)
                }
                cards.update(titles)
                if len(composite) == 4:
                    cards.add("每日活动")
                xml_artifact = self.artifacts.put_file(xml_path, media_type="application/xml")
                views.append(
                    {
                        "index": index,
                        "titles": sorted(titles | composite),
                        "xml_sha256": xml_artifact.sha256,
                    }
                )
                scrollable = tree.scrollable()
                if scrollable is None:
                    break
                if not self._swipe_scrollable(scrollable, down=False):
                    break
                time.sleep(0.6)
        self._scroll_to_top()
        primary_titles = {
            title
            for profile in UI_PROFILES.values()
            for title in profile.home_titles
        }
        included = sorted(card for card in cards if card in primary_titles or card == "每日活动")
        excluded = sorted(card for card in cards if card in EXCLUDED_HOME_CARDS)
        unknown = sorted(cards - set(included) - set(excluded))
        return {
            "cards": sorted(cards),
            "included": included,
            "excluded": excluded,
            "unknown": unknown,
            "views": views,
        }

    def enter_metric(self, metric: str, max_scrolls: int = 12) -> UiTree:
        if metric not in UI_PROFILES:
            raise ExporterError(f"未知 UI 指标：{metric}")
        profile = UI_PROFILES[metric]
        current = self._reuse_current_metric_page(metric, profile)
        if current is not None:
            self._event(
                "INFO",
                "navigation.reuse_page",
                "当前已经位于目标指标日详情页，直接复用",
                metric=metric,
                page=metric,
                activity=self.adb.foreground(),
                action="reuse_current_page",
                status="complete",
            )
            return current
        self._event(
            "INFO",
            "navigation.home",
            "返回健康首页并查找指标卡片",
            metric=metric,
            page="health_home",
            action="find_metric_card",
            status="started",
        )
        self._ensure_home()
        self.dismiss_known_popups()
        self._scroll_to_top()
        with tempfile.TemporaryDirectory(prefix=f"ohealth-enter-{metric}-") as temp:
            for index in range(max_scrolls):
                tree, _ = self.dump_tree(Path(temp), f"view-{index:02d}")
                target: ET.Element | None = None
                if metric == "activity":
                    for node in tree.nodes:
                        if node.attrib.get("clickable") != "true":
                            continue
                        descendants = {node_label(child) for child in node.iter("node")}
                        if {"步数", "活动消耗", "锻炼时长", "活动次数"}.issubset(descendants):
                            target = node
                            break
                else:
                    for title in profile.home_titles:
                        label_node = tree.by_text(title)
                        if label_node is not None:
                            target = tree.clickable_ancestor(label_node)
                            if target is not None:
                                break
                if target is not None:
                    bounds = parse_bounds(target.attrib.get("bounds", ""))
                    if bounds:
                        self._event(
                            "INFO",
                            "navigation.tap_metric",
                            f"在首页第 {index + 1} 个视口点击指标卡片",
                            metric=metric,
                            page="health_home",
                            activity=self.adb.foreground(),
                            action="tap",
                            status="started",
                            details={"viewport": index, "bounds": bounds},
                        )
                        self.adb.tap(*center(bounds))
                        time.sleep(1.5)
                        detail, _ = self.dump_tree(Path(temp), "detail")
                        expected_activity = (
                            metric == "weight"
                            and self.adb.foreground().endswith(
                                "/com.heytap.health.bodyfat.ui.BodyFatDetailsActivity"
                            )
                        )
                        if expected_activity or any(
                            detail.by_text(title) is not None for title in profile.page_titles
                        ):
                            self._select_day_tab(detail)
                            self._event(
                                "INFO",
                                "navigation.metric_opened",
                                "已进入指标详情页并确认日视图",
                                metric=metric,
                                page=metric,
                                activity=self.adb.foreground(),
                                action="open_metric_detail",
                                status="complete",
                            )
                            return detail
                        self._event(
                            "ERROR",
                            "navigation.unexpected_page",
                            "点击指标卡后没有进入预期详情页",
                            metric=metric,
                            page=metric,
                            activity=self.adb.foreground(),
                            action="verify_metric_detail",
                            status="failed",
                        )
                        raise ExporterError(f"点击 {metric} 后未进入预期详情页。")
                scrollable = tree.scrollable()
                if scrollable is None or not self._swipe_scrollable(scrollable, down=False):
                    break
                self._event(
                    "DEBUG",
                    "navigation.home_scroll",
                    f"首页未找到目标，向下滚动到视口 {index + 2}",
                    metric=metric,
                    page="health_home",
                    action="swipe_up",
                    status="complete",
                    details={"viewport": index + 1},
                )
                time.sleep(0.6)
        self._event(
            "ERROR",
            "navigation.metric_missing",
            "遍历健康首页后仍未找到指标卡",
            metric=metric,
            page="health_home",
            action="find_metric_card",
            status="failed",
            details={"max_scrolls": max_scrolls},
        )
        raise ExporterError(f"健康首页未找到指标卡：{metric}")

    def _reuse_current_metric_page(
        self,
        metric: str,
        profile: MetricUiProfile,
    ) -> UiTree | None:
        foreground = self.adb.foreground()
        if not foreground.startswith(PACKAGE + "/") or foreground.endswith(
            "/com.heytap.health.main.MainActivity"
        ):
            return None
        with tempfile.TemporaryDirectory(prefix=f"ohealth-reuse-{metric}-") as temp:
            tree, _ = self.dump_tree(Path(temp), "current")
        if tree.date_label(profile) is None:
            return None
        day_tab_selected = any(
            node_label(node) == "日" and node.attrib.get("selected") == "true"
            for node in tree.nodes
        )
        if not day_tab_selected:
            return None
        if metric == "weight" and foreground.endswith(
            "/com.heytap.health.bodyfat.ui.BodyFatDetailsActivity"
        ):
            return tree
        if any(tree.by_text(title) is not None for title in profile.page_titles):
            return tree
        return None

    def capture_history(
        self,
        metric: str,
        *,
        earliest_date: dt.date,
        max_days: int | None = None,
        resume: bool = True,
        capture_subpages: bool = True,
        max_scrolls: int = 12,
    ) -> dict[str, Any]:
        profile = UI_PROFILES[metric]
        self._event(
            "INFO",
            "history.start",
            f"开始遍历 {profile.metric} 历史页面",
            metric=metric,
            page=metric,
            action="enter_metric",
            status="started",
            details={
                "earliest_date": earliest_date.isoformat(),
                "max_days": max_days,
                "resume": resume,
                "capture_subpages": capture_subpages,
            },
        )
        tree = self.enter_metric(metric)
        label = tree.date_label(profile)
        if not label:
            self._event(
                "ERROR",
                "page.date_missing",
                "指标详情页没有可识别日期",
                metric=metric,
                page=metric,
                action="read_date",
                status="failed",
            )
            raise ExporterError(f"{metric} 页面没有可识别日期。")
        current_date = date_from_label(label, dt.date.today())
        if current_date is None:
            self._event(
                "ERROR",
                "page.date_parse_failed",
                f"无法解析页面日期：{label}",
                metric=metric,
                page=metric,
                action="parse_date",
                status="failed",
                details={"ui_label": label},
            )
            raise ExporterError(f"无法解析 {metric} 页面日期：{label}")
        today = dt.date.today()
        checkpoint = self.db.get_checkpoint(self.source_id, metric) if resume else None
        checkpoint_date: dt.date | None = None
        if checkpoint is not None and checkpoint["last_date"]:
            try:
                checkpoint_date = dt.date.fromisoformat(str(checkpoint["last_date"]))
            except ValueError as exc:
                raise ExporterError(
                    f"{metric} checkpoint 日期无效：{checkpoint['last_date']}"
                ) from exc
        self._event(
            "INFO",
            "history.position",
            f"当前页面日期 {current_date.isoformat()}，checkpoint "
            f"{checkpoint_date.isoformat() if checkpoint_date else '无'}",
            metric=metric,
            local_date=current_date.isoformat(),
            page=metric,
            action="read_checkpoint",
            status="ready",
            details={"ui_label": label, "checkpoint": checkpoint_date},
        )
        if (
            checkpoint_date is not None
            and current_date > checkpoint_date
            and (current_date - checkpoint_date).days >= 3
        ):
            calendar_date = self._resume_via_calendar(profile, checkpoint_date)
            if calendar_date is not None:
                current_date = calendar_date
            else:
                self._event(
                    "WARNING",
                    "calendar.fallback",
                    "日历直达未完成，改用逐日批量翻页",
                    metric=metric,
                    local_date=current_date.isoformat(),
                    page=metric,
                    action="resume_previous_day_batches",
                    status="fallback",
                    details={"checkpoint": checkpoint_date},
                )
                current_date = self._resume_to_checkpoint(
                    profile,
                    current_date,
                    checkpoint_date,
                )
        captured = skipped = failures = attempted = 0
        visited = 0
        while current_date >= earliest_date:
            if max_days is not None and attempted >= max_days:
                break
            visited += 1
            day = current_date.isoformat()
            already_done = (
                checkpoint_date is not None
                and current_date >= checkpoint_date
                and current_date != today
            )
            self._event(
                "INFO",
                "day.begin",
                f"处理日期 {day}" + ("（已有 checkpoint，将跳过）" if already_done else ""),
                metric=metric,
                local_date=day,
                page=metric,
                action="evaluate_day",
                status="skipped" if already_done else "started",
                details={"visited": visited, "attempted": attempted},
            )
            try:
                if already_done:
                    skipped += 1
                else:
                    attempted += 1
                    self._event(
                        "INFO",
                        "page.capture_start",
                        "开始采集主详情页全部滚动区域",
                        metric=metric,
                        local_date=day,
                        page=metric,
                        action="capture_page",
                        status="started",
                    )
                    main_capture_id = self.capture_page(
                        metric,
                        day,
                        metric,
                        max_scrolls=max_scrolls,
                    )
                    if capture_subpages:
                        self._capture_subpages(
                            metric,
                            day,
                            profile,
                            main_capture_id=main_capture_id,
                            max_scrolls=max_scrolls,
                        )
                    has_source_day = self._has_source_day(metric, day)
                    self.db.set_coverage(
                        self.source_id,
                        metric,
                        day,
                        "exact_ui" if has_source_day else "no_data",
                        {
                            "database": has_source_day,
                            "ui": True,
                            "no_data": not has_source_day,
                            "basis": (
                                "source_and_ui_evidence"
                                if has_source_day
                                else "exact_source_absence_with_ui_evidence"
                            ),
                        },
                        self.run_id,
                    )
                    next_checkpoint, moved = advance_checkpoint_date(
                        checkpoint_date,
                        current_date,
                        today,
                    )
                    if moved:
                        self.db.set_checkpoint(
                            self.source_id,
                            metric,
                            next_checkpoint.isoformat(),
                            {"page_date": day, "status": "captured"},
                        )
                        checkpoint_date = next_checkpoint
                    self.db.connection.commit()
                    captured += 1
                    self._event(
                        "INFO",
                        "day.complete",
                        f"日期 {day} 已完整提交，capture_id={main_capture_id}",
                        metric=metric,
                        local_date=day,
                        page=metric,
                        action="commit_day",
                        status="complete",
                        details={
                            "capture_id": main_capture_id,
                            "has_source_day": has_source_day,
                            "checkpoint": checkpoint_date,
                        },
                    )
            except Exception as exc:
                failures += 1
                self.db.record_failure(
                    self.run_id,
                    "ui_capture",
                    exc,
                    metric=metric,
                    local_date=day,
                )
                self.db.set_coverage(
                    self.source_id,
                    metric,
                    day,
                    "failed",
                    {"error": str(exc)},
                    self.run_id,
                )
                self.db.connection.commit()
                self._event(
                    "ERROR",
                    "day.failed",
                    str(exc),
                    metric=metric,
                    local_date=day,
                    page=metric,
                    action="capture_day",
                    status="failed",
                    details={"exception_type": type(exc).__name__},
                )
            if max_days is not None and attempted >= max_days:
                break
            if current_date == earliest_date:
                break
            previous_date = self._go_previous_day(profile, current_date)
            if previous_date is None:
                self.db.record_failure(
                    self.run_id,
                    "date_navigation",
                    "日期没有变化",
                    metric=metric,
                    local_date=day,
                )
                failures += 1
                self._event(
                    "WARNING",
                    "navigation.date_unchanged",
                    "点击上一天后日期没有变化，停止当前指标",
                    metric=metric,
                    local_date=day,
                    page=metric,
                    action="go_previous_day",
                    status="blocked",
                )
                break
            self._event(
                "INFO",
                "navigation.previous_day",
                f"已从 {current_date.isoformat()} 切换到 {previous_date.isoformat()}",
                metric=metric,
                local_date=previous_date.isoformat(),
                page=metric,
                action="go_previous_day",
                status="complete",
            )
            current_date = previous_date
        self._event(
            "INFO" if failures == 0 else "WARNING",
            "history.complete",
            f"{profile.metric} 本轮结束：采集 {captured}，跳过 {skipped}，失败 {failures}",
            metric=metric,
            page=metric,
            action="capture_history",
            status="complete" if failures == 0 else "completed_with_warnings",
            details={
                "captured": captured,
                "skipped": skipped,
                "failures": failures,
                "attempted": attempted,
                "visited": visited,
            },
        )
        return {
            "metric": metric,
            "captured": captured,
            "skipped": skipped,
            "failures": failures,
            "attempted": attempted,
            "visited": visited,
        }

    def capture_page(
        self,
        metric: str,
        local_date: str,
        page_key: str,
        max_scrolls: int = 12,
    ) -> int:
        activity = self.adb.foreground()
        self._event(
            "INFO",
            "page.scroll_top",
            "将页面恢复到顶部，准备逐屏采集",
            metric=metric,
            local_date=local_date,
            page=page_key,
            activity=activity,
            action="scroll_to_top",
            status="started",
        )
        initial_view = self._scroll_to_top(max_swipes=8)
        evidences: list[ViewportEvidence] = []
        seen: set[str] = set()
        with tempfile.TemporaryDirectory(prefix=f"ohealth-page-{metric}-") as temp:
            temp_root = Path(temp)
            for index in range(max_scrolls):
                if index == 0 and initial_view is not None:
                    tree, xml_bytes = initial_view
                    xml_path = temp_root / f"viewport-{index:02d}.xml"
                    xml_path.write_bytes(xml_bytes)
                else:
                    tree, xml_path = self.dump_tree(
                        temp_root, f"viewport-{index:02d}"
                    )
                payload = tree.visible_payload()
                fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                if fingerprint in seen:
                    self._event(
                        "DEBUG",
                        "viewport.repeat",
                        f"视口 {index + 1} 与已有内容重复，确认到达页面底部",
                        metric=metric,
                        local_date=local_date,
                        page=page_key,
                        activity=activity,
                        action="compare_viewport",
                        status="complete",
                        details={"viewport": index},
                    )
                    break
                seen.add(fingerprint)
                # Only take the relatively expensive PNG after proving this
                # viewport is new. At the bottom of a page the final swipe
                # commonly leaves the hierarchy unchanged.
                screenshot_path = self.screenshot(temp_root, f"viewport-{index:02d}")
                xml_artifact = self.artifacts.put_file(xml_path, media_type="application/xml")
                screenshot_artifact = self.artifacts.put_file(screenshot_path, media_type="image/png")
                scrollable = tree.scrollable()
                scroll_bounds = (
                    parse_bounds(scrollable.attrib.get("bounds", ""))
                    if scrollable is not None
                    else None
                )
                chart_artifacts = self._capture_chart_crops(tree, screenshot_path, temp_root, index)
                evidences.append(
                    ViewportEvidence(
                        index,
                        screenshot_artifact,
                        xml_artifact,
                        payload,
                        scroll_bounds,
                        tuple(chart_artifacts),
                    )
                )
                self._event(
                    "INFO",
                    "viewport.captured",
                    f"已保存第 {index + 1} 个视口的 PNG、XML"
                    f"及 {len(chart_artifacts)} 个图表裁剪",
                    metric=metric,
                    local_date=local_date,
                    page=page_key,
                    activity=activity,
                    action="capture_viewport",
                    status="complete",
                    details={
                        "viewport": index,
                        "visible_nodes": len(payload),
                        "screenshot_sha256": screenshot_artifact.sha256,
                        "xml_sha256": xml_artifact.sha256,
                        "chart_count": len(chart_artifacts),
                    },
                )
                if scrollable is None or not self._swipe_scrollable(scrollable, down=False):
                    self._event(
                        "DEBUG",
                        "page.bottom",
                        "当前视口没有可继续滚动的区域，结束本页采集",
                        metric=metric,
                        local_date=local_date,
                        page=page_key,
                        activity=activity,
                        action="detect_page_bottom",
                        status="complete",
                        details={"viewport": index},
                    )
                    break
                self._event(
                    "INFO",
                    "viewport.scroll",
                    f"向下滚动，准备读取第 {index + 2} 个视口",
                    metric=metric,
                    local_date=local_date,
                    page=page_key,
                    activity=activity,
                    action="swipe_up",
                    status="complete",
                    details={"from_viewport": index, "to_viewport": index + 1},
                )
                time.sleep(0.6)
            mosaic_path = temp_root / "page-mosaic.png"
            self._build_mosaic(evidences, mosaic_path)
            mosaic_artifact = self.artifacts.put_file(mosaic_path, media_type="image/png")

        display_payload = {
            "date": local_date,
            "page_key": page_key,
            "viewports": [
                {
                    "index": item.index,
                    "nodes": item.payload,
                    "scroll_bounds": item.scroll_bounds,
                    "screenshot_sha256": item.screenshot.sha256,
                    "xml_sha256": item.xml.sha256,
                }
                for item in evidences
            ],
            "stitch_mode": "lossless_viewport_stack",
        }
        capture_hash = hashlib.sha256(
            canonical_json(
                {
                    "payload": display_payload,
                    "mosaic": mosaic_artifact.sha256,
                }
            ).encode()
        ).hexdigest()
        capture_id = self.db.add_page_capture(
            source_id=self.source_id,
            run_id=self.run_id,
            metric=metric,
            local_date=local_date,
            page_key=page_key,
            app_activity=self.adb.foreground(),
            app_version=self.app_version,
            display_profile=self.display_profile,
            display_payload=display_payload,
            capture_hash=capture_hash,
            fidelity="exact_ui",
        )
        self.db.link_evidence(capture_id, mosaic_artifact.sha256, "page_mosaic")
        for item in evidences:
            self.db.link_evidence(capture_id, item.screenshot.sha256, "viewport_png", item.index)
            self.db.link_evidence(capture_id, item.xml.sha256, "ui_xml", item.index)
            for chart_id, artifact, bounds in item.chart_artifacts:
                self.db.link_evidence(
                    capture_id,
                    artifact.sha256,
                    f"chart:{chart_id}",
                    item.index,
                    {"bounds": bounds},
                )
        self.db.connection.commit()
        self._event(
            "INFO",
            "page.committed",
            f"页面已入库，共 {len(evidences)} 个视口",
            metric=metric,
            local_date=local_date,
            page=page_key,
            activity=activity,
            action="commit_page",
            status="complete",
            details={
                "capture_id": capture_id,
                "viewport_count": len(evidences),
                "mosaic_sha256": mosaic_artifact.sha256,
            },
        )
        return capture_id

    def _capture_subpages(
        self,
        metric: str,
        local_date: str,
        profile: MetricUiProfile,
        main_capture_id: int,
        max_scrolls: int = 12,
    ) -> None:
        visible_titles = self._visible_subpage_titles(main_capture_id, profile)
        self._event(
            "INFO",
            "subpages.discovered",
            f"发现 {len(visible_titles)} 个可采集子页面",
            metric=metric,
            local_date=local_date,
            page=metric,
            action="discover_subpages",
            status="complete",
            details={"titles": sorted(visible_titles)},
        )
        for title in profile.subpage_titles:
            if title not in visible_titles:
                continue
            if not self._open_text_target(title):
                self._event(
                    "WARNING",
                    "subpage.open_failed",
                    f"发现子页面入口但无法打开：{title}",
                    metric=metric,
                    local_date=local_date,
                    page=f"{metric}/{title}",
                    action="open_subpage",
                    status="failed",
                )
                continue
            self._event(
                "INFO",
                "subpage.opened",
                f"已打开子页面：{title}",
                metric=metric,
                local_date=local_date,
                page=f"{metric}/{title}",
                activity=self.adb.foreground(),
                action="open_subpage",
                status="complete",
            )
            time.sleep(0.9)
            try:
                self.capture_page(
                    metric,
                    local_date,
                    f"{metric}/{title}",
                    max_scrolls=max_scrolls,
                )
            finally:
                self.adb.keyevent("BACK")
                time.sleep(0.8)
                self._event(
                    "INFO",
                    "subpage.returned",
                    f"已从子页面返回主详情：{title}",
                    metric=metric,
                    local_date=local_date,
                    page=metric,
                    action="back",
                    status="complete",
                )

    def _visible_subpage_titles(
        self,
        main_capture_id: int,
        profile: MetricUiProfile,
    ) -> set[str]:
        row = self.db.connection.execute(
            "SELECT display_payload_json FROM page_captures WHERE id=?",
            (main_capture_id,),
        ).fetchone()
        if row is None:
            raise ExporterError(f"主页面采集记录不存在：{main_capture_id}")
        payload = json.loads(row["display_payload_json"])
        expected = set(profile.subpage_titles)
        visible: set[str] = set()
        for viewport in payload.get("viewports", []):
            if not isinstance(viewport, dict):
                continue
            for node in viewport.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                label = str(node.get("text") or node.get("content_desc") or "").strip()
                if label in expected:
                    visible.add(label)
        return visible

    def _open_text_target(self, title: str, max_scrolls: int = 10) -> bool:
        with tempfile.TemporaryDirectory(prefix="ohealth-find-subpage-") as temp:
            temp_root = Path(temp)
            # capture_page normally leaves the parent page close to the card
            # that was just recorded. Try that visible state first; forcing a
            # scroll-to-top before every subpage adds several hierarchy dumps.
            current, _ = self.dump_tree(temp_root, "current")
            if self._tap_text_target(current, title):
                return True

            initial_view = self._scroll_to_top(max_swipes=8)
            seen: set[str] = set()
            for index in range(max_scrolls):
                if index == 0 and initial_view is not None:
                    tree = initial_view[0]
                else:
                    tree, _ = self.dump_tree(temp_root, f"find-{index:02d}")
                fingerprint = hashlib.sha256(
                    canonical_json(tree.visible_payload()).encode()
                ).hexdigest()
                if fingerprint in seen:
                    return False
                seen.add(fingerprint)
                if self._tap_text_target(tree, title):
                    return True
                scrollable = tree.scrollable()
                if scrollable is None or not self._swipe_scrollable(scrollable, down=False):
                    return False
                time.sleep(0.5)
        return False

    def _tap_text_target(self, tree: UiTree, title: str) -> bool:
        node = tree.by_text(title)
        target = tree.clickable_ancestor(node) if node is not None else None
        if target is None:
            return False
        bounds = parse_bounds(target.attrib.get("bounds", ""))
        if not bounds:
            return False
        self.adb.tap(*center(bounds))
        return True

    def _go_previous_day(
        self,
        profile: MetricUiProfile,
        current: dt.date,
    ) -> dt.date | None:
        initial_view = self._scroll_to_top(max_swipes=8)
        with tempfile.TemporaryDirectory(prefix="ohealth-date-nav-") as temp:
            if initial_view is not None:
                tree = initial_view[0]
            else:
                tree, _ = self.dump_tree(Path(temp), "before")
            point = self._previous_tap_point(tree, profile)
            if point is None:
                return None
            self.adb.tap(*point)
            deadline = time.time() + 10
            while time.time() < deadline:
                time.sleep(0.5)
                updated, _ = self.dump_tree(Path(temp), "after")
                label = updated.date_label(profile)
                parsed = date_from_label(label or "", current - dt.timedelta(days=1))
                if parsed and parsed != current:
                    return parsed
        return None

    def _resume_to_checkpoint(
        self,
        profile: MetricUiProfile,
        current: dt.date,
        checkpoint: dt.date,
        *,
        batch_size: int = 5,
        tap_delay: float = 0.35,
    ) -> dt.date:
        """Quickly cross dates already proven complete by a resume checkpoint.

        Every small click batch is verified from the actual UI date. If the page
        does not advance, the caller resumes the slower per-day navigation path.
        """
        with tempfile.TemporaryDirectory(prefix="ohealth-resume-nav-") as temp:
            temp_root = Path(temp)
            rounds = 0
            self._event(
                "INFO",
                "resume.batch_start",
                f"开始逐日批量翻页：{current.isoformat()} → {checkpoint.isoformat()}",
                metric=profile.metric,
                local_date=current.isoformat(),
                page=profile.metric,
                action="previous_day_batches",
                status="started",
            )
            while current > checkpoint:
                rounds += 1
                if rounds > 1000:
                    return current
                initial_view = self._scroll_to_top(max_swipes=8)
                if initial_view is not None:
                    tree = initial_view[0]
                else:
                    tree, _ = self.dump_tree(temp_root, f"before-{rounds:03d}")
                point = self._previous_tap_point(tree, profile)
                if point is None:
                    return current
                requested = min(batch_size, (current - checkpoint).days)
                self._event(
                    "INFO",
                    "resume.batch_tap",
                    f"连续点击上一天 {requested} 次",
                    metric=profile.metric,
                    local_date=current.isoformat(),
                    page=profile.metric,
                    action="tap_previous_day",
                    status="started",
                    details={"round": rounds, "requested": requested},
                )
                for _ in range(requested):
                    self.adb.tap(*point)
                    if tap_delay:
                        time.sleep(tap_delay)
                time.sleep(0.5)
                updated, _ = self.dump_tree(temp_root, f"after-{rounds:03d}")
                label = updated.date_label(profile)
                parsed = date_from_label(label or "", current - dt.timedelta(days=requested))
                if parsed is None or parsed >= current:
                    self._event(
                        "WARNING",
                        "resume.batch_stalled",
                        "批量翻页后未读取到更早日期",
                        metric=profile.metric,
                        local_date=current.isoformat(),
                        page=profile.metric,
                        action="verify_date",
                        status="stalled",
                        details={"ui_label": label},
                    )
                    return current
                if parsed < checkpoint:
                    raise ExporterError(
                        f"续跑日期越过检查点：{parsed} < {checkpoint}"
                    )
                current = parsed
                self._event(
                    "INFO",
                    "resume.batch_verified",
                    f"批量翻页后确认日期 {current.isoformat()}",
                    metric=profile.metric,
                    local_date=current.isoformat(),
                    page=profile.metric,
                    action="verify_date",
                    status="complete",
                    details={"ui_label": label, "round": rounds},
                )
        return current

    def _resume_via_calendar(
        self,
        profile: MetricUiProfile,
        checkpoint: dt.date,
        *,
        max_rounds: int = 24,
    ) -> dt.date | None:
        """Jump to a checkpoint through OPPO's month calendar when available.

        The date cells are custom-drawn and absent from UIAutomator. Month and
        year labels remain semantic nodes, so the collector navigates by those
        labels, aligns the whole target month on screen, then calculates the
        date cell from the Gregorian weekday layout. Every tap is accepted only
        after the detail page reports the exact requested date.
        """

        def month_index(year: int, month: int) -> int:
            return year * 12 + month - 1

        def screen_bounds(tree: UiTree) -> tuple[int, int]:
            bounds = [
                value
                for node in tree.nodes
                if (value := parse_bounds(node.attrib.get("bounds", "")))
            ]
            return (
                max((value[2] for value in bounds), default=0),
                max((value[3] for value in bounds), default=0),
            )

        calendar_open = False
        self._event(
            "INFO",
            "calendar.start",
            f"准备通过日历直达 checkpoint {checkpoint.isoformat()}",
            metric=profile.metric,
            local_date=checkpoint.isoformat(),
            page=profile.metric,
            action="open_calendar",
            status="started",
        )
        with tempfile.TemporaryDirectory(prefix="ohealth-calendar-resume-") as temp:
            temp_root = Path(temp)
            try:
                initial_view = self._scroll_to_top(max_swipes=8)
                if initial_view is not None:
                    page_tree = initial_view[0]
                else:
                    page_tree, _ = self.dump_tree(temp_root, "page")
                opener = page_tree.by_id("iv_down") or page_tree.date_node(profile)
                if opener is None:
                    self._event(
                        "WARNING",
                        "calendar.opener_missing",
                        "未找到日期下拉入口",
                        metric=profile.metric,
                        local_date=checkpoint.isoformat(),
                        page=profile.metric,
                        action="find_calendar_opener",
                        status="failed",
                    )
                    return None
                clickable = page_tree.clickable_ancestor(opener) or opener
                opener_bounds = parse_bounds(clickable.attrib.get("bounds", ""))
                if opener_bounds is None:
                    self._event(
                        "WARNING",
                        "calendar.opener_bounds_missing",
                        "日期下拉入口没有可点击坐标",
                        metric=profile.metric,
                        local_date=checkpoint.isoformat(),
                        page=profile.metric,
                        action="find_calendar_opener",
                        status="failed",
                    )
                    return None
                self._event(
                    "INFO",
                    "calendar.tap_opener",
                    "点击日期右侧下拉箭头",
                    metric=profile.metric,
                    local_date=checkpoint.isoformat(),
                    page=profile.metric,
                    action="tap",
                    status="started",
                    details={"bounds": opener_bounds},
                )
                self.adb.tap(*center(opener_bounds))
                deadline = time.time() + 8
                calendar_tree: UiTree | None = None
                while time.time() < deadline:
                    time.sleep(0.4)
                    candidate, _ = self.dump_tree(temp_root, "calendar-open")
                    if candidate.by_id("recycler_view") is not None and calendar_month_views(candidate):
                        calendar_tree = candidate
                        calendar_open = True
                        visible = calendar_month_views(candidate)
                        self._event(
                            "INFO",
                            "calendar.opened",
                            "日历已打开并识别到月份列表",
                            metric=profile.metric,
                            local_date=checkpoint.isoformat(),
                            page="calendar",
                            action="verify_calendar",
                            status="complete",
                            details={
                                "visible_months": [
                                    f"{item.year:04d}-{item.month:02d}" for item in visible
                                ]
                            },
                        )
                        break
                if calendar_tree is None:
                    self._event(
                        "WARNING",
                        "calendar.open_timeout",
                        "点击后 8 秒内未识别到日历",
                        metric=profile.metric,
                        local_date=checkpoint.isoformat(),
                        page=profile.metric,
                        action="verify_calendar",
                        status="timeout",
                    )
                    return None

                target_index = month_index(checkpoint.year, checkpoint.month)
                for round_index in range(max_rounds):
                    months = calendar_month_views(calendar_tree)
                    if not months:
                        return None
                    width, height = screen_bounds(calendar_tree)
                    recycler = calendar_tree.by_id("recycler_view")
                    recycler_bounds = (
                        parse_bounds(recycler.attrib.get("bounds", ""))
                        if recycler is not None
                        else None
                    )
                    if width <= 0 or height <= 0 or recycler_bounds is None:
                        return None
                    matching = next(
                        (
                            item
                            for item in months
                            if (item.year, item.month) == (checkpoint.year, checkpoint.month)
                        ),
                        None,
                    )
                    x1, y1, x2, y2 = recycler_bounds
                    gesture_x = (x1 + x2) // 2
                    gesture_top = y1 + round((y2 - y1) * 0.15)
                    gesture_bottom = y1 + round((y2 - y1) * 0.85)
                    if matching is not None:
                        # UIAutomator clips a partially visible custom month
                        # view to the screen. Move it upward before calculating
                        # a date coordinate; clipped bottom-row taps are unsafe.
                        if matching.grid_bounds[1] < y1:
                            align_start, align_end = gesture_top, gesture_bottom
                        elif matching.grid_bounds[3] >= height - 24:
                            align_start, align_end = gesture_bottom, gesture_top
                        else:
                            align_start = align_end = 0
                        if align_start != align_end:
                            self._event(
                                "INFO",
                                "calendar.align_month",
                                f"目标月份 {checkpoint.year}-{checkpoint.month:02d} 尚未完整显示，进行对齐滚动",
                                metric=profile.metric,
                                local_date=checkpoint.isoformat(),
                                page="calendar",
                                action="swipe_calendar",
                                status="started",
                                details={"round": round_index, "grid_bounds": matching.grid_bounds},
                            )
                            self.adb.swipe(
                                gesture_x,
                                align_start,
                                gesture_x,
                                align_end,
                                500,
                            )
                            time.sleep(0.7)
                            calendar_tree, _ = self.dump_tree(
                                temp_root, f"calendar-align-{round_index:02d}"
                            )
                            continue
                        point = calendar_day_point(matching, checkpoint.day, width)
                        if point is None:
                            return None
                        self._event(
                            "INFO",
                            "calendar.tap_day",
                            f"目标月份已完整对齐，点击 {checkpoint.isoformat()}",
                            metric=profile.metric,
                            local_date=checkpoint.isoformat(),
                            page="calendar",
                            action="tap_calendar_day",
                            status="started",
                            details={"point": point, "grid_bounds": matching.grid_bounds},
                        )
                        self.adb.tap(*point)
                        deadline = time.time() + 10
                        while time.time() < deadline:
                            time.sleep(0.5)
                            result_tree, _ = self.dump_tree(temp_root, "calendar-result")
                            label = result_tree.date_label(profile)
                            parsed = date_from_label(label or "", checkpoint)
                            if parsed == checkpoint:
                                self._event(
                                    "INFO",
                                    "calendar.confirmed",
                                    f"详情页回读日期确认：{checkpoint.isoformat()}",
                                    metric=profile.metric,
                                    local_date=checkpoint.isoformat(),
                                    page=profile.metric,
                                    action="verify_date",
                                    status="complete",
                                    details={"ui_label": label, "tap_point": point},
                                )
                                return parsed
                        self._event(
                            "WARNING",
                            "calendar.date_timeout",
                            "点击目标日期后未在 10 秒内回读到准确日期",
                            metric=profile.metric,
                            local_date=checkpoint.isoformat(),
                            page=profile.metric,
                            action="verify_date",
                            status="timeout",
                        )
                        return None

                    first_index = month_index(months[0].year, months[0].month)
                    last_index = month_index(months[-1].year, months[-1].month)
                    if target_index < first_index:
                        distance = first_index - target_index
                        start_y, end_y = gesture_top, gesture_bottom
                    elif target_index > last_index:
                        distance = target_index - last_index
                        start_y, end_y = gesture_bottom, gesture_top
                    else:
                        return None
                    for _ in range(min(max(1, distance), 8)):
                        self.adb.swipe(
                            gesture_x,
                            start_y,
                            gesture_x,
                            end_y,
                            500,
                        )
                        time.sleep(0.12)
                    self._event(
                        "INFO",
                        "calendar.scroll_months",
                        f"向目标月份滚动 {min(max(1, distance), 8)} 次",
                        metric=profile.metric,
                        local_date=checkpoint.isoformat(),
                        page="calendar",
                        action="swipe_calendar",
                        status="complete",
                        details={
                            "round": round_index,
                            "distance_months": distance,
                            "visible_first": f"{months[0].year:04d}-{months[0].month:02d}",
                            "visible_last": f"{months[-1].year:04d}-{months[-1].month:02d}",
                        },
                    )
                    time.sleep(0.6)
                    calendar_tree, _ = self.dump_tree(
                        temp_root, f"calendar-nav-{round_index:02d}"
                    )
            except Exception as exc:
                self._event(
                    "WARNING",
                    "calendar.exception",
                    f"日历直达异常：{exc}",
                    metric=profile.metric,
                    local_date=checkpoint.isoformat(),
                    page="calendar" if calendar_open else profile.metric,
                    action="calendar_resume",
                    status="failed",
                    details={"exception_type": type(exc).__name__},
                )
            finally:
                if calendar_open:
                    with tempfile.TemporaryDirectory(prefix="ohealth-calendar-close-") as close_temp:
                        try:
                            final_tree, _ = self.dump_tree(Path(close_temp), "state")
                            if final_tree.by_id("recycler_view") is not None:
                                self.adb.keyevent("BACK")
                                time.sleep(0.4)
                                self._event(
                                    "INFO",
                                    "calendar.closed",
                                    "日历直达未完成，已安全关闭日历",
                                    metric=profile.metric,
                                    local_date=checkpoint.isoformat(),
                                    page=profile.metric,
                                    action="back",
                                    status="complete",
                                )
                        except Exception:
                            pass
        return None

    @staticmethod
    def _previous_tap_point(
        tree: UiTree,
        profile: MetricUiProfile,
    ) -> tuple[int, int] | None:
        previous = tree.by_id(*profile.previous_ids)
        date_node = tree.date_node(profile)
        date_bounds = (
            parse_bounds(date_node.attrib.get("bounds", ""))
            if date_node is not None
            else None
        )
        if previous is None and date_bounds:
            candidates = []
            for node in tree.nodes:
                bounds = parse_bounds(node.attrib.get("bounds", ""))
                if (
                    node.attrib.get("clickable") == "true"
                    and bounds
                    and bounds[2] <= date_bounds[0]
                ):
                    candidates.append((bounds[2], node))
            previous = max(candidates, default=(0, None), key=lambda item: item[0])[1]
        bounds = (
            parse_bounds(previous.attrib.get("bounds", ""))
            if previous is not None
            else None
        )
        if bounds:
            return center(bounds)
        if date_bounds and profile.fallback_previous_x_ratio is not None:
            screen_width = max(
                (
                    node_bounds[2]
                    for node in tree.nodes
                    if (node_bounds := parse_bounds(node.attrib.get("bounds", "")))
                ),
                default=0,
            )
            if screen_width > 0:
                return canvas_previous_point(
                    date_bounds,
                    screen_width,
                    profile.fallback_previous_x_ratio,
                )
        return None

    def _select_day_tab(self, tree: UiTree) -> None:
        node = tree.by_text("日")
        if node is not None and node.attrib.get("clickable") == "true":
            bounds = parse_bounds(node.attrib.get("bounds", ""))
            if bounds:
                self.adb.tap(*center(bounds))
                time.sleep(0.5)

    def dismiss_known_popups(self, max_attempts: int = 4) -> list[str]:
        dismissed: list[str] = []
        with tempfile.TemporaryDirectory(prefix="ohealth-popup-") as temp:
            for attempt in range(max_attempts):
                tree, _ = self.dump_tree(Path(temp), f"popup-{attempt}")
                labels = {node_label(node) for node in tree.nodes if node_label(node)}
                target: ET.Element | None = None
                reason = ""
                if "风险提示" in labels:
                    target = tree.by_text("好的")
                    reason = "root_risk_notice"
                elif "柱状图切换为曲线图移到这里了，点击看看" in labels:
                    target = tree.by_id("dismissIv")
                    reason = "chart_guide"
                if target is None:
                    break
                clickable = tree.clickable_ancestor(target) or target
                bounds = parse_bounds(clickable.attrib.get("bounds", ""))
                if not bounds:
                    break
                self.adb.tap(*center(bounds))
                dismissed.append(reason)
                time.sleep(0.6)
        return dismissed

    def _ensure_home(self, max_backs: int = 10) -> None:
        main_suffix = "/com.heytap.health.main.MainActivity"
        foreground = self.adb.foreground()
        if foreground.endswith(main_suffix):
            return
        for _ in range(max_backs):
            if not foreground.startswith(PACKAGE + "/"):
                break
            self.adb.keyevent("BACK")
            time.sleep(0.45)
            foreground = self.adb.foreground()
            if foreground.endswith(main_suffix):
                return
        self.adb.start_activity(MAIN_ACTIVITY)
        time.sleep(1.2)
        foreground = self.adb.foreground()
        if not foreground.endswith(main_suffix):
            raise ExporterError(f"无法复位到 OPPO 健康首页，当前前台：{foreground}")

    def _scroll_to_top(
        self, max_swipes: int = 12
    ) -> tuple[UiTree, bytes] | None:
        self.adb.shell("cmd", "statusbar", "collapse", check=False)
        with tempfile.TemporaryDirectory(prefix="ohealth-scroll-top-") as temp:
            previous: str | None = None
            latest: tuple[UiTree, bytes] | None = None
            for index in range(max_swipes):
                tree, xml_path = self.dump_tree(Path(temp), f"top-{index}")
                latest = (tree, xml_path.read_bytes())
                payload = tree.visible_payload()
                fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                if fingerprint == previous:
                    return latest
                previous = fingerprint
                scrollable = tree.scrollable()
                if scrollable is None or not self._swipe_scrollable(scrollable, down=True):
                    return latest
                time.sleep(0.25)
            return latest

    def _swipe_scrollable(self, node: ET.Element, *, down: bool) -> bool:
        bounds = parse_bounds(node.attrib.get("bounds", ""))
        if not bounds:
            return False
        x1, y1, x2, y2 = bounds
        x = (x1 + x2) // 2
        height = y2 - y1
        long_top = y1 + max(40, int(height * 0.20))
        long_bottom = y1 + min(height - 40, int(height * 0.85))
        if long_bottom <= long_top:
            return False
        if down:
            # Returning to the top is asymmetric on OPPO chart pages: a
            # gesture beginning in the chart is consumed by its ViewPager.
            # Start and finish in the lower detail-card region; the caller's
            # fingerprint loop repeats this shorter move until stable.
            if local_id(node) in {"content_scroll_view", "coordinator_layout"}:
                card_start = y1 + min(height - 80, int(height * 0.72))
                card_end = y1 + min(height - 40, int(height * 0.94))
                if card_end <= card_start:
                    return False
                self.adb.swipe(x, card_start, x, card_end, 500)
            else:
                # Ordinary lists/home feeds have no nested chart and can use
                # the full reverse gesture to reach the top quickly.
                self.adb.swipe(x, long_top, x, long_bottom, 700)
        else:
            # Moving toward lower content works best as a long gesture whose
            # starting point is below the embedded chart.
            if local_id(node) == "coordinator_layout":
                feed_start = y1 + min(height - 40, int(height * 0.65))
                feed_end = y1 + max(40, int(height * 0.35))
                if feed_start <= feed_end:
                    return False
                self.adb.swipe(x, feed_start, x, feed_end, 700)
            else:
                self.adb.swipe(x, long_bottom, x, long_top, 700)
        return True

    def _capture_chart_crops(
        self,
        tree: UiTree,
        screenshot_path: Path,
        directory: Path,
        viewport_index: int,
    ) -> list[tuple[str, Artifact, tuple[int, int, int, int]]]:
        result = []
        with Image.open(screenshot_path) as image:
            for chart_index, node in enumerate(tree.nodes):
                resource_name = local_id(node)
                if not resource_name or not re.search(r"chart|graph|timeline", resource_name, re.I):
                    continue
                bounds = parse_bounds(node.attrib.get("bounds", ""))
                if not bounds:
                    continue
                x1, y1, x2, y2 = bounds
                if x2 <= x1 or y2 <= y1:
                    continue
                crop_path = directory / f"chart-{viewport_index:02d}-{chart_index:02d}.png"
                image.crop((x1, y1, x2, y2)).save(crop_path, format="PNG")
                result.append(
                    (
                        resource_name,
                        self.artifacts.put_file(crop_path, media_type="image/png"),
                        bounds,
                    )
                )
        return result

    @staticmethod
    def _build_mosaic(evidences: list[ViewportEvidence], destination: Path) -> None:
        if not evidences:
            raise ExporterError("页面没有生成任何视口证据。")
        images = [Image.open(item.screenshot.path).convert("RGB") for item in evidences]
        try:
            width = max(image.width for image in images)
            total_height = sum(image.height for image in images)
            mosaic = Image.new("RGB", (width, total_height), "black")
            y = 0
            for image in images:
                mosaic.paste(image, (0, y))
                y += image.height
            mosaic.save(destination, format="PNG", optimize=True)
        finally:
            for image in images:
                image.close()

    def _has_source_day(self, metric: str, local_date: str) -> bool:
        row = self.db.connection.execute(
            """
            SELECT 1 FROM daily_metrics_current
            WHERE source_id=? AND canonical_metric=? AND local_date=?
            """,
            (self.source_id, metric, local_date),
        ).fetchone()
        return row is not None
