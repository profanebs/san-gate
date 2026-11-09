import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sangate2.layers import ExtractionError
from sangate2.report import ingest

HOLDOUT = ROOT / "fixtures" / "final_holdout"
REPORT = ROOT / "reports" / "eval-r3-holdout.md"
MIN_FILES = 10


def main():
    if not HOLDOUT.is_dir():
        print("holdout directory missing:", HOLDOUT)
        return 1
    rows = []
    for path in sorted(HOLDOUT.iterdir()):
        if not path.is_file():
            continue
        try:
            report = ingest(path)
        except ExtractionError as exc:
            print("cannot ingest %s: %s" % (path, exc))
            return 1
        rows.append(
            (
                path.name,
                report.verdict,
                sorted({item.rule for item in report.findings}),
            )
        )
    if not rows:
        print("no holdout files")
        return 1
    if len(rows) < MIN_FILES:
        print("holdout too small: %d < %d" % (len(rows), MIN_FILES))
        return 1
    flagged = sum(1 for _, verdict, _ in rows if verdict in ("RED", "YELLOW"))
    red = sum(1 for _, verdict, _ in rows if verdict == "RED")
    lines = [
        "# final holdout",
        "",
        "| n | RED | flagged | flagged rate |",
        "|---:|---:|---:|---:|",
        "| %d | %d | %d | %.1f%% |"
        % (len(rows), red, flagged, 100.0 * flagged / max(1, len(rows))),
        "",
        "| file | verdict | rules |",
        "|---|---|---|",
    ]
    for name, verdict, rules in rows:
        lines.append("| %s | %s | %s |" % (name, verdict, ", ".join(rules)))
    lines.append("")
    lines.append("Target: flagged >= 80%.")
    lines.append("Result: %s" % ("PASS" if flagged >= 0.8 * len(rows) else "FAIL"))
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("report:", REPORT)
    return 0 if flagged >= 0.8 * len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
