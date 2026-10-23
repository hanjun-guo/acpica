# UBRT data-table support

This implementation starts from master `e85aa3beba`. The existing UBRT
structures and root-table support are retained; previous experimental
subtable implementations are not included.

## Model: the same separation as FPDT/S3PT

UBRT contains a standard ACPI header, Count and an array of 16-byte pointer
records. Each record contains Type, seven reserved bytes and a physical
Pointer. It does not contain the pointed-to subtable body.

Like S3PT, a UBRT subtable is a separate native binary object. iASL compiles
and decodes it independently. Firmware places the object and writes its
physical address into UBRT. iASL does not allocate physical addresses,
perform relocations or dereference physical pointers in an input file.

Like S3PT's Signature, the first field's actual name string selects the
native compiler: `ubc`, `ummu` or `ub_reserved_mem`. Matching includes the
NUL terminator and is bounded by the available input. It does not depend on
the display label `Table Name`, decode a hexadecimal buffer to guess its
type, or require a table-specific command-line option.

The generic `ACPI_DMT_NAME16` type follows the existing NAME4/NAME6/NAME8
fixed-width string mechanism. It uses the existing string compiler and
bounded ASCII display helper; the native Name still occupies 16 bytes.
`DtCompileBuffer()` and the root compiler are unchanged from master.

## Code map

- `source/common/dmtbinfo2.c`: fields and one shared layout description for
  each type; `AcpiDmGetUbrtInfo()` selects the layout by native Name. Compiler
  dispatch and the file reader pass the selected layout to their handlers
  rather than looking up the same name again during compilation or validation.
- `source/compiler/dttable2.c`: `DtCompileUbrt()` compiles root pointer
  records; `DtCompileUbrtSubtable()` compiles an independent native subtable.
- `source/common/dmtbdump3.c`: bounds validation, checksum and native
  subtable disassembly. `AcpiDmDumpUbrt()` only decodes root pointer records.
- `source/common/acfileio.c`: the existing common reader selects TotalSize
  and the native checksum for UBRT subtables. All header types share the
  same file-length validation; no second loader is used. The private reader
  returns the recognized header format with the buffer. The table-list
  function uses that result for header printing, without detecting UBRT
  again. Existing ACPI, FACS and CDAT header printing is retained; UBRT
  subtable headers are not passed to the ACPI header printer. Their format
  is explicitly identified as `AC_HEADER_TYPE_UBRT_SUBTABLE`.
- `source/compiler/dttemplate.h`: static `TemplateUbrtUbc`,
  `TemplateUbrtUmmu` and `TemplateUbrtReservedMem` binary examples, in the
  same form as the existing `TemplateS3pt` example. The separate
  `DtSubtableTemplates` registry associates each companion with its parent,
  local identifier, filename, actual buffer length and dump handler.
- `source/compiler/dttemplate.c`: generates a table's registered companions
  through `DtCreateOneTemplateFile` and the single-file `DtCreateOneTemplate`.
  It contains no UBRT-specific selection, template array or recursion.

## Native layouts (UBRT Specification 1.0, Tables 3-8)

Every subtable starts with a 32-byte header:

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 16 | NUL-terminated Name |
| 16 | 4 | TotalSize, including all allocated bytes |
| 20 | 1 | Version, currently 1 |
| 21 | 3 | Reserved |
| 24 | 4 | RemainingSize, unused capacity |
| 28 | 4 | 32-bit checksum |

| Name | Fixed part, including header | Record size | Count offset |
| --- | --- | --- | --- |
| ubc | 56 | 384 | 54 |
| ummu | 40 | 160 | 32 |
| ub_reserved_mem | 40 | 24 | 32 |

The field descriptors use the upstream structures and `ACPI_OFFSET`, not
duplicated numeric offsets. Vendor Info is a fixed 256-byte UBC or 80-byte
UMMU buffer. Input must contain exactly that number of bytes; short and
oversized buffers are errors. No implicit padding changes later offsets.

Name is a `NAME16`; UBC Vendor Info is
represented by two `BUF128` fields (0-127 and 128-255), and UMMU Vendor Info
by `BUF32`, `BUF32` and `BUF16` fields (0-31, 32-63 and 64-79). Each segment
must have its exact length. These are source-format divisions only: the
native binary layout, record sizes, offsets and checksum coverage are unchanged.

Name is displayed as a quoted string and ordinary compilation zero-fills
the bytes after its terminator. Only the supported name string identifies
the table; trailing bytes in the 16-byte Name field are ignored and are not
preserved by disassembly. There is no Name Padding source field. Recompiling
normalizes these bytes to zero and regenerates the checksum. The Name field
still occupies 16 bytes and later fields retain their native offsets.

