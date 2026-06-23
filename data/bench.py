"""Find the fastest --workers setting for entities.py against your LM Studio server.

Runs a *fixed* batch of entries through the exact extraction path entities.py
uses, at a few concurrency levels, and prints entries/sec for each so you can
read off the sweet spot. It only calls the model and times -- it writes nothing,
so it never touches diary_entities.parquet.

    uv run data/bench.py                 # 12 entries, workers 1 2 3 4
    uv run data/bench.py --n 24          # bigger sample -> steadier numbers
    uv run data/bench.py --workers 1 2 3 4 6 8

Read it as: throughput climbs with workers until the server stops parallelizing,
then flattens. Pick the smallest worker count at that plateau -- more past it just
adds contention. If 1..4 are all about the same, LM Studio is serializing requests
and --workers won't help (look at its parallel-slots setting instead).
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

# Reuse entities.py's machinery so we benchmark the real code path, not a copy.
from entities import (
    entry_text,
    extract_one,
    get_client,
    load_entries,
    resolve_model,
)


def run_once(client, model, rows, workers) -> float:
    """Run every row through the model at `workers` concurrency; return seconds."""

    def task(row: dict) -> dict:
        meta = {"uri": row["uri"], "author": row["author"], "title": row["title"]}
        return extract_one(client, model, meta, entry_text(row))

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(task, rows))  # drain so we wait for all of them
    return time.perf_counter() - start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--n", type=int, default=12, help="entries per timing run (default 12)"
    )
    ap.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=[1, 2, 3, 4],
        help="worker counts to sweep (default: 1 2 3 4)",
    )
    args = ap.parse_args()

    rows = list(load_entries(args.n).iter_rows(named=True))
    if not rows:
        raise SystemExit("no entries to benchmark; run parse.py first")

    client = get_client()
    model = resolve_model(client)
    print(f"benchmarking '{model}': {len(rows)} entries x workers {args.workers}")

    # Warm-up: one untimed call so model load / cold first-token latency isn't
    # charged to the first real run.
    print("warming up...", flush=True)
    run_once(client, model, rows[:1], 1)

    print(f"\n{'workers':>7}  {'time (s)':>9}  {'entries/s':>10}  {'speedup':>8}")
    base: float | None = None
    for w in args.workers:
        secs = run_once(client, model, rows, w)
        rate = len(rows) / secs
        if base is None:
            base = secs
        print(f"{w:>7}  {secs:>9.1f}  {rate:>10.2f}  {base / secs:>7.2f}x", flush=True)


if __name__ == "__main__":
    main()
