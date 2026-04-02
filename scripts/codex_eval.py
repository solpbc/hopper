#!/usr/bin/env python3
"""Evaluate Codex effectiveness from Claude's perspective.

Pass 1: Structural analysis of retry patterns, exit codes, and timing.
Pass 2: Content analysis of correction prompts and audit findings.

Usage:
    python scripts/codex_eval.py             # full report
    python scripts/codex_eval.py --json      # machine-readable output
    python scripts/codex_eval.py --samples 5 # show N sample corrections
"""

import json
import re
import statistics
import sys
from pathlib import Path

LODES_DIR = Path.home() / ".local/share/hopper/lodes"
ARCHIVED = Path.home() / ".local/share/hopper/archived.jsonl"
ACTIVE = Path.home() / ".local/share/hopper/active.jsonl"

STAGES = ("prep", "design", "implement", "audit", "commit")


def fmt_dur(secs):
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def fmt_dur_ms(ms):
    return fmt_dur(ms / 1000)


def load_lodes():
    """Load all lode records from archived + active JSONL."""
    lodes = {}
    for path in (ARCHIVED, ACTIVE):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lode = json.loads(line)
                lodes[lode["id"]] = lode
            except (json.JSONDecodeError, KeyError):
                continue
    return lodes


def scan_lode_dir(lode_dir):
    """Scan a lode directory for stage artifacts.

    Returns dict with:
      stages: {stage_name: {versions: int, jsons: [metadata], has_audit: bool}}
    """
    stages = {}
    for stage in STAGES:
        versions = []
        jsons = []

        # Base version
        base_out = lode_dir / f"{stage}.out.md"
        base_json = lode_dir / f"{stage}.json"
        if base_out.exists() or base_json.exists():
            versions.append(None)  # None = base version
            if base_json.exists():
                try:
                    jsons.append(json.loads(base_json.read_text()))
                except (json.JSONDecodeError, OSError):
                    pass

        # Versioned retries
        n = 1
        while True:
            retry_out = lode_dir / f"{stage}_{n}.out.md"
            retry_json = lode_dir / f"{stage}_{n}.json"
            if not retry_out.exists() and not retry_json.exists():
                break
            versions.append(n)
            if retry_json.exists():
                try:
                    jsons.append(json.loads(retry_json.read_text()))
                except (json.JSONDecodeError, OSError):
                    pass
            n += 1

        if versions:
            stages[stage] = {
                "versions": len(versions),
                "retries": max(0, len(versions) - 1),
                "jsons": jsons,
            }

    return stages


def read_correction_prompt(lode_dir, stage, version):
    """Read a retry's .in.md to see Claude's correction directions."""
    suffix = f"{stage}_{version}" if version else stage
    path = lode_dir / f"{suffix}.in.md"
    if path.exists():
        try:
            return path.read_text()
        except OSError:
            pass
    return None


def read_stage_output(lode_dir, stage, version=None):
    """Read a stage's .out.md output."""
    suffix = f"{stage}_{version}" if version else stage
    path = lode_dir / f"{suffix}.out.md"
    if path.exists():
        try:
            return path.read_text()
        except OSError:
            pass
    return None


def extract_audit_issues(audit_text):
    """Extract issue counts from audit output text by severity.

    Looks for severity headers followed by numbered findings. Skips
    sections that say "none", "no ... found", or similar.
    """
    if not audit_text:
        return {}
    counts = {}
    for severity in ("critical", "high", "medium", "minor", "low"):
        # Find sections starting with a severity header (bold or heading)
        # and capture everything until the next severity header or end
        section_pattern = (
            rf"(?:^|\n)\s*(?:\*\*|#+\s*){severity}\b[^*#\n]*(?:\*\*)?[ \t]*\n"
            rf"(.*?)"
            rf"(?=\n\s*(?:\*\*|#+\s*)(?:critical|high|medium|minor|low|validated)|$)"
        )
        sections = re.findall(section_pattern, audit_text, re.DOTALL | re.IGNORECASE)
        items = 0
        for section in sections:
            section_stripped = section.strip().lower()
            # Skip "none found" / "no ... issues" sections
            if re.match(r"^[-*]?\s*(none|no\s|n/a)", section_stripped):
                continue
            if re.match(
                r"^[-*]?\s*`?(?:critical|high|medium|minor|low)`?\s*:?\s*(?:none|no\b|n/a|0\b)",
                section_stripped,
            ):
                continue
            # Count numbered list items (1. 2. etc.)
            numbered = re.findall(r"^\s*\d+\.\s+\S", section, re.MULTILINE)
            # Also count bold-prefixed items (common audit format)
            bold_items = re.findall(r"^\s*[-*]\s+\*\*\S", section, re.MULTILINE)
            items += max(len(numbered), len(bold_items))
        if items:
            counts[severity] = items
    return counts


