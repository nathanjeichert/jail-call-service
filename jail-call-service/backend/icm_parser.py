"""
ICM report XML parser for GTL/ViaPath jail call batches.

Parses ICM_report.xml found in each call batch folder and extracts
per-call metadata keyed by the WAV filename (recordfilename element).

XML structure (child elements on each <Call>):
  <btn>          Outside phone number ("billed telephone number")
  <firstname>    Inmate first name
  <lastname>     Inmate last name
  <pin>          Inmate PIN
  <cdate>        Call date as YYYYMMDD (e.g. "20221130")
  <ctime>        Call time as HHMM without leading zero (e.g. "2139" or "139")
  <dur>          Duration in seconds
  <descr>        Call outcome (e.g. "Inmate Hungup", "CP-Hungup", "Time Up")
  <inmatephone>  Housing unit / facility (e.g. "Men - 7A-6")
  <ctdescr>      Call type (e.g. "Prepay (Public)")
  <notes>        Notes (often "N/A")
  <recordfilename> WAV filename — used as the dict key
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


@dataclass
class CallMeta:
    inmate_name: str           # "JULIAN LOWE"
    inmate_pin: str            # "22005751"
    outside_number: str        # raw "4083164547"
    outside_number_fmt: str    # "(408) 316-4547"
    call_date: str             # "2022-11-30"
    call_time: str             # "21:39"
    call_datetime_str: str     # "2022-11-30 21:39"
    facility: str              # "Men - 7A-6"
    call_outcome: str          # "Inmate Hungup"
    call_type: str             # "Prepay (Public)"
    xml_duration_seconds: int  # from <dur>
    notes: str                 # empty string unless note is non-N/A


def format_phone(number: str) -> str:
    """Format a 10-digit phone number as (XXX) XXX-XXXX; pass through otherwise."""
    digits = re.sub(r'\D', '', number)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return number


def _parse_date(cdate: str) -> str:
    """Convert '20221130' -> '2022-11-30'."""
    cdate = cdate.strip()
    if len(cdate) == 8 and cdate.isdigit():
        return f"{cdate[:4]}-{cdate[4:6]}-{cdate[6:]}"
    return cdate


def _parse_time(ctime: str) -> str:
    """Convert '2139' or '139' -> '21:39' (zero-pad to 4 digits first)."""
    ctime = ctime.strip()
    ctime_padded = ctime.zfill(4)
    if len(ctime_padded) == 4 and ctime_padded.isdigit():
        return f"{ctime_padded[:2]}:{ctime_padded[2:]}"
    return ctime


def find_icm_report(folder: str) -> Optional[str]:
    """Case-insensitive scan of folder for ICM_report.xml."""
    try:
        for entry in os.scandir(folder):
            if entry.name.lower() == "icm_report.xml":
                return entry.path
    except Exception as e:
        logger.debug("Could not scan folder %s: %s", folder, e)
    return None


def parse_icm_report(xml_path: str) -> Dict[str, CallMeta]:
    """
    Parse ICM_report.xml and return a dict keyed by recordfilename.
    Returns {} if the file is missing or malformed (graceful degradation).
    """
    if not xml_path or not os.path.exists(xml_path):
        return {}

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        logger.warning("Failed to parse ICM report %s: %s", xml_path, e)
        return {}

    result: Dict[str, CallMeta] = {}

    for call_el in root.iter('Call'):
        def _get(tag: str) -> str:
            el = call_el.find(tag)
            return el.text.strip() if el is not None and el.text else ''

        filename = _get('recordfilename')
        if not filename:
            continue

        # Inmate name from firstname + lastname child elements
        first = _get('firstname').upper()
        last = _get('lastname').upper()
        inmate_name = f"{first} {last}".strip() if (first or last) else "INMATE"

        inmate_pin = _get('pin')

        # Outside party: <btn> = "billed telephone number" = called party
        raw_number = _get('btn')
        outside_number_fmt = format_phone(raw_number) if raw_number else ''

        # Date/time
        cdate_raw = _get('cdate')
        ctime_raw = _get('ctime')
        call_date = _parse_date(cdate_raw) if cdate_raw else ''
        call_time = _parse_time(ctime_raw) if ctime_raw else ''
        call_datetime_str = f"{call_date} {call_time}".strip() if call_date or call_time else ''

        # Housing unit / facility: <inmatephone> contains the housing location
        facility = _get('inmatephone')

        # Call outcome / call type
        call_outcome = _get('descr')
        call_type = _get('ctdescr')

        # Duration
        dur_text = _get('dur')
        try:
            xml_duration_seconds = int(dur_text) if dur_text else 0
        except ValueError:
            xml_duration_seconds = 0

        # Notes
        note_raw = _get('notes')
        notes = '' if not note_raw or note_raw.upper() == 'N/A' else note_raw

        result[filename] = CallMeta(
            inmate_name=inmate_name,
            inmate_pin=inmate_pin,
            outside_number=raw_number,
            outside_number_fmt=outside_number_fmt,
            call_date=call_date,
            call_time=call_time,
            call_datetime_str=call_datetime_str,
            facility=facility,
            call_outcome=call_outcome,
            call_type=call_type,
            xml_duration_seconds=xml_duration_seconds,
            notes=notes,
        )

    logger.info("Parsed %d records from %s", len(result), xml_path)
    return result
