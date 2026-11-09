#!/usr/bin/env python3
"""san-gate preflight - deterministic risk scan for untrusted artifacts.

Scans a file, directory, archive or URL for markers that commonly indicate
prompt injection, malware staging, or credential theft. Every verdict comes
from fixed rules in this file and config/patterns.json. No model judgement
is involved, so the same input always produces the same verdict.

Exit codes:
  0 GREEN   no finding above LOW
  1 YELLOW  at least one MEDIUM finding
  2 RED     at least one HIGH finding
  3 ERROR   could not scan

ASCII-only source on purpose. All user-facing text is English so the same
file runs unchanged on any machine and any console codepage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import unicodedata
import urllib.parse
import urllib.request
import zipfile

VERSION = "1.0.0"

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"
HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
LEVEL_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_URL_BYTES = 5 * 1024 * 1024
MAX_FILES = 2000
MAX_ARCHIVE_ENTRIES = 5000
MAX_ARCHIVE_UNCOMPRESSED = 200 * 1024 * 1024
SNIPPET_MAX = 160

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERNS_FILE = os.path.join(HERE, "data", "patterns.json")

CODE_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".psm1", ".sh", ".bash",
    ".bat", ".cmd", ".vbs", ".vbe", ".jse", ".wsf", ".hta", ".php", ".rb",
    ".pl", ".lua", ".rs", ".go", ".java", ".kt", ".cs", ".jar",
}
DOC_EXT = {".md", ".txt", ".rst", ".html", ".htm", ".xml", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
EXEC_IN_ARCHIVE = {
    ".exe": HIGH, ".dll": HIGH, ".scr": HIGH, ".com": HIGH, ".pif": HIGH,
    ".lnk": HIGH, ".bat": HIGH, ".cmd": HIGH, ".ps1": HIGH, ".vbs": HIGH,
    ".jse": HIGH, ".wsf": HIGH, ".hta": HIGH, ".msi": HIGH, ".reg": HIGH,
    ".jar": MEDIUM, ".apk": MEDIUM, ".sh": MEDIUM, ".js": MEDIUM,
}
MACRO_EXT = {".docm", ".xlsm", ".pptm", ".xlam", ".dotm", ".xltm", ".potm"}

INVISIBLE = {
    0x200B: ("zero-width space", MEDIUM),
    0x200C: ("zero-width non-joiner", MEDIUM),
    0x200D: ("zero-width joiner", MEDIUM),
    0x2060: ("word joiner", MEDIUM),
    0xFEFF: ("zero-width no-break space / BOM", MEDIUM),
    0x202A: ("bidi LRE", MEDIUM),
    0x202B: ("bidi RLE", MEDIUM),
    0x202C: ("bidi PDF", MEDIUM),
    0x202D: ("bidi LRO", MEDIUM),
    0x202E: ("bidi RLO", MEDIUM),
    0x2066: ("bidi LRI", MEDIUM),
    0x2067: ("bidi RLI", MEDIUM),
    0x2068: ("bidi FSI", MEDIUM),
    0x2069: ("bidi PDI", MEDIUM),
    0x00AD: ("soft hyphen", LOW),
    0x180E: ("mongolian vowel separator", LOW),
}
LOW_REPORT_THRESHOLD = 40

# Container formats that are never scanned as raw text.
BINARY_CONTAINER = {
    ".docx", ".xlsx", ".pptx", ".zip", ".jar", ".apk", ".pdf", ".png",
    ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".mp4", ".mov",
    ".mp3", ".wav", ".ogg", ".woff", ".woff2", ".ttf", ".otf", ".7z", ".rar",
}

# ---------------------------------------------------------------- findings


class Finding:
    __slots__ = ("level", "rule", "where", "detail", "snippet")

    def __init__(self, level, rule, where, detail, snippet=""):
        self.level = level
        self.rule = rule
        self.where = where
        self.detail = detail
        self.snippet = snippet

    def as_dict(self):
        return {
            "level": self.level,
            "rule": self.rule,
            "where": self.where,
            "detail": self.detail,
            "snippet": self.snippet,
        }


def clip(text, limit=SNIPPET_MAX):
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


# ---------------------------------------------------------------- patterns


def load_patterns():
    defaults = {
        "instruction": [],
        "action_verb": [],
    }
    if not os.path.isfile(PATTERNS_FILE):
        return defaults
    try:
        with open(PATTERNS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return defaults
    for key in defaults:
        if isinstance(data.get(key), list):
            defaults[key] = [str(p) for p in data[key] if p]
    return defaults


PATTERNS = load_patterns()
INSTRUCTION_RES = [re.compile(p, re.IGNORECASE) for p in PATTERNS["instruction"]]
ACTION_RES = [re.compile(p, re.IGNORECASE) for p in PATTERNS["action_verb"]]

EXEC_CHAIN = [
    (r"(?i)\b(curl|wget)\b[^\n|]{0,300}\|\s*(?:ba)?sh\b", "download piped to a shell"),
    (r"(?i)\b(irm|iwr|invoke-restmethod|invoke-webrequest)\b[^\n|]{0,300}\|\s*(iex|invoke-expression)\b",
     "download piped to Invoke-Expression"),
    (r"(?i)\binvoke-expression\b", "Invoke-Expression call"),
    (r"(?i)\biex\s*\(", "Invoke-Expression call"),
    (r"(?i)\.downloadstring\s*\(", "remote string downloaded then executed"),
    (r"(?i)\bnet\.webclient\b", "WebClient download primitive"),
    (r"(?i)\b(start-bitstransfer|bitsadmin|certutil[^\n]{0,80}-urlcache)\b", "lolbin download"),
    (r"(?i)\bfrombase64string\b", "base64 decode in a script"),
    (r"(?i)\beval\s*\(\s*(atob|buffer\.from|decodeURIComponent)", "obfuscated eval"),
    (r"(?i)\bnew\s+function\s*\(", "dynamic Function constructor"),
    (r"(?i)\bchild_process\b", "node child_process"),
    (r"(?i)\bos\.system\s*\(", "python os.system"),
    (r"(?i)\bsubprocess\.[a-z_]+\([^)]{0,200}shell\s*=\s*true", "subprocess with shell=True"),
    (r"(?i)\bpowershell(?:\.exe)?\b[^\n]{0,120}\s-(?:e|enc|encodedcommand)\s+[A-Za-z0-9+/]{40,}={0,2}",
     "encoded powershell payload"),
    (r"(?i)\breg\s+add\b[^\n]{0,200}\\/f\b", "silent registry write"),
    (r"(?i)\bschtasks\b[^\n]{0,200}\/create\b", "scheduled task creation"),
]
EXEC_CHAIN_RES = [(re.compile(p), label) for p, label in EXEC_CHAIN]

# High value targets: private key material, cloud credentials, browser secrets.
CRED_HIGH = re.compile(
    r"(?i)(?:\.ssh\b|\.aws\b|\.gnupg\b|\.docker[/\\]config\.json|"
    r"id_rsa|id_ed25519|id_ecdsa|\.pem\b|credentials\.json|wallet\.dat|"
    r"login data|\bcookies\b|keychain|secrets[/\\]|\.npmrc\b|\.netrc\b|"
    r"\.kube[/\\]config|\.git-credentials)"
)
# Generic secret-ish words: only raise when paired with egress in the same file.
CRED_WORD = re.compile(
    r"(?i)(?:password|passwd|api[_-]?key|access[_-]?token|secret[_-]?key|private[_-]?key|\.env\b)"
)
EGRESS = re.compile(
    r"(?i)(?:https?://|\brequests\.(?:get|post|put)\b|\burllib\b|\bfetch\s*\(|\bhttp\.client\b|"
    r"\bsocket\.\w+\(|\binvoke-webrequest\b|\bnet\.webclient\b|\baxios\b|\bcurl\b|\bwget\b|"
    r"\bfile_get_contents\b|\bfsockopen\b)"
)
B64_BLOB = re.compile(r"[A-Za-z0-9+/]{400,}={0,2}")
MAGIC = [
    (b"MZ", "PE executable"),
    (b"\x7fELF", "ELF executable"),
    (b"%PDF", "PDF document"),
    (b"PK\x03\x04", "ZIP container"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal binary"),
    (b"\xd0\xcf\x11\xe0", "legacy OLE/Office container"),
]
TRAVERSAL = re.compile(r"(^[/\\])|(^[A-Za-z]:)|(\.\.)")

LEVEL_LABEL = {HIGH: "HIGH", MEDIUM: "MED", LOW: "LOW"}


# ---------------------------------------------------------------- helpers


def read_text(path):
    """Return (text, encoding) or (None, reason)."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return None, str(exc)
    if size > MAX_TEXT_BYTES:
        return None, "too large for text scan (%d bytes)" % size
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, str(exc)
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1/replace"


