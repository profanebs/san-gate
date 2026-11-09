import copy
import re
import zipfile
from collections import Counter
from pathlib import Path

import pymupdf
from lxml import etree
from lxml import html as lxml_html

from .model import LayerItem

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP_NS = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
NS = {"w": W_NS[1:-1], "wp": WP_NS[1:-1]}


def _local(tag):
    return tag.split("}")[-1]


def _rgb_from_int(value):
    return (
        ((value >> 16) & 255) / 255.0,
        ((value >> 8) & 255) / 255.0,
        (value & 255) / 255.0,
    )


def _luminance(rgb):
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def _page_bg(page):
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(0.15, 0.15), alpha=False)
        width, height = pix.width, pix.height
        points = [
            (0, 0),
            (max(0, width - 1), 0),
            (0, max(0, height - 1)),
            (max(0, width - 1), max(0, height - 1)),
            (width // 2, height // 2),
        ]
        colors = []
        for x, y in points:
            pixel = pix.pixel(x, y)
            if isinstance(pixel, tuple):
                colors.append(tuple(round(c / 255.0, 2) for c in pixel[:3]))
        if colors:
            return Counter(colors).most_common(1)[0][0]
    except Exception:
        pass
    return (1.0, 1.0, 1.0)


def _inside_page(bbox, rect, margin=2.0):
    x0, y0, x1, y1 = bbox
    return (
        x0 >= rect.x0 - margin
        and y0 >= rect.y0 - margin
        and x1 <= rect.x1 + margin
        and y1 <= rect.y1 + margin
    )


def _pdf_layers(path):
    doc = pymupdf.open(path)
    visible = []
    items = []
    for page_no, page in enumerate(doc, 1):
        visible_rect = page.cropbox
        background = _page_bg(page)
        page.set_cropbox(page.mediabox)
        data = page.get_text("dict")
        invisible_rects = []
        try:
            for trace in page.get_texttrace():
                if trace.get("type") == 3 and trace.get("bbox"):
                    invisible_rects.append(pymupdf.Rect(trace["bbox"]))
        except Exception:
            invisible_rects = []
        hidden_texts = []
        hidden_reasons = set()
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    color = _rgb_from_int(span.get("color", 0))
                    size = float(span.get("size", 0) or 0)
                    bbox = span.get("bbox")
                    reasons = []
                    if _contrast(color, background) < 1.5:
                        reasons.append("low contrast")
                    if 0 < size < 2.0:
                        reasons.append("tiny font")
                    if bbox and not _inside_page(bbox, visible_rect):
                        reasons.append("outside page")
                    if bbox and any(
                        pymupdf.Rect(bbox).intersects(rect)
                        for rect in invisible_rects
                    ):
                        reasons.append("invisible render mode")
                    if reasons:
                        hidden_texts.append(text)
                        hidden_reasons.update(reasons)
                    else:
                        visible.append(text)

        if hidden_texts:
            items.append(
                LayerItem(
                    "hidden_text",
                    " ".join(hidden_texts),
                    "pdf:p%d" % page_no,
                    ", ".join(sorted(hidden_reasons)),
                )
            )

    for key, value in (doc.metadata or {}).items():
        if value and str(value).strip():
            items.append(
                LayerItem(
                    "metadata",
                    str(value),
                    "pdf:metadata:%s" % key,
                    "document metadata",
                )
            )

    for page_no, page in enumerate(doc, 1):
        try:
            annots = page.annots()
        except Exception:
            annots = None
        if not annots:
            continue
        for annot in annots:
            info = annot.info or {}
            parts = [
                str(info.get(key, ""))
                for key in ("title", "subject", "content")
                if info.get(key)
            ]
            text = " ".join(parts).strip()
            if text:
                items.append(
                    LayerItem(
                        "annotation",
                        text,
                        "pdf:p%d:annotation" % page_no,
                        "annotation",
                    )
                )

    doc.close()
    return "\n".join(visible), items


def _near_white(value):
    if not value or len(value) != 6:
        return False
    try:
        rgb = tuple(int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return False
    # Light gray (#D9D9D9..#F0F0F0) is low-contrast on white and is used by
    # real canary documents; darker grays remain visible.
    return _luminance(rgb) > 0.55


def _has_ancestor(element, local_name):
    parent = element.getparent()
    while parent is not None:
        if _local(parent.tag) == local_name:
            return True
        parent = parent.getparent()
    return False


def _docx_run_text(run):
    parts = []
    for child in run:
        name = _local(child.tag)
        if name in ("t", "delText"):
            parts.append(child.text or "")
        elif name in ("br", "tab"):
            parts.append(" ")
    return "".join(parts)


def _docx_text(element):
    return "".join(
        node.text or ""
        for node in element.iter()
        if _local(node.tag) in ("t", "delText")
    )


def _docx_rpr_props(rpr):
    props = {}
    if rpr is None:
        return props
    for child in rpr:
        name = _local(child.tag)
        if name == "vanish":
            value = (child.get(W_NS + "val") or "").strip().lower()
            props["vanish"] = value not in ("0", "false", "off")
        elif name == "color":
            props["color"] = child.get(W_NS + "val")
        elif name == "sz":
            props["sz"] = child.get(W_NS + "val")
        elif name == "position":
            props["position"] = child.get(W_NS + "val")
        elif name == "rStyle":
            props["rStyle"] = child.get(W_NS + "val")
    return props


def _docx_paragraph_style_id(run):
    parent = run.getparent()
    while parent is not None and _local(parent.tag) != "p":
        parent = parent.getparent()
    if parent is None:
        return None
    ppr = next((child for child in parent if _local(child.tag) == "pPr"), None)
    if ppr is None:
        return None
    pstyle = next((child for child in ppr if _local(child.tag) == "pStyle"), None)
    return pstyle.get(W_NS + "val") if pstyle is not None else None


def _docx_styles(z):
    styles = {}
    if "word/styles.xml" not in z.namelist():
        return styles
    try:
        root = etree.fromstring(z.read("word/styles.xml"))
    except Exception:
        return styles
    for style in root.iter(W_NS + "style"):
        style_id = style.get(W_NS + "styleId")
        if not style_id:
            continue
        based_on = None
        rpr = None
        for child in style:
            name = _local(child.tag)
            if name == "basedOn":
                based_on = child.get(W_NS + "val")
            elif name == "rPr":
                rpr = child
        styles[style_id] = {"basedOn": based_on, "rPr": rpr}
    return styles


def _docx_style_props(style_id, styles, seen=None):
    if not style_id or style_id not in styles:
        return {}
    if seen is None:
        seen = set()
    if style_id in seen:
        return {}
    seen.add(style_id)
    props = {}
    based_on = styles[style_id].get("basedOn")
    if based_on:
        props.update(_docx_style_props(based_on, styles, seen))
    props.update(_docx_rpr_props(styles[style_id].get("rPr")))
    return props


def _docx_run_props(run, styles):
    props = {}
    paragraph_style = _docx_paragraph_style_id(run)
    if paragraph_style:
        props.update(_docx_style_props(paragraph_style, styles))
    rpr = next((child for child in run if _local(child.tag) == "rPr"), None)
    direct = _docx_rpr_props(rpr)
    run_style = direct.pop("rStyle", None)
    if run_style:
        props.update(_docx_style_props(run_style, styles))
    props.update(direct)
    return props


def _docx_props_reasons(props):
    reasons = []
    if props.get("vanish"):
        reasons.append("hidden run")
    if _near_white(props.get("color")):
        reasons.append("white text")
    try:
        if int(props.get("sz")) < 8:
            reasons.append("tiny font")
    except (TypeError, ValueError):
        pass
    try:
        if int(props.get("position")) < -20:
            reasons.append("offset text")
    except (TypeError, ValueError):
        pass
    return reasons


def _docx_run_reasons(run, styles):
    return _docx_props_reasons(_docx_run_props(run, styles))


def _docx_layers(path):
    visible = []
    items = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        document = etree.fromstring(z.read("word/document.xml"))
        styles = _docx_styles(z)

        for run in document.iter(W_NS + "r"):
            if (
                _has_ancestor(run, "txbxContent")
                or _has_ancestor(run, "ins")
                or _has_ancestor(run, "del")
            ):
                continue
            text = _docx_run_text(run)
            if not text.strip():
                continue
            reasons = _docx_run_reasons(run, styles)
            if reasons:
                items.append(
                    LayerItem("hidden_text", text, "docx:document", ", ".join(reasons))
                )
            else:
                visible.append(text)

        for box in document.xpath(".//w:txbxContent", namespaces=NS):
            text = _docx_text(box)
            if text.strip():
                items.append(
                    LayerItem("text_box", text, "docx:textbox", "text box")
                )

        for change in document.xpath(".//w:ins | .//w:del", namespaces=NS):
            text = _docx_text(change)
            if text.strip():
                items.append(
                    LayerItem(
                        "tracked_change",
                        text,
                        "docx:tracked_change",
                        "tracked change",
                    )
                )

        for name in names:
            if name.startswith("word/header") or name.startswith("word/footer"):
                root = etree.fromstring(z.read(name))
                text = _docx_text(root)
                if text.strip():
                    items.append(
                        LayerItem(
                            "header_footer",
                            text,
                            "docx:%s" % name,
                            "header or footer",
                        )
                    )

        if "word/comments.xml" in names:
            root = etree.fromstring(z.read("word/comments.xml"))
            for comment in root.iter(W_NS + "comment"):
                text = _docx_text(comment)
                if text.strip():
                    items.append(
                        LayerItem("comment", text, "docx:comments", "comment")
                    )

        for name, tag in (
            ("word/footnotes.xml", "footnote"),
            ("word/endnotes.xml", "endnote"),
        ):
            if name in names:
                root = etree.fromstring(z.read(name))
                for note in root.iter(W_NS + tag):
                    text = _docx_text(note)
                    if text.strip():
                        items.append(
                            LayerItem(
                                "footnote",
                                text,
                                "docx:%s" % name,
                                "footnote or endnote",
                            )
                        )

        for name in ("docProps/core.xml", "docProps/app.xml"):
            if name in names:
                root = etree.fromstring(z.read(name))
                text = " ".join(
                    t.text.strip()
                    for t in root.iter()
                    if t.text and t.text.strip()
                )
                if text:
                    items.append(
                        LayerItem("metadata", text, "docx:%s" % name, "document metadata")
                    )

        for element in document.iter(WP_NS + "docPr"):
            for attr in ("descr", "title"):
                value = element.get(attr)
                if value and value.strip():
                    items.append(
                        LayerItem(
                            "alt_text",
                            value.strip(),
                            "docx:alt_text:%s" % attr,
                            "alternative text",
                        )
                    )

    return "\n".join(visible), items


def _parse_style(value):
    result = {}
    for part in (value or "").split(";"):
        if ":" in part:
            key, val = part.split(":", 1)
            val = val.strip().lower()
            val = re.sub(r"\s*!\s*important\s*$", "", val).strip()
            result[key.strip().lower()] = val
    return result


def _parse_css(text):
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text or ""):
        selector_text = match.group(1).strip()
        declarations = _parse_style(match.group(2))
        for selector in selector_text.split(","):
            selector = selector.strip()
            if selector:
                rules.append((selector, declarations))
    return rules


def _selector_matches(element, selector):
    selector = selector.strip().lower()
    if selector in ("*", ""):
        return True
    if selector.startswith("."):
        classes = (element.get("class") or "").lower().split()
        return selector[1:] in classes
    if selector.startswith("#"):
        return (element.get("id") or "").lower() == selector[1:]
    if any(ch in selector for ch in (" ", ">", "+", "~", ":", "[")):
        return False
    return element.tag.lower() == selector


def _html_element_hidden(element, css_rules):
    if not isinstance(element.tag, str):
        return None
    if element.tag.lower() == "template":
        return "template"
    if element.get("hidden") is not None:
        return "hidden attribute"
    if (element.get("aria-hidden") or "").lower() == "true":
        return "aria-hidden"
    style = {}
    for selector, declarations in css_rules:
        if _selector_matches(element, selector):
            style.update(declarations)
    style.update(_parse_style(element.get("style", "")))
    if style.get("display") == "none":
        return "display:none"
    if style.get("visibility") in ("hidden", "collapse"):
        return "visibility:hidden"
    if style.get("content-visibility") == "hidden":
        return "content-visibility:hidden"
    opacity = style.get("opacity", "").strip().lower()
    if opacity in ("0", "0.0", "0.00"):
        return "opacity:0"
    try:
        if opacity.endswith("%"):
            if float(opacity[:-1]) <= 5:
                return "near-zero opacity"
        elif opacity and float(opacity) <= 0.05:
            return "near-zero opacity"
    except ValueError:
        pass
    if style.get("color") in ("transparent", "rgba(0,0,0,0)"):
        return "transparent text"
    font_size = style.get("font-size", "").strip().lower()
    if re.match(r"^0(?:\.0+)?(?:px|pt|em|rem|%)?$", font_size):
        return "zero font size"
    try:
        if font_size.endswith(("px", "pt")) and float(font_size[:-2]) <= 0.5:
            return "near-zero font size"
    except ValueError:
        pass
    clip_path = style.get("clip-path", "").replace(" ", "").lower()
    if clip_path.startswith("inset(") and clip_path.endswith(")"):
        values = re.findall(r"(-?\d+(?:\.\d+)?)(%|px)?", clip_path[6:-1])
        if values and all(unit == "%" for _, unit in values):
            numbers = [float(number) for number, _ in values]
            if len(numbers) == 1:
                top = right = bottom = left = numbers[0]
            elif len(numbers) == 2:
                top = bottom = numbers[0]
                right = left = numbers[1]
            elif len(numbers) == 3:
                top, right, bottom = numbers
                left = right
            else:
                top, right, bottom, left = numbers[:4]
            if top + bottom >= 100 or left + right >= 100:
                return "clipped text"
    if re.match(r"circle\(0(?:\.0+)?(?:%|px)?(?:at[^)]+)?\)", clip_path):
        return "clipped text"
    if re.match(
        r"ellipse\(0(?:\.0+)?(?:%|px)?(?:[,\s]*0(?:\.0+)?(?:%|px)?)?(?:at[^)]+)?\)",
        clip_path,
    ):
        return "clipped text"
    clip = style.get("clip", "").replace(" ", "").lower()
    if clip in (
        "rect(0,0,0,0)",
        "rect(1px,1px,1px,1px)",
        "rect(0px,0px,0px,0px)",
    ):
        return "clipped text"
    transform = style.get("transform", "").replace(" ", "")
    if re.search(r"scale\(\s*0(?:\.0+)?\s*(?:,\s*0(?:\.0+)?)?\s*\)", transform):
        return "zero scale"
    if (
        style.get("width") in ("0", "0px")
        and style.get("height") in ("0", "0px")
        and style.get("overflow") == "hidden"
    ):
        return "zero box"
    if style.get("position") in ("absolute", "fixed"):
        for prop in ("left", "top", "right", "bottom"):
            raw = style.get(prop, "").strip().lower()
            match = re.match(r"^(-?\d+(?:\.\d+)?)([a-z%]*)$", raw)
            if not match:
                continue
            number = float(match.group(1))
            unit = match.group(2)
            if unit in ("vw", "vh", "%") and abs(number) >= 100:
                return "off-screen position"
            if abs(number) > 5000:
                return "off-screen position"
    return None


BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "table",
    "ul",
    "ol",
    "br",
    "td",
    "th",
    "dt",
    "dd",
    "caption",
    "figcaption",
    "pre",
}


