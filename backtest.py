from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

import pandas as pd

from data import CandleData
from indicators import IndicatorSnapshot, build_snapshot
from strategy_scoring import trading_days_between_inclusive
from strategy_params import StrategyParams
from strategy import DecideOptions, PositionState, decide
