"""Render a template's content into HTML with clickable placeholder spans."""

from __future__ import annotations

import html as html_mod
import json
from typing import Any
from xml.etree import ElementTree as ET


def _escape_jsonptr(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _span(idx: int, placeholder: dict[str, Any], text: str, *, quoted: bool = False) -> str:
    mode = placeholder.get("mode", "literal")
    safe = html_mod.escape(text)
    inner = f'<span class="placeholder {mode}" data-idx="{idx}" title="{html_mod.escape(placeholder.get("location", ""))}">{safe}</span>'
    if quoted:
        return f'"{inner}"'
    return inner


def _render_json(
    node: Any,
    path: str,
    ph_by_location: dict[str, int],
    placeholders: list[dict[str, Any]],
    buf: list[str],
    indent: int,
) -> None:
    sp = "  " * indent
    if isinstance(node, dict):
        if not node:
            buf.append("{}")
            return
        buf.append("{\n")
        items = list(node.items())
        for i, (k, v) in enumerate(items):
            buf.append(sp + "  ")
            buf.append(html_mod.escape(json.dumps(k, ensure_ascii=False)))
            buf.append(": ")
            child_path = f"{path}/{_escape_jsonptr(k)}"
            _render_json(v, child_path, ph_by_location, placeholders, buf, indent + 1)
            if i < len(items) - 1:
                buf.append(",")
            buf.append("\n")
        buf.append(sp + "}")
        return
    if isinstance(node, list):
        if not node:
            buf.append("[]")
            return
        buf.append("[\n")
        for i, v in enumerate(node):
            child_path = f"{path}/{i}"
            buf.append(sp + "  ")
            _render_json(v, child_path, ph_by_location, placeholders, buf, indent + 1)
            if i < len(node) - 1:
                buf.append(",")
            buf.append("\n")
        buf.append(sp + "]")
        return
    if isinstance(node, bool):
        buf.append("true" if node else "false")
        return
    if node is None:
        buf.append("null")
        return
    if isinstance(node, (int, float)):
        idx = ph_by_location.get(path)
        if idx is not None:
            buf.append(_span(idx, placeholders[idx], str(node)))
        else:
            buf.append(html_mod.escape(json.dumps(node)))
        return
    # string
    idx = ph_by_location.get(path)
    if idx is not None:
        buf.append(_span(idx, placeholders[idx], str(node), quoted=True))
    else:
        buf.append(html_mod.escape(json.dumps(str(node), ensure_ascii=False)))


def _render_xml(
    elem: ET.Element,
    path: str,
    ph_by_location: dict[str, int],
    placeholders: list[dict[str, Any]],
    buf: list[str],
    indent: int,
) -> None:
    sp = "  " * indent
    buf.append(f"{sp}&lt;{html_mod.escape(elem.tag)}")
    # attributes
    for attr, value in elem.attrib.items():
        attr_path = f"{path}/@{attr}"
        idx = ph_by_location.get(attr_path)
        rendered = (
            _span(idx, placeholders[idx], value)
            if idx is not None
            else html_mod.escape(value)
        )
        buf.append(f' {html_mod.escape(attr)}="{rendered}"')
    text = (elem.text or "").strip()
    children = list(elem)
    if not text and not children:
        buf.append("/&gt;\n")
        return
    buf.append("&gt;")
    if text:
        text_path = f"{path}/#text"
        idx = ph_by_location.get(text_path)
        if idx is not None:
            buf.append(_span(idx, placeholders[idx], text))
        else:
            buf.append(html_mod.escape(text))
    if children:
        buf.append("\n")
        counts: dict[str, int] = {}
        for child in children:
            tag = child.tag
            ci = counts.get(tag, 0)
            counts[tag] = ci + 1
            child_path = f"{path}/{tag}[{ci}]"
            _render_xml(child, child_path, ph_by_location, placeholders, buf, indent + 1)
        buf.append(sp)
    buf.append(f"&lt;/{html_mod.escape(elem.tag)}&gt;\n")


def render_template_html(template) -> str:
    placeholders = list(template.placeholders or [])
    ph_by_location = {p["location"]: i for i, p in enumerate(placeholders) if p.get("location")}
    if template.format == "json":
        try:
            data = json.loads(template.content)
        except json.JSONDecodeError:
            return html_mod.escape(template.content)
        buf: list[str] = []
        _render_json(data, "", ph_by_location, placeholders, buf, indent=0)
        return "".join(buf)
    if template.format == "xml":
        try:
            root = ET.fromstring(template.content)
        except ET.ParseError:
            return html_mod.escape(template.content)
        buf = []
        _render_xml(root, f"/{root.tag}", ph_by_location, placeholders, buf, indent=0)
        return "".join(buf)
    return html_mod.escape(template.content)
