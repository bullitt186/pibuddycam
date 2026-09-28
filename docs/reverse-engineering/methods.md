# Reverse-engineering methods

How the Buddy3D firmware was analysed, and how to reproduce every piece of evidence the other
reverse-engineering pages cite. The firmware itself is not in this repository; see
[sources](sources.md) for how to obtain it.

Paths below use two placeholders: `$FW_WORKDIR` is your local analysis directory (firmware
packages, Ghidra projects, exports), and `$GHIDRA_HOME` is your Ghidra installation.

## Tools

| Tool | Used for |
|---|---|
| `ubi_reader` (`pip install ubi_reader`) | Unpack `oem.img` (UBI) into the camera's `/oem` filesystem |
| binutils `strings -t d` | URLs, event names, config keys, log formats and mangled C++ symbols from `lp_app` |
| Ghidra 12.x with OpenJDK 21 | Decompiling the stripped ARM `lp_app` (`ARM:LE:32:v7`, uClibc) |
| radare2 / ARM binutils | Independent disassembly, ELF section and unwind-table comparison |
| GhidrAssistMCP (optional) | Lets an MCP-capable agent query the open Ghidra GUI (decompile, structs, xrefs) |
| tcpdump, browser HAR export | Live traffic of the Pi and of the Prusa Connect web app. HAR files contain account tokens, so they are git-ignored and never committed |

Domain knowledge that mattered: the **nanopb** protobuf descriptor layout (the firmware encodes
protobuf through nanopb callbacks) and **Socket.IO/Engine.IO** framing.

## 1. Extract the firmware

```bash
ubireader_extract_files oem.img -o "$FW_WORKDIR/cam-3.1.6/oem-extracted"
strings "$FW_WORKDIR"/cam-3.1.6/oem-extracted/*/oem/usr/sbin/lp_app \
  > "$FW_WORKDIR/cam-3.1.6/lp_app.strings"
```

The main binary is `oem/usr/sbin/lp_app` (ARM ELF 32-bit, stripped, uClibc).

## 2. Ghidra

Import `lp_app` into a project at `$FW_WORKDIR/ghidra-projects/buddy3d-3.1.6` as `ARM:LE:32:v7` and
run auto-analysis once. Afterwards run the checked-in scripts headless with `-noanalysis`; scripts
are referenced by file name only:

```bash
"$GHIDRA_HOME/support/analyzeHeadless" "$FW_WORKDIR/ghidra-projects" buddy3d-3.1.6 \
  -process lp_app -noanalysis -readOnly \
  -scriptPath "$PWD/research/ghidra" \
  -postScript DecompileFunctions.java 0x62d74 0xb996c
```

| Script (`research/ghidra/`) | Purpose |
|---|---|
| `ExportAllDecomp.java` | Export every function to one C file each, plus `functions.tsv` and `summary.txt` |
| `DecompileFunctions.java` | Decompile selected VMAs without writing a corpus |
| `ExportFidHashes.java` | Relocation-insensitive Function-ID hashes, for comparing firmware versions |
| `ListXrefs.java` | References to addresses and their containing functions |
| `ShowData.java` | Resolve raw words and pointer/string targets |

### The full 3.1.6 export

The [gap tracker](gap-tracker.md) and [firmware behaviour](firmware-behaviour.md) cite a complete
per-function export. Recreate it locally. The output is copyrighted decompilation: **never commit
it**.

```bash
"$GHIDRA_HOME/support/analyzeHeadless" "$FW_WORKDIR/ghidra-projects" buddy3d-3.1.6 \
  -process lp_app -noanalysis -readOnly -scriptPath "$PWD/research/ghidra" \
  -postScript ExportAllDecomp.java "$FW_WORKDIR/decompiled-3.1.6-full"
grep -E '^(program|total|success|failed)=' "$FW_WORKDIR/decompiled-3.1.6-full/summary.txt"
```

`summary.txt` must name `lp_app` and report `failed=0`. Evidence is cited by function VMA (for
example `FUN_000a1394`), so it survives re-analysis. If a cited detail can't be found, locate the
function by VMA and then by the constants and branches the citation describes. A missing export is
a prerequisite to recover, never permission to fall back on older prose.

`research/compare_decomp.py` aligns two exports after normalising addresses. That is how the
[firmware versions](firmware-versions.md) comparison was produced.

## 3. Techniques that worked

**Finding functions in the stripped binary.** String literals in `.rodata` are loaded through ARM
literal pools (PC-relative `ldr`). Search `.text` for a known string's VMA as a 32-bit
little-endian word to find the literal pool; the function that owns that pool is the one you want.
Scan backwards for `push {r4,…,lr}` (`0xe92d____`) to find its entry.

**Finding protobuf descriptors.** `pb_encode_string_cus` (VMA `0x0009c294`) is the callback for
every string field, so each literal-pool reference to it marks an encode site. The same pool
references the message's descriptor table (`0x3f5xxx`–`0x3f6xxx`), laid out as
`[field_info_ptr, submsg_ptr, 0, callback, field_count, largest_tag]`.

**nanopb descriptor encoding in this firmware.** Field info is paired 32-bit words per field. Type
byte `0x57` is a callback string, `0x18` an optional submessage and `0x15` a uvarint/fixed32.
`largest_tag = 0` means sequential numbering from 1. A NULL callback means the field is never sent.

**Finding Socket.IO event names.** Each `Send*` function's literal pool holds its event name next to
the strings `"Checking sio_client and locking mutex"` and `"Binary message with X sent"`.

## Key 3.1.6 functions

| VMA | Function |
|---|---|
| `0x00062d74` | `/c/info` JSON and HTTP request builder |
| `0x00063bfc` | `/c/info` dirty/retry service loop |
| `0x0005f42c` | Snapshot capture and HTTP upload |
| `0x0006cf34` | QR/configuration semantic dispatcher |
| `0x00072f08` | Raw quality live change and optional persistence |
| `0x0007d7c4` | Raw quality value to dimensions |
| `0x000a76c8` / `0x000a11f4` | Protobuf quality enum to raw value / to string |
| `0x00097e78` | Interface MAC retrieval and uppercase formatting |
| `0x00096cd8` / `0x00097a4c` | Fingerprint seed selection / MD5 lowercase-hex encoding |
| `0x000a3058` | Camera authentication sender |
| `0x000a3570` | Protobuf schema version sender |
| `0x000a1394` | Camera status construction and sender |
| `0x000a8ed0` | Supported features construction, hash and sender |
| `0x000a3e90` | WebRTC answer/candidate encoder and sender |
| `0x000b6d9c` | WebRTC numeric type translator |
| `0x000b75e0` | Local ICE candidate emission |
| `0x000b94ac` | WebRTC enable/disable mode application |
| `0x000b996c` | WebRTC offer gate and peer-work enqueue |

What is still ambiguous, and must be recovered before the dependent gap is implemented, is listed
in the [gap tracker](gap-tracker.md).
