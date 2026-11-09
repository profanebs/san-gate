import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sangate2.layers import ExtractionError
from sangate2.report import ingest

CONFIG = ROOT / "eval" / "real_corpus.local.json"
REPORT = ROOT / "reports" / "real-benign.md"


def main():
    if not CONFIG.exists():
        print("missing", CONFIG)
        return 1
    try:
        paths = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print("invalid json:", exc)
        return 1
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        print("config must be a JSON array of path strings")
        return 1
    rows = []
    for index, raw in enumerate(paths, 1):
        path = Path(raw)
        if not path.is_file():
            rows.append((index, "MISSING", []))
            continue
        try:
            report = ingest(path)
        except ExtractionError as exc:
            print("cannot ingest %s: %s" % (path, exc))
            return 1
        rows.append(
            (
                index,
                report.verdict,
                sorted({item.rule for item in report.findings}),
            )
        )

    total = len(rows)
    missing = sum(1 for _, verdict, _ in rows if verdict == "MISSING")
    red = sum(1 for _, verdict, _ in rows if verdict == "RED")
    yellow = sum(1 for _, verdict, _ in rows if verdict == "YELLOW")
    green = sum(1 for _, verdict, _ in rows if verdict == "GREEN")
    ok = total >= 10 and missing == 0 and (red / total) <= 0.05
    lines = [
        "# real benign requirement corpus",
        "",
        "| n | RED | YELLOW | GREEN |",
        "|---:|---:|---:|---:|",
        "| %d | %d | %d | %d |" % (total, red, yellow, green),
        "",
        "| # | verdict | rules |",
        "|---:|---|---|",
    ]
    for index, verdict, rules in rows:
        lines.append("| %d | %s | %s |" % (index, verdict, ", ".join(rules)))
    lines.append("")
    lines.append("RED target: <= 5% of real benign requirements.")
    lines.append("Missing files: %d" % missing)
    lines.append("Result: %s" % ("PASS" if ok else "FAIL"))
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("report:", REPORT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