def extract_directions_section(in_md_text):
    """Extract just the 'Directions' section from a correction .in.md file."""
    if not in_md_text:
        return ""
    # The directions are after "## Directions" header
    match = re.search(r"^## Directions\s*\n(.*)", in_md_text, re.DOTALL | re.MULTILINE)
    if match:
        return match.group(1).strip()
    # Fallback: last 60% of the file (directions tend to be at the end)
    lines = in_md_text.strip().splitlines()
    start = len(lines) * 2 // 5
    return "\n".join(lines[start:]).strip()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def main():
    show_json = "--json" in sys.argv
    sample_count = 5
    for i, arg in enumerate(sys.argv):
        if arg == "--samples" and i + 1 < len(sys.argv):
            sample_count = int(sys.argv[i + 1])

    lode_records = load_lodes()

    # Scan all lode directories
    all_lode_stats = {}  # lode_id -> {stages, project, ...}
    for lode_dir in sorted(LODES_DIR.iterdir()):
        if not lode_dir.is_dir():
            continue
        lode_id = lode_dir.name
        stages = scan_lode_dir(lode_dir)
        if not stages:
            continue

        record = lode_records.get(lode_id, {})
        all_lode_stats[lode_id] = {
            "stages": stages,
            "project": record.get("project", "unknown"),
            "title": record.get("title", ""),
            "lode_dir": str(lode_dir),
        }

    # ---------------------------------------------------------------------------
    # Pass 1: Structural Analysis
    # ---------------------------------------------------------------------------

    total_lodes = len(all_lode_stats)
    lodes_with_codex = sum(1 for s in all_lode_stats.values() if any(s["stages"]))
    lodes_with_retries = sum(
        1 for s in all_lode_stats.values() if any(st["retries"] > 0 for st in s["stages"].values())
    )

    # Per-stage stats
    stage_stats = {}
    for stage in STAGES:
        runs = 0
        retried = 0
        total_retries = 0
        durations_first = []
        durations_retry = []
        exit_failures = 0
        total_exits = 0

        for lode_id, info in all_lode_stats.items():
            if stage not in info["stages"]:
                continue
            st = info["stages"][stage]
            runs += 1
            if st["retries"] > 0:
                retried += 1
                total_retries += st["retries"]

            for i, meta in enumerate(st["jsons"]):
                dur = meta.get("duration_ms")
                ec = meta.get("exit_code")
                if dur is not None:
                    if i == 0:
                        durations_first.append(dur)
                    else:
                        durations_retry.append(dur)
                if ec is not None:
                    total_exits += 1
                    if ec != 0:
                        exit_failures += 1

        stage_stats[stage] = {
            "runs": runs,
            "retried": retried,
            "retry_rate": retried / runs if runs else 0,
            "total_retries": total_retries,
            "durations_first": durations_first,
            "durations_retry": durations_retry,
            "exit_failures": exit_failures,
            "total_exits": total_exits,
        }

    # Per-project retry rates
    project_stats = {}
    for lode_id, info in all_lode_stats.items():
        proj = info["project"]
        if proj not in project_stats:
            project_stats[proj] = {"total": 0, "retried": 0, "total_retries": 0}
        project_stats[proj]["total"] += 1
        lode_retries = sum(st["retries"] for st in info["stages"].values())
        if lode_retries > 0:
            project_stats[proj]["retried"] += 1
            project_stats[proj]["total_retries"] += lode_retries

    # ---------------------------------------------------------------------------
    # Pass 2: Content Analysis
    # ---------------------------------------------------------------------------

    # Collect all correction prompts (the _N.in.md files)
    corrections = []  # [(lode_id, project, stage, version, directions)]
    for lode_id, info in all_lode_stats.items():
        lode_dir = Path(info["lode_dir"])
        for stage, st in info["stages"].items():
            if st["retries"] == 0:
                continue
            for v in range(1, st["retries"] + 1):
                raw = read_correction_prompt(lode_dir, stage, v)
                directions = extract_directions_section(raw) if raw else ""
                corrections.append((lode_id, info["project"], stage, v, directions))

    # Analyze audit findings across all lodes
    audit_findings = []  # [(lode_id, project, issues_dict)]
    for lode_id, info in all_lode_stats.items():
        if "audit" not in info["stages"]:
            continue
        lode_dir = Path(info["lode_dir"])
        # Read most recent audit output
        st = info["stages"]["audit"]
        last_version = st["versions"] - 1
        v = last_version if last_version > 0 else None
        text = read_stage_output(lode_dir, "audit", v)
        issues = extract_audit_issues(text)
        if issues:
            audit_findings.append((lode_id, info["project"], issues))

    # Categorize corrections by keyword patterns in directions
    categories = {
        "test_failures": 0,
        "missing_implementation": 0,
        "wrong_approach": 0,
        "audit_fixes": 0,
        "style_naming": 0,
        "other": 0,
    }
    category_keywords = {
        "test_failures": [
            r"test.*fail",
            r"fail.*test",
            r"pytest",
            r"make test",
            r"assert.*error",
            r"test.*broke",
            r"test.*pass",
        ],
        "missing_implementation": [
            r"missing",
            r"forgot",
            r"didn.t implement",
            r"incomplete",
            r"not implemented",
            r"still need",
            r"also need",
        ],
        "wrong_approach": [
            r"wrong",
            r"incorrect",
            r"instead",
            r"should have",
            r"shouldn.t",
            r"not what",
            r"rethink",
            r"different approach",
        ],
        "audit_fixes": [
            r"audit",
            r"finding",
            r"fix.*issue",
            r"issue.*found",
            r"review",
            r"three audit",
            r"two audit",
        ],
        "style_naming": [r"rename", r"naming", r"style", r"convention", r"format", r"consistent"],
    }
    for _, _, stage, _, directions in corrections:
        if not directions:
            categories["other"] += 1
            continue
        d_lower = directions.lower()
        matched = False
        for cat, patterns in category_keywords.items():
            if any(re.search(p, d_lower) for p in patterns):
                categories[cat] += 1
                matched = True
                break
        if not matched:
            categories["other"] += 1

    # Aggregate audit severity counts
    total_audit_issues = {}
    for _, _, issues in audit_findings:
        for sev, count in issues.items():
            total_audit_issues[sev] = total_audit_issues.get(sev, 0) + count

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------

    if show_json:
        result = {
            "summary": {
                "total_lodes": total_lodes,
                "lodes_with_codex": lodes_with_codex,
                "lodes_with_retries": lodes_with_retries,
                "retry_rate": lodes_with_retries / lodes_with_codex if lodes_with_codex else 0,
            },
            "stage_stats": {
                stage: {
                    "runs": s["runs"],
                    "retried": s["retried"],
                    "retry_rate": round(s["retry_rate"], 3),
                    "total_retries": s["total_retries"],
                    "avg_first_duration_ms": int(statistics.mean(s["durations_first"]))
                    if s["durations_first"]
                    else None,
                    "avg_retry_duration_ms": int(statistics.mean(s["durations_retry"]))
                    if s["durations_retry"]
                    else None,
                    "exit_failures": s["exit_failures"],
                }
                for stage, s in stage_stats.items()
            },
            "project_stats": {
                proj: {
                    "total": p["total"],
                    "retried": p["retried"],
                    "retry_rate": round(p["retried"] / p["total"], 3) if p["total"] else 0,
                }
                for proj, p in sorted(project_stats.items(), key=lambda x: -x[1]["total"])
            },
            "correction_categories": categories,
            "audit_issues_total": total_audit_issues,
            "total_corrections": len(corrections),
        }
        print(json.dumps(result, indent=2))
        return

    # Human-readable report
    print()
    print("  Codex Effectiveness Report")
    print(f"  {'=' * 72}")
    print()

    # Summary
    retry_pct = lodes_with_retries / lodes_with_codex * 100 if lodes_with_codex else 0
    first_pass_pct = 100 - retry_pct
    print(
        f"  {lodes_with_codex} lodes with Codex stages,"
        f" {lodes_with_retries} required corrections ({retry_pct:.0f}%)"
    )
    print(f"  First-pass success rate: {first_pass_pct:.0f}%")
    print(f"  Total correction cycles: {len(corrections)}")
    print()

    # Per-stage breakdown
    print("  Per-Stage Retry Rates")
    print(f"  {'-' * 72}")
    hdr = (
        f"  {'stage':12s} {'runs':>6s} {'retried':>8s} {'rate':>6s} {'retries':>8s}"
        f"  {'avg 1st':>8s} {'avg retry':>10s} {'failures':>9s}"
    )
    print(hdr)
    print(f"  {'':12s} {'':>6s} {'':>8s} {'':>6s} {'':>8s}  {'':>8s} {'':>10s} {'':>9s}")
    for stage in STAGES:
        s = stage_stats[stage]
        avg_first = (
            fmt_dur_ms(statistics.mean(s["durations_first"])) if s["durations_first"] else "-"
        )
        avg_retry = (
            fmt_dur_ms(statistics.mean(s["durations_retry"])) if s["durations_retry"] else "-"
        )
        fail_str = str(s["exit_failures"]) if s["exit_failures"] else "-"
        row = (
            f"  {stage:12s} {s['runs']:>6d} {s['retried']:>8d}"
            f" {s['retry_rate']:>5.0%} {s['total_retries']:>8d}"
            f"  {avg_first:>8s} {avg_retry:>10s} {fail_str:>9s}"
        )
        print(row)
    print()

    # Per-project breakdown
    print("  Per-Project Retry Rates")
    print(f"  {'-' * 72}")
    print(f"  {'project':20s} {'lodes':>6s} {'retried':>8s} {'rate':>6s} {'total retries':>14s}")
    for proj, p in sorted(project_stats.items(), key=lambda x: -x[1]["total"]):
        rate = p["retried"] / p["total"] if p["total"] else 0
        row = (
            f"  {proj:20s} {p['total']:>6d} {p['retried']:>8d}"
            f" {rate:>5.0%} {p['total_retries']:>14d}"
        )
        print(row)
    print()

    # Correction categories
    total_cats = sum(categories.values())
    print("  Correction Categories (from retry prompt analysis)")
    print(f"  {'-' * 72}")
    if total_cats:
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            label = cat.replace("_", " ").title()
            pct = count / total_cats * 100
            bar = "#" * int(pct / 2)
            print(f"  {label:25s} {count:>4d} ({pct:4.0f}%)  {bar}")
    print()

    # Audit findings
    print("  Audit Findings (aggregated across all lodes)")
    print(f"  {'-' * 72}")
    print(f"  {len(audit_findings)} lodes had audit issues detected")
    if total_audit_issues:
        for sev in ("critical", "high", "medium", "minor", "low"):
            if sev in total_audit_issues:
                print(f"  {sev:12s} {total_audit_issues[sev]:>4d} issues")
    print()

    # Timing analysis: cost of corrections
    all_first = []
    all_retry = []
    for stage in STAGES:
        s = stage_stats[stage]
        all_first.extend(s["durations_first"])
        all_retry.extend(s["durations_retry"])
    if all_first and all_retry:
        total_first_hrs = sum(all_first) / 1000 / 3600
        total_retry_hrs = sum(all_retry) / 1000 / 3600
        overhead = total_retry_hrs / (total_first_hrs + total_retry_hrs) * 100
        print("  Correction Cost")
        print(f"  {'-' * 72}")
        print(f"  Total first-attempt time:  {total_first_hrs:.1f} hours")
        print(f"  Total retry time:          {total_retry_hrs:.1f} hours")
        print(f"  Retry overhead:            {overhead:.1f}%")
        print()

    # Sample corrections
    if corrections and sample_count > 0:
        n_show = min(sample_count, len(corrections))
        print(f"  Sample Corrections (showing {n_show} of {len(corrections)})")
        print(f"  {'-' * 72}")
        # Pick a spread: prioritize multi-retry lodes
        shown = set()
        samples = []
        # First pick any _2 or _3 retries
        for c in corrections:
            if c[3] >= 2 and c[0] not in shown and len(samples) < sample_count:
                samples.append(c)
                shown.add(c[0])
        # Then fill with _1 retries
        for c in corrections:
            if c[0] not in shown and len(samples) < sample_count:
                samples.append(c)
                shown.add(c[0])

        for lode_id, project, stage, version, directions in samples:
            print()
            print(f"  [{lode_id}] {project} | {stage} retry #{version}")
            # Show first ~8 lines of directions
            if directions:
                lines = directions.strip().splitlines()
                for line in lines[:8]:
                    print(f"    {line}")
                if len(lines) > 8:
                    print(f"    ... ({len(lines) - 8} more lines)")
            else:
                print("    (no directions captured)")
        print()


if __name__ == "__main__":
    main()
