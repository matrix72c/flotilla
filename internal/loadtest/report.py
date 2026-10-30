"""Summarize loadtest event JSONL: compatibility matrix + bring-up latency percentiles.

Usage: python -I scripts/loadtest/report.py --events <events.jsonl> [--out report.md]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import quantiles

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadtest.events import read_events


def dataset_of(task: str) -> str:
    parts = Path(task).parts
    for part in parts:
        if part.endswith("2tb") or part == "terminal-bench-2":
            return part
    return parts[-2] if len(parts) >= 2 else "?"


def pcts(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    q = quantiles(ordered, n=20) if len(ordered) >= 2 else [ordered[0]] * 19
    return {
        "n": len(ordered),
        "p50": round(q[9], 2),
        "p95": round(q[18], 2),
        "max": round(ordered[-1], 2),
    }


def summarize(records: list[dict]) -> str:
    ends = [r for r in records if r.get("kind") == "episode_end"]
    ready = [r for r in records if r.get("kind") == "ready"]
    by_task = {(r["arm"], r["task"]): r for r in ends}

    lines: list[str] = []
    ok = sum(1 for r in by_task.values() if r["ok"])
    lines.append(f"# episodes: {len(by_task)}  ok={ok}  fail={len(by_task) - ok}")

    fail_classes = Counter(
        (dataset_of(r["task"]), (r.get("error") or {}).get("class", "?"), (r.get("error") or {}).get("stage"))
        for r in by_task.values()
        if not r["ok"]
    )
    if fail_classes:
        lines.append("\n## failures by dataset x class x stage")
        lines.append("| dataset | class | stage | n |")
        lines.append("|---|---|---|---|")
        for (dataset, klass, stage), n in fail_classes.most_common():
            lines.append(f"| {dataset} | {klass} | {stage} | {n} |")

    per_dataset = defaultdict(list)
    for r in by_task.values():
        per_dataset[(dataset_of(r["task"]), r["arm"])].append(r)
    lines.append("\n## compatibility by dataset")
    lines.append("| dataset | arm | n | ok | fail | ok% |")
    lines.append("|---|---|---|---|---|---|")
    for (dataset, arm), rows in sorted(per_dataset.items()):
        o = sum(1 for r in rows if r["ok"])
        lines.append(f"| {dataset} | {arm} | {len(rows)} | {o} | {len(rows) - o} | {100 * o // max(1, len(rows))}% |")

    ready_by = defaultdict(list)
    for r in ready:
        ready_by[dataset_of(r["task"])].append(r["ready_s"])
    lines.append("\n## bring-up ready_s")
    lines.append("| dataset | p50 | p95 | max | n |")
    lines.append("|---|---|---|---|---|")
    for dataset, values in sorted(ready_by.items()):
        stats = pcts(values)
        lines.append(f"| {dataset} | {stats.get('p50')} | {stats.get('p95')} | {stats.get('max')} | {stats.get('n')} |")

    stage_totals = defaultdict(list)
    for r in ready:
        for stage, seconds in (r.get("timings") or {}).items():
            stage_totals[stage].append(seconds)
    if stage_totals:
        lines.append("\n## flotilla stage seconds (all tasks)")
        lines.append("| stage | p50 | p95 | max | n |")
        lines.append("|---|---|---|---|---|")
        for stage, values in sorted(stage_totals.items()):
            stats = pcts(values)
            lines.append(f"| {stage} | {stats.get('p50')} | {stats.get('p95')} | {stats.get('max')} | {stats.get('n')} |")

    execs = [r for r in records if r.get("kind") == "exec"]
    if execs:
        by_mix = defaultdict(list)
        for r in execs:
            by_mix[r.get("mix")].append(r["exec_s"])
        lines.append("\n## exec_s by mix")
        lines.append("| mix | p50 | p95 | n |")
        lines.append("|---|---|---|---|")
        for mix, values in sorted(by_mix.items(), key=lambda kv: str(kv[0])):
            stats = pcts(values)
            lines.append(f"| {mix} | {stats.get('p50')} | {stats.get('p95')} | {stats.get('n')} |")

    uploads = [r for r in records if r.get("kind") == "upload"]
    if uploads:
        lines.append(f"\n## upload: {pcts([r['upload_s'] for r in uploads])} verified="
                     f"{sum(1 for r in uploads if r.get('verified'))}/{len(uploads)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    text = summarize(read_events(args.events))
    if args.out:
        args.out.write_text(text)
        print(f"wrote {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
