#!/usr/bin/env python
"""Thin shim around ``entitlements_sync.cli`` for the legacy laptop workflow.

The Databricks Job uses the wheel directly via ``python_wheel_task`` (see
``databricks.yml``); this script exists so existing
``python scripts/run_sync.py --config config/config.yaml`` invocations keep
working from a checked-out repo.
"""
from __future__ import annotations

import sys

from entitlements_sync.cli import main

if __name__ == "__main__":
    sys.exit(main())