def is_probably_binary(raw):
    if not raw:
        return False
    if b"\x00" in raw[:4096]:
        return True
    printable = sum(1 for b in raw[:4096] if 32 <= b < 127 or b in (9, 10, 13))
    return printable / float(min(len(raw), 4096)) < 0.75


def sniff_magic(raw):
    for sig, label in MAGIC:
        if raw.startswith(sig):
            return label
    return None


def entropy(text):
    if not text:
        return 0.0
    counts = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = float(len(text))
    score = 0.0
    for value in counts.values():
        p = value / total
        score -= p * (p and __import__("math").log2(p) or 0)
    return score


# ---------------------------------------------------------------- scanners


def scan_unicode(text, where, findings):
    counts = {}
    first_line = {}
    line_no = 1
    for ch in text:
        if ch == "\n":
            line_no += 1
            continue
        cp = ord(ch)
        if 0xE0000 <= cp <= 0xE007F:
            label, kind = "unicode tag character block U+E0000-E007F (invisible ASCII smuggling)", HIGH
        elif cp in INVISIBLE:
            label, kind = INVISIBLE[cp]
        else:
            continue
        counts.setdefault(label, [0, kind])
        counts[label][0] += 1
        if label not in first_line:
            first_line[label] = line_no

    for label, (count, kind) in counts.items():
        if kind == MEDIUM and count <= 2:
            continue
        if kind == MEDIUM and "zero-width joiner" in label and count <= 12:
            # emoji sequences and some scripts use ZWJ heavily
            continue
        if kind == LOW and count <= LOW_REPORT_THRESHOLD:
            continue
        findings.append(Finding(
            kind, "R_UNICODE", "%s:%d" % (where, first_line[label]),
            "%s x%d (first at line %d)" % (label, count, first_line[label])))


