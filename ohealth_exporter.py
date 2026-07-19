#!/usr/bin/env python3
"""Compatibility entry point for the package CLI."""

from health_exporter.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
