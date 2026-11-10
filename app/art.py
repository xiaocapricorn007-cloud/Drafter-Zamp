"""The picture on the card. The AI draws it as simple shapes, and code decides what may be kept.

Nothing the AI writes reaches the page until it has been rebuilt here from a short list of
allowed shapes, sizes and colours.
"""
import re
import xml.etree.ElementTree as ET

WIDTH, HEIGHT = 480, 628
MAX_CHARS = 40_000
MIN_SHAPES, MAX_SHAPES = 4, 160

COLOURS = {"#f2e9da", "#faf5ea", "#6e1423", "#4a0e19", "#b98a90", "none"}
_NUMBER = re.compile(r"^-?\d*\.?\d+$")
_PATH = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.\-]+$")
_POINTS = re.compile(r"^[0-9eE\s,.\-]+$")
_TRANSFORM = re.compile(r"^((translate|rotate|scale)\([0-9eE\s,.\-]+\)\s*)+$")
_NUMERIC = {"cx", "cy", "r", "rx", "ry", "x", "y", "width", "height", "x1", "y1", "x2", "y2", "stroke-width", "opacity", "fill-opacity", "stroke-opacity"}
_CHOICE = {"stroke-linecap": {"butt", "round", "square"}, "stroke-linejoin": {"miter", "round", "bevel"}}
SHAPES = {"g", "path", "circle", "ellipse", "rect", "line", "polygon", "polyline"}


def _allowed(name: str, value: str) -> bool:
    value = value.strip()
    if name in ("fill", "stroke"):
        return value.lower() in COLOURS
    if name in _NUMERIC:
        return bool(_NUMBER.match(value))
    if name == "d":
        return bool(_PATH.match(value))
    if name == "points":
        return bool(_POINTS.match(value))
    if name == "transform":
        return bool(_TRANSFORM.match(value))
    return value in _CHOICE.get(name, ())


def _bare(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rebuild(node) -> str:
    tag = _bare(node.tag)
    if tag not in SHAPES:
        raise ValueError(tag)
    attrs = "".join(f' {k}="{v.strip()}"' for k, v in node.attrib.items() if _allowed(k, v))
    if tag == "path" and ' d="' not in attrs:
        raise ValueError("a path with no safe outline")
    return f"<{tag}{attrs}>{''.join(_rebuild(kid) for kid in node)}</{tag}>"


def clean_svg(markup: str) -> str | None:
    """Returns a safe drawing, or None if this one can't be trusted or isn't really a picture."""
    markup = (markup or "").strip()
    if not markup or len(markup) > MAX_CHARS or "<!" in markup or "<?" in markup:
        return None
    try:
        root = ET.fromstring(markup)
        if _bare(root.tag) != "svg":
            return None
        shapes = sum(1 for node in root.iter() if _bare(node.tag) in SHAPES and _bare(node.tag) != "g")
        if not MIN_SHAPES <= shapes <= MAX_SHAPES:
            return None
        inner = "".join(_rebuild(kid) for kid in root)
    except (ET.ParseError, ValueError):
        return None
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" fill="#f2e9da">{inner}</svg>'
