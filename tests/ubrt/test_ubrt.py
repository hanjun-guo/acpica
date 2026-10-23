#!/usr/bin/env python3
"""Run with: python3 tests/ubrt/test_ubrt.py /path/to/iasl -v."""

import pathlib
import random
import re
import struct
import subprocess
import sys
import tempfile
import unittest

IASL = str(pathlib.Path(sys.argv.pop(1)).resolve())
# Spec 1.0, Tables 3-8: fixed header, record size, count offset, template file.
LAYOUTS = {
    "ubc": (56, 384, 54, "ubrt-ubc"),
    "ummu": (40, 160, 32, "ubrt-ummu"),
    "ub_reserved_mem": (40, 24, 32, "ubrt-reserved-mem"),
}


def checksum(data):
    padded = bytes(data) + bytes(-len(data) % 4)
    return sum(word[0] for word in struct.iter_unpack("<I", padded)) & 0xFFFFFFFF


def fixture(name, count=1, unused=b""):
    fixed, size, offset, _ = LAYOUTS[name]
    data = bytearray(fixed + size * count)
    data[:16] = name.encode().ljust(16, b"\0")
    data[20] = 1
    struct.pack_into("<H", data, offset, count)
    for i in range(count):
        start = fixed + size * i
        if name == "ubc":
            struct.pack_into("<IIQQ", data, start, 16 + i, 32 + i,
                             0x1234567800000000 + 0x10000 * i, 0x10000)
            data[start + 24] = 48
            data[start + 25] = 1
            struct.pack_into("<HH", data, start + 26, i, 2)
            struct.pack_into("<QQHHB", data, start + 32,
                             0xFEDCBA9800000000, 0x10000, 16, 30, 3)
            data[start + 112:start + 128] = bytes(range(16))
            data[start + 128:start + 384] = bytes(range(256))
        elif name == "ummu":
            struct.pack_into("<QQ", data, start, 0x2345678900000000, 0x10000)
            struct.pack_into("<H", data, start + 20, 0xFFFF)
            struct.pack_into("<QQ", data, start + 24, 0x3456789000000000, 0x10000)
            struct.pack_into("<II", data, start + 44, 1, 0x12345678)
            struct.pack_into("<H", data, start + 78, 0x1234)
            data[start + 80:start + 160] = bytes(range(80))
        else:
            data[start] = 1
            struct.pack_into("<QQ", data, start + 8, 0x100000000 + 0x10000 * i, 0x10000)
    data += unused
    struct.pack_into("<I", data, 16, len(data))
    struct.pack_into("<I", data, 24, len(unused))
    struct.pack_into("<I", data, 28, -checksum(data[:len(data) - len(unused)]) & 0xFFFFFFFF)
    return data


class UbrtTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ubrt-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def run_iasl(self, *args, ok=True, input_text=""):
        result = subprocess.run([IASL, *args], cwd=self.root,
                                capture_output=True, text=True,
                                input=input_text, timeout=90)
        output = result.stdout + result.stderr
        self.assertGreaterEqual(result.returncode, 0, output)
        self.assertNotIn("AddressSanitizer", output)
        self.assertNotIn("runtime error:", output)
        self.assertEqual(result.returncode == 0, ok, output)
        return output

    def roundtrip(self, data, expected=None):
        (self.root / "table.dat").write_bytes(data)
        self.run_iasl("-d", "table.dat")
        text = (self.root / "table.dsl").read_text()
        self.run_iasl("-p", "rebuilt", "table.dsl")
        self.assertEqual((self.root / "rebuilt.aml").read_bytes(),
                         data if expected is None else expected)
        return text

    def test_records_and_native_header(self):
        for name in LAYOUTS:
            for count in (0, 1, 3):
                with self.subTest(name=name, count=count):
                    data = fixture(name, count)
                    text = self.roundtrip(data)
                    self.assertRegex(text, rf'Table Name\s*:\s*"{name}"')
                    self.assertNotIn("OEM Table ID", text)
                    self.assertEqual(checksum(data), 0)

    def test_native_header_is_not_printed_as_acpi(self):
        for name in LAYOUTS:
            with self.subTest(name=name):
                (self.root / "native.dat").write_bytes(fixture(name))
                output = self.run_iasl("-d", "native.dat")
                self.assertNotRegex(output,
                    r"(?m)^ACPI: .{4} 0x[0-9A-F]{16} ")
                self.assertIn(f"Acpi Data Table [{name}] decoded", output)

    def test_unused_capacity_is_zero_filled(self):
        rng = random.Random(0x55425254)
        for name in LAYOUTS:
            for length in (0, 1, 2, 3, 4, 17, 33):
                with self.subTest(name=name, length=length):
                    unused = bytes(rng.randrange(256) for _ in range(length))
                    data = fixture(name, 2, unused)
                    expected = fixture(name, 2, bytes(length))
                    text = self.roundtrip(data, expected)
                    valid_length = len(data) - length
                    self.assertNotIn("Valid Data", text)
                    self.assertNotIn("Unused Data", text)
                    self.assertEqual(data[:valid_length], expected[:valid_length])
                    self.assertEqual(checksum(expected[:valid_length]), 0)
                    self.roundtrip(expected)

    def test_remaining_size_generates_zero_padding(self):
        for name in LAYOUTS:
            for count in (0, 2):
                text = self.roundtrip(fixture(name, count))
                valid_length = len(fixture(name, count))
                for remaining in (0, 1, 3, 4, 64, 65536):
                    with self.subTest(name=name, count=count, remaining=remaining):
                        source = re.sub(r"(Remaining Size\s*:)\s*[0-9A-F]+",
                                        rf"\g<1> {remaining:08X}", text)
                        source = re.sub(r"(Total Size\s*:)\s*[0-9A-F]+",
                                        rf"\g<1> {valid_length + remaining:08X}", source)
                        (self.root / "capacity.asl").write_text(source)
                        self.run_iasl("capacity.asl")
                        data = (self.root / "capacity.aml").read_bytes()
                        self.assertEqual(data, fixture(name, count, unused=bytes(remaining)))

    def test_source_capacity_validation(self):
        for name, (fixed, _, _, _) in LAYOUTS.items():
            for count in (0, 1):
                text = self.roundtrip(fixture(name, count))
                valid_length = len(fixture(name, count))
                for total, remaining in ((0, 0), (31, 0), (fixed - 1, 0),
                        (valid_length, valid_length + 1), (valid_length, valid_length),
                        (valid_length - 1, 0), (valid_length + 1, 0), (valid_length, 1),
                        (0xFFFFFFFF, 0xFFFFFFFF), (0xFFFFFFFF, 0x100000000 - fixed),
                        (0xFFFFFFFF, 0)):
                    with self.subTest(name=name, count=count,
                                      total=total, remaining=remaining):
                        source = re.sub(r"(Total Size\s*:)\s*[0-9A-F]+",
                                        rf"\g<1> {total:08X}", text)
                        source = re.sub(r"(Remaining Size\s*:)\s*[0-9A-F]+",
                                        rf"\g<1> {remaining:08X}", source)
                        (self.root / "bad-capacity.asl").write_text(source)
                        output = self.run_iasl("bad-capacity.asl", ok=False)
                        self.assertIn("UBRT", output)

    def test_total_size_includes_unused_capacity(self):
        data = fixture("ummu", 1, bytes(64))
        self.assertEqual(len(data), 264)
        self.assertEqual(struct.unpack_from("<I", data, 16)[0], 264)
        text = self.roundtrip(data)
        self.assertRegex(text, r"Total Size\s*:\s*00000108")
        self.assertRegex(text, r"Remaining Size\s*:\s*00000040")
        self.assertEqual(checksum(data[:200]), 0)

    def test_unused_bytes_do_not_affect_checksum(self):
        for name in LAYOUTS:
            data = fixture(name, unused=b"\xA5\x5A\xFF\x80")
            (self.root / "unused.dat").write_bytes(data)
            output = self.run_iasl("-d", "unused.dat")
            self.assertNotIn("invalid checksum", output)
            text = (self.root / "unused.dsl").read_text()
            self.assertNotIn("Unused Data", text)
            self.run_iasl("unused.dsl")
            rebuilt = (self.root / "unused.aml").read_bytes()
            self.assertEqual(rebuilt[:-4], data[:-4])
            self.assertEqual(rebuilt[-4:], bytes(4))

    def test_ubc_decoded_flags_roundtrip(self):
        for features in range(4):
            for attributes in range(4):
                with self.subTest(features=features, attributes=attributes):
                    data = fixture("ubc", 2)
                    data[48] = features
                    # Different attributes in adjacent records catch offset errors.
                    data[56 + 52] = attributes
                    data[56 + 384 + 52] = attributes ^ 3
                    struct.pack_into("<I", data, 28, 0)
                    struct.pack_into("<I", data, 28, -checksum(data) & 0xFFFFFFFF)
                    text = self.roundtrip(data)
                    for label, value in (("MMIO Token Value", features & 1),
                                         ("MCTP over UB", features >> 1)):
                        self.assertRegex(text, rf"{label}\s*:\s*{value}\b")
                    for label, shift in (("Edge Triggered", 0), ("Active Low", 1)):
                        values = re.findall(rf"{label}\s*:\s*([01])\b", text)
                        self.assertEqual(values, [str((attributes >> shift) & 1),
                                                 str(((attributes ^ 3) >> shift) & 1)])

    def test_ubc_compile_decoded_flags(self):
        self.run_iasl("-T", "UBRT")
        template = (self.root / "ubrt-ubc.asl").read_text()
        fields = ("MMIO Token Value", "MCTP over UB", "Edge Triggered", "Active Low")
        for features in range(4):
            for attributes in range(4):
                with self.subTest(features=features, attributes=attributes):
                    text = template
                    # Master bytes are informational, not the compiler input.
                    for label in ("Feature Sets", "MSG Queue Interrupt Attribute"):
                        pattern = rf"({label} \(decoded below\)\s*:)\s*[0-9A-F]+"
                        text, changed = re.subn(pattern, r"\g<1> FF", text)
                        self.assertEqual(changed, 1)
                    values = (features & 1, features >> 1, attributes & 1, attributes >> 1)
                    for label, value in zip(fields, values):
                        pattern = rf"({label}\s*:)\s*[0-9A-F]+"
                        text, changed = re.subn(pattern, rf"\g<1> {value}", text)
                        self.assertEqual(changed, 1)
                    (self.root / "flags.asl").write_text(text)
                    self.run_iasl("flags.asl")
                    data = (self.root / "flags.aml").read_bytes()
                    self.assertEqual(len(data), 440)
                    self.assertEqual(data[48], features)
                    self.assertEqual(data[56 + 52], attributes)
                    self.assertEqual(struct.unpack_from("<H", data, 54)[0], 1)
                    self.assertEqual(checksum(data), 0)

        for label in fields:
            with self.subTest(invalid_bit=label):
                pattern = rf"({label}\s*:)\s*[0-9A-F]+"
                text, changed = re.subn(pattern, r"\g<1> 2", template)
                self.assertEqual(changed, 1)
                (self.root / "bad-flags.asl").write_text(text)
                output = self.run_iasl("bad-flags.asl", ok=False)
                self.assertIn("Maximum 1 bit", output)

    def test_templates_and_generated_counts_checksum(self):
        self.run_iasl("-T", "UBRT")
        for name, (fixed, size, offset, filename) in LAYOUTS.items():
            source = self.root / (filename + ".asl")
            text = source.read_text()
            self.assertIn(f"Template for [UBRT/{name}] native subtable", text)
            self.assertNotIn("Raw Table Data:", text)
            expected = bytearray(fixed + size)
            expected[:16] = name.encode().ljust(16, b"\0")
            expected[20] = 1
            struct.pack_into("<I", expected, 16, len(expected))
            struct.pack_into("<H", expected, offset, 1)
            value = -checksum(expected) & 0xFFFFFFFF
            struct.pack_into("<I", expected, 28, value)
            self.assertRegex(text, rf"Checksum\s*:\s*{value:08X}")
            self.assertNotIn("-cu", text)
            self.assertRegex(text, rf'Table Name\s*:\s*"{name}"')
            self.run_iasl(source.name)
            self.assertEqual(source.with_suffix(".aml").read_bytes(), expected)
            text = re.sub(r"(Count\s*:)\s*[0-9A-F]+", r"\g<1> FFFF", text)
            text = re.sub(r"(Checksum\s*:)\s*[0-9A-F]+", r"\g<1> 12345678", text)
            source.write_text(text)
            self.run_iasl(source.name)
            data = source.with_suffix(".aml").read_bytes()
            self.assertEqual(len(data), fixed + size)
            self.assertEqual(struct.unpack_from("<I", data, 16)[0], len(data))
            self.assertEqual(struct.unpack_from("<H", data, offset)[0], 1)
            self.assertEqual(data[:16], name.encode().ljust(16, b"\0"))
            self.assertEqual(checksum(data), 0)
            self.roundtrip(data)

    def test_no_subtable_template_aliases(self):
        for name, (_, _, _, filename) in LAYOUTS.items():
            for alias in (filename.upper(), name.upper(), "UBRT/" + name):
                with self.subTest(alias=alias):
                    output = self.run_iasl("-T", alias, ok=False)
                    self.assertRegex(output, r"(?:Invalid|Unrecognized) ACPI table signature")

    def test_companion_templates_are_scoped_to_parent(self):
        self.run_iasl("-T", "FPDT", "S3PT", "MADT", "FADT")
        self.assertEqual({path.name for path in self.root.glob("*.asl")},
                         {"fpdt.asl", "s3pt.asl", "apic.asl", "facp.asl"})
        self.run_iasl("fpdt.asl", "s3pt.asl", "apic.asl", "facp.asl")
        self.run_iasl("-T", "UBRT")
        for name, (_, _, _, filename) in LAYOUTS.items():
            text = (self.root / (filename + ".asl")).read_text()
            self.assertIn(f"[UBRT/{name}]", text)
        self.assertNotIn("native subtable", (self.root / "ubrt.asl").read_text())

    def test_all_templates_include_companions_once(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                self.root = pathlib.Path(self.temp.name) / str(verbose)
                self.root.mkdir()
                options = ["-vt"] if verbose else []
                output = self.run_iasl(*options, "-T", "ALL")
                self.assertEqual(output.count('written to "ubrt.asl"'), 1)
                for name, (fixed, size, offset, filename) in LAYOUTS.items():
                    self.assertEqual(output.count(f'written to "{filename}.asl"'), 1)
                    source = self.root / (filename + ".asl")
                    text = source.read_text()
                    self.assertIn(f"[UBRT/{name}]", text)
                    self.assertEqual("Raw Table Data:" in text, verbose)
                    self.run_iasl(source.name)
                    data = source.with_suffix(".aml").read_bytes()
                    self.assertEqual(len(data), fixed + size)
                    self.assertEqual(data[:16], name.encode().ljust(16, b"\0"))
                    self.assertEqual(struct.unpack_from("<H", data, offset)[0], 1)
                    self.assertEqual(checksum(data), 0)
                    self.roundtrip(data)
                self.run_iasl("ubrt.asl", "cdat.asl", "s3pt.asl",
                              "facs.asl", "rsdp.asl", "dsdt.asl", "ssdt.asl")

    def test_skipping_root_template_still_creates_companions(self):
        source = self.root / "ubrt.asl"
        source.write_text("Keep the existing root template.\n")
        self.run_iasl("-T", "UBRT", input_text="n\n")
        self.assertEqual(source.read_text(), "Keep the existing root template.\n")
        for _, _, _, filename in LAYOUTS.values():
            self.assertTrue((self.root / (filename + ".asl")).is_file())
            self.run_iasl(filename + ".asl")

    def test_skipping_companion_template_does_not_skip_siblings(self):
        source = self.root / "ubrt-ubc.asl"
        source.write_text("Keep the existing UBC template.\n")
        self.run_iasl("-T", "UBRT", input_text="n\n")
        self.assertEqual(source.read_text(), "Keep the existing UBC template.\n")
        self.run_iasl("ubrt.asl", "ubrt-ummu.asl", "ubrt-reserved-mem.asl")

    def test_companion_open_failure_stops_generation(self):
        (self.root / "ubrt-ummu.asl").mkdir()
        output = self.run_iasl("-T", "UBRT", input_text="y\n", ok=False)
        self.assertIn("Could not open output file ubrt-ummu.asl", output)
        self.assertTrue((self.root / "ubrt.asl").is_file())
        self.assertTrue((self.root / "ubrt-ubc.asl").is_file())
        self.assertFalse((self.root / "ubrt-reserved-mem.asl").exists())

    @unittest.skipUnless(pathlib.Path("/dev/full").exists(), "requires /dev/full")
    def test_companion_write_failure_is_reported(self):
        (self.root / "ubrt-ubc.asl").symlink_to("/dev/full")
        output = self.run_iasl("-T", "UBRT", input_text="y\n", ok=False)
        self.assertRegex(output, r"Could not (?:write|close) output file ubrt-ubc.asl")
        self.assertNotIn('written to "ubrt-ubc.asl"', output)
        self.assertFalse((self.root / "ubrt-ummu.asl").exists())

    def test_default_and_multiple_ssdt_templates(self):
        self.run_iasl("-T")
        source = self.root / "dsdt.asl"
        self.assertEqual(source.read_text().count("DefinitionBlock"), 1)
        self.run_iasl("-T", "2", input_text="y\n")
        self.assertEqual(source.read_text().count("DefinitionBlock"), 3)
        self.assertEqual({path.name for path in self.root.glob("*.asl")}, {"dsdt.asl"})
        self.run_iasl("dsdt.asl")

    def test_vendor_buffers_have_fixed_sizes(self):
        segments = {"ubc": ((0, 128), (128, 128)),
                    "ummu": ((0, 32), (32, 32), (64, 16))}
        for name, chunks in segments.items():
            text = self.roundtrip(fixture(name, 2))
            for offset, size in chunks:
                label = f"Vendor Info {offset}-{offset + size - 1}"
                pattern = (rf"(\[[^\n]*\]\s*{re.escape(label)}\s*:)[^\n]*"
                           r"(?:\n\s*[0-9A-F]{2}(?:\s+[0-9A-F]{2})*[^\n]*)*")
                for count in (1, size - 1, size + 1):
                    with self.subTest(name=name, field=label, count=count):
                        bad, changed = re.subn(pattern, lambda m: m[1] + " 5A" * count, text)
                        self.assertEqual(changed, 2)
                        (self.root / "bad.asl").write_text(bad)
                        self.run_iasl("bad.asl", ok=False)

    def test_name_string_identity(self):
        for name in LAYOUTS:
            text = self.roundtrip(fixture(name))
            for invalid in ("", "u", "ub", name.upper(), name + "x",
                            name + " ", "x" * 17):
                with self.subTest(name=name, invalid=invalid):
                    bad, changed = re.subn(r'(Table Name\s*:) [^\n]*',
                                          lambda m: m[1] + ' "' + invalid + '"', text)
                    self.assertEqual(changed, 1)
                    (self.root / "bad-name.asl").write_text(bad)
                    output = self.run_iasl("bad-name.asl", ok=False)
                    self.assertIn('expected "Signature"', output)

    def test_buffer_elements_report_errors(self):
        # Exercise the unchanged upstream buffer compiler's diagnostic path.
        text = self.roundtrip(fixture("ummu"))
        pattern = r"(Vendor Info 64-79\s*:)[^\n]*"
        for position in (0, 7, 15):
            for value in ("000", "GG"):
                with self.subTest(position=position, value=value):
                    tokens = ["00"] * 16
                    tokens[position] = value
                    bad, changed = re.subn(pattern, lambda m: m[1] + " " + " ".join(tokens), text)
                    self.assertEqual(changed, 1)
                    (self.root / "bad-buffer.asl").write_text(bad)
                    output = self.run_iasl("bad-buffer.asl", ok=False)
                    self.assertIn("Invalid element in buffer initializer list", output)

    def test_name_trailing_bytes_are_ignored(self):
        for name in LAYOUTS:
            data = fixture(name, 0)
            for offset in range(len(name) + 1, 16):
                data[offset] = 0x80 + offset
            struct.pack_into("<I", data, 28, 0)
            struct.pack_into("<I", data, 28, -checksum(data) & 0xFFFFFFFF)
            text = self.roundtrip(data, fixture(name, 0))
            self.assertRegex(text, rf'Table Name\s*:\s*"{name}"')
            self.assertNotIn("Name Padding", text)

    def test_name_trailing_opaque_bytes_are_ignored(self):
        for name in ("ubc", "ummu"):
            data = fixture(name)
            length = 16 - len(name) - 1
            padding = (b'\x00\x22\x5C\x0A\xFF\x00' * 2)[:length]
            data[len(name) + 1:16] = padding
            struct.pack_into("<I", data, 28, 0)
            struct.pack_into("<I", data, 28, -checksum(data) & 0xFFFFFFFF)
            text = self.roundtrip(data, fixture(name))
            self.assertRegex(text, rf'Table Name\s*:\s*"{name}"')
            self.assertNotIn("Name Padding", text)

    def test_native_dispatch_uses_name_value(self):
        for sig in ("FPDT", "S3PT", "APIC", "UBRT"):
            with self.subTest(signature=sig):
                self.run_iasl("-T", sig)
                source = self.root / (sig.lower() + ".asl")
                text, changed = re.subn(r"\bSignature(\s*:)", r"Table Name\1",
                                        source.read_text(), count=1)
                self.assertEqual(changed, 1)
                source.write_text(text)
                output = self.run_iasl(source.name, ok=False)
                self.assertIn('expected "Signature"', output)
                self.assertNotIn('expected "Total Size"', output)

        for name in LAYOUTS:
            with self.subTest(native=name):
                text = self.roundtrip(fixture(name))
                (self.root / "bad-label.asl").write_text(
                    text.replace("Table Name", "Signature", 1))
                output = self.run_iasl("bad-label.asl", ok=False)
                self.assertIn('expected "Table Name"', output)

    def test_no_name_padding_source_field(self):
        for name in LAYOUTS:
            text = self.roundtrip(fixture(name))
            source, changed = re.subn(
                r"(Checksum\s*:\s*[0-9A-F]+\n)",
                r"\1[001] Name Padding : 5A\n", text, count=1)
            self.assertEqual(changed, 1)
            (self.root / "padding.asl").write_text(source)
            self.run_iasl("padding.asl", ok=False)

    def test_no_native_compiler_option(self):
        self.assertNotIn("-cu", self.run_iasl("-h"))
        output = self.run_iasl("-cu", "missing.asl", ok=False)
        self.assertIn("Unknown option: -cu", output)

    def test_native_compiler_multiple_sources(self):
        sources = []
        for name in LAYOUTS:
            text = self.roundtrip(fixture(name))
            filename = name + ".asl"
            (self.root / filename).write_text(text)
            sources.append(filename)
        self.run_iasl(*sources)
        for name in LAYOUTS:
            self.assertEqual((self.root / (name + ".aml")).read_bytes(), fixture(name))

        self.run_iasl("-T", "DSDT", "UBRT", "FPDT")
        self.run_iasl("dsdt.asl", "ubrt.asl", *sources, "fpdt.asl")
        self.assertTrue((self.root / "dsdt.aml").exists())
        self.assertTrue((self.root / "fpdt.aml").exists())
        self.assertTrue((self.root / "ubrt.aml").exists())
        for name in LAYOUTS:
            self.assertEqual((self.root / (name + ".aml")).read_bytes(), fixture(name))

    def test_truncated_native_tables(self):
        prefix = fixture("ub_reserved_mem", 0)
        for name in LAYOUTS:
            data = fixture(name)
            for size in range(len(data)):
                (self.root / "bad.dat").write_bytes(data[:size])
                self.run_iasl("-d", "bad.dat", ok=False)
                if size:
                    (self.root / "bad.dat").write_bytes(prefix + data[:size])
                    self.run_iasl("-d", "bad.dat", ok=False)

    def test_version_is_preserved(self):
        for name in LAYOUTS:
            original = fixture(name, 2, bytes(4))
            text = self.roundtrip(original)
            for version in (0, 1, 2, 0x7F, 0xFF):
                with self.subTest(name=name, version=version):
                    source = re.sub(r"(Version\s*:)\s*[0-9A-F]+",
                                    rf"\g<1> {version:02X}", text)
                    (self.root / "version.asl").write_text(source)
                    self.run_iasl("version.asl")
                    data = (self.root / "version.aml").read_bytes()
                    expected = bytearray(original)
                    expected[20] = version
                    struct.pack_into("<I", expected, 28, 0)
                    struct.pack_into("<I", expected, 28,
                                     -checksum(expected[:-4]) & 0xFFFFFFFF)
                    self.assertEqual(data, expected)
                    decoded = self.roundtrip(data)
                    self.assertRegex(decoded, rf"Version\s*:\s*{version:02X}")

    def test_bad_lengths_and_counts(self):
        for name, (_, size, count_offset, _) in LAYOUTS.items():
            for offset, fmt, value in ((16, "I", 31), (16, "I", 0xFFFFFFFF),
                    (24, "I", 0xFFFFFFFF), (24, "I", size + 1),
                    (count_offset, "H", 65535)):
                data = fixture(name)
                struct.pack_into("<" + fmt, data, offset, value)
                (self.root / "bad.dat").write_bytes(data)
                self.run_iasl("-d", "bad.dat", ok=False)

    def test_valid_extent_matches_record_array(self):
        for name, (_, size, count_offset, _) in LAYOUTS.items():
            original = fixture(name)
            for delta in (-1, 1, 2, 3, size):
                with self.subTest(name=name, delta=delta):
                    data = original[:-1] if delta < 0 else original + bytes(delta)
                    struct.pack_into("<I", data, 16, len(data))
                    struct.pack_into("<I", data, 28, 0)
                    struct.pack_into("<I", data, 28, -checksum(data) & 0xFFFFFFFF)
                    (self.root / "bad-extent.dat").write_bytes(data)
                    self.run_iasl("-d", "bad-extent.dat", ok=False)
            for count in (0, 2):
                with self.subTest(name=name, count=count):
                    data = bytearray(original)
                    struct.pack_into("<H", data, count_offset, count)
                    struct.pack_into("<I", data, 28, 0)
                    struct.pack_into("<I", data, 28, -checksum(data) & 0xFFFFFFFF)
                    (self.root / "bad-count.dat").write_bytes(data)
                    self.run_iasl("-d", "bad-count.dat", ok=False)

    def test_checksum_warning_only_once(self):
        for name in LAYOUTS:
            with self.subTest(name=name):
                data = fixture(name)
                data[-1] ^= 1
                (self.root / "bad.dat").write_bytes(data)
                output = self.run_iasl("-d", "bad.dat")
                self.assertEqual(output.count("invalid checksum"), 1)
                self.run_iasl("-p", "fixed", "bad.dsl")
                self.assertEqual(checksum((self.root / "fixed.aml").read_bytes()), 0)

    def test_source_identity_and_unknown_fields(self):
        for name in LAYOUTS:
            text = self.roundtrip(fixture(name))
            bad = text.replace(f'"{name}"', '"unknown"', 1)
            (self.root / "bad.asl").write_text(bad)
            self.run_iasl("bad.asl", ok=False)
            source = text.split("\nRaw Table Data:", 1)[0]
            self.assertNotEqual(source, text)
            for label in ("Valid Data", "Unused Data", "Unknown Data"):
                with self.subTest(name=name, label=label):
                    (self.root / "bad.asl").write_text(source + f"\n[001] {label} : 01\n")
                    self.run_iasl("bad.asl", ok=False)

    def test_root_physical_addresses_only(self):
        self.run_iasl("-T", "UBRT")
        source = self.root / "ubrt.asl"
        text = re.sub(r"(Sub-table Physical Address\s*:\s*)[0-9A-F]+",
                      r"\g<1>FEDCBA9876543210", source.read_text())
        self.assertNotIn("Table Name", text)
        source.write_text(text)
        self.run_iasl(source.name)
        data = (self.root / "ubrt.aml").read_bytes()
        count = struct.unpack_from("<I", data, 36)[0]
        self.assertEqual(data[8], 1)
        self.assertEqual(len(data), 40 + 16 * count)
        self.assertEqual(sum(data) & 255, 0)
        for i in range(count):
            self.assertEqual(struct.unpack_from("<Q", data, 48 + 16 * i)[0], 0xFEDCBA9876543210)
        self.run_iasl("-d", "ubrt.aml")
        self.assertNotIn("Table Name", (self.root / "ubrt.dsl").read_text())
        source.write_text(text + '\n[016] Table Name : "ubc"\n')
        self.run_iasl(source.name, ok=False)

    def test_standard_tables(self):
        for sig in ("FPDT", "S3PT", "CDAT", "FACS", "APIC", "SLIT"):
            args = ["-ds", "CDAT"] if sig == "CDAT" else []
            self.run_iasl(*args, "-T", sig)
            self.run_iasl(sig.lower() + ".asl")
            data = (self.root / (sig.lower() + ".aml")).read_bytes()
            output = self.run_iasl("-d", *args, sig.lower() + ".aml")
            self.assertRegex(output, rf"(?m)^ACPI: {sig} +0x[0-9A-F]{{16}} ")
            self.run_iasl("-p", "standard", sig.lower() + ".dsl")
            self.assertEqual((self.root / "standard.aml").read_bytes(), data)

    def test_mixed_tables_at_nonzero_offsets(self):
        self.run_iasl("-T", "FPDT", "S3PT")
        source = self.root / "fpdt.asl"
        source.write_text(re.sub(r"(S3PT Record Address\s*:\s*)[0-9A-F]+",
                                 r"\g<1>FEDCBA9876543210", source.read_text()))
        self.run_iasl("fpdt.asl", "s3pt.asl")
        objects = [(self.root / (name + ".aml")).read_bytes() for name in ("fpdt", "s3pt")]
        objects += [fixture("ubc", 0, bytes(1)),
                    fixture("ummu", 2, bytes(3)),
                    fixture("ub_reserved_mem", 1, bytes(7))]
        for order in (objects, objects[::-1]):
            (self.root / "mixed.dat").write_bytes(b"".join(order))
            output = self.run_iasl("-d", "mixed.dat")
            headers = re.findall(r"(?m)^ACPI: (.{4}) 0x[0-9A-F]{16} ", output)
            expected = [data[:4].decode("ascii") for data in order
                        if data[:4] in (b"FPDT", b"S3PT")]
            self.assertEqual(headers, expected)
            for name in ("FPDT", "S3PT", *LAYOUTS):
                self.assertEqual(output.count(f"Acpi Data Table [{name}] decoded"), 1)
            self.assertIn("FEDCBA9876543210", (self.root / "mixed.dsl").read_text())

    def test_children_not_external_aml(self):
        self.run_iasl("-T", "DSDT")
        self.run_iasl("dsdt.asl")
        for name in LAYOUTS:
            (self.root / "child.dat").write_bytes(fixture(name, 0))
            output = self.run_iasl("-e", "child.dat", "-d", "dsdt.aml")
            self.assertIn("is not an AML table - ignoring", output)

    def test_root_bounds(self):
        self.run_iasl("-T", "UBRT")
        self.run_iasl("ubrt.asl")
        data = bytearray((self.root / "ubrt.aml").read_bytes())
        struct.pack_into("<I", data, 36, 0xFFFFFFFF)
        (self.root / "bad-root.dat").write_bytes(data)
        self.run_iasl("-d", "bad-root.dat")
        self.assertIn("descriptor count exceeds table length",
                      (self.root / "bad-root.dsl").read_text())
        data = data[:36]
        struct.pack_into("<I", data, 4, len(data))
        (self.root / "bad-root.dat").write_bytes(data)
        self.run_iasl("-d", "bad-root.dat")
        self.assertIn("Truncated UBRT header", (self.root / "bad-root.dsl").read_text())

    def test_many_records(self):
        data = fixture("ub_reserved_mem", 512)
        self.roundtrip(data)
        self.assertEqual(struct.unpack_from("<H", data, 32)[0], 512)

    def test_maximum_stored_record_count(self):
        data = fixture("ub_reserved_mem", 0xFFFF)
        (self.root / "maximum.dat").write_bytes(data)
        self.run_iasl("-d", "maximum.dat")
        text = (self.root / "maximum.dsl").read_text()
        self.assertRegex(text, r"Memory Ranges Count\s*:\s*FFFF")
        self.assertEqual(len(re.findall(r"Memory Base\s*:", text)), 0xFFFF)


if __name__ == "__main__":
    unittest.main()
