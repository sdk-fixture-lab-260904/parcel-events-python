"""Portable date, time, datetime and UUID assertions on wire strings."""
import re
import ipaddress


def _date(value: str) -> bool:
    match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", value)
    if match is None:
        return False
    year, month, day = map(int, match.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return 1 <= month <= 12 and 1 <= day <= days[month - 1]


def _time(value: str) -> bool:
    match = re.fullmatch(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?(?:[zZ]|([+-])([0-9]{2}):([0-9]{2}))", value)
    if match is None:
        return False
    hour, minute, second = map(int, match.groups()[:3])
    offset_hour, offset_minute = int(match[5] or 0), int(match[6] or 0)
    if hour > 23 or minute > 59 or second > 60 or offset_hour > 23 or offset_minute > 59:
        return False
    offset = (offset_hour * 60 + offset_minute) * (-1 if match[4] == "-" else 1)
    return second != 60 or (hour * 60 + minute - offset) % 1440 == 1439


def matches_format(format_name: str, value: str) -> bool:
    if format_name in ("ipv4", "ipv6"):
        try:
            address = ipaddress.ip_address(value)
            return "%" not in value and address.version == (4 if format_name == "ipv4" else 6)
        except ValueError:
            return False
    if format_name == "date":
        return _date(value)
    if format_name == "time":
        return _time(value)
    if format_name == "date-time":
        return len(value) >= 20 and value[10] in "tT" and _date(value[:10]) and _time(value[11:])
    if format_name == "uuid":
        return re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value) is not None
    return True
