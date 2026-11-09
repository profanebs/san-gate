import argparse
import json
import sys

from .layers import ExtractionError, extract_layers
from .report import ingest, to_markdown


def _cmd_inspect(args):
    visible, layers = extract_layers(args.path)
    payload = {
        "target": args.path,
        "visible_text": visible,
        "layers": [
            {
                "kind": item.kind,
                "text": item.text,
                "location": item.location,
                "reason": item.reason,
            }
            for item in layers
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_ingest(args):
    report = ingest(args.path)
    if args.json:
        payload = {
            "target": report.target,
            "verdict": report.verdict,
            "summary": report.summary,
            "findings": [
                {
                    "rule": item.rule,
                    "severity": item.severity,
                    "category": item.category,
                    "location": item.location,
                    "evidence": item.evidence,
                    "message": item.message,
                }
                for item in report.findings
            ],
            "layers": [
                {
                    "kind": item.kind,
                    "text": item.text,
                    "location": item.location,
                    "reason": item.reason,
                }
                for item in report.layers
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = to_markdown(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0 if report.verdict != "RED" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sangate2")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="show visible and hidden layers")
    inspect_parser.add_argument("path")
    inspect_parser.set_defaults(func=_cmd_inspect)

    ingest_parser = sub.add_parser("ingest", help="classify a document")
    ingest_parser.add_argument("path")
    ingest_parser.add_argument("--json", action="store_true")
    ingest_parser.add_argument("--out")
    ingest_parser.set_defaults(func=_cmd_ingest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ExtractionError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