MAX_PER_RULE = 3


def _key_of(finding):
    return (finding.rule, re.sub(r":\d+$", "", finding.where))


def _add(findings, counts, finding):
    key = _key_of(finding)
    seen = counts.get(key, 0)
    if seen < MAX_PER_RULE:
        findings.append(finding)
        counts[key] = seen + 1
    elif seen == MAX_PER_RULE:
        findings.append(Finding(finding.level, finding.rule, _key_of(finding)[1],
                                "further matches of this rule suppressed", ""))
        counts[key] = seen + 1


def scan_text_file(text, where, ext, findings):
    is_code = ext in CODE_EXT
    is_doc = ext in DOC_EXT
    lines = text.splitlines()
    counts = {}
    action_hits = set()
    for idx, line in enumerate(lines, 1):
        for rx in ACTION_RES:
            if rx.search(line):
                action_hits.add(idx)
                break

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        for rx in INSTRUCTION_RES:
            if rx.search(line):
                nearby = any(abs(a - idx) <= 2 for a in action_hits)
                _add(findings, counts, Finding(
                    HIGH if nearby else MEDIUM,
                    "R_INJECT_ACT" if nearby else "R_INJECT",
                    "%s:%d" % (where, idx),
                    "instruction-shaped text" + (" next to an action verb" if nearby else ""),
                    clip(stripped)))
                break
        for rx, label in EXEC_CHAIN_RES:
            if rx.search(line):
                _add(findings, counts, Finding(
                    HIGH if is_code else MEDIUM,
                    "R_EXEC_CHAIN",
                    "%s:%d" % (where, idx),
                    label,
                    clip(stripped)))
                break
        if not is_doc and EGRESS.search(line):
            if CRED_HIGH.search(line):
                _add(findings, counts, Finding(
                    HIGH, "R_CRED_EGRESS", "%s:%d" % (where, idx),
                    "private key material read and network call on the same line",
                    clip(stripped)))
            elif CRED_WORD.search(line):
                _add(findings, counts, Finding(
                    HIGH, "R_CRED_EGRESS", "%s:%d" % (where, idx),
                    "secret-looking value and network call on the same line",
                    clip(stripped)))
        if B64_BLOB.search(line) and not where.lower().endswith((".min.js", ".min.css")):
            blob = B64_BLOB.search(line).group(0)
            if entropy(blob) > 4.6:
                _add(findings, counts, Finding(
                    MEDIUM, "R_B64_BLOB", "%s:%d" % (where, idx),
                    "long high-entropy base64 blob (%d chars)" % len(blob)))

    if not is_doc and not any(f.rule == "R_CRED_EGRESS" for f in findings):
        if EGRESS.search(text):
            if CRED_HIGH.search(text):
                findings.append(Finding(
                    HIGH, "R_CRED_EGRESS", where,
                    "file touches private key material and also makes network calls"))
            elif CRED_WORD.search(text):
                findings.append(Finding(
                    MEDIUM, "R_CRED_EGRESS", where,
                    "file mentions secret-looking values and also makes network calls"))


