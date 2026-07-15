#!/usr/bin/env python3
"""Generate an alternative AI-authored plan (de novo or blended) for the
dashboard's "AI Plan" tab — writes docs/data/llm_plan.json only, never
plan.json. Reuses the existing metrics.json from the last refresh rather
than re-syncing Garmin/recomputing metrics, so this is fast and
Garmin-independent; run pipeline/refresh.py first if metrics.json is stale
or missing.

Not part of the nightly automated refresh — triggered on demand from the
AI Plan tab (or `python pipeline/generate_ai_plan.py --mode denovo|blended`).

Usage:
    python pipeline/generate_ai_plan.py --mode denovo
    python pipeline/generate_ai_plan.py --mode blended
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_race_config, log, read_json, setup_logging, write_json
import plan_generator
from deepseek_client import DeepSeekClient
from refresh import LOCK_PATH, VAR_DIR, load_dotenv


class AIPlanError(Exception):
    pass


def _run(mode: str) -> None:
    race_cfg = load_race_config()
    llm = DeepSeekClient()
    if not llm.available:
        raise AIPlanError("DeepSeek is not configured (DEEPSEEK_API_KEY missing) — "
                         "set it in .env")

    mx = read_json("metrics.json")
    if not mx:
        raise AIPlanError("no metrics.json found — run a refresh first "
                         "(pipeline/refresh.py) before generating an AI plan")

    today = date.today()

    if mode == "denovo":
        result = llm.generate_plan_denovo(race_cfg, mx, today)
    else:
        catalog_plan = plan_generator.generate_plan(
            race_cfg, mx["fitness"], mx["rolling_weekly"], mx["long_runs"], today)
        result = llm.generate_plan_blended(race_cfg, mx, today, catalog_plan)

    if not result:
        raise AIPlanError(f"DeepSeek returned an invalid/unusable {mode} plan — "
                         "see the log above for details. The previous AI plan (if "
                         "any) was left untouched.")

    write_json("llm_plan.json", {
        "mode": mode,
        "provider": "deepseek",
        "source": f"llm-{mode}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "race": race_cfg.get("race"),
        "units": race_cfg["preferences"].get("units", "miles"),
        "days": result["days"],
        "generation_note": result["generation_note"],
    })
    print("\nAI plan generation complete.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["denovo", "blended"], required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    load_dotenv()

    # Shares refresh.py's single-flight lock (same LOCK_PATH) — both touch
    # Garmin-derived data/metrics, so this must not race a live refresh.
    VAR_DIR.mkdir(mode=0o700, exist_ok=True)
    LOCK_PATH.touch(exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("A refresh or another AI-plan generation is already running — skipping.")
        os.close(lock_fd)
        return

    try:
        _run(args.mode)
    except AIPlanError as e:
        log.error("AI plan generation failed: %s", e)
        sys.exit(1)
    except Exception as e:
        log.exception("AI plan generation failed")
        sys.exit(1)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    main()
