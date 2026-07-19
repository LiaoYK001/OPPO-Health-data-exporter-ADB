from __future__ import annotations

import datetime as dt
import hashlib
import json
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
        return max(
            candidates,
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

    def date_label(self, profile: MetricUiProfile | None = None) -> str | None:
        if profile:
            node = self.by_id(*profile.date_ids)
            if node and node_label(node):
                return node_label(node)
        for node in self.nodes:
            label = node_label(node)
            if DATE_PATTERN.search(label):
                return label
        return None


@dataclass(frozen=True)
class ViewportEvidence:
    index: int
    screenshot: Artifact
    xml: Artifact
    payload: list[dict[str, Any]]
    scroll_bounds: tuple[int, int, int, int] | None
    chart_artifacts: tuple[tuple[str, Artifact, tuple[int, int, int, int]], ...]


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
        self.adb.start_activity(MAIN_ACTIVITY)
        time.sleep(1.5)
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
        self.adb.start_activity(MAIN_ACTIVITY)
        time.sleep(1.2)
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
                        self.adb.tap(*center(bounds))
                        time.sleep(1.5)
                        detail, _ = self.dump_tree(Path(temp), "detail")
                        if any(detail.by_text(title) is not None for title in profile.page_titles):
                            self._select_day_tab(detail)
                            return detail
                        raise ExporterError(f"点击 {metric} 后未进入预期详情页。")
                scrollable = tree.scrollable()
                if scrollable is None or not self._swipe_scrollable(scrollable, down=False):
                    break
                time.sleep(0.6)
        raise ExporterError(f"健康首页未找到指标卡：{metric}")

    def capture_history(
        self,
        metric: str,
        *,
        earliest_date: dt.date,
        max_days: int | None = None,
        resume: bool = True,
        capture_subpages: bool = True,
    ) -> dict[str, Any]:
        profile = UI_PROFILES[metric]
        tree = self.enter_metric(metric)
        label = tree.date_label(profile)
        if not label:
            raise ExporterError(f"{metric} 页面没有可识别日期。")
        current_date = date_from_label(label, dt.date.today())
        if current_date is None:
            raise ExporterError(f"无法解析 {metric} 页面日期：{label}")
        captured = skipped = failures = 0
        visited = 0
        while current_date >= earliest_date:
            if max_days is not None and visited >= max_days:
                break
            visited += 1
            day = current_date.isoformat()
            checkpoint = self.db.get_checkpoint(self.source_id, metric) if resume else None
            already_done = (
                checkpoint is not None
                and checkpoint["last_date"] is not None
                and day >= str(checkpoint["last_date"])
            )
            try:
                if already_done:
                    skipped += 1
                else:
                    self.capture_page(metric, day, metric)
                    if capture_subpages:
                        self._capture_subpages(metric, day, profile)
                    self.db.set_coverage(
                        self.source_id,
                        metric,
                        day,
                        "exact_ui",
                        {"database": self._has_source_day(metric, day), "ui": True},
                        self.run_id,
                    )
                    self.db.set_checkpoint(
                        self.source_id,
                        metric,
                        day,
                        {"page_date": day, "status": "captured"},
                    )
                    self.db.connection.commit()
                    captured += 1
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
            if current_date == earliest_date:
                break
            if not self._go_previous_day(profile, current_date):
                self.db.record_failure(
                    self.run_id,
                    "date_navigation",
                    "日期没有变化",
                    metric=metric,
                    local_date=day,
                )
                failures += 1
                break
            current_date -= dt.timedelta(days=1)
        return {
            "metric": metric,
            "captured": captured,
            "skipped": skipped,
            "failures": failures,
            "visited": visited,
        }

    def capture_page(
        self,
        metric: str,
        local_date: str,
        page_key: str,
        max_scrolls: int = 12,
    ) -> int:
        self._scroll_to_top(max_swipes=8)
        evidences: list[ViewportEvidence] = []
        seen: set[str] = set()
        with tempfile.TemporaryDirectory(prefix=f"ohealth-page-{metric}-") as temp:
            temp_root = Path(temp)
            for index in range(max_scrolls):
                tree, xml_path = self.dump_tree(temp_root, f"viewport-{index:02d}")
                screenshot_path = self.screenshot(temp_root, f"viewport-{index:02d}")
                payload = tree.visible_payload()
                fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                if fingerprint in seen:
                    break
                seen.add(fingerprint)
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
                if scrollable is None or not self._swipe_scrollable(scrollable, down=False):
                    break
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
        return capture_id

    def _capture_subpages(
        self,
        metric: str,
        local_date: str,
        profile: MetricUiProfile,
    ) -> None:
        for title in profile.subpage_titles:
            if not self._open_text_target(title):
                continue
            time.sleep(0.9)
            try:
                self.capture_page(metric, local_date, f"{metric}/{title}")
            finally:
                self.adb.keyevent("BACK")
                time.sleep(0.8)

    def _open_text_target(self, title: str, max_scrolls: int = 10) -> bool:
        self._scroll_to_top(max_swipes=8)
        with tempfile.TemporaryDirectory(prefix="ohealth-find-subpage-") as temp:
            for index in range(max_scrolls):
                tree, _ = self.dump_tree(Path(temp), f"find-{index:02d}")
                node = tree.by_text(title)
                target = tree.clickable_ancestor(node) if node is not None else None
                if target is not None:
                    bounds = parse_bounds(target.attrib.get("bounds", ""))
                    if bounds:
                        self.adb.tap(*center(bounds))
                        return True
                scrollable = tree.scrollable()
                if scrollable is None or not self._swipe_scrollable(scrollable, down=False):
                    return False
                time.sleep(0.5)
        return False

    def _go_previous_day(self, profile: MetricUiProfile, current: dt.date) -> bool:
        self._scroll_to_top(max_swipes=8)
        with tempfile.TemporaryDirectory(prefix="ohealth-date-nav-") as temp:
            tree, _ = self.dump_tree(Path(temp), "before")
            previous = tree.by_id(*profile.previous_ids)
            if previous is None:
                date_node = tree.by_id(*profile.date_ids)
                date_bounds = parse_bounds(date_node.attrib.get("bounds", "")) if date_node is not None else None
                if date_bounds:
                    candidates = []
                    for node in tree.nodes:
                        bounds = parse_bounds(node.attrib.get("bounds", ""))
                        if node.attrib.get("clickable") == "true" and bounds and bounds[2] <= date_bounds[0]:
                            candidates.append((bounds[2], node))
                    previous = max(candidates, default=(0, None), key=lambda item: item[0])[1]
            bounds = parse_bounds(previous.attrib.get("bounds", "")) if previous is not None else None
            if not bounds:
                return False
            self.adb.tap(*center(bounds))
            deadline = time.time() + 10
            while time.time() < deadline:
                time.sleep(0.5)
                updated, _ = self.dump_tree(Path(temp), "after")
                label = updated.date_label(profile)
                parsed = date_from_label(label or "", current - dt.timedelta(days=1))
                if parsed and parsed != current:
                    return True
        return False

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

    def _scroll_to_top(self, max_swipes: int = 6) -> None:
        self.adb.shell("cmd", "statusbar", "collapse", check=False)
        with tempfile.TemporaryDirectory(prefix="ohealth-scroll-top-") as temp:
            previous: str | None = None
            for index in range(max_swipes):
                tree, _ = self.dump_tree(Path(temp), f"top-{index}")
                payload = tree.visible_payload()
                fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                if fingerprint == previous:
                    break
                previous = fingerprint
                scrollable = tree.scrollable()
                if scrollable is None or not self._swipe_scrollable(scrollable, down=True):
                    break
                time.sleep(0.25)

    def _swipe_scrollable(self, node: ET.Element, *, down: bool) -> bool:
        bounds = parse_bounds(node.attrib.get("bounds", ""))
        if not bounds:
            return False
        x1, y1, x2, y2 = bounds
        x = (x1 + x2) // 2
        top = y1 + max(40, (y2 - y1) // 8)
        bottom = y2 - max(40, (y2 - y1) // 8)
        if bottom <= top:
            return False
        if down:
            self.adb.swipe(x, top, x, bottom, 350)
        else:
            self.adb.swipe(x, bottom, x, top, 350)
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
