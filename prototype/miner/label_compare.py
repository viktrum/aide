#!/usr/bin/env python3
"""One-shot 3-model label comparison on the latest mine archive.

Runs agy with three models on all shortlist segments (batched internally).
Outputs land in <run>/label_compare/<slug>/ without touching live rulebook.

Usage:
  python3 label_compare.py
  python3 label_compare.py --run-id 2026-07-08_235329
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "judge"))
from judge_store import resolve_run_dir
from label import execute_label

# (agy --model slug, human label from `agy models`)
COMPARE_MODELS = [
    ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
    ("claude-sonnet-4.6-thinking", "Claude Sonnet 4.6 (Thinking)"),
    ("claude-opus-4.6-thinking", "Claude Opus 4.6 (Thinking)"),
]


def main():
    ap = argparse.ArgumentParser(description="Compare label quality across 3 agy models")
    ap.add_argument("--run-id", help="Mine archive run id (default: latest)")
    ap.add_argument("--max-segments", type=int, default=60)
    args = ap.parse_args()

    run_dir = resolve_run_dir(args.run_id)
    compare_root = run_dir / "label_compare"
    compare_root.mkdir(parents=True, exist_ok=True)

    print(f"Compare run: {run_dir}", file=sys.stderr)
    print(f"Models: {', '.join(label for _, label in COMPARE_MODELS)}", file=sys.stderr)

    results = []
    for i, (model_slug, model_label) in enumerate(COMPARE_MODELS, start=1):
        slug = model_slug.replace(".", "-")  # folder-safe
        out_dir = compare_root / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== [{i}/{len(COMPARE_MODELS)}] {model_label} ({model_slug}) ===",
              file=sys.stderr)
        t0 = time.perf_counter()
        try:
            summary = execute_label(
                run_dir,
                backend="agy",
                model=model_slug,
                max_segments=args.max_segments,
                output_suffix=f"compare-{slug}",
                publish=False,
            )
            elapsed = round(time.perf_counter() - t0, 1)
            row = {**summary, "elapsed_s": elapsed, "slug": slug, "model_label": model_label}
            results.append(row)
            # Copy into compare subfolder for easy browsing
            for key in ("guide", "rulebook", "candidates_file"):
                src = Path(summary[key])
                if src.exists():
                    dst = out_dir / src.name
                    dst.write_text(src.read_text())
            print(f"Done in {elapsed}s — {summary['candidates']} candidates, "
                  f"{summary['patterns']} patterns", file=sys.stderr)
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            results.append({"model": model_slug, "model_label": model_label,
                            "slug": slug, "error": str(exc)})

    manifest = {
        "run_dir": str(run_dir),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "models": [{"slug": s, "label": l} for s, l in COMPARE_MODELS],
        "results": results,
    }
    manifest_path = compare_root / "compare_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1))

    print("\n=== Summary ===")
    for row in results:
        if row.get("error"):
            print(f"  {row.get('model_label', row.get('model'))}: FAILED — {row['error']}")
        else:
            print(f"  {row.get('model_label', row['model'])}: {row['candidates']} candidates, "
                  f"{row['patterns']} patterns ({row['elapsed_s']}s)")
            print(f"    guide → {row['guide']}")
    print(f"\nManifest → {manifest_path}")
    print("Pick a winner, then promote:")
    print("  cp <run>/guide-compare-<slug>.md <run>/guide.md")
    print("  cp <run>/rulebook-label-compare-<slug>.json <run>/rulebook.json")


if __name__ == "__main__":
    main()
