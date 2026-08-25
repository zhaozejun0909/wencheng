from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass(frozen=True)
class UiNode:
    text: str
    content_desc: str
    resource_id: str
    class_name: str
    bounds: tuple[int, int, int, int] | None


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _bounds(value: str) -> tuple[int, int, int, int] | None:
    match = _BOUNDS_RE.fullmatch(value or "")
    return tuple(int(item) for item in match.groups()) if match else None


def parse_ui_xml(xml: str) -> list[UiNode]:
    root = ET.fromstring(xml)
    nodes: list[UiNode] = []
    for element in root.iter():
        nodes.append(
            UiNode(
                text=(element.attrib.get("text") or "").strip(),
                content_desc=(element.attrib.get("content-desc") or "").strip(),
                resource_id=(element.attrib.get("resource-id") or "").strip(),
                class_name=(element.attrib.get("class") or "").strip(),
                bounds=_bounds(element.attrib.get("bounds", "")),
            )
        )
    return nodes


def extract_texts(xml: str) -> list[str]:
    values: list[str] = []
    for node in parse_ui_xml(xml):
        for value in (node.text, node.content_desc):
            if value and value not in values:
                values.append(value)
    return values


def find_text_bounds(xml: str, target: str) -> tuple[int, int, int, int] | None:
    target = target.strip()
    for node in parse_ui_xml(xml):
        if (
            target == node.text
            or target == node.content_desc
            or target in node.text
            or target in node.content_desc
        ):
            return node.bounds
    return None


def find_exact_text_bounds(xml: str, target: str) -> tuple[int, int, int, int] | None:
    """Find a node whose visible label is exactly ``target``.

    Search entry labels are often nested in other labels (for example,
    ``搜索本店商品``).  The workflow uses this stricter lookup for the global
    search button so a shop-local search field cannot be mistaken for it.
    """
    target = target.strip()
    for node in parse_ui_xml(xml):
        if target in {node.text, node.content_desc}:
            return node.bounds
    return None
