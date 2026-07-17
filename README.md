# BitCracker V2

[![CI](https://github.com/PAllen0518/bitcracker_dev/actions/workflows/ci.yml/badge.svg)](https://github.com/PAllen0518/bitcracker_dev/actions/workflows/ci.yml)
[![License: GPL v2](https://img.shields.io/badge/License-GPLv2-blue.svg)](LICENSE)

GPU-accelerated MultiBit Classic wallet password recovery. A custom CUDA kernel does about 11M passwords/sec on an RTX 2060, roughly 20x what the original Python+OpenCL version managed. Reads btcrecover-style token lists and supports save/restore.

---

## Overview

Several tools cover different parts of the search space:

| Tool | Language | Rate | Typos |
|---|---|---|---|
| `multibit_cuda_threads.exe` | CUDA C++ | ~11M/s | Yes |
| `multibit_gpu.py` | Python 3 + OpenCL | ~500K–11M/s | No |
| `multibit_check.py` | Python 3 (CPU) | ~67K/s | Yes |
| `btcrecover.py` | Python 2.7 (legacy) | ~54K/s | Yes |

The CUDA tool is the primary workhorse. `multibit_check.py` is a small, dependency-light
Python 3 checker for portability and for the typo modes it adds; it replaces the vendored
Python 2.7 `btcrecover.py`, which is kept only for reference.

---

## Architecture

### multibit_cuda_threads.exe (primary)

- **CPU producer thread**: iterates token list combinations, calls `std::next_permutation` to enumerate permutations, assembles password bytes using stack-allocated char arrays (zero heap allocation in the hot loop)
- **GPU main thread**: receives 1M-password batches and runs the MultiBit crypto check (3x MD5, AES-256-CBC, base58 validation), one CUDA thread per password
- The two overlap: GPU checks batch N while CPU fills batch N+1

### multibit_gpu.py

Python + OpenCL version. CPU generates passwords using Python's `itertools.permutations`; GPU checks them. Throughput varies with token list complexity (CPU is the bottleneck for complex permutation sets).

### btcrecover.py

Original btcrecover by gurnec, modified with:
- Hot path tweaks: `str.translate()` for base58 validation, pre-sliced encrypted blocks
- Fast restore: saves `combo_idx` alongside the password count so `--restore` jumps to the right token combination via `itertools.islice` (seconds, not hours)

---

## MultiBit Classic Key Derivation

```
salted  = password_bytes + salt        (8-byte random salt from wallet file)
key1    = MD5(salted)
key2    = MD5(key1 + salted)
iv      = MD5(key2 + salted)
aes_key = key1 + key2                  (32 bytes → AES-256-CBC)
```

Decrypt the first 32 bytes of the wallet's encrypted section. A valid password produces a Bitcoin WIF private key: first byte in `{L, K, 5, Q}` and all 32 bytes valid base58 characters.

---

## Requirements

### CUDA tool (`multibit_cuda_threads.exe`)
- NVIDIA GPU, compute capability ≥ sm_75 (RTX 2060 or newer)
- CUDA Toolkit 13.0
- Visual Studio 2022 Build Tools (MSVC 14.44); note VS 2026 isn't supported by CUDA 13.0
- Windows SDK 10.0.22621.0

See `BitCracker/btcrecover-master/multibit_cuda_requirements.txt` for full details and known build issues.

### Python GPU tool (`multibit_gpu.py`)
```
pip install -r BitCracker/btcrecover-master/requirements-py3.txt
```

### CPU tool (`btcrecover.py`)
- Python 2.7
- PyCrypto (recommended for speed):
  ```
  C:\python27\python -m pip install -r BitCracker/btcrecover-master/requirements-py2.txt
  ```

---

## Building the CUDA Tool

```bat
cd BitCracker\btcrecover-master
build_cuda.bat
```

The batch file handles VS 2022 environment setup, CUDA paths, and the TEMP redirect required when the Windows username contains spaces.

---

## Usage

### CUDA tool
```bat
.\multibit_cuda_threads.exe --wallet multi.key --tokenlist search46.txt --autosave save46.bin
.\multibit_cuda_threads.exe --restore save46.bin
```

`--delimiter <c>` changes the character that separates tokens on a line (default
is a space). Use it when a token needs to contain a literal space, e.g. set it to
a comma and write `correct horse` as a single token.

The CUDA tool also takes btcrecover's typo flags: `--typos N` plus any of
`--typos-capslock`, `--typos-swap`, `--typos-repeat`, `--typos-delete`,
`--typos-closecase`, `--typos-insert <charset>`. These generate up to N typos per
base password and are meant for a focused pass over a few close candidates, not
the full tokenlist sweep (typo generation allocates and runs well below the
11M/s base rate).

### Python GPU tool
```
python multibit_gpu.py --wallet multi.key --tokenlist search46.txt --autosave save46.pkl
python multibit_gpu.py --restore save46.pkl --autosave save46.pkl
```

### Python 3 CPU checker
```
python multibit_check.py --wallet multi.key --tokenlist search.txt
python multibit_check.py --wallet multi.key --tokenlist search.txt \
  --typos 2 --typos-capslock --typos-swap --typos-insert 0123456789
```
Needs only `pycryptodome`. Supports `--typos-replace` and `--typos-map`, which the
GPU tools don't. Writes a hit to `RECOVERED_PASSWORD.txt` (owner read/write only).

### Legacy CPU btcrecover (Python 2.7)
Kept for reference only; `multibit_check.py` above supersedes it for MultiBit.
```
C:\python27\python btcrecover.py --tokenlist search45.txt --wallet multi.key ^
  --no-dup --typos 2 --typos-capslock --typos-swap --typos-repeat ^
  --typos-delete --typos-closecase --typos-insert %a ^
  --utf8 --no-eta --autosave savefileBitCrackerResults --threads 10
```

---

## Token List Format

Compatible with btcrecover's format:

```
# Lines prefixed with + are required (must contribute a token)
# Optional lines (no +) may be skipped

+ ^Alpha ^alpha            # anchored to first position
+ ^2^one ^2^One            # anchored to position 2
Bravo bravo                # optional, any position
%0,4d                      # digit wildcard: 0-4 digits
```

- Tokens on the same line are mutually exclusive (pick one per combination)
- `^N^token` fixes the token at position N (1-indexed)
- `%0,4d` expands to all digit strings of 0-4 digits

---

## Save / Restore

All three tools can be interrupted and resumed. The CUDA and Python GPU tools save a `combo_idx`, so restore jumps straight to the saved position instead of replaying every previous password.

Save files (`.bin`, `.pkl`, `savefile*`) are excluded from version control.

---

## Performance Notes

- The CUDA tool achieves ~11M passwords/sec for token lists with 4–9 free tokens per combination (search46 structure)
- The big win was stack char arrays in the password assembly loop instead of `std::string`, which cut out a malloc/free per password and took it from ~3M/s to ~11M/s
- For token lists with anchored required tokens and few free tokens (e.g. search47), the CUDA tool completes 76M passwords in ~7 seconds
- The CUDA tool generates typo variants (see `--typos` above); typo generation allocates and runs below the 11M/s base rate, so it's for focused passes over close candidates rather than full sweeps

---

## Project Structure

```
BitCracker/btcrecover-master/
├── multibit_cuda_threads.cu    # Primary CUDA tool (C++ gen + GPU check)
├── multibit_cuda.cu            # Experimental: GPU-side generation (slower)
├── multibit_gpu.py             # Python + OpenCL GPU tool
├── multibit_gpu_2.py           # Experimental: local memory kernel variant
├── build_cuda.bat              # CUDA build script
├── multibit_cuda_readme.txt    # CUDA tool documentation
├── multibit_cuda_requirements.txt
├── multibit_check.py           # Standalone Python 3 CPU checker (MultiBit + typos)
├── tests/                      # pytest suite (crypto KATs, typos, tokenlist)
├── btcrecover.py               # Legacy btcrecover entry point (Python 2.7)
└── btcrecover/
    └── btcrpass.py             # Modified: fast restore + base58 tweak
```

---

## Development

```bash
cd BitCracker/btcrecover-master
pip install -r requirements-dev.txt
ruff check multibit_check.py tests/     # lint
python -m pytest tests/ -v              # tests
```

The tests pin the MultiBit key derivation against a known-answer test wallet and
cover the typo generator and tokenlist parser. They need only `pycryptodome`, so
they run without a GPU (this is what CI runs). See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

Licensed under the GNU General Public License v2.0 (see [LICENSE](LICENSE)).

This project began as a fork of [btcrecover](https://github.com/gurnec/btcrecover)
by Christopher Gurnee, which is GPLv2, so this derivative is GPLv2 as well. The
CUDA kernels, the OpenCL GPU tool, the Python 3 checker, and the fast-restore work
are new code (© 2026 Paul Allen); the vendored `btcrecover/` tree remains under its
original copyright.
