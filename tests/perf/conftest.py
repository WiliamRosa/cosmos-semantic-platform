"""Performance baseline (DEC-4). Opt-in: `uv run pytest -m perf tests/perf`."""

from __future__ import annotations

TIMINGS: list[dict] = []


def pytest_terminal_summary(terminalreporter):
    if not TIMINGS:
        return
    write = terminalreporter.write_line
    sf = TIMINGS[0]["sf"]
    terminalreporter.section(f"TPC-H baseline on DuckDB, scale factor {sf} (best of runs, ms)")
    write(f"{'case':<6}{'compile':>10}{'engine':>10}{'hand SQL':>10}{'engine/hand':>13}  rows")
    for t in TIMINGS:
        ratio = t["engine"] / t["hand"] if t["hand"] else float("nan")
        write(
            f"{t['id']:<6}{t['compile'] * 1000:>10.2f}{t['engine'] * 1000:>10.1f}"
            f"{t['hand'] * 1000:>10.1f}{ratio:>13.2f}  {t['rows']}"
        )
