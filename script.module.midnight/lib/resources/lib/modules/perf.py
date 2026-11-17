# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on — lightweight perf probes (devmode only)
*
* @file perf.py
* @package script.module.midnight
********************************************************cm*
'''

import time
from functools import wraps


def perf_enabled():
    """Perf logging is tied to devmode so end users see no overhead."""
    try:
        from .crewruntime import c
        return bool(c.devmode)
    except Exception:
        return False


def perf_log(label, start_time):
    """Log elapsed milliseconds since start_time (perf_counter)."""
    if not perf_enabled():
        return
    try:
        from .crewruntime import c
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        c.log(f'[Perf] {label} {elapsed_ms:.1f}ms')
    except Exception:
        pass


def timed(section=None):
    """
    Decorator: log wall time for a function when devmode is active.

    Usage:
        @timed('router')
        def router(params):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not perf_enabled():
                return func(*args, **kwargs)
            label = section or func.__name__
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                perf_log(label, start)
        return wrapper
    return decorator
