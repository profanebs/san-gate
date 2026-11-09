import base64
import re
from pathlib import Path

from .layers import extract_layers
from .model import Finding, Report
from .patterns import COMPILED, _matches, classify_text, clip

AUX_KINDS = {
    "metadata",
    "comment",
    "header_footer",
    "text_box",
    "tracked_change",
    "alt_text",
    "footnote",
    "style",
    "script",
}
ALWAYS_REPORT = {"hidden_text", "zero_width", "annotation"}


B64_CANDIDATE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")


def _decoded_candidates(text):
    for match in B64_CANDIDATE.finditer(text):
        value = match.group(0)
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        try:
            raw = base64.b64decode(padded, validate=False)
            decoded = raw.decode("utf-8", errors="ignore")
        except Exception:
            continue
        if sum(ch.isalpha() for ch in decoded) >= 8:
            yield decoded


def _has_imperative(text):
    return bool(_matches(COMPILED["imperative"], text))


def _dedupe(findings):
    seen = set()
    result = []
    for finding in findings:
        key = (finding.rule, finding.location, finding.evidence)
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def ingest(path):
    target = str(Path(path))
    visible, layers = extract_layers(path)
    findings = []

    findings.extend(classify_text(visible, "visible"))
    for decoded in _decoded_candidates(visible):
        if classify_text(decoded, "visible:base64"):
            findings.append(
                Finding(
                    "R_ENCODED_INSTRUCTION",
                    "RED",
                    "canary",
                    "visible:base64",
                    clip(decoded),
                    "A base64-encoded instruction was found in visible text.",
                )
            )
            break

    for item in layers:
        encoded_hit = False
        for decoded in _decoded_candidates(item.text):
            if classify_text(decoded, item.location + ":base64"):
                findings.append(
                    Finding(
                        "R_HIDDEN_INSTRUCTION",
                        "RED",
                        "canary",
                        item.location + ":base64",
                        clip(decoded),
                        "A base64-encoded instruction was found in %s." % item.kind,
                    )
                )
                encoded_hit = True
                break
        if encoded_hit:
            continue
        semantic = classify_text(item.text, item.location)
        if semantic:
            findings.append(
                Finding(
                    "R_HIDDEN_INSTRUCTION",
                    "RED",
                    "canary",
                    item.location,
                    clip(item.text),
                    "Instruction-like content in %s (%s)." % (item.kind, item.reason),
                )
            )
        elif item.kind in ALWAYS_REPORT:
            findings.append(
                Finding(
                    "R_HIDDEN_TEXT",
                    "YELLOW",
                    "hidden",
                    item.location,
                    clip(item.text),
                    "Hidden or auxiliary text found (%s)." % item.reason,
                )
            )
        elif item.kind in AUX_KINDS and _has_imperative(item.text):
            findings.append(
                Finding(
                    "R_AUX_TEXT",
                    "YELLOW",
                    "hidden",
                    item.location,
                    clip(item.text),
                    "Auxiliary text contains imperative language (%s)." % item.reason,
                )
            )

    findings = _dedupe(findings)
    red = sum(1 for item in findings if item.severity == "RED")
    yellow = sum(1 for item in findings if item.severity == "YELLOW")
    verdict = "RED" if red else ("YELLOW" if yellow else "GREEN")
    summary = {
        "red": red,
        "yellow": yellow,
        "green": 0 if findings else 1,
        "layers": len(layers),
    }
    return Report(target, verdict, findings, layers, visible, summary)


def to_markdown(report):
    lines = []
    lines.append("# san-gate 2.0 report")
    lines.append("")
    lines.append("Target: `%s`" % report.target)
    lines.append("")
    lines.append("Verdict: **%s**" % report.verdict)
    lines.append("")
    lines.append(
        "Summary: %d red, %d yellow, %d hidden/auxiliary layer item(s)."
        % (
            report.summary.get("red", 0),
            report.summary.get("yellow", 0),
            report.summary.get("layers", 0),
        )
    )
    lines.append("")

    for severity, title in (("RED", "Red flags"), ("YELLOW", "Yellow flags")):
        rows = [item for item in report.findings if item.severity == severity]
        if not rows:
            continue
        lines.append("## %s" % title)
        lines.append("")
        for item in rows:
            lines.append(
                "- **%s** at `%s`: %s" % (item.rule, item.location, item.message)
            )
            lines.append("")
            lines.append("  > %s" % item.evidence)
            lines.append("")

    if report.layers:
        lines.append("## Hidden / auxiliary layers")
        lines.append("")
        for item in report.layers:
            lines.append(
                "- `%s` (%s): %s" % (item.location, item.reason, clip(item.text, 160))
            )
        lines.append("")

    lines.append("## Visible text preview")
    lines.append("")
    preview = " ".join(report.visible_text.split())
    lines.append(preview[:1200] if preview else "(empty)")
    lines.append("")
    return "\n".join(lines)
