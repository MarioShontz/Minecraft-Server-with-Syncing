#!/usr/bin/env python3
"""
Minecraft Server Wrapper - Safe multi-user server management with Syncthing integration.

This is the entry point script. All logic lives in the lib/ package.
"""

import sys

# Check Python version before importing anything else
if sys.version_info < (3, 8):
    print(f"Error: Python 3.8+ required, but running {sys.version_info.major}.{sys.version_info.minor}")
    sys.exit(1)

from lib.main import run

if __name__ == "__main__":
    sys.exit(run())
