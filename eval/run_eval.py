import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sangate2.layers import ExtractionError
from sangate2.report import ingest

FIXTURES = ROOT / "fixtures"
REPORTS = ROOT / "reports"


def _label(path):
    parts = path.relative_to(FIXTURES).parts
    category = parts[0] if parts else "unknown"
    sub = parts[1] if len(parts) > 2 else "root"
    return category, sub


def run():
    rows = defaultdict(lambda: {"n": 0, "red": 0, "yellow": 0, "green": 0})
    details = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file():
            continue
        if path.relative_to(FIXTURES).parts[0] == "final_holdout":
            continue
        category, sub = _label(path)
        try:
            report = ingest(path)
        except ExtractionError as exc:
            print("cannot ingest %s: %s" % (path, exc))
            return False, details
        key = "%s/%s" % (category, sub)
        rows[key]["n"] += 1
        rows[key][report.verdict.lower()] += 1
        details.append(
            {
                "path": str(path.relative_to(ROOT)),
                "category": category,
                "sub": sub,
                "verdict": report.verdict,
                "findings": [
                    {
                        "rule": item.rule,
                        "severity": item.severity,
                        "location": item.location,
                        "evidence": item.evidence,
                    }
                    for item in report.findings
                ],
            }
        )

    lines = ["# san-gate 2.0 evaluation", "", "| category | n | RED | YELLOW | GREEN | flagged |", "|---|---:|---:|---:|---:|---:|"]
    for key in sorted(rows):
        row = rows[key]
        flagged = row["red"] + row["yellow"]
        lines.append(
            "| %s | %d | %d | %d | %d | %d |"
            % (key, row["n"], row["red"], row["yellow"], row["green"], flagged)
        )

    def rate(key, field):
        row = rows.get(key)
        if not row or row["n"] == 0:
            return 0.0
        return row[field] / row["n"]

    expected = {
        "benign/visible": 16,
        "benign/hidden": 3,
        "canary/visible": 10,
        "canary/hidden": 16,
        "canary/heldout": 7,
        "classic/root": 4,
        "redteam/benign": 7,
        "redteam/canary": 10,
        "redteam/hidden": 11,
    }
    missing = [
        key
        for key, minimum in expected.items()
        if rows.get(key, {}).get("n", 0) != minimum
    ]
    unknown = sorted(set(rows) - set(expected))
    if unknown:
        print("unknown fixture categories:", ", ".join(unknown))

    checks = [
        ("expected fixture counts", not missing and not unknown),
        ("classic RED", rate("classic/root", "red") == 1.0),
        ("canary visible RED >= 70%", rate("canary/visible", "red") >= 0.70),
        ("canary visible flagged >= 95%", rate("canary/visible", "red") + rate("canary/visible", "yellow") >= 0.95),
        ("canary hidden RED >= 90%", rate("canary/hidden", "red") >= 0.90),
        ("canary hidden flagged >= 95%", rate("canary/hidden", "red") + rate("canary/hidden", "yellow") >= 0.95),
        ("canary heldout flagged >= 80%", rate("canary/heldout", "red") + rate("canary/heldout", "yellow") >= 0.80),
        ("benign visible RED <= 5%", rate("benign/visible", "red") <= 0.05),
        ("benign visible flagged <= 10%", rate("benign/visible", "red") + rate("benign/visible", "yellow") <= 0.10),
        ("benign hidden RED <= 5%", rate("benign/hidden", "red") <= 0.05),
        ("redteam canary flagged >= 95%", rate("redteam/canary", "red") + rate("redteam/canary", "yellow") >= 0.95),
        ("redteam benign RED <= 10%", rate("redteam/benign", "red") <= 0.10),
    ]

    lines.append("")
    lines.append("## Checks")
    lines.append("")
    for name, passed in checks:
        lines.append("- %s: **%s**" % (name, "PASS" if passed else "FAIL"))

    lines.append("")
    lines.append("## Details")
    lines.append("")
    for item in details:
        lines.append(
            "- `%s` -> **%s** %s"
            % (
                item["path"],
                item["verdict"],
                ", ".join(f["rule"] for f in item["findings"]) or "",
            )
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / "eval-r2.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:40]))
    print("")
    print("report:", out)
    return all(passed for _, passed in checks), details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    ok, details = run()
    if args.json:
        print(json.dumps(details, ensure_ascii=False, indent=2))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