Feature Sets and MSG Queue Interrupt Attribute use the existing `DT_FLAG`,
`FLAG0` and `FLAG1` descriptors. Feature Sets bit 0 is MMIO Token Value
support and bit 1 is MCTP over UB support (0 = unsupported, 1 = supported).
Interrupt Attribute bit 0 is Edge Triggered (0 = level, 1 = edge); bit 1
is Active Low (0 = high level/rising edge, 1 = low level/falling edge).
Both bytes reserve bits 2-7. As with other ACPICA decoded flag fields,
compilation builds the byte from the named bits, not the displayed master
value; reserved bits are emitted as zero. Each named bit accepts only 0
or 1. These descriptors also expand the static UBC template without
changing its binary data or layout.

UMMU Mapping is an index into the
separate UMMU table, not a pointer. Reserved-memory Base and Size are in
bytes and are specified to be 64-KB aligned. iASL is a format compiler and
disassembler, not an OS-side device configuration validator; it does not
validate cross-table mappings or platform address/resource assignments.

## Usage

```sh
iasl -T UBRT
iasl ubrt.asl ubrt-ubc.asl ubrt-ummu.asl ubrt-reserved-mem.asl
iasl -d ubrt-ubc.aml
iasl ubrt-ubc.dsl
```

The ordinary UBRT template operation emits `ubrt.asl`, `ubrt-ubc.asl`,
`ubrt-ummu.asl` and `ubrt-reserved-mem.asl`. The latter three names are file
labels, not new ACPI signatures or additional `-T` aliases. No native binary
template is dynamically constructed at runtime. Each generated subtable
source starts with a quoted, fixed-width Table Name, for example:

```text
[016] Table Name : "ubc"
```

The compiled binary starts directly with the 16-byte native Name, without
an ACPI header or another wrapper. `-T ALL` also includes the new templates.

Companions are identified in the registry by `(ParentSignature, LocalId)`,
for example `(UBRT, ubc)`. LocalId is registry metadata, not a required binary
field, and need not be unique across unrelated parent tables. It is not
added to `AcpiDmTableData` or accepted as a global ACPI signature. Filename
labels are separate from both identifiers. The generated comment identifies
the object as `[UBRT/ubc]`, but this context is not inserted into its binary
data or treated as an extra source field.

The template writer calls the registered handler with the actual static
buffer length; it does not inspect a binary name to discover the format.
The UBRT handler reuses the existing layout validation and field dumper,
returns errors to the caller, and rejects declarations beyond that buffer.
After validating the stored count against the valid extent, it walks that
extent directly without rereading the count or selecting the layout again.
This mechanism concerns template registration and generation only. Source
and raw-binary input recognition retain their existing behavior; arbitrary
formats with ambiguous local identifiers still require parent context.

Both `-T UBRT` and `-T ALL` use the same table-group entry point. Each output
file has its own overwrite confirmation: declining the root does not skip
missing companions, and declining a companion does not skip its siblings.
Open, write and close failures are reported and stop further generation;
already completed files are retained. Verbose templates retain raw bytes.

Root, native and ordinary ACPI source files can be compiled in the same
invocation. There is no `-cu` option. Earlier experimental source files
using a hexadecimal Table Name buffer must be regenerated with the current
disassembler or template generator; the binary format has not changed.

Counts and checksum are generated from the source. TotalSize and RemainingSize
are input fields: TotalSize is the complete allocated length, including the
unused capacity specified by RemainingSize. The valid extent, starting at
the header, is exactly TotalSize - RemainingSize. Only the defined fixed
fields and record arrays are supported:

```text
TotalSize - RemainingSize = FixedLength + Count * EntryLength
```

FixedLength includes the 32-byte native header. Compilation requires the
header, fixed fields and records to fill exactly the declared valid extent.
It rejects RemainingSize larger than TotalSize, a valid extent shorter than
the fixed fields, and content that exceeds or does not fill the declared
valid extent. These checks precede allocating any unused capacity.
Loading also requires the stored
record count and record sizes to account for every valid byte; unknown valid
tails and incomplete records are rejected rather than exposed as raw fields.
Both paths use the fixed layout and record count for this check. Compilation
does not maintain a separate running length for fixed-size records.