def scan_package_json(text, where, findings):
    try:
        data = json.loads(text)
    except Exception:
        return
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return
    for name in ("preinstall", "install", "postinstall", "prepare"):
        cmd = scripts.get(name)
        if not cmd:
            continue
        level = HIGH
        findings.append(Finding(
            level, "R_INSTALL_HOOK", where,
            "npm lifecycle script '%s' runs on install" % name,
            clip(cmd)))
    deps = data.get("dependencies") or {}
    for dep, spec in list(deps.items()):
        if isinstance(spec, str) and (spec.startswith(("http://", "https://", "git+", "file:"))):
            findings.append(Finding(
                MEDIUM, "R_DEP_URL", where,
                "dependency '%s' resolves to a non-registry source" % dep,
                clip(spec)))


def scan_setup_py(text, where, findings):
    for idx, line in enumerate(text.splitlines(), 1):
        if re.search(r"(?i)\bcmdclass\b", line):
            findings.append(Finding(
                HIGH, "R_INSTALL_HOOK", "%s:%d" % (where, idx),
                "custom build command in setup.py", clip(line)))
        if re.search(r"(?i)^\s*(import\s+os|from\s+os\s+import|import\s+subprocess)", line):
            if re.search(r"(?i)os\.system|subprocess", text):
                findings.append(Finding(
                    HIGH, "R_INSTALL_HOOK", "%s:%d" % (where, idx),
                    "setup.py executes shell commands at import/build time", clip(line)))
                break


def scan_magic_file(path, ext, findings):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError:
        return
    kind = sniff_magic(raw)
    if kind is None:
        return
    expected = {
        ".pdf": "PDF", ".zip": "ZIP", ".docx": "ZIP", ".xlsx": "ZIP",
        ".pptx": "ZIP", ".jar": "ZIP", ".apk": "ZIP",
        ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF",
    }
    want = expected.get(ext)
    reasons = []
    if want and want not in kind:
        reasons.append("extension says %s but content is %s" % (ext or "none", kind))
    if kind in ("PE executable", "ELF executable", "Mach-O executable", "Mach-O universal binary"):
        if ext in DOC_EXT or ext in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mp3"):
            reasons.append("document extension hides a %s" % kind)
    if reasons:
        findings.append(Finding(HIGH, "R_MAGIC_MISMATCH", path, "; ".join(reasons)))


def scan_office_macro(path, ext, findings):
    if ext in MACRO_EXT:
        findings.append(Finding(
            HIGH, "R_MACRO", path, "macro-enabled Office file (%s)" % ext))
        return
    if ext not in (".docx", ".xlsx", ".pptx"):
        return
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if "vbaProject.bin" in name or name.endswith("vbaData.xml"):
                    findings.append(Finding(
                        HIGH, "R_MACRO", path,
                        "macro payload inside a plain Office container", name))
                    return
    except Exception:
        return


