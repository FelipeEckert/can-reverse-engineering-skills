#!/usr/bin/env python3
"""Build deterministic, read-only encoding priors from an opendbc signal index.

The input is the ``signals.tsv`` artifact produced by ``opendbc_reference.py``.
This script never opens the corpus or the index for writing.  It emits descriptive
JSON and Markdown artifacts; it does not score physical-signal candidates or alter
the reverse-engineering workflow.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


DEFAULT_SIGNALS = Path("temp-output/opendbc-reference/signals.tsv")
DEFAULT_OUTPUT_DIR = Path("temp-output/opendbc-priors")
JSON_NAME = "opendbc-priors.json"
MARKDOWN_NAME = "opendbc-priors.md"

REQUIRED_COLUMNS = {
    "oem_family",
    "category",
    "origin_type",
    "dbc_filename",
    "message_name",
    "can_id",
    "signal_name",
    "start_bit",
    "length",
    "byte_order",
    "signedness",
    "factor",
    "offset",
    "unit",
    "minimum",
    "maximum",
    "parser_mode",
}

PARSER_GROUPS = {
    "cantools-strict": "strict",
    "cantools-lenient": "lenient",
    "line-fallback": "fallback",
    "line-supplement": "fallback",
}
VALID_ORIGINS = {"direct", "generated"}
NON_INDEPENDENT_FAMILIES = {"Unclassified"}
STRENGTH_ORDER = {"strong": 0, "moderate": 1, "weak": 2, "insufficient": 3}


def normalized(value: str) -> str:
    """Return an uppercase, underscore-delimited form used only for matching."""

    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]+", "_", value.upper())).strip("_")


def compact_token(value: str) -> str:
    """Normalize spelling and separator variants for unit comparisons."""

    return normalized(value.replace("Â", "")).replace("_", "")


def has_token(value: str, *tokens: str) -> bool:
    parts = set(normalized(value).split("_"))
    return any(token in parts for token in tokens)


def has_any(value: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def decimal_text(value: str) -> str:
    """Canonicalize a finite decimal without changing its exact numeric value."""

    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite decimal {value!r}")
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def optional_decimal(value: str) -> Decimal | None:
    if value == "":
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def canonical_unit(unit: str, family: str) -> str:
    raw = unit.strip()
    key = compact_token(raw)
    if family == "pedal_throttle" and raw == "%":
        return "%"
    if not key:
        return ""

    if family == "engine_rpm":
        if key in {"RPM", "RMIN", "REVMIN", "1MIN", "REVOLUTIONSPERMINUTE"}:
            return "rpm"
    if family == "pedal_throttle":
        if key in {"PERCENT", "UNITPERCENT", "PCT"}:
            return "%"
    if family in {"vehicle_speed", "wheel_speed"}:
        if key in {"KMH", "KMHR", "KPH", "KMHOUR", "KMHOURS", "KMH1"}:
            return "km/h"
        if key in {"MS", "MSEC", "METERSEC", "METERSSEC", "METERSECOND", "METERSSECOND"}:
            return "m/s"
        if key in {"MPH", "MIH", "MILESHOUR"}:
            return "mph"
        if key in {"RPM", "RMIN", "REVMIN", "1MIN"}:
            return "rpm"
    if family == "steering_angle":
        if key in {"DEG", "DEGREE", "DEGREES", "DEGS"}:
            return "deg"
        if key in {"RAD", "RADIAN", "RADIANS"}:
            return "rad"
    if family == "temperature":
        if key in {
            "C", "DEGC", "DEGREEC", "DEGREESC", "CELSIUS", "DEGCELSIUS",
            "DEGREECELSIUS", "DEGREESCELSIUS", "UNITDEGREC", "UNITDEGRECELSI",
            "UNITDEGREECELSIUS", "CEL",
        } or raw.replace("Â", "") == "°C":
            return "degC"
        if key in {"DEG", "DEGREE", "DEGREES"}:
            return "degC"
        if key in {"F", "DEGF", "DEGREEF", "FAHRENHEIT"}:
            return "degF"
        if key in {"K", "KELVIN"}:
            return "K"
    if family.startswith("torque"):
        if key in {"NM", "NEWTONMETER", "NEWTONMETERS"}:
            return "Nm"
    if family.startswith("voltage"):
        if key in {"V", "VOLT", "VOLTS", "VOLTAGE"}:
            return "V"
        if key in {"MV", "MILLIVOLT", "MILLIVOLTS", "UNITMILLIVOLT", "UNITMILLIVOLTS"}:
            return "mV"
    return raw


def parser_group(mode: str) -> str:
    try:
        return PARSER_GROUPS[mode]
    except KeyError as exc:
        raise ValueError(f"unknown parser mode {mode!r}") from exc


def unit_token(row: Mapping[str, str]) -> str:
    return compact_token(row["unit"])


def integer_length(row: Mapping[str, str]) -> int:
    try:
        value = int(row["length"])
    except ValueError as exc:
        raise ValueError(f"invalid signal length {row['length']!r}") from exc
    if value <= 0:
        raise ValueError(f"non-positive signal length {value}")
    return value


def rpm_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    explicit = bool(
        re.search(r"(^|_)ENGINE_?(RPM|SPEED)(?=$|_|[0-9])|(^|_)ENG_?SPD($|_)", name)
        or name in {"ENGSPD", "ENG_SPD"}
        or name in {"RPM", "LOW_RES_RPM", "CRUISE_RPM"}
    )
    excluded = has_token(
        name, "TARGET", "TGT", "TAR", "REQUEST", "REQ", "CMD", "COMMAND",
        "MAX", "MIN", "LIMIT", "LIM", "SETPOINT", "VALID", "FLAG", "STATE",
        "STATUS", "ERROR", "ERR", "RATE", "GRAD", "CHECKSUM", "CHKSM", "CRC",
    )
    return explicit and not excluded and integer_length(row) >= 8


def pedal_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    semantic = bool(
        re.search(r"ACCELERATOR.*PEDAL|ACCEL_?PEDAL|ACC_?PEDAL|GAS_?PEDAL|PEDAL_?GAS", name)
        or re.search(r"PEDAL.*(POSITION|POS)", name)
        or re.search(r"THROTTLE.*(POSITION|POS|ANGLE|PEDAL|BODY)", name)
        or re.search(r"FAHR_?PEDAL.*(ROH_?WERT|WERT|WINKEL|POSITION|POS)", name)
        or name == "THROTTLE"
    )
    excluded = has_token(
        name, "BRAKE", "CLUTCH", "REQ", "REQUEST", "CMD", "COMMAND", "TARGET",
        "TGT", "LIMIT", "LIM", "STATE", "STATUS", "FLAG", "FAULT", "ERROR",
        "ERR", "VALID", "PRESSED", "RELEASED", "CHECKSUM", "COUNTER", "CRUISE",
    ) or has_any(name, [r"OFF_?THROTTLE", r"NOT_?FULL"])
    return semantic and not excluded and integer_length(row) >= 6


def wheel_speed_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    semantic = bool(
        re.search(r"WHEEL.*(SPEED|SPD)|(SPEED|SPD).*WHEEL", name)
        or re.search(r"WHL.*SPD|SPD.*WHL", name)
    )
    excluded = has_token(
        name, "FAULT", "QF", "QUALITY", "VALID", "VALIDITY", "DIR", "DIRECTION",
        "COUNTER", "CHECKSUM", "CRC", "ALIVE", "STATUS", "STATE", "SIGN", "PULSE",
    )
    return semantic and not excluded and integer_length(row) >= 6


def vehicle_speed_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    known_stems = {
        "VEHICLE_SPEED", "VEH_SPEED", "VEH_SPD", "VEHSPD", "CAR_SPEED", "PCM_SPEED",
        "XMISSION_SPEED", "TRANSMISSION_SPEED", "V_EGO", "DI_VEHICLESPEED", "VEHSPDAV",
    }
    semantic = any(stem in name for stem in known_stems) or bool(
        re.search(r"VEH(ICLE)?_?(SPEED|SPD)", name)
    )
    excluded = wheel_speed_match(row) or has_token(
        name, "QF", "QUALITY", "VALID", "VALIDITY", "DIR", "DIRECTION", "COUNTER",
        "CHECKSUM", "CRC", "BZ", "GROUP", "SOURCE", "SRC", "FLAG", "STATUS", "STATE",
        "LIMIT", "LIM", "MAX", "MIN", "SET", "SETPOINT", "CRUISE", "REQ", "REQUEST",
        "CMD", "TARGET", "TGT", "DESIRED", "ROUGH", "FACTOR", "GPS",
    ) or has_any(name, [r"TOP_?SPD", r"OV_?SPD", r"DUTO"])
    return semantic and not excluded and integer_length(row) >= 6


def steering_angle_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    semantic = bool(
        re.search(r"(STEER|STEERING).*ANGLE|ANGLE.*(STEER|STEERING)", name)
        or name in {"SAS_ANGLE", "SAS_STATUS_ANGLESAFE"}
    )
    excluded = has_token(
        name, "RATE", "SPEED", "REQ", "REQUEST", "CMD", "COMMAND", "TARGET", "TGT",
        "CRC", "COUNTER", "STATUS", "VALID", "VALIDITY", "INIT", "DIRECTION", "SIGN",
        "ACK", "OFFSET", "SANITY", "ROUGH", "HP", "SERVO",
    ) or has_any(name, [r"REQ[0-9]*($|_)", r"RESERV(E)?D", r"RESERVD"])
    declared_unit = canonical_unit(row["unit"], "steering_angle")
    unitless_names = {
        "STEER_ANGLE", "STEERING_ANGLE", "STEER_WHEEL_ANGLE", "STEERING_WHEEL_ANGLE",
        "SAS_ANGLE", "SAS_STATUS_ANGLESAFE", "STEER_ANGLE_2",
    }
    admissible_unit = declared_unit in {"deg", "rad"} or (
        not row["unit"].strip() and name in unitless_names
    )
    return semantic and not excluded and admissible_unit and integer_length(row) >= 6


def temperature_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    semantic = "TEMP" in name or "TEMPERATURE" in name or has_token(name, "TMP")
    excluded = (
        name == "ZEITSTEMPEL"
        or has_token(
            name, "FAULT", "ERROR", "ERR", "WARNING", "WARN", "STATUS", "STATE", "VALID",
            "VALIDITY", "QUALITY", "QBIT", "UNIT", "TEMPORARY", "TEMPOLIMIT", "SETPT",
            "SETPOINT", "SET", "TARGET", "TRGT", "REQUEST", "REQ", "RQ", "CMD", "COMMAND",
            "BUTTON", "INDICATOR", "GROUP", "MAYBE",
        )
        or has_any(name, [r"SET_?TEMP", r"ACTIVE_?MY_?TEMP", r"SAVE_?MY_?TEMP", r"HIGH_?FAULT", r"LOW_?FAULT"])
    )
    bad_unit = unit_token(row) in {
        "ASCII", "UNITMILLIVOLT", "UNITMILLIVOLTS", "DEGREESCW", "MIXED",
    }
    booleanish = integer_length(row) <= 4 and has_any(name, [r"TEMP_?HIGH", r"TEMP_?LOW"])
    return semantic and not excluded and not bad_unit and not booleanish and integer_length(row) >= 5


def voltage_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    message = normalized(row["message_name"])
    explicit = bool(
        re.search(r"(BATT|BATTERY|VBATT|UBATT).*(VOLT|VOLTAGE|_U_(ACTL|MEAS)|_V$)", name)
        or re.search(r"(VOLT|VOLTAGE).*(BATT|BATTERY)", name)
        or name in {
            "CAN_BATT_VOLTS", "PACK_VOLTAGE", "HV_VOLTAGE", "LV_VOLTAGE", "BAT_SNSR_V",
            "VB", "VBATT", "UBATT", "BATVLT", "BAT_VLT", "BATTERIESPANNUNG",
        }
        or name.startswith("BATTERIESPANNUNG_")
    )
    contextual = unit_token(row) in {"V", "VOLT", "VOLTS", "VOLTAGE"} and bool(
        re.search(r"BATT|BATTERY|PACK", message)
    ) and name in {"VOLTAGE", "BATVLT"}
    excluded = has_token(
        name, "REQ", "REQUEST", "CMD", "COMMAND", "LIMIT", "LIM", "MAX", "MIN", "PRED",
        "PREDICT", "RATE", "FAULT", "ERROR", "ERR", "STATUS", "STATE", "VALID", "QUALITY",
        "QF", "PERCENT", "SOC", "CURRENT", "TEMP", "HEAT", "GROUP",
    )
    module_tap = bool(re.search(r"VOLTAGE_\d+_\d+_[A-Z]$", name))
    return (explicit or contextual) and not excluded and not module_tap and integer_length(row) >= 6


def torque_match(row: Mapping[str, str]) -> bool:
    name = normalized(row["signal_name"])
    semantic = "TORQUE" in name or "TORQ" in name or has_token(name, "TRQ", "TQ")
    excluded = has_token(
        name, "ENABLE", "ACTIVE", "STATUS", "STATE", "FLAG", "FAULT", "ERROR", "ERR",
        "VALID", "VALIDITY", "QUALITY", "QF", "CHECKSUM", "COUNTER", "MODE", "MODIFIER",
        "FACTOR", "RATE", "GRAD", "SIGN", "CHKSM", "CRC",
    )
    return semantic and not excluded and integer_length(row) >= 6


def torque_subfamily(row: Mapping[str, str]) -> str | None:
    """Classify torque declarations once, with role overriding physical domain."""

    name = normalized(row["signal_name"])
    context = normalized(f"{row['signal_name']} {row['message_name']}")
    if has_token(name, "MAX", "MIN", "LIMIT", "LIM", "MX", "MN"):
        return "limit"
    if has_token(
        context, "REQ", "REQUEST", "CMD", "COMMAND", "RQ", "DESIRED", "TARGET", "TGT",
        "SETPOINT", "DEMAND",
    ):
        return "request_command"
    if has_any(context, [
        r"STEER", r"STEERING", r"COLUMN", r"(^|_)EPS($|_)", r"EPAS",
        r"(^|_)MDPS($|_)", r"(^|_)LKA(S)?($|_)",
    ]):
        return "steering_torque"
    if has_any(context, [r"ENGINE", r"(^|_)ENG($|_)", r"ENG.*(TORQ|TRQ|TQ)", r"TQ_STND"]):
        return "engine_torque"
    if has_any(context, [r"MOTOR", r"(^|_)MTR($|_)"]):
        return "motor_torque"
    if has_any(context, [
        r"WHEEL", r"(^|_)WHL($|_)", r"AXLE", r"DRIVELINE", r"PROP", r"PRPL",
        r"TRANSM", r"(^|_)TRN($|_)", r"GEAR", r"(^|_)AWD($|_)", r"CLUTCH",
    ]):
        return "driveline_wheel_torque"
    return None


def voltage_subfamily(row: Mapping[str, str]) -> str:
    name = normalized(row["signal_name"])
    context = normalized(f"{row['signal_name']} {row['message_name']}")
    factor = abs(Decimal(row["factor"]))
    if has_any(name, [r"SNSR", r"SENSOR", r"MEAS", r"SENSED"]) or (factor < Decimal("0.01") and integer_length(row) >= 14):
        return "sensor_fine_measurement"
    maximum = optional_decimal(row["maximum"])
    if has_any(context, [r"(^|_)HV($|_)", r"PACK", r"TRAC", r"TRACTION", r"HVEM"]) or (maximum is not None and maximum >= 100):
        return "hv_pack"
    return "low_voltage_system"


FAMILY_MATCHERS: tuple[tuple[str, str, Callable[[Mapping[str, str]], bool]], ...] = (
    ("engine_rpm", "Engine RPM", rpm_match),
    ("pedal_throttle", "Pedal / throttle", pedal_match),
    ("vehicle_speed", "Vehicle speed", vehicle_speed_match),
    ("wheel_speed", "Wheel speed", wheel_speed_match),
    ("steering_angle", "Steering angle", steering_angle_match),
    ("temperature", "Temperature", temperature_match),
)

TORQUE_LABELS = {
    "engine_torque": "Engine torque",
    "steering_torque": "Steering torque",
    "motor_torque": "Motor torque",
    "driveline_wheel_torque": "Driveline / wheel torque",
    "request_command": "Torque request / command",
    "limit": "Torque limit",
}

VOLTAGE_LABELS = {
    "low_voltage_system": "Low-voltage / system",
    "hv_pack": "HV pack",
    "sensor_fine_measurement": "Sensor / fine measurement",
}


def read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing TSV header")
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
        rows = []
        for line_number, row in enumerate(reader, 2):
            try:
                integer_length(row)
                row["factor"] = decimal_text(row["factor"])
                row["offset"] = decimal_text(row["offset"])
                parser_group(row["parser_mode"])
                if row["origin_type"] not in VALID_ORIGINS:
                    raise ValueError(f"unknown origin type {row['origin_type']!r}")
                if row["signedness"] not in {"signed", "unsigned"}:
                    raise ValueError(f"unknown signedness {row['signedness']!r}")
                if row["byte_order"] not in {"little_endian", "big_endian"}:
                    raise ValueError(f"unknown byte order {row['byte_order']!r}")
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            rows.append(dict(row))
    return rows, list(reader.fieldnames)


def grouped_counts(rows: Iterable[Mapping[str, str]]) -> dict[str, int]:
    counts = Counter(parser_group(row["parser_mode"]) for row in rows)
    return {key: counts.get(key, 0) for key in ("strict", "lenient", "fallback")}


def strength_for(oem_count: int, total_oems: int, parser_counts: Mapping[str, int]) -> tuple[str, str]:
    """Assign a qualitative label from independent support, never row volume."""

    if oem_count >= 4 and oem_count * 4 >= total_oems:
        strength = "strong"
    elif oem_count >= 3:
        strength = "moderate"
    elif oem_count == 2:
        strength = "weak"
    else:
        strength = "insufficient"
    fallback_only = parser_counts["strict"] + parser_counts["lenient"] == 0
    if fallback_only and strength == "strong":
        strength = "moderate"
    qualifier = "fallback-only; capped at moderate" if fallback_only else "cantools-parsed evidence present"
    basis = (
        f"Cross-OEM corpus support: exact structural tuple occurs in {oem_count} of "
        f"{total_oems} independent OEM/families; {qualifier}. This is not decoding "
        f"confidence. Declaration and DBC counts are descriptive only."
    )
    return strength, basis


def pattern_sort_key(pattern: Mapping[str, object]) -> tuple[object, ...]:
    return (
        STRENGTH_ORDER[str(pattern["strength"])],
        -int(pattern["independent_oem_family_count"]),
        int(pattern["bit_length"]),
        str(pattern["signedness"]),
        Decimal(str(pattern["factor"])),
        Decimal(str(pattern["offset"])),
        str(pattern["unit"]),
    )


def build_family(key: str, label: str, rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    oems = sorted({row["oem_family"] for row in rows})
    independent_oems = sorted(set(oems) - NON_INDEPENDENT_FAMILIES)
    pattern_rows: dict[tuple[object, ...], list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        pattern_key = (
            integer_length(row), row["signedness"], row["factor"], row["offset"],
            canonical_unit(row["unit"], key),
        )
        pattern_rows[pattern_key].append(row)

    patterns: list[dict[str, object]] = []
    for (length, signedness, factor, offset, unit), members in pattern_rows.items():
        parser_counts = grouped_counts(members)
        member_oems = sorted({row["oem_family"] for row in members})
        independent_member_oems = sorted(set(member_oems) - NON_INDEPENDENT_FAMILIES)
        strength, basis = strength_for(
            len(independent_member_oems), len(independent_oems), parser_counts
        )
        byte_orders = Counter(row["byte_order"] for row in members)
        origins = Counter(row["origin_type"] for row in members)
        units = Counter(row["unit"] for row in members)
        fallback_modes = Counter(
            row["parser_mode"] for row in members if parser_group(row["parser_mode"]) == "fallback"
        )
        patterns.append(
            {
                "bit_length": length,
                "signedness": signedness,
                "factor": factor,
                "offset": offset,
                "unit": unit,
                "declaration_count": len(members),
                "unique_dbc_count": len({row["dbc_filename"] for row in members}),
                "unique_oem_family_count": len(member_oems),
                "independent_oem_family_count": len(independent_member_oems),
                "parser_counts": parser_counts,
                "strength": strength,
                "strength_basis": basis,
                "byte_order_counts": {
                    name: byte_orders.get(name, 0) for name in ("little_endian", "big_endian")
                },
                "origin_counts": {name: origins.get(name, 0) for name in ("direct", "generated")},
                "fallback_mode_counts": {
                    name: fallback_modes.get(name, 0) for name in ("line-fallback", "line-supplement")
                },
                "unit_variants": dict(sorted(units.items())),
                "oem_families": member_oems,
                "independent_oem_families": independent_member_oems,
            }
        )
    patterns.sort(key=pattern_sort_key)

    origins = Counter(row["origin_type"] for row in rows)
    return {
        "key": key,
        "label": label,
        "declaration_count": len(rows),
        "unique_dbc_count": len({row["dbc_filename"] for row in rows}),
        "unique_oem_family_count": len(oems),
        "independent_oem_family_count": len(independent_oems),
        "parser_counts": grouped_counts(rows),
        "origin_counts": {name: origins.get(name, 0) for name in ("direct", "generated")},
        "oem_families": oems,
        "independent_oem_families": independent_oems,
        "encoding_pattern_count": len(patterns),
        "patterns": patterns,
    }


def common_names(rows: Sequence[Mapping[str, str]], limit: int = 20) -> list[dict[str, object]]:
    counts = Counter(row["signal_name"] for row in rows)
    return [
        {"signal_name": name, "declaration_count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def build_document(rows: Sequence[Mapping[str, str]], source_path: Path, source_hash: str) -> dict[str, object]:
    families: dict[str, object] = {}
    for key, label, matcher in FAMILY_MATCHERS:
        matched = [row for row in rows if matcher(row)]
        families[key] = build_family(key, label, matched)

    torque_rows = [row for row in rows if torque_match(row)]
    torque_buckets: dict[str, list[Mapping[str, str]]] = {key: [] for key in TORQUE_LABELS}
    ambiguous_torque: list[Mapping[str, str]] = []
    for row in torque_rows:
        subgroup = torque_subfamily(row)
        if subgroup is None:
            ambiguous_torque.append(row)
        else:
            torque_buckets[subgroup].append(row)
    families["torque"] = {
        "key": "torque",
        "label": "Torque",
        "classification_precedence": [
            "limit", "request_command", "steering_torque", "engine_torque",
            "motor_torque", "driveline_wheel_torque",
        ],
        "candidate_declaration_count": len(torque_rows),
        "classified_declaration_count": len(torque_rows) - len(ambiguous_torque),
        "ambiguous_unclassified": {
            "declaration_count": len(ambiguous_torque),
            "unique_dbc_count": len({row["dbc_filename"] for row in ambiguous_torque}),
            "unique_oem_family_count": len({row["oem_family"] for row in ambiguous_torque}),
            "common_signal_names": common_names(ambiguous_torque),
        },
        "subfamilies": {
            key: build_family(f"torque.{key}", label, torque_buckets[key])
            for key, label in TORQUE_LABELS.items()
        },
    }

    voltage_rows = [row for row in rows if voltage_match(row)]
    voltage_buckets: dict[str, list[Mapping[str, str]]] = {key: [] for key in VOLTAGE_LABELS}
    for row in voltage_rows:
        voltage_buckets[voltage_subfamily(row)].append(row)
    families["voltage"] = {
        "key": "voltage",
        "label": "Voltage",
        "classification_precedence": [
            "sensor_fine_measurement", "hv_pack", "low_voltage_system",
        ],
        "candidate_declaration_count": len(voltage_rows),
        "classified_declaration_count": len(voltage_rows),
        "subfamilies": {
            key: build_family(f"voltage.{key}", label, voltage_buckets[key])
            for key, label in VOLTAGE_LABELS.items()
        },
    }

    relative_source = source_path.as_posix()
    return {
        "schema_version": 1,
        "title": "opendbc structured encoding priors",
        "source": {
            "signals_tsv": relative_source,
            "sha256": source_hash,
            "declaration_count": len(rows),
            "unique_dbc_count": len({row["dbc_filename"] for row in rows}),
            "unique_oem_family_count": len({row["oem_family"] for row in rows}),
        },
        "methodology": {
            "scope": "Descriptive reference priors only; no hypothesis score or workflow integration.",
            "pattern_identity": ["bit_length", "signedness", "factor", "offset", "canonical_unit"],
            "byte_order_note": (
                "Byte order is reported diagnostically but is not part of pattern identity because it often "
                "reflects an OEM/network convention rather than a physical scaling prior."
            ),
            "variant_handling": (
                "Declaration and unique-DBC counts include indexed direct and generated declarations. "
                "Prior strength uses unique OEM/family support, so repeated generated DBC variants cannot "
                "act as independent cross-OEM evidence."
            ),
            "independence_note": (
                "The literal Unclassified corpus bucket is reported in unique OEM/family counts but is "
                "excluded from independent support used for strength."
            ),
            "parser_groups": {
                "strict": ["cantools-strict"],
                "lenient": ["cantools-lenient"],
                "fallback": ["line-fallback", "line-supplement"],
            },
            "strength_rules": {
                "interpretation": (
                    "Qualitative labels represent cross-OEM corpus support for an exact encoding tuple. "
                    "They are not decoding confidence and cannot confirm a candidate signal."
                ),
                "strong": "At least four independent OEM/families and at least one quarter of family coverage.",
                "moderate": "At least three independent OEM/families, or fallback-only evidence otherwise strong.",
                "weak": "Exactly two independent OEM/families.",
                "insufficient": "One independent OEM/family.",
                "fallback_cap": "Fallback-only patterns cannot be stronger than moderate.",
                "non_evidence": (
                    "Declaration and DBC volume neither increase qualitative strength nor break semantic "
                    "ordering ties."
                ),
            },
            "matching_note": (
                "Conservative naming, unit, and encoding checks are used. Differently named declarations "
                "are not merged unless their family semantics are reasonably supported."
            ),
            "torque_note": (
                "Torque roles are mutually exclusive. Limit and request/command semantics override physical "
                "domain; declarations lacking defensible role/domain context remain explicitly unclassified."
            ),
            "voltage_note": (
                "Voltage roles are mutually exclusive. Explicit sensor/fine-measurement evidence takes "
                "precedence over HV-pack context, then remaining candidates are low-voltage/system."
            ),
        },
        "families": families,
    }


def markdown_cell(value: object) -> str:
    text = str(value) if value != "" else "(blank)"
    return text.replace("|", "\\|").replace("\n", " ")


def render_pattern_table(family: Mapping[str, object]) -> list[str]:
    patterns = family["patterns"]
    assert isinstance(patterns, list)
    if not patterns:
        return ["No declarations matched this family.", ""]
    lines = [
        "| Strength | Bits | Signedness | Factor | Offset | Unit | Declarations | DBCs | OEM/families | Independent | Strict | Lenient | Fallback | Byte order (LE/BE) | OEM/families |",
        "|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for pattern in patterns:
        assert isinstance(pattern, dict)
        parser = pattern["parser_counts"]
        byte_order = pattern["byte_order_counts"]
        assert isinstance(parser, dict) and isinstance(byte_order, dict)
        lines.append(
            "| " + " | ".join(
                markdown_cell(value)
                for value in (
                    pattern["strength"], pattern["bit_length"], pattern["signedness"],
                    pattern["factor"], pattern["offset"], pattern["unit"],
                    pattern["declaration_count"], pattern["unique_dbc_count"],
                    pattern["unique_oem_family_count"], pattern["independent_oem_family_count"],
                    parser["strict"], parser["lenient"],
                    parser["fallback"], f"{byte_order['little_endian']}/{byte_order['big_endian']}",
                    ", ".join(pattern["oem_families"]),
                )
            ) + " |"
        )
    lines.append("")
    return lines


def render_family_section(family: Mapping[str, object], heading_level: int = 2) -> list[str]:
    parser = family["parser_counts"]
    origins = family["origin_counts"]
    assert isinstance(parser, dict) and isinstance(origins, dict)
    lines = [
        f"{'#' * heading_level} {family['label']}",
        "",
        (
            f"{family['declaration_count']} declarations; {family['unique_dbc_count']} unique DBCs; "
            f"{family['unique_oem_family_count']} unique OEM/families "
            f"({family['independent_oem_family_count']} independent for strength); "
            f"parser evidence strict/lenient/fallback = {parser['strict']}/{parser['lenient']}/{parser['fallback']}; "
            f"provenance direct/generated = {origins['direct']}/{origins['generated']}."
        ),
        "",
    ]
    lines.extend(render_pattern_table(family))
    return lines


def render_markdown(document: Mapping[str, object]) -> str:
    source = document["source"]
    methodology = document["methodology"]
    families = document["families"]
    assert isinstance(source, dict) and isinstance(methodology, dict) and isinstance(families, dict)
    lines = [
        "# opendbc structured encoding priors",
        "",
        (
            f"Source: `{source['signals_tsv']}` (`sha256:{source['sha256']}`). "
            f"The index contains {source['declaration_count']} signal declarations across "
            f"{source['unique_dbc_count']} DBCs and {source['unique_oem_family_count']} OEM/families."
        ),
        "",
        "These artifacts are descriptive reference data only. They do not change physical-signal scoring or the reverse-engineering workflow.",
        "",
        "## Method",
        "",
        "An encoding pattern is the exact tuple `(bit length, signedness, factor, offset, canonical unit)`. Byte order is shown as a diagnostic count but is not part of pattern identity.",
        "",
        (
            "Strength is based on independent OEM/family support: **strong** requires at least four OEM/families "
            "and one-quarter family coverage; **moderate** requires at least three; **weak** means two; "
            "**insufficient** means one. Fallback-only evidence is capped at moderate. Declaration and DBC "
            "volume never increases strength, so repeated generated variants cannot manufacture cross-OEM support."
        ),
        "",
        "These labels describe cross-OEM corpus support for an exact tuple. They are not decoding confidence and cannot confirm that a hypothesis is correct.",
        "",
        "The literal `Unclassified` corpus bucket is retained in reported unique-family counts but excluded from independent support used for strength.",
        "",
        "Strict and lenient counts come from cantools parsing. Fallback combines line-fallback and line-supplement evidence.",
        "",
    ]

    for key, _, _ in FAMILY_MATCHERS:
        family = families[key]
        assert isinstance(family, dict)
        lines.extend(render_family_section(family))

    torque = families["torque"]
    assert isinstance(torque, dict)
    ambiguous = torque["ambiguous_unclassified"]
    assert isinstance(ambiguous, dict)
    lines.extend(
        [
            "## Torque",
            "",
            (
                f"{torque['candidate_declaration_count']} conservative torque candidates were found; "
                f"{torque['classified_declaration_count']} were assigned once to a requested subtype. "
                f"{ambiguous['declaration_count']} declarations remain explicitly ambiguous/unclassified "
                f"({ambiguous['unique_dbc_count']} DBCs, {ambiguous['unique_oem_family_count']} OEM/families)."
            ),
            "",
            "Classification precedence is limit → request/command → steering → engine → motor → driveline/wheel. Role semantics therefore override physical domain.",
            "",
        ]
    )
    torque_subfamilies = torque["subfamilies"]
    assert isinstance(torque_subfamilies, dict)
    for key in TORQUE_LABELS:
        family = torque_subfamilies[key]
        assert isinstance(family, dict)
        lines.extend(render_family_section(family, 3))

    voltage = families["voltage"]
    assert isinstance(voltage, dict)
    lines.extend(
        [
            "## Voltage",
            "",
            (
                f"{voltage['candidate_declaration_count']} conservative voltage candidates were found and "
                "assigned once to a requested subtype. Classification precedence is sensor/fine measurement "
                "→ HV pack → low-voltage/system."
            ),
            "",
        ]
    )
    voltage_subfamilies = voltage["subfamilies"]
    assert isinstance(voltage_subfamilies, dict)
    for key in VOLTAGE_LABELS:
        family = voltage_subfamilies[key]
        assert isinstance(family, dict)
        lines.extend(render_family_section(family, 3))

    return "\n".join(lines).rstrip() + "\n"


def ensure_output_is_separate(signals_path: Path, output_dir: Path) -> None:
    source_dir = signals_path.resolve().parent
    output = output_dir.resolve()
    try:
        output.relative_to(source_dir)
    except ValueError:
        return
    raise ValueError(
        f"refusing to write outputs inside the input artifact directory: {source_dir}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_if_changed(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    path.write_bytes(encoded)


def selftest() -> None:
    base = {
        "category": "powertrain", "origin_type": "direct", "message_name": "EngineData",
        "can_id": "0x100", "start_bit": "0", "byte_order": "little_endian",
        "minimum": "0", "maximum": "8000", "parser_mode": "cantools-strict",
    }
    rows = []
    for oem in ("A", "B", "C", "D"):
        row = dict(base)
        row.update(
            oem_family=oem, dbc_filename=f"{oem.lower()}.dbc", signal_name="EngineRPM",
            length="16", signedness="unsigned", factor="0.25", offset="0", unit="rpm",
        )
        rows.append(row)
    family = build_family("engine_rpm", "Engine RPM", rows)
    assert family["patterns"][0]["strength"] == "strong"
    generated_duplicate = dict(rows[0], dbc_filename="a_generated.dbc", origin_type="generated")
    family = build_family("engine_rpm", "Engine RPM", [rows[0], generated_duplicate])
    assert family["patterns"][0]["strength"] == "insufficient"
    fallback_rows = [dict(row, parser_mode="line-fallback") for row in rows]
    family = build_family("engine_rpm", "Engine RPM", fallback_rows)
    assert family["patterns"][0]["strength"] == "moderate"
    assert canonical_unit("Unit_PerCent", "pedal_throttle") == "%"
    assert canonical_unit("Unit_DegreCelsi", "temperature") == "degC"
    assert canonical_unit("Unit_MilliVolt", "voltage.low_voltage_system") == "mV"
    assert not temperature_match(dict(rows[0], signal_name="SCR_Temp_Aktivtank", unit="Unit_MilliVolt", length="8"))
    assert pedal_match(dict(rows[0], signal_name="MO_Fahrpedalrohwert_01", length="8"))
    assert vehicle_speed_match(dict(rows[0], signal_name="VehicleSpeed2", length="16"))
    assert not vehicle_speed_match(dict(rows[0], signal_name="ROUGH_CAR_SPEED", length="8"))
    assert steering_angle_match(dict(rows[0], signal_name="STEER_ANGLE", unit="", length="16"))
    assert not steering_angle_match(dict(rows[0], signal_name="STEER_ANGLE_9000", unit="", length="16"))
    assert steering_angle_match(dict(rows[0], signal_name="CAN_RX_STEERING_ANGLE", unit="degree", length="16"))
    assert not torque_match(dict(rows[0], signal_name="CHKSM_TORQ_2_DME", length="8"))
    assert torque_subfamily(dict(rows[0], signal_name="CR_Mdps_StrTq", message_name="MDPS")) == "steering_torque"
    assert torque_subfamily(dict(rows[0], signal_name="AwdLck_Tq_Actl", message_name="AWD")) == "driveline_wheel_torque"
    assert torque_subfamily(dict(rows[0], signal_name="UnqualifiedTorque", message_name="Generic")) is None
    sort_pattern = {
        "strength": "weak", "independent_oem_family_count": 2, "bit_length": 8,
        "signedness": "unsigned", "factor": "1", "offset": "0", "unit": "",
        "declaration_count": 1, "unique_dbc_count": 1,
    }
    volume_variant = dict(sort_pattern, declaration_count=100, unique_dbc_count=50)
    assert pattern_sort_key(sort_pattern) == pattern_sort_key(volume_variant)
    assert voltage_subfamily(dict(rows[0], signal_name="PackVoltage", message_name="HVPack", factor="0.1", maximum="500")) == "hv_pack"
    assert voltage_subfamily(dict(rows[0], signal_name="BatSensorVoltage", factor="0.001")) == "sensor_fine_measurement"
    print("opendbc_priors self-test: ok")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS, help="input signals.tsv")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")
    parser.add_argument("--selftest", action="store_true", help="run in-memory checks and exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        selftest()
        return 0
    if not args.signals.is_file():
        print(f"error: input not found: {args.signals}", file=sys.stderr)
        return 2
    try:
        ensure_output_is_separate(args.signals, args.out_dir)
        source_hash = sha256_file(args.signals)
        rows, _ = read_rows(args.signals)
        document = build_document(rows, args.signals, source_hash)
        json_text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        markdown_text = render_markdown(document)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        write_if_changed(args.out_dir / JSON_NAME, json_text)
        write_if_changed(args.out_dir / MARKDOWN_NAME, markdown_text)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out_dir / JSON_NAME}")
    print(f"wrote {args.out_dir / MARKDOWN_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