def _html_text(element):
    parts = []
    if element.text:
        parts.append(element.text)
    for child in element:
        if not isinstance(child.tag, str):
            if child.tail:
                parts.append(child.tail)
            continue
        tag = child.tag.lower()
        if tag in BLOCK_TAGS:
            parts.append(" ")
        parts.append(_html_text(child))
        if tag in BLOCK_TAGS:
            parts.append(" ")
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _remove_keep_tail(element):
    parent = element.getparent()
    if parent is None:
        return
    tail = element.tail
    if tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(element)


def _html_layers(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = lxml_html.fromstring(text)
    items = []

    for comment in tree.xpath("//comment()"):
        value = str(comment).strip()
        if value:
            items.append(LayerItem("comment", value, "html:comment", "HTML comment"))

    for meta in tree.xpath("//meta"):
        value = meta.get("content")
        if value and value.strip():
            items.append(
                LayerItem("metadata", value.strip(), "html:meta", "meta tag")
            )

    css_text = " ".join(style.text_content() for style in tree.xpath("//style"))
    css_rules = _parse_css(css_text)
    if css_text.strip():
        items.append(LayerItem("style", css_text, "html:style", "stylesheet"))
    for script in tree.xpath("//script"):
        value = script.text_content().strip()
        if value:
            items.append(LayerItem("script", value, "html:script", "script"))
    for css_comment in re.findall(r"/\*([\s\S]*?)\*/", css_text):
        value = css_comment.strip()
        if value:
            items.append(
                LayerItem("comment", value, "html:css_comment", "CSS comment")
            )
    root_reason = _html_element_hidden(tree, css_rules)
    if root_reason:
        value = tree.text_content().strip()
        if value:
            items.append(LayerItem("hidden_text", value, "html:root", root_reason))
        return "", items

    hidden_elements = []
    for element in tree.iter():
        reason = _html_element_hidden(element, css_rules)
        if reason:
            hidden_elements.append((element, reason))
    for element, reason in hidden_elements:
        value = element.text_content().strip()
        if value:
            items.append(
                LayerItem("hidden_text", value, "html:%s" % element.tag, reason)
            )

    for element, _ in hidden_elements:
        _remove_keep_tail(element)

    for tag in ("style", "script"):
        for element in tree.xpath("//" + tag):
            _remove_keep_tail(element)

    visible = _html_text(tree)
    return visible, items


def _text_layers(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    zero_width = re.findall(
        r"[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", text
    )
    tag_chars = re.findall(r"[\U000E0000-\U000E007F]", text)
    items = []
    if zero_width:
        items.append(
            LayerItem(
                "zero_width",
                text,
                "text:zero_width",
                "zero-width or bidi characters",
            )
        )
    if tag_chars:
        decoded = "".join(
            chr(ord(ch) - 0xE0000)
            for ch in tag_chars
            if 0xE0000 <= ord(ch) <= 0xE007F
        )
        items.append(
            LayerItem(
                "zero_width",
                decoded or text,
                "text:tag_chars",
                "Unicode tag characters",
            )
        )
    cleaned = re.sub(
        r"[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\U000E0000-\U000E007F]",
        "",
        text,
    )
    return cleaned, items


class ExtractionError(Exception):
    pass


def extract_layers(path):
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf_layers(path)
        if suffix == ".docx":
            return _docx_layers(path)
        if suffix in (".html", ".htm"):
            return _html_layers(path)
        return _text_layers(path)
    except Exception as exc:
        raise ExtractionError("cannot read %s: %s" % (path, exc)) from exc