def scan_archive(path, findings):
    entries = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()[:MAX_ARCHIVE_ENTRIES]
                for info in infos:
                    entries.append((info.filename, info.file_size, info.compress_size))
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                for info in tf.getmembers()[:MAX_ARCHIVE_ENTRIES]:
                    entries.append((info.name, info.size, info.size))
    except Exception as exc:
        findings.append(Finding(MEDIUM, "R_ARCHIVE", path, "archive unreadable: %s" % exc))
        return

    total_uncompressed = 0
    total_compressed = 0
    dangerous = {}
    for name, size, csize in entries:
        total_uncompressed += size
        total_compressed += csize or 0
        if TRAVERSAL.search(name):
            findings.append(Finding(
                HIGH, "R_ARCHIVE_TRAVERSAL", path,
                "entry escapes the extraction directory", clip(name)))
        ext = os.path.splitext(name)[1].lower()
        if ext in EXEC_IN_ARCHIVE:
            dangerous.setdefault(ext, []).append(name)
        if re.search(r"\.(pdf|docx?|xlsx?|pptx?|jpe?g|png|txt)\.(exe|scr|bat|cmd|ps1|vbs|js)$", name, re.I):
            findings.append(Finding(
                HIGH, "R_ARCHIVE_MASQ", path,
                "double extension masking an executable", clip(name)))

    for ext, names in dangerous.items():
        level = EXEC_IN_ARCHIVE[ext]
        findings.append(Finding(
            level, "R_ARCHIVE_EXEC", path,
            "%d file(s) with extension %s inside the archive" % (len(names), ext),
            clip(", ".join(names[:5]))))

    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED:
        findings.append(Finding(HIGH, "R_ARCHIVE_BOMB", path,
                                "archive expands to %d bytes" % total_uncompressed))
    elif total_compressed and total_uncompressed / float(total_compressed) > 120:
        findings.append(Finding(MEDIUM, "R_ARCHIVE_BOMB", path,
                                "compression ratio %.0f:1" % (total_uncompressed / float(total_compressed))))


def scan_pdf(path, findings):
    try:
        import warnings
        warnings.filterwarnings("ignore")
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
    except Exception:
        return
    try:
        with open(path, "rb") as fh:
            if sniff_magic(fh.read(8)) not in (None, "PDF document"):
                return
    except OSError:
        return
    try:
        doc = fitz.open(path)
    except Exception as exc:
        findings.append(Finding(MEDIUM, "R_PDF", path, "pdf unreadable: %s" % exc))
        return
    suspicious_spans = 0
    sample = ""
    try:
        for page_no, page in enumerate(doc, 1):
            try:
                blocks = page.get_text("dict")["blocks"]
            except Exception:
                continue
            for block in blocks:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        color = span.get("color", 0)
                        size = span.get("size", 12)
                        text = span.get("text", "").strip()
                        if not text:
                            continue
                        near_white = color >= 0xF0F0F0
                        tiny = size < 2.0
                        if near_white or tiny:
                            suspicious_spans += 1
                            if not sample:
                                sample = "p%d %r" % (page_no, clip(text, 60))
    finally:
        doc.close()
    if suspicious_spans:
        findings.append(Finding(
            HIGH, "R_PDF_HIDDEN", path,
            "%d near-invisible text span(s) (white or sub-2pt)" % suspicious_spans,
            sample))


