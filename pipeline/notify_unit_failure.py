#!/usr/bin/env python3
"""Invoked by deploy/systemd/runanalyze-notify-failure@.service's
OnFailure= hook with the failed unit's name as argv[1]. Redundant safety
net — see that unit file's comment for why refresh.py's own notify_failure()
call is the primary path and this one is the backstop.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notify import notify_failure

if __name__ == "__main__":
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown unit"
    notify_failure(f"systemd unit {unit} failed — check `journalctl -u {unit}`.")
