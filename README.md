# BitCracker V2

GPU-accelerated MultiBit Classic wallet password recovery. Custom CUDA C++ kernel achieves 11M passwords/sec on RTX 2060 — 22× faster than the Python+OpenCL baseline. Supports btcrecover-compatible token list files with save/restore.

---

## Overview

Three tools run in parallel, each covering a different part of the search space:

| Tool | Language | Rate | Typos |
|---|---|---|---|
| `multibit_cuda_threads.exe` | CUDA C++ | ~11M/s | No |
| `multibit_gpu.py` | Python + OpenCL | ~500K–11M/s | No |
| `btcrecover.py` | Python 2.7 | ~54K/s | Yes |

The CUDA tool is the primary workhorse. btcrecover covers typo variants that the GPU tools don't attempt.

---

## Architecture

### multibit_cuda_threads.exe (primary)

- **CPU producer thread**: iterates token list combinations, calls `std::next_permutation` to enumerate permutations, assembles password bytes using stack-allocated char arrays (zero heap allocation in the hot loop)
- **GPU main thread**: receives 1M-password batches and runs the MultiBit crypto check — 3× MD5 + AES-256-CBC + base58 validation — one CUDA thread per password
- The two overlap: GPU checks batch N while CPU fills batch N+1

### multibit_gpu.py

Python + OpenCL version. CPU generates passwords using Python's `itertools.permutations`; GPU checks them. Throughput varies with token list complexity (CPU is the bottleneck for complex permutation sets).

### btcrecover.py

Original btcrecover by gurnec, modified with:
- Hot path optimisation: `str.translate()` for base58 validation, pre-sliced encrypted blocks
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
- Visual Studio 2022 Build Tools (MSVC 14.44) — VS 2026 not supported by CUDA 13.0
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

### Python GPU tool
```
python multibit_gpu.py --wallet multi.key --tokenlist search46.txt --autosave save46.pkl
python multibit_gpu.py --restore save46.pkl --autosave save46.pkl
```

### CPU btcrecover (typos)
```
C:\python27\python btcrecover.py --tokenlist search45.txt --wallet multi.key ^
  --no-dup --typos 2 --typos-capslock --typos-swap --typos-repeat ^
  --typos-delete --typos-closecase --typos-insert %a ^
  --utf8 --no-eta --autosave savefileBitCrackerResults --threads 10

C:\python27\python btcrecover.py --restore savefileBitCrackerResults
```

---

## Token List Format

Compatible with btcrecover's format:

```
# Lines prefixed with + are required (must contribute a token)
# Optional lines (no +) may be skipped

+ ^Freedom ^freedom        # anchored to first position
+ ^2^is ^2^Is              # anchored to position 2
USMC usmc                  # optional, any position
%0,4d                      # digit wildcard: 0–4 digits
```

- Tokens on the same line are mutually exclusive (pick one per combination)
- `^N^token` fixes the token at position N (1-indexed)
- `%0,4d` expands to all digit strings of 0–4 digits

---

## Save / Restore

All three tools support interruption and resumption. The CUDA and Python GPU tools save a `combo_idx` so restore is near-instant — the iterator jumps directly to the saved position rather than replaying every previous password.

Save files (`.bin`, `.pkl`, `savefile*`) are excluded from version control.

---

## Performance Notes

- The CUDA tool achieves ~11M passwords/sec for token lists with 4–9 free tokens per combination (search46 structure)
- The key optimisation: stack-allocated char arrays in the password assembly hot loop eliminate ~11M malloc/free calls per second vs. the `std::string` version (which ran at ~3M/s)
- For token lists with anchored required tokens and few free tokens (e.g. search47), the CUDA tool completes 76M passwords in ~7 seconds
- Typo variants are not supported in the GPU tools; the btcrecover CPU tool covers those at ~54K/s

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
├── btcrecover.py               # btcrecover entry point
└── btcrecover/
    └── btcrpass.py             # Modified: fast restore + base58 optimisation
```
