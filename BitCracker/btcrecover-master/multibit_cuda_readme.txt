multibit_cuda_threads — GPU-Accelerated MultiBit Classic Password Recovery
==========================================================================

OVERVIEW
--------
CUDA C++ program that generates passwords on the CPU in C++ and checks them
on the GPU, achieving ~11M passwords/second on an RTX 2060.

Performance history (search46.txt, RTX 2060):
  Python + OpenCL (multibit_gpu.py)    :   ~500K/s  -- Python generation bottleneck
  CUDA GPU-side gen (multibit_cuda.cu) :   ~277K/s  -- nth_permutation divergence overhead
  C++ gen + std::string assembly       :    ~3M/s   -- heap allocation per password
  C++ gen + char array assembly        :   ~11M/s   -- current version, no hot-path alloc

At 11M/s, search46 (22 trillion passwords) takes ~23 days.
At 11M/s, search47 (76M passwords) takes ~7 seconds.

Architecture:
  - CPU (producer thread): iterates product combos, calls std::next_permutation
    to generate all permutations, assembles password bytes into a batch buffer
    using stack-allocated char arrays (zero heap allocation in the hot loop)
  - GPU (main thread): receives 1M-password batches, runs MD5+AES+base58 check,
    one thread per password
  - The two overlap: GPU checks batch N while CPU fills batch N+1

Why char arrays, not std::string:
  std::string allocates on the heap per object.  In the hot loop that means
  malloc/free per password — at 11M passwords/sec that would be 11M malloc
  calls/sec.  Stack char arrays eliminate all allocations from the critical path.

Why C++ host generation, not GPU-side generation:
  GPU-side generation (multibit_cuda.cu) used nth_permutation — O(n^2) with
  divergent control flow across threads.  For n=9 free tokens it's slower than
  just sending pre-built strings from the CPU.  C++ std::next_permutation is
  O(n) per step and runs without the divergence penalty.


BUILDING
--------
Requirements: see multibit_cuda_requirements.txt

  build_cuda.bat

The batch file handles VS 2022 environment, CUDA paths, TEMP redirect.
Note: multibit_cuda_threads.exe must not be running when rebuilding (file lock).


USAGE
-----
Fresh start:
  .\multibit_cuda_threads.exe --wallet multi.key --tokenlist search46.txt --autosave save.bin

Resume from checkpoint:
  .\multibit_cuda_threads.exe --restore save.bin

Options:
  --wallet    <file>   MultiBit Classic .key file
  --tokenlist <file>   btcrecover-compatible token list file
  --autosave  <file>   Save progress every 30 seconds (.bin extension)
  --restore   <file>   Resume from a save file


SAVE FORMAT
-----------
Binary SaveState struct (compatible with multibit_cuda.exe saves):
  tokenlist path, wallet path, combo_idx (uint64), total_combos, passwords_checked

Save uses combo_idx not password count — restore jumps directly to the right
combo in O(combo_idx) C++ operations (microseconds, not hours).


TOKEN LIST FORMAT
-----------------
Same format as btcrecover:
  + prefix      Required line
  ^token        First position (positional anchor)
  ^N^token      Position N (1-indexed)
  token$        Last position
  %0,4d         Digit wildcard (0-4 digits)
  No + prefix   Optional line (may be skipped)
  Multiple tokens on one line are mutually exclusive


CRYPTO CHECK
------------
MultiBit Classic key derivation (OpenSSL EVP_BytesToKey with MD5):
  key1    = MD5(password + salt)
  key2    = MD5(key1 + password + salt)
  iv      = MD5(key2 + password + salt)
  aes_key = key1 + key2  (32 bytes, AES-256)

Decrypt 32 bytes from wallet using AES-256-CBC.
Valid password → Bitcoin WIF private key:
  byte[0] in {L, K, 5, Q}  and  all 32 bytes are valid base58 chars.

Early rejection after block 1 (first 16 bytes) rejects 98.4% of wrong
passwords before decrypting block 2.  Any GPU hit is CPU-verified before
being reported.


KNOWN LIMITATIONS
-----------------
- No typos support (--typos-* flags)
- Middle anchors (^N,M^) not implemented
- Digit wildcard only (%0,4d); other wildcard types ignored
- Found password index is reported but string reconstruction is TODO
