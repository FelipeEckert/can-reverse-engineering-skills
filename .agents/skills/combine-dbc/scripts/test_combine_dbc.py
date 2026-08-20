from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import cantools
from cantools.database.can import Message, Signal


SCRIPT = Path(__file__).resolve().with_name("combine_dbc.py")
SPEC = importlib.util.spec_from_file_location("combine_dbc", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
combine_dbc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(combine_dbc)


def signal(name: str, start: int, length: int, byte_order: str, **kwargs) -> Signal:
    return Signal(
        name=name,
        start=start,
        length=length,
        byte_order=byte_order,
        is_signed=False,
        **kwargs,
    )


class CombineDbcTests(unittest.TestCase):
    def assert_geometry_round_trip(
        self,
        signals: list[Signal],
        expected_length: int,
        values: dict[str, int],
    ) -> None:
        base = Message(0x123, "Geometry", 0, signals=[])
        rebuilt = combine_dbc._rebuild_message(base, signals)
        self.assertEqual(rebuilt.length, expected_length)

        dbc_text = cantools.database.Database(messages=[rebuilt]).as_dbc_string()
        parsed = cantools.database.load_string(
            dbc_text,
            database_format="dbc",
            strict=True,
        ).get_message_by_name("Geometry")
        encoded = parsed.encode(values)
        self.assertEqual(len(encoded), expected_length)
        self.assertEqual(parsed.decode(encoded), values)

    def test_byte_order_aware_geometry(self) -> None:
        cases = [
            (
                "LE start 7 length 16",
                [signal("LE16", 7, 16, "little_endian")],
                3,
                {"LE16": 0xA5C3},
            ),
            (
                "BE start 7 length 16",
                [signal("BE16", 7, 16, "big_endian")],
                2,
                {"BE16": 0xA5C3},
            ),
            (
                "BE start 15 length 16",
                [signal("BE16B1", 15, 16, "big_endian")],
                3,
                {"BE16B1": 0xA5C3},
            ),
            (
                "BE start 0 length 8",
                [signal("BE8", 0, 8, "big_endian")],
                2,
                {"BE8": 0x5A},
            ),
            (
                "mixed LE 0:8 and BE 15:16",
                [
                    signal("LE8", 0, 8, "little_endian"),
                    signal("BE16B1", 15, 16, "big_endian"),
                ],
                3,
                {"LE8": 0x12, "BE16B1": 0x3456},
            ),
            (
                "mixed LE 0:8 and BE 8:8",
                [
                    signal("LE8", 0, 8, "little_endian"),
                    signal("BE8B1LSB", 8, 8, "big_endian"),
                ],
                3,
                {"LE8": 0x12, "BE8B1LSB": 0x34},
            ),
        ]
        for label, signals, expected_length, values in cases:
            with self.subTest(label):
                self.assert_geometry_round_trip(signals, expected_length, values)

    def test_existing_message_is_not_shrunk(self) -> None:
        existing = Message(
            0x123,
            "ExistingLength",
            8,
            signals=[signal("Old", 0, 8, "little_endian")],
        )
        rebuilt = combine_dbc._rebuild_message(
            existing,
            [signal("Replacement", 7, 16, "big_endian")],
        )
        self.assertEqual(rebuilt.length, 8)

    def test_incoming_declared_length_is_not_shrunk(self) -> None:
        existing = Message(0x123, "Existing", 2, signals=[])
        rebuilt = combine_dbc._rebuild_message(
            existing,
            [signal("Small", 0, 8, "little_endian")],
            minimum_length=6,
        )
        self.assertEqual(rebuilt.length, 6)

    def test_extended_frame_and_message_metadata_are_preserved(self) -> None:
        original_signal = signal("Value", 0, 8, "little_endian")
        original = Message(
            frame_id=0x18FF50E5,
            name="Metadata",
            length=8,
            signals=[original_signal],
            header_id=0x123456,
            header_byte_order="little_endian",
            unused_bit_pattern=0xAA,
            comment={None: "Primary comment", "DE": "Kommentar"},
            senders=["ECU_A", "ECU_B"],
            send_type="cyclic",
            cycle_time=100,
            is_extended_frame=True,
            is_fd=True,
            bus_name="CAN_A",
        )

        rebuilt = combine_dbc._rebuild_message(original, [original_signal])

        self.assertEqual(rebuilt.frame_id, original.frame_id)
        self.assertTrue(rebuilt.is_extended_frame)
        self.assertEqual(rebuilt.name, original.name)
        self.assertEqual(rebuilt.length, original.length)
        self.assertEqual(rebuilt.comments, original.comments)
        self.assertEqual(rebuilt.senders, original.senders)
        self.assertEqual(rebuilt.send_type, original.send_type)
        self.assertEqual(rebuilt.cycle_time, original.cycle_time)
        self.assertEqual(rebuilt.protocol, original.protocol)
        self.assertEqual(rebuilt.is_fd, original.is_fd)
        self.assertEqual(rebuilt.header_id, original.header_id)
        self.assertEqual(rebuilt.header_byte_order, original.header_byte_order)
        self.assertEqual(rebuilt.unused_bit_pattern, original.unused_bit_pattern)
        self.assertEqual(rebuilt.bus_name, original.bus_name)

        dbc_text = cantools.database.Database(messages=[rebuilt]).as_dbc_string()
        parsed = cantools.database.load_string(
            dbc_text,
            database_format="dbc",
            strict=True,
        ).get_message_by_frame_id(original.frame_id)
        self.assertTrue(parsed.is_extended_frame)
        self.assertEqual(parsed.comment, original.comment)
        self.assertEqual(parsed.senders, original.senders)
        self.assertEqual(parsed.cycle_time, original.cycle_time)
        self.assertEqual(parsed.encode({"Value": 0x5A})[0], 0x5A)

    def test_duplicate_signal_is_replaced_and_other_signals_are_merged(self) -> None:
        first = Message(
            0x123,
            "Merged",
            2,
            signals=[
                signal("Keep", 0, 8, "little_endian", comment="keep metadata"),
                signal("Shared", 8, 8, "little_endian", comment="old metadata"),
            ],
        )
        second = Message(
            0x123,
            "Incoming",
            4,
            signals=[
                signal("Shared", 16, 8, "little_endian", comment="new metadata"),
                signal("Added", 24, 8, "little_endian"),
            ],
        )

        with tempfile.TemporaryDirectory(prefix="combine_dbc_test_") as directory:
            root = Path(directory)
            paths = []
            for index, message in enumerate((first, second)):
                path = root / f"input_{index}.dbc"
                path.write_text(
                    cantools.database.Database(messages=[message]).as_dbc_string(),
                    encoding="utf-8",
                    newline="",
                )
                paths.append(path)
            merged_database = combine_dbc.merge_databases(paths)
            repeated_database = combine_dbc.merge_databases(paths)
            merged = merged_database.get_message_by_frame_id(0x123)
            self.assertEqual(
                merged_database.as_dbc_string(),
                repeated_database.as_dbc_string(),
            )

        self.assertEqual(
            [item.name for item in merged.signals],
            ["Keep", "Shared", "Added"],
        )
        self.assertEqual(merged.get_signal_by_name("Shared").start, 16)
        self.assertEqual(merged.get_signal_by_name("Shared").comment, "new metadata")
        self.assertEqual(merged.get_signal_by_name("Keep").comment, "keep metadata")
        self.assertEqual(merged.length, 4)

    def test_dbc_can_fd_and_protocol_metadata_survive_output_round_trip(self) -> None:
        template = '''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 2566869221 Metadata: 8 ECU
 SG_ Value : 0|8@1+ (1,0) [0|255] "" ECU
BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN","ExtendedCAN","StandardCAN_FD","ExtendedCAN_FD","J1939PG";
BA_DEF_DEF_ "VFrameFormat" "";
BA_ "VFrameFormat" BO_ 2566869221 {frame_format};
'''
        cases = [
            ("Extended CAN FD", 3, True, None),
            ("J1939", 4, False, "j1939"),
        ]
        with tempfile.TemporaryDirectory(prefix="combine_dbc_metadata_") as directory:
            root = Path(directory)
            for label, frame_format, is_fd, protocol in cases:
                with self.subTest(label):
                    path = root / f"metadata_{frame_format}.dbc"
                    path.write_text(
                        template.format(frame_format=frame_format),
                        encoding="utf-8",
                        newline="",
                    )
                    combined = combine_dbc.merge_databases([path])
                    reparsed = cantools.database.load_string(
                        combined.as_dbc_string(),
                        database_format="dbc",
                        strict=True,
                    ).get_message_by_frame_id(0x18FF50E5)
                    self.assertTrue(reparsed.is_extended_frame)
                    self.assertEqual(reparsed.is_fd, is_fd)
                    self.assertEqual(reparsed.protocol, protocol)
                    self.assertEqual(reparsed.encode({"Value": 0x5A})[0], 0x5A)


if __name__ == "__main__":
    unittest.main()
