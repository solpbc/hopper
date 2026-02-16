#!/usr/bin/env python3
"""Analyze timing of lode stages and phases."""

import os
import statistics
from pathlib import Path

LODES = Path.home() / ".local/share/hopper/lodes"


def mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def fmt(secs):
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def percentile(data, p):
    data = sorted(data)
    k = (len(data) - 1) * p / 100
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[-1]
    return data[f] + (k - f) * (data[c] - data[f])


def iqr_filter(data):
    """Remove outliers using 1.5x IQR rule."""
    if len(data) < 4:
        return data
    q1 = percentile(data, 25)
    q3 = percentile(data, 75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    return [x for x in data if lo <= x <= hi]


def phase_dur(d, name):
    """Return duration for a codex-style phase (name.in.md -> last name[_N].out.md)."""
    start = mtime(d / f"{name}.in.md")
    if start is None:
        return None

    # Find the last retry
    end = mtime(d / f"{name}.out.md")
    n = 1
    while True:
        t = mtime(d / f"{name}_{n}.out.md")
        if t is None:
            break
        end = t
        n += 1
    return (end - start) if end else None


def collect():
    mill_durs = []
    refine_durs = []
    ship_durs = []
    prep_durs = []
    design_durs = []
    implement_durs = []
    audit_durs = []
    commit_durs = []

    for d in sorted(LODES.iterdir()):
        if not d.is_dir():
            continue

        # Stage boundaries
        mill_in = mtime(d / "mill_in.md")
        mill_out = mtime(d / "mill_out.md")
        refine_out = mtime(d / "refine_out.md")
        ship_out = mtime(d / "ship_out.md")

        if not all([mill_in, mill_out, refine_out, ship_out]):
            continue

        # Require all core refine phases
        impl = phase_dur(d, "implement")
        commit = phase_dur(d, "commit")
        if impl is None or commit is None:
            continue

        mill_durs.append(mill_out - mill_in)
        refine_durs.append(refine_out - mill_out)
        ship_durs.append(ship_out - refine_out)

        implement_durs.append(impl)
        commit_durs.append(commit)

        p = phase_dur(d, "prep")
        if p is not None:
            prep_durs.append(p)

        ds = phase_dur(d, "design")
        if ds is not None:
            design_durs.append(ds)

        a = phase_dur(d, "audit")
        if a is not None:
            audit_durs.append(a)

    return {
        "mill": mill_durs,
        "refine": refine_durs,
        "ship": ship_durs,
        "prep": prep_durs,
        "design": design_durs,
        "implement": implement_durs,
        "audit": audit_durs,
        "commit": commit_durs,
    }


def print_stats(label, data, indent=2):
    pad = " " * indent
    n = len(data)
    if n == 0:
        print(f"{pad}{label:12s}  (no data)")
        return
    avg = statistics.mean(data)
    med = statistics.median(data)
    p95 = percentile(data, 95)
    lo = min(data)
    hi = max(data)
    print(
        f"{pad}{label:12s}  n={n:<4d}"
        f"  avg={fmt(avg):>6s}  med={fmt(med):>6s}"
        f"  p95={fmt(p95):>6s}  min={fmt(lo):>6s}  max={fmt(hi):>6s}"
    )


def main():
    raw = collect()

    # Filter each series independently
    filtered = {k: iqr_filter(v) for k, v in raw.items()}

    dropped = sum(len(raw[k]) - len(filtered[k]) for k in raw)

    total_raw = [m + r + s for m, r, s in zip(raw["mill"], raw["refine"], raw["ship"])]
    total_filtered = iqr_filter(total_raw)

    print(f"\n  Lode Timing Report")
    print(f"  {'=' * 76}")
    print(f"  {len(raw['mill'])} completed lodes, {dropped} outlier values removed (IQR 1.5x)")

    print(f"\n  Stages")
    print(f"  {'-' * 76}")
    print_stats("mill", filtered["mill"])
    print_stats("refine", filtered["refine"])
    print_stats("ship", filtered["ship"])
    print_stats("TOTAL", total_filtered)

    print(f"\n  Refine sub-phases")
    print(f"  {'-' * 76}")
    print_stats("prep", filtered["prep"])
    print_stats("design", filtered["design"])
    print_stats("implement", filtered["implement"])
    print_stats("audit", filtered["audit"])
    print_stats("commit", filtered["commit"])

    print()


if __name__ == "__main__":
    main()
