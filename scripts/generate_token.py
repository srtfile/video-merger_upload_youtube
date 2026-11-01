#!/usr/bin/env python3
"""
Generate or renew YouTube OAuth token for local and GitHub Actions use.
Uses standard library to avoid missing dependency errors.

Usage:
    python scripts/generate_token.py
"""

import os
import sys

from renew_token import run_auth_flow

if __name__ == "__main__":
    manual_mode = "--manual" in sys.argv
    run_auth_flow(manual=manual_mode)
