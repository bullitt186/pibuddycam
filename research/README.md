# Research helpers

Standalone artifacts from the firmware reverse engineering. How and when to use them is described
in [docs/reverse-engineering/methods.md](../docs/reverse-engineering/methods.md).

| File | What it is |
|---|---|
| `ghidra/*.java` | Ghidra scripts (export, decompile, Function-ID hashes, xrefs, data). See the table in *methods* |
| `compare_decomp.py` | Aligns two `ExportAllDecomp` corpora after address normalisation (firmware version diffs) |
| `camera_info_struct.c` | Byte-exact C struct of the firmware's `CameraInfoMessage` (field offsets from Ghidra) |
| `compute_camerainfo_offsets.py` | Computes those struct offsets from the descriptor tables |
