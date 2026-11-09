import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sangate2.report import ingest

FIXTURES = ROOT / "fixtures"


def main():
    times = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file():
            continue
        start = time.perf_counter()
        ingest(path)
        times.append((time.perf_counter() - start) * 1000.0)
    if not times:
        print("no fixtures")
        return 1
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    print("files:", len(times))
    print("mean_ms: %.1f" % statistics.mean(times))
    print("median_ms: %.1f" % statistics.median(times))
    print("p95_ms: %.1f" % p95)
    print("max_ms: %.1f" % max(times))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