The existing DtCompilePadding() helper fills the final RemainingSize bytes
with zeros. This padding is already included in TotalSize and does not
increase it; neither input size is regenerated. Set RemainingSize to zero
when no unused capacity is needed and adjust TotalSize when changing the
source content. For example, one UMMU record has 200 valid bytes (40 fixed
bytes plus a 160-byte record). With RemainingSize = 64, TotalSize must be
264, and the output is exactly 264 bytes, not 328 bytes.

Disassembly retains RemainingSize but does not emit Unused Data fields.
Unused bytes have no defined content and are normalized to zero when
recompiled; both capacity fields remain unchanged. Valid content is preserved
except for ignored Name padding, and the checksum is regenerated.
Binary-identical round trips require zero padding after the Name terminator
and in unused capacity. The obsolete source-only Name Padding, Valid Data
and Unused Data fields are not accepted.

The native checksum sums little-endian 32-bit words over
`TotalSize - RemainingSize` bytes, including the native header. High bytes
of a final incomplete word are treated as zero; no extra bytes are emitted.
Compilation calculates the checksum before appending unused capacity, so
every buffer being summed contains valid data only. It sums each record as
it is compiled, then includes the header and fixed fields after generating
the record count; no separate checksum walk of the output tree is needed.
Each of these native buffers contains a whole number of 32-bit words, so
separate sums can be added without tracking their byte offsets. Physical
buffer addresses need not be aligned.
This follows the partial-word interpretation of note 2 in Tables 3, 5 and 7.
Its wording uses "remaining size" and should be clarified with the specification
owner before relying on a different interpretation. Invalid checksums warn
once during loading; malformed lengths and out-of-range record counts are
rejected before decoding. Version is preserved as an ordinary UINT8 field,
without restricting it to 1. Known field layouts and bounds checks still apply.

## Validation

```sh
make -j4 all
python3 tests/ubrt/test_ubrt.py generate/unix/bin/iasl -v
```

For a UBSan build, define `ACPI_MISALIGNMENT_NOT_SUPPORTED` to select
ACPICA's existing byte-wise access macros. The default x86 fast-access
macros trigger alignment diagnostics in unchanged namespace initialization,
before any table is processed. The unchanged AML name display in `dmnames.c`
also uses direct unaligned reads; its alignment instrumentation must be
excluded separately when testing DSDT disassembly. Keep all sanitizer checks
enabled for the UBRT implementation files.

The tests build independent fixtures and use a separate word-based checksum
oracle. They cover native offsets, valid-data preservation, zero-filled
unused capacity, preservation of both input sizes, capacity-bound validation,
all truncation boundaries at zero and nonzero file offsets, fixed vendor
lengths, invalid headers, exact record-array extents, rejected raw fields,
root address preservation and FPDT/S3PT/CDAT/FACS/APIC
regressions, plus SLIT's existing buffer-compiler caller. Tests cover exact
source-name matching, repeated field labels, quoted NAME16 fields, ignored
nonzero trailing Name bytes and rejection of obsolete Name Padding fields.
Mixed native, standard-table and ASL sources retain the generic buffer parser.
All 16 combinations of UBC feature and interrupt bits are checked
for decoded values, compilation and byte-identical round trips, including
different attributes in adjacent records. Out-of-range one-bit values are
rejected. The tests reject sanitizer diagnostics even on negative-input tests.
Version values 00, 01, 02, 7F and FF are preserved through compilation,
disassembly and binary-identical round trips, including checksum generation.
Mixed-file tests interleave native layouts with empty and populated record
arrays and odd unused capacities, so later objects also have unaligned file
offsets. Invalid-checksum warnings are checked for each native layout.
Header-printing tests check UBRT subtable suppression and preservation of
existing ACPI and CDAT diagnostics, including per-object format changes in
mixed files.
Disassembly also checks the maximum stored count of 65,535 records.
Template tests cover parent scoping, rejected local-name aliases, normal and
verbose `-T ALL` generation without duplicate companions, independent overwrite
decisions, file-open and buffered-write errors, and default/multiple-SSDT modes.

Specification: https://www.unifiedbus.com/static/UB-Root-Table-Spec-1.0-en.pdf

Known upstream limitation: the unchanged buffer compiler can read past the
end of a one-digit final element. Fixing that generic parser issue is outside
this UBRT series; malformed-element tests here use overlong or non-hex tokens.

The unchanged IORT template also contains 629 bytes but declares 632. ASan
detects an overread with `-T IORT` and therefore `-T ALL`; the expanded ALL
test intentionally reports this rather than hiding sanitizer diagnostics.
The other UBRT tests can be checked independently under sanitizers. Fixing
the IORT template is outside this change.
