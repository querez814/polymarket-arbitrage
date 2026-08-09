#!/usr/bin/env python3
"""Generate an explicitly non-executable political-event price-history study."""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.historical_data import parse_timestamp
from utils.political_event_study import EventSpec, PricePoint, run_price_only_study


def _read_events(manifest_path: Path) -> list[EventSpec]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("manifest must contain an events list")
    result = []
    for item in events:
        history_path = (manifest_path.parent / item["history_file"]).resolve()
        points = []
        with history_path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                price = record.get("price")
                if price is None:
                    continue
                points.append(
                    PricePoint(
                        timestamp=parse_timestamp(record["timestamp"]),
                        price=float(price),
                        source=str(record.get("source", "unknown")),
                    )
                )
        result.append(
            EventSpec(
                event_id=str(item["event_id"]),
                family=str(item["family"]),
                occurrence_at=parse_timestamp(item["occurrence_at"]),
                points=points,
            )
        )
    return result


def _write_csv(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"split": split, **trade}
        for split in ("train", "holdout")
        for trade in result[split]["trades"]
    ]
    fields = [
        "split",
        "event_id",
        "family",
        "direction",
        "baseline_price",
        "entry_price",
        "exit_price",
        "signal",
        "gross_probability_points",
        "assumed_round_trip_cost",
        "net_probability_points",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_html(path: Path, result: dict, manifest: Path) -> None:
    coverage = result["coverage"]
    holdout = result["holdout"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Political Event Price Study</title>
<style>body{{font:16px system-ui;max-width:900px;margin:40px auto;line-height:1.5}}.warning{{background:#fff3cd;padding:16px;border-left:5px solid #b7791f}}table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:8px;text-align:left}}</style></head>
<body><h1>Political Event Price Study</h1>
<p class=\"warning\"><strong>Price-only signal research — not executable PnL.</strong> Results use minute price histories and assumed costs, without historical bid/ask depth, queue position, or fill evidence.</p>
<p><strong>Manifest:</strong> {html.escape(str(manifest))}</p>
<table><tr><th>Coverage</th><th>Value</th></tr>
<tr><td>Event clusters requested</td><td>{coverage['events_requested']}</td></tr>
<tr><td>Complete windows</td><td>{coverage['complete_events']}</td></tr>
<tr><td>Missing windows</td><td>{coverage['missing_window_events']}</td></tr>
<tr><td>Selected threshold (train only)</td><td>{result['walk_forward']['selected_threshold']:.4f}</td></tr>
<tr><td>Holdout triggers</td><td>{holdout['trigger_count']}</td></tr>
<tr><td>Holdout cumulative net probability points</td><td>{holdout['cumulative_net_probability_points']:.4f}</td></tr>
<tr><td>Holdout max drawdown (probability points)</td><td>{holdout['max_drawdown_probability_points']:.4f}</td></tr></table>
<h2>Method</h2><p>Threshold selection is restricted to the chronological training event clusters. The later clusters are held out. Assumed round-trip cost is {result['parameters']['assumed_round_trip_cost']:.4f}; it is a sensitivity input, not a reconstructed fee or fill.</p>
</body></html>""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--baseline-minutes", type=int, default=30)
    parser.add_argument("--entry-minutes", type=int, default=15)
    parser.add_argument("--horizon-minutes", type=int, default=15)
    parser.add_argument("--point-tolerance-minutes", type=int, default=2)
    parser.add_argument("--thresholds", default="0.01,0.02,0.03")
    parser.add_argument("--assumed-round-trip-cost", type=float, default=0.02)
    args = parser.parse_args()
    thresholds = tuple(float(value) for value in args.thresholds.split(",") if value)
    events = _read_events(args.manifest)
    result = run_price_only_study(
        events,
        baseline_minutes=args.baseline_minutes,
        entry_minutes=args.entry_minutes,
        horizon_minutes=args.horizon_minutes,
        threshold_candidates=thresholds,
        assumed_round_trip_cost=args.assumed_round_trip_cost,
        point_tolerance_minutes=args.point_tolerance_minutes,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "political_price_study.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_csv(args.output_dir / "political_price_study_trades.csv", result)
    _write_html(args.output_dir / "political_price_study.html", result, args.manifest)
    print("Wrote price-only research artifacts to", args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
