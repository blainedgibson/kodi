# -*- coding: utf-8 -*-

'''
 ***********************************************************
 *
 * Midnight Addon
 *
 * @file cleandate.py
 * @package script.module.midnight
 *
 * @copyright 2025, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''

import time
import datetime
import re


def new_iso_to_utc(iso_ts):
    if not iso_ts:
        return 0
    return int(time.mktime(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%S.000Z")))

def now_to_iso():
    return datetime.datetime.now().isoformat('T', 'milliseconds') + 'Z'


def has_aired(air_date):
    """
    Check if content has aired based on premiere/air date.

    Centralized utility for consistent aired date validation across the addon.
    Replaces scattered date comparison logic in episodes.py, seasons.py, movies.py, etc.

    :param str air_date: Air/premiere date in format 'YYYY-MM-DD' or '0' or None/''
    :return: True if aired (date <= today), False if not aired (date > today) or no date
    :rtype: bool

    Examples:
        has_aired('2025-01-15')  # True if today is >= 2025-01-15
        has_aired('2027-12-31')  # False (future date)
        has_aired('0')           # False (no date)
        has_aired(None)          # False (no date)
        has_aired('')            # False (no date)
    """
    if not air_date or air_date == '0' or air_date == '':
        return False

    try:
        # Prefer YYYY-MM-DD so ISO timestamps do not inflate the numeric compare.
        m = re.search(r'(\d{4}-\d{2}-\d{2})', str(air_date))
        day = m.group(1) if m else str(air_date)
        air_date_num = int(re.sub(r'[^0-9]', '', day)[:8])
        today_num = int(datetime.datetime.now().strftime('%Y%m%d'))

        # Has aired if air date <= today
        return air_date_num <= today_num

    except (ValueError, TypeError):
        # If date parsing fails, assume not aired (safe default)
        return False


def is_known_future(air_date):
    """
    True only when we have a parseable air date strictly after today.

    Unknown / empty / '0' → False (do not treat as unaired for drop filters).
    """
    if not air_date or str(air_date).strip() in ('', '0'):
        return False
    m = re.search(r'(\d{4}-\d{2}-\d{2})', str(air_date))
    if not m:
        return False
    return not has_aired(m.group(1))
