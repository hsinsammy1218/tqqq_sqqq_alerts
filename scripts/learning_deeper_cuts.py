#!/usr/bin/env python3
"""Learning-only deeper cuts on full pre-seal ETF history (frozen weights).

Does not retune, does not run sealed OOS, does not change live wiring.

Writes JSON + markdown under ``reports/`` by default. Override with
``LEARNING_DEEPER_CUTS_JSON`` / ``LEARNING_DEEPER_CUTS_MD``.
"""
