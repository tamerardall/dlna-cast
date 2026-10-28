#!/usr/bin/env python3
"""Runs dlna-cast from a git checkout without installing it."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "src"))

from dlna_cast.cli import main

main()
