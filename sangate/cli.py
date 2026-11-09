"""Unified san-gate command line.

Two engines, one entry point:

  artifact  -- 1.0 preflight: files, directories, archives, URLs, supply-chain
  document  -- 2.0 layer-aware: hidden text and compliance canaries

`audit` runs both engines for a single document.
"""

from __future__ import annotations

import sys

from sangate1 import guard, preflight
from sangate2.cli import main as document_main

DOC_EXTS = (".pdf", ".docx", ".html", ".htm", ".txt", ".md", ".rst")


def _usage():
    print(
        "san-gate 2.0 - one gate for artifacts and requirement documents\n"
        "\n"
        "  check <file|dir|url>   artifact scan (1.0 preflight)\n"
        "  scan <file|dir|url>    alias for check\n"
        "  inspect <document>     show visible/hidden/auxiliary layers (2.0)\n"
        "  ingest <document>      classify a document for canaries (2.0)\n"
        "  audit <document>       artifact scan + document scan\n"
        "  on | off | status      guard posture\n"
        "  run -- <cmd>           run a command with a scrubbed environment\n"
        "  sanitize <file>        write a copy with invisible characters removed\n"
        "  verify                 self-test against bundled artifact fixtures\n"
        "  version                print versions\n"
    )


def _cmd_audit(argv):
    if not argv:
        print("usage: san-gate audit <document>", file=sys.stderr)
        return 3
    if "--json" in argv:
        print("audit does not support --json; run check and ingest separately", file=sys.stderr)
        return 3
    target = argv[0]
    artifact_code = preflight.main([target])
    document_code = 0
    if target.lower().endswith(DOC_EXTS):
        print("\n--- document scan ---\n")
        document_code = document_main(["ingest", target])
    return max(artifact_code, document_code)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    command = argv[0]
    rest = argv[1:]
    if command in ("check", "scan"):
        return preflight.main(rest)
    if command in ("inspect", "ingest"):
        return document_main([command] + rest)
    if command == "audit":
        return _cmd_audit(rest)
    if command in ("on", "off", "status", "run", "sanitize", "verify"):
        return guard.main([command] + rest)
    if command in ("version", "--version", "-V"):
        print("san-gate 2.0.0 (artifact 1.0.0 + document 2.0.0)")
        return 0
    print("san-gate: unknown command '%s'" % command, file=sys.stderr)
    _usage()
    return 3


if __name__ == "__main__":
    sys.exit(main())