def scan_ooxml(path, ext, findings):
    """Extract visible text from an Office Open XML container and scan that."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if ext == ".pptx":
                targets = [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            elif ext == ".xlsx":
                targets = [n for n in names if n in ("xl/sharedStrings.xml",)]
            else:
                targets = [n for n in names if n in ("word/document.xml",)]
            chunks = []
            for name in targets[:80]:
                try:
                    xml = zf.read(name).decode("utf-8", "replace")
                except Exception:
                    continue
                xml = re.sub(r"(?i)</w:p>|</a:p>|<w:br\s*/>", "\n", xml)
                chunks.append(re.sub(r"(?s)<[^>]+>", " ", xml))
    except Exception:
        return
    body = "\n".join(chunks).strip()
    if not body:
        return
    scan_unicode(body, path, findings)
    scan_text_file(body, path, ".txt", findings)


def scan_file(path, findings, rel=None):
    ext = os.path.splitext(path)[1].lower()
    label = rel or path
    scan_magic_file(path, ext, findings)
    scan_office_macro(path, ext, findings)
    if ext in (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"):
        scan_archive(path, findings)
    if ext == ".pdf":
        scan_pdf(path, findings)
    if ext in (".docx", ".xlsx", ".pptx"):
        scan_ooxml(path, ext, findings)
        return
    if ext in BINARY_CONTAINER:
        return
    text, meta = read_text(path)
    if text is None:
        return
    if meta == "latin-1/replace":
        # Undecodable bytes: only then trust a binary sniff.
        return
    scan_unicode(text, label, findings)
    scan_text_file(text, label, ext, findings)
    if os.path.basename(path).lower() == "package.json":
        scan_package_json(text, label, findings)
    if os.path.basename(path).lower() == "setup.py":
        scan_setup_py(text, label, findings)


def scan_dir(root, findings):
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", ".hg", ".svn", "__pycache__", ".venv", "venv")]
        for name in files:
            if count >= MAX_FILES:
                findings.append(Finding(MEDIUM, "R_LIMIT", root,
                                        "stopped after %d files" % MAX_FILES))
                return
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            try:
                scan_file(full, findings, rel)
            except Exception as exc:
                findings.append(Finding(LOW, "R_SCAN_ERROR", rel, str(exc)))
            count += 1


def strip_html(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return text


def scan_url(url, findings):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        findings.append(Finding(HIGH, "R_URL_SCHEME", url, "unsupported scheme '%s'" % parsed.scheme))
        return
    if parsed.scheme == "http":
        findings.append(Finding(LOW, "R_URL_TLS", url, "plain http, no transport security"))
    host = parsed.hostname or ""
    if re.fullmatch(r"[0-9.]+", host):
        findings.append(Finding(MEDIUM, "R_URL_IP", url, "IP literal instead of a name", host))
    if "xn--" in host:
        findings.append(Finding(MEDIUM, "R_URL_PUNY", url, "punycode host (homograph risk)", host))

    req = urllib.request.Request(url, headers={"User-Agent": "san-gate-preflight/%s" % VERSION})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            final = resp.geturl()
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_URL_BYTES)
    except Exception as exc:
        findings.append(Finding(MEDIUM, "R_URL_FETCH", url, "fetch failed: %s" % exc))
        return

    final_host = urllib.parse.urlparse(final).hostname or ""
    if final_host and final_host != host:
        findings.append(Finding(MEDIUM, "R_URL_REDIRECT", url,
                                "redirected to a different host", final_host))

    if "text" in ctype or "html" in ctype or "json" in ctype or "xml" in ctype:
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                text = raw.decode("latin-1", "replace")
        body = strip_html(text) if "html" in ctype else text
        scan_unicode(body, url, findings)
        ext = ".html" if "html" in ctype else ".txt"
        scan_text_file(body, url, ext, findings)
    elif "pdf" in ctype:
        findings.append(Finding(MEDIUM, "R_URL_BINARY", url,
                                "remote pdf, download it and scan locally for hidden text"))
    else:
        findings.append(Finding(MEDIUM, "R_URL_BINARY", url,
                                "binary response (%s), download and scan locally" % ctype))


# ---------------------------------------------------------------- output


def verdict_of(findings):
    if any(f.level == HIGH for f in findings):
        return RED
    if any(f.level == MEDIUM for f in findings):
        return YELLOW
    return GREEN


def report(target, findings, as_json, quiet):
    findings.sort(key=lambda f: (-LEVEL_RANK[f.level], f.rule, f.where))
    verdict = verdict_of(findings)
    if as_json:
        print(json.dumps({
            "tool": "san-gate-preflight",
            "version": VERSION,
            "target": target,
            "verdict": verdict,
            "findings": [f.as_dict() for f in findings],
        }, ensure_ascii=True, indent=2))
        return verdict
    if not quiet:
        print("san-gate preflight v%s" % VERSION)
        print("target : %s" % target)
        print("verdict: %s" % verdict)
        print("")
        if not findings:
            print("  no findings")
        for f in findings:
            print("[%-4s] %-18s %s" % (LEVEL_LABEL[f.level], f.rule, f.where))
            print("       %s" % f.detail)
            if f.snippet:
                print("       > %s" % f.snippet)
    else:
        print(verdict)
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="Deterministic risk scan for untrusted files, archives and URLs.")
    parser.add_argument("target", help="file, directory or URL to scan")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--quiet", action="store_true", help="print the verdict only")
    args = parser.parse_args(argv)

    target = args.target
    findings = []
    try:
        if re.match(r"(?i)^https?://", target):
            scan_url(target, findings)
        elif os.path.isdir(target):
            scan_dir(target, findings)
        elif os.path.isfile(target):
            scan_file(target, findings)
        else:
            sys.stderr.write("san-gate: no such file, directory or URL: %s\n" % target)
            return 3
    except Exception as exc:
        sys.stderr.write("san-gate: scan failed: %s\n" % exc)
        return 3

    verdict = report(target, findings, args.json, args.quiet)
    return {GREEN: 0, YELLOW: 1, RED: 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
