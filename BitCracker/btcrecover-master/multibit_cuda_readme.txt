multibit_cuda — GPU-Side MultiBit Classic Password Recovery
===========================================================

OVERVIEW
--------
CUDA C++ program that generates AND checks passwords entirely on the GPU,
eliminating the Python CPU generation bottleneck of multibit_gpu.py.

Architecture:
  - CPU enumerates token list combinations (integer indices only, no strings)
  - GPU receives batches of combo descriptors; one CUDA block per combo
  - Threads within each block enumerate all permutations of the combo's
    tokens using the factoradic number system, assemble the password string,
    and run the MultiBit Classic check (3x MD5 + AES-256-CBC + base58)
  - No password strings ever cross the PCIe bus

Measured speedup vs multibit_gpu.py:
  search47 (simple anchored combos): 7s vs 180s = 26x faster
  search46 (complex free permutations): TBD — rate stabilises ~1050 combos/s


BUILDING
--------
See multibit_cuda_requirements.txt for full dependency details.

Quick build (from btcrecover-master directory):
  build_cuda.bat

The batch file handles:
  - VS 2022 environment setup (vcvars64.bat)
  - CUDA_PATH and PATH configuration
  - TEMP redirect to C:\Temp (required — nvcc has a bug with usernames
    containing spaces; unquoted temp file paths break cl.exe)


USAGE
-----
Fresh start:
  .\multibit_cuda.exe --wallet multi.key --tokenlist search46.txt --autosave cuda_save46.bin

Resume from save:
  .\multibit_cuda.exe --restore cuda_save46.bin

Options:
  --wallet    <file>   MultiBit Classic .key file
  --tokenlist <file>   btcrecover-compatible token list file
  --autosave  <file>   Save progress to this file every 30 seconds (.bin)
  --restore   <file>   Resume from a save file (replaces --wallet/--tokenlist)
  --batch-size <n>     Combos per kernel launch (default: 65536)


TOKEN LIST FORMAT
-----------------
Reads the same format as btcrecover:
  + prefix      Required line (must contribute a token)
  ^token        Positional anchor: this token always goes first
  ^N^token      Positional anchor: token goes at position N
  %0,4d         Digit wildcard: expands to 0-4 digit strings
  Lines without + are optional (may be skipped)
  Tokens on the same line are mutually exclusive (pick one)


SAVE FILE FORMAT
----------------
Binary file containing a SaveState struct:
  - tokenlist path  (512 bytes)
  - wallet path     (512 bytes)
  - combo_idx       uint64 — next combo to process
  - total_combos    uint64
  - passwords_checked uint64

Save files use the .bin extension (covered by .gitignore).


HOW THE CRYPTO CHECK WORKS
---------------------------
MultiBit Classic key derivation (OpenSSL EVP_BytesToKey with MD5):
  salted  = password_bytes + salt   (8-byte salt from wallet file)
  key1    = MD5(salted)
  key2    = MD5(key1 + salted)
  iv      = MD5(key2 + salted)
  aes_key = key1 + key2             (32 bytes for AES-256)

Decrypt first 32 bytes of encrypted wallet data with AES-256-CBC.
A valid password produces Bitcoin private key bytes in WIF base58 format:
  - First byte must be L, K, 5, or Q
  - All 32 bytes must be valid base58 characters

The GPU checks the first byte immediately after decrypting block 1;
98.4% of wrong passwords are rejected there without decrypting block 2.
Any GPU hit is re-verified on CPU before being reported.


KNOWN LIMITATIONS
-----------------
- Does not support btcrecover typos (--typos-* flags)
- Middle anchors (^N,M^) and relative anchors (^r1^) not fully implemented
- Digit wildcards only: %0,4d pattern; other wildcard types ignored
- Found combo/perm indices are reported but the actual password string
  reconstruction from indices is not yet implemented (TODO)
