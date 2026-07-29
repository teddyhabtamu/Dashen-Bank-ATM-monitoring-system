"""
config/modes.py — Centralized configuration for data source selection.

This module provides a single source of truth for whether the system
is running in simulation or production mode. All components read from
this module instead of having their own mode flags.

Usage:
    from config.modes import DATA_SOURCE, is_simulation, is_production

    if is_simulation():
        # use simulated data
    else:
        # use real data
"""
import os

DATA_SOURCE = os.environ.get('DATA_SOURCE', 'simulation')  # 'simulation' | 'production'

def is_simulation():
    """Check if system is in simulation mode."""
    return DATA_SOURCE == 'simulation'

def is_production():
    """Check if system is in production mode."""
    return DATA_SOURCE == 'production'

def get_mode():
    """Return current mode string."""
    return DATA_SOURCE
