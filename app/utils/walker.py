"""Walk JSON / XML documents and collect leaf string values with their paths.

This module is intentionally simple: it supports JSON via the stdlib and XML
via :mod:`xml.etree.ElementTree`. It's enough for analyzing template payloads
and substituting placeholder values without pulling in an XPath engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET


@dataclass
class Leaf:
    location: str  # JSON pointer or XML path
    value: str


# ---------- JSON ----------

def _walk_json(node: Any, prefix: str, out: list[Leaf]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}/{_escape_jsonptr(key)}"
            _walk_json(value, child, out)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            _walk_json(value, f"{prefix}/{idx}", out)
    else:
        if node is None:
            return
        if isinstance(node, bool):
            return  # booleans aren't editable as text placeholders
        out.append(Leaf(location=prefix or "/", value=str(node)))


def _escape_jsonptr(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def walk_json(content: str) -> list[Leaf]:
    data = json.loads(content)
    out: list[Leaf] = []
    _walk_json(data, "", out)
    return out


def replace_json(content: str, replacements: dict[str, str]) -> str:
    """Return JSON content with values at the given JSON-pointer paths replaced.

    Values that aren't strings in the source are stringified to keep types simple.
    """

    data = json.loads(content)
    for path, new_value in replacements.items():
        _set_jsonptr(data, path, new_value)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _set_jsonptr(data: Any, path: str, new_value: Any) -> None:
    if path in ("", "/"):
        return
    tokens = [tok.replace("~1", "/").replace("~0", "~") for tok in path.lstrip("/").split("/")]
    node = data
    for tok in tokens[:-1]:
        if isinstance(node, list):
            node = node[int(tok)]
        else:
            node = node[tok]
    last = tokens[-1]
    if isinstance(node, list):
        node[int(last)] = new_value
    else:
        node[last] = new_value


# ---------- XML ----------

def _walk_xml(elem: ET.Element, prefix: str, out: list[Leaf]) -> None:
    # element text
    text = (elem.text or "").strip()
    if text:
        out.append(Leaf(location=f"{prefix}/#text", value=text))
    # attributes
    for attr, value in elem.attrib.items():
        if value:
            out.append(Leaf(location=f"{prefix}/@{attr}", value=value))
    # children — index repeated tags
    counts: dict[str, int] = {}
    for child in list(elem):
        tag = child.tag
        idx = counts.get(tag, 0)
        counts[tag] = idx + 1
        _walk_xml(child, f"{prefix}/{tag}[{idx}]", out)
    # tail not included — usually whitespace


def walk_xml(content: str) -> list[Leaf]:
    root = ET.fromstring(content)
    out: list[Leaf] = []
    _walk_xml(root, f"/{root.tag}", out)
    return out


def replace_xml(content: str, replacements: dict[str, str]) -> str:
    root = ET.fromstring(content)
    base = f"/{root.tag}"
    for path, new_value in replacements.items():
        if not path.startswith(base):
            continue
        _set_xml(root, path[len(base):].lstrip("/"), new_value)
    return ET.tostring(root, encoding="unicode")


def _set_xml(root: ET.Element, rel_path: str, new_value: str) -> None:
    tokens = rel_path.split("/") if rel_path else []
    node = root
    for tok in tokens[:-1]:
        if not tok:
            continue
        if tok.startswith("@"):
            return  # invalid in the middle
        # tag with index: tag[i]
        if "[" in tok and tok.endswith("]"):
            tag, idx_part = tok.split("[", 1)
            idx = int(idx_part.rstrip("]"))
        else:
            tag, idx = tok, 0
        children = [c for c in list(node) if c.tag == tag]
        if idx >= len(children):
            return
        node = children[idx]
    last = tokens[-1] if tokens else ""
    if last == "#text":
        node.text = new_value
    elif last.startswith("@"):
        node.set(last[1:], new_value)
    elif "[" in last:
        tag, idx_part = last.split("[", 1)
        idx = int(idx_part.rstrip("]"))
        children = [c for c in list(node) if c.tag == tag]
        if idx < len(children):
            children[idx].text = new_value
    elif last:
        target = node.find(last)
        if target is not None:
            target.text = new_value
