"""ORACLE's clock. Every tool dates its work in US Central time, wherever it runs.

The cloud routine's computer runs on UTC, so a plain date.today() there would stamp an evening run with
tomorrow's date. This gives the same answer on the owner's PC and in the cloud.
"""
import datetime as dt

TZ_NAME = "America/Chicago"


def today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(TZ_NAME)).date()
    except Exception:
        # No timezone database (Windows without the tzdata package): the owner's PC is on Central time anyway.
        return dt.date.today()
