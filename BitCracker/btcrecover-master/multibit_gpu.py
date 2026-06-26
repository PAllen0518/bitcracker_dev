#!/usr/bin/env python3
"""
multibit_gpu.py - GPU-accelerated MultiBit Classic wallet password recovery.

Reads btcrecover-compatible token list files, generates passwords on CPU,
and checks them against a MultiBit .key file using an OpenCL kernel on the GPU.

Architecture overview:
  - Passwords are generated on the CPU (Python/itertools) in a background thread.
  - Each batch of ~1M passwords is transferred to the RTX 2060 via OpenCL.
  - The GPU kernel runs 3x MD5 + AES-256-CBC + base58 validation for every
    password in parallel, one password per work item.
  - The CPU and GPU overlap: while the GPU checks batch N, the CPU generates batch N+1.
  - OpenCL was chosen over CUDA for portability, and because PyOpenCL is available
    for Python 3.10 on Windows without a full CUDA toolkit install.

Usage:
    python multibit_gpu.py --wallet multi.key --tokenlist search45.txt
    python multibit_gpu.py --wallet multi.key --tokenlist search45.txt --autosave save.pkl
    python multibit_gpu.py --restore save.pkl
"""

import sys
import argparse
import hashlib
import base64
import pickle
import time
import itertools
import string
import re
import threading
import queue as _queue
from math import factorial
from dataclasses import dataclass
from typing import Optional, List, Tuple

try:
    import pyopencl as cl
    import numpy as np
except ImportError:
    sys.exit("Required: pip install pyopencl numpy")

try:
    from Crypto.Cipher import AES as _AES
    def _cpu_aes_decrypt(key, iv, ct):
        return _AES.new(key, _AES.MODE_CBC, iv).decrypt(ct)
except ImportError:
    sys.exit("Required: pip install pycryptodome")

# ---------------------------------------------------------------------------
# AES TD table precomputation
#
# AES decryption using the "equivalent inverse cipher" (FIPS 197 §5.3.5) relies
# on four 256-entry lookup tables (TD0-TD3) that fuse InvSubBytes, InvShiftRows,
# and InvMixColumns into a single XOR operation per round.  This gives 4 lookups
# per column per round instead of ~12 separate operations.
#
# We compute the tables in Python at import time and inject them into the OpenCL
# kernel source as literal uint arrays.  The alternative — computing them inside
# the kernel at startup — wastes GPU cycles on every run and on every work item.
# Embedding them as __constant arrays lets the GPU cache them in L2 (3 MB on the
# RTX 2060), which comfortably holds our 16 KB of table data.
# ---------------------------------------------------------------------------

def _build_td_tables():
    SBOX_INV = [
        0x52,0x09,0x6a,0xd5,0x30,0x36,0xa5,0x38,0xbf,0x40,0xa3,0x9e,0x81,0xf3,0xd7,0xfb,
        0x7c,0xe3,0x39,0x82,0x9b,0x2f,0xff,0x87,0x34,0x8e,0x43,0x44,0xc4,0xde,0xe9,0xcb,
        0x54,0x7b,0x94,0x32,0xa6,0xc2,0x23,0x3d,0xee,0x4c,0x95,0x0b,0x42,0xfa,0xc3,0x4e,
        0x08,0x2e,0xa1,0x66,0x28,0xd9,0x24,0xb2,0x76,0x5b,0xa2,0x49,0x6d,0x8b,0xd1,0x25,
        0x72,0xf8,0xf6,0x64,0x86,0x68,0x98,0x16,0xd4,0xa4,0x5c,0xcc,0x5d,0x65,0xb6,0x92,
        0x6c,0x70,0x48,0x50,0xfd,0xed,0xb9,0xda,0x5e,0x15,0x46,0x57,0xa7,0x8d,0x9d,0x84,
        0x90,0xd8,0xab,0x00,0x8c,0xbc,0xd3,0x0a,0xf7,0xe4,0x58,0x05,0xb8,0xb3,0x45,0x06,
        0xd0,0x2c,0x1e,0x8f,0xca,0x3f,0x0f,0x02,0xc1,0xaf,0xbd,0x03,0x01,0x13,0x8a,0x6b,
        0x3a,0x91,0x11,0x41,0x4f,0x67,0xdc,0xea,0x97,0xf2,0xcf,0xce,0xf0,0xb4,0xe6,0x73,
        0x96,0xac,0x74,0x22,0xe7,0xad,0x35,0x85,0xe2,0xf9,0x37,0xe8,0x1c,0x75,0xdf,0x6e,
        0x47,0xf1,0x1a,0x71,0x1d,0x29,0xc5,0x89,0x6f,0xb7,0x62,0x0e,0xaa,0x18,0xbe,0x1b,
        0xfc,0x56,0x3e,0x4b,0xc6,0xd2,0x79,0x20,0x9a,0xdb,0xc0,0xfe,0x78,0xcd,0x5a,0xf4,
        0x1f,0xdd,0xa8,0x33,0x88,0x07,0xc7,0x31,0xb1,0x12,0x10,0x59,0x27,0x80,0xec,0x5f,
        0x60,0x51,0x7f,0xa9,0x19,0xb5,0x4a,0x0d,0x2d,0xe5,0x7a,0x9f,0x93,0xc9,0x9c,0xef,
        0xa0,0xe0,0x3b,0x4d,0xae,0x2a,0xf5,0xb0,0xc8,0xeb,0xbb,0x3c,0x83,0x53,0x99,0x61,
        0x17,0x2b,0x04,0x7e,0xba,0x77,0xd6,0x26,0xe1,0x69,0x14,0x63,0x55,0x21,0x0c,0x7d,
    ]
    def gmul(a, b):
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = ((a << 1) & 0xff)
            if hi:
                a ^= 0x1b
            b >>= 1
        return p
    def rotr8(x): return ((x >> 8) | (x << 24)) & 0xFFFFFFFF
    td0 = []
    for x in range(256):
        s = SBOX_INV[x]
        v = (gmul(14,s)<<24)|(gmul(9,s)<<16)|(gmul(13,s)<<8)|gmul(11,s)
        td0.append(v)
    td1 = [rotr8(v) for v in td0]
    td2 = [rotr8(v) for v in td1]
    td3 = [rotr8(v) for v in td2]
    return td0, td1, td2, td3

_TD0, _TD1, _TD2, _TD3 = _build_td_tables()

# Format tables as C initialiser lists for string-injection into the kernel source.
# String injection is used because OpenCL has no preprocessor mechanism to embed
# large constant arrays from the host at compile time.
def _arr_src(t): return "{ " + ", ".join("0x{:08x}u".format(v) for v in t) + " }"
_TD0_SRC = _arr_src(_TD0)
_TD1_SRC = _arr_src(_TD1)
_TD2_SRC = _arr_src(_TD2)
_TD3_SRC = _arr_src(_TD3)

# ---------------------------------------------------------------------------
# OpenCL kernel source
# ---------------------------------------------------------------------------

KERNEL_SOURCE = r"""
/*
 * MultiBit Classic GPU checker: 3x MD5 + AES-256-CBC + base58 validation
 *
 * MultiBit Classic key derivation (OpenSSL EVP_BytesToKey with MD5):
 *   salted    = password_bytes + salt          (8-byte random salt from wallet file)
 *   key1      = MD5(salted)                    (first 16 bytes of AES key)
 *   key2      = MD5(key1 + salted)             (second 16 bytes of AES key)
 *   iv        = MD5(key2 + salted)             (AES IV)
 *   aes_key   = key1 + key2                    (32 bytes → AES-256)
 *
 * Password bytes: MultiBit uses UTF-16-LE then takes every other byte, which
 * for pure ASCII passwords is identical to plain ASCII — so we pass ASCII bytes
 * directly and get the same result.
 *
 * Validation: decrypt the first 32 bytes of the wallet's encrypted section.
 * A valid password produces Bitcoin private key bytes, which are base58-encoded.
 * The first byte is always L, K, 5, or Q (WIF format prefix) and all 32 bytes
 * must be valid base58 characters.  This lets us reject wrong passwords after
 * decrypting just the first AES block (16 bytes) in the common case.
 */

/* ---- MD5 ---- */
/*
 * MD5 is hand-rolled in OpenCL C because there is no standard crypto library
 * available in OpenCL kernels.  The macro-based approach expands all 64 rounds
 * inline, which lets the compiler schedule instructions freely across rounds
 * without call overhead.  Using the OpenCL built-in rotate() for ROTL ensures
 * the compiler emits a single rotate instruction rather than two shifts + OR.
 */
#define F(x,y,z) (((x)&(y))|(~(x)&(z)))
#define G(x,y,z) (((x)&(z))|((y)&~(z)))
#define H(x,y,z) ((x)^(y)^(z))
#define II(x,y,z) ((y)^((x)|~(z)))
#define ROTL(x,n) rotate((uint)(x),(uint)(n))
#define STEP(f,a,b,c,d,x,t,s) a+=f(b,c,d)+(x)+(uint)(t); a=ROTL(a,(uint)(s))+b;

void md5_compress(uint *st, __private uint *blk) {
    uint a=st[0],b=st[1],c=st[2],d=st[3];
    STEP(F,a,b,c,d,blk[ 0],0xd76aa478u, 7) STEP(F,d,a,b,c,blk[ 1],0xe8c7b756u,12)
    STEP(F,c,d,a,b,blk[ 2],0x242070dbu,17) STEP(F,b,c,d,a,blk[ 3],0xc1bdceeeu,22)
    STEP(F,a,b,c,d,blk[ 4],0xf57c0fafu, 7) STEP(F,d,a,b,c,blk[ 5],0x4787c62au,12)
    STEP(F,c,d,a,b,blk[ 6],0xa8304613u,17) STEP(F,b,c,d,a,blk[ 7],0xfd469501u,22)
    STEP(F,a,b,c,d,blk[ 8],0x698098d8u, 7) STEP(F,d,a,b,c,blk[ 9],0x8b44f7afu,12)
    STEP(F,c,d,a,b,blk[10],0xffff5bb1u,17) STEP(F,b,c,d,a,blk[11],0x895cd7beu,22)
    STEP(F,a,b,c,d,blk[12],0x6b901122u, 7) STEP(F,d,a,b,c,blk[13],0xfd987193u,12)
    STEP(F,c,d,a,b,blk[14],0xa679438eu,17) STEP(F,b,c,d,a,blk[15],0x49b40821u,22)
    STEP(G,a,b,c,d,blk[ 1],0xf61e2562u, 5) STEP(G,d,a,b,c,blk[ 6],0xc040b340u, 9)
    STEP(G,c,d,a,b,blk[11],0x265e5a51u,14) STEP(G,b,c,d,a,blk[ 0],0xe9b6c7aau,20)
    STEP(G,a,b,c,d,blk[ 5],0xd62f105du, 5) STEP(G,d,a,b,c,blk[10],0x02441453u, 9)
    STEP(G,c,d,a,b,blk[15],0xd8a1e681u,14) STEP(G,b,c,d,a,blk[ 4],0xe7d3fbc8u,20)
    STEP(G,a,b,c,d,blk[ 9],0x21e1cde6u, 5) STEP(G,d,a,b,c,blk[14],0xc33707d6u, 9)
    STEP(G,c,d,a,b,blk[ 3],0xf4d50d87u,14) STEP(G,b,c,d,a,blk[ 8],0x455a14edu,20)
    STEP(G,a,b,c,d,blk[13],0xa9e3e905u, 5) STEP(G,d,a,b,c,blk[ 2],0xfcefa3f8u, 9)
    STEP(G,c,d,a,b,blk[ 7],0x676f02d9u,14) STEP(G,b,c,d,a,blk[12],0x8d2a4c8au,20)
    STEP(H,a,b,c,d,blk[ 5],0xfffa3942u, 4) STEP(H,d,a,b,c,blk[ 8],0x8771f681u,11)
    STEP(H,c,d,a,b,blk[11],0x6d9d6122u,16) STEP(H,b,c,d,a,blk[14],0xfde5380cu,23)
    STEP(H,a,b,c,d,blk[ 1],0xa4beea44u, 4) STEP(H,d,a,b,c,blk[ 4],0x4bdecfa9u,11)
    STEP(H,c,d,a,b,blk[ 7],0xf6bb4b60u,16) STEP(H,b,c,d,a,blk[10],0xbebfbc70u,23)
    STEP(H,a,b,c,d,blk[13],0x289b7ec6u, 4) STEP(H,d,a,b,c,blk[ 0],0xeaa127fau,11)
    STEP(H,c,d,a,b,blk[ 3],0xd4ef3085u,16) STEP(H,b,c,d,a,blk[ 6],0x04881d05u,23)
    STEP(H,a,b,c,d,blk[ 9],0xd9d4d039u, 4) STEP(H,d,a,b,c,blk[12],0xe6db99e5u,11)
    STEP(H,c,d,a,b,blk[15],0x1fa27cf8u,16) STEP(H,b,c,d,a,blk[ 2],0xc4ac5665u,23)
    STEP(II,a,b,c,d,blk[ 0],0xf4292244u, 6) STEP(II,d,a,b,c,blk[ 7],0x432aff97u,10)
    STEP(II,c,d,a,b,blk[14],0xab9423a7u,15) STEP(II,b,c,d,a,blk[ 5],0xfc93a039u,21)
    STEP(II,a,b,c,d,blk[12],0x655b59c3u, 6) STEP(II,d,a,b,c,blk[ 3],0x8f0ccc92u,10)
    STEP(II,c,d,a,b,blk[10],0xffeff47du,15) STEP(II,b,c,d,a,blk[ 1],0x85845dd1u,21)
    STEP(II,a,b,c,d,blk[ 8],0x6fa87e4fu, 6) STEP(II,d,a,b,c,blk[15],0xfe2ce6e0u,10)
    STEP(II,c,d,a,b,blk[ 6],0xa3014314u,15) STEP(II,b,c,d,a,blk[13],0x4e0811a1u,21)
    STEP(II,a,b,c,d,blk[ 4],0xf7537e82u, 6) STEP(II,d,a,b,c,blk[11],0xbd3af235u,10)
    STEP(II,c,d,a,b,blk[ 2],0x2ad7d2bbu,15) STEP(II,b,c,d,a,blk[ 9],0xeb86d391u,21)
    st[0]+=a; st[1]+=b; st[2]+=c; st[3]+=d;
}

/* Computes MD5 of data[0..len-1], writes 16-byte digest. Handles up to 128 bytes. */
void md5(const uchar *data, uint len, uchar *digest) {
    uint st[4] = {0x67452301u, 0xefcdab89u, 0x98badcfeu, 0x10325476u};
    uint blk[16];
    uint i;

    /* Process complete 64-byte blocks */
    uint pos = 0;
    while (pos + 64 <= len) {
        for (i = 0; i < 16; i++) {
            uint j = pos + i*4;
            blk[i] = (uint)data[j] | ((uint)data[j+1]<<8) | ((uint)data[j+2]<<16) | ((uint)data[j+3]<<24);
        }
        md5_compress(st, blk);
        pos += 64;
    }

    /* Final block(s) with padding */
    uint rem = len - pos;
    uchar buf[128];
    for (i = 0; i < rem; i++)  buf[i] = data[pos+i];
    buf[rem] = 0x80;
    for (i = rem+1; i < 128; i++) buf[i] = 0;

    /* Bit-length as 64-bit little-endian */
    ulong bits = (ulong)len * 8;
    uint off = (rem < 56) ? 56 : 120;
    buf[off+0]=(uchar)(bits    ); buf[off+1]=(uchar)(bits>> 8);
    buf[off+2]=(uchar)(bits>>16); buf[off+3]=(uchar)(bits>>24);
    buf[off+4]=(uchar)(bits>>32); buf[off+5]=(uchar)(bits>>40);
    buf[off+6]=(uchar)(bits>>48); buf[off+7]=(uchar)(bits>>56);

    uint nblocks = (rem < 56) ? 1 : 2;
    for (uint b = 0; b < nblocks; b++) {
        for (i = 0; i < 16; i++) {
            uint j = b*64 + i*4;
            blk[i] = (uint)buf[j] | ((uint)buf[j+1]<<8) | ((uint)buf[j+2]<<16) | ((uint)buf[j+3]<<24);
        }
        md5_compress(st, blk);
    }

    for (i = 0; i < 4; i++) {
        digest[i*4+0]=(uchar)(st[i]    ); digest[i*4+1]=(uchar)(st[i]>> 8);
        digest[i*4+2]=(uchar)(st[i]>>16); digest[i*4+3]=(uchar)(st[i]>>24);
    }
}

/* ---- AES-256 ---- */
/*
 * SBOX and SBOX_INV are stored in __constant memory (NVIDIA maps this to a
 * dedicated 64 KB constant cache).  TD0-TD3 are also __constant; they total
 * ~16 KB which fits comfortably in the RTX 2060's 3 MB L2 cache.
 *
 * We tried moving these to __local (shared) memory to avoid non-broadcast
 * constant-cache accesses, but measured a slowdown: the cooperative fill + barrier
 * overhead exceeded the benefit, because the L2 already held the tables hot.
 */
__constant uint SBOX[256] = {
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
};

/* AES InvMixColumns tables, precomputed by Python */
__constant uint TD0[256] = """ + _TD0_SRC + r""";
__constant uint TD1[256] = """ + _TD1_SRC + r""";
__constant uint TD2[256] = """ + _TD2_SRC + r""";
__constant uint TD3[256] = """ + _TD3_SRC + r""";

/*
 * AES-256 key schedule — equivalent inverse cipher (FIPS 197 §5.3.5).
 *
 * The TD-table decryption loop computes InvMixColumns implicitly via table
 * lookups.  For this to work, the round keys for rounds 1-13 (words 4-55)
 * must have InvMixColumns applied to them in advance.  The first and last
 * round keys (words 0-3 and 56-59) are used in AddRoundKey steps that don't
 * go through the TD tables, so they stay as-is.
 *
 * The alternative — the "direct inverse cipher" — would require separate
 * InvSubBytes and InvMixColumns tables and is slower per round.
 */
void aes256_key_expand(const uchar *key, __private uint *rk) {
    /* Step 1: standard AES-256 key expansion */
    const uint RCON[7] = {0x01000000u,0x02000000u,0x04000000u,0x08000000u,
                          0x10000000u,0x20000000u,0x40000000u};
    for (int i = 0; i < 8; i++)
        rk[i] = ((uint)key[i*4]<<24)|((uint)key[i*4+1]<<16)|((uint)key[i*4+2]<<8)|key[i*4+3];
    for (int i = 8; i < 60; i++) {
        uint t = rk[i-1];
        if (i % 8 == 0) {
            t = (SBOX[(t>>16)&0xff]<<24)|(SBOX[(t>>8)&0xff]<<16)|(SBOX[t&0xff]<<8)|SBOX[(t>>24)&0xff];
            t ^= RCON[i/8 - 1];
        } else if (i % 8 == 4) {
            t = (SBOX[(t>>24)&0xff]<<24)|(SBOX[(t>>16)&0xff]<<16)|(SBOX[(t>>8)&0xff]<<8)|SBOX[t&0xff];
        }
        rk[i] = rk[i-8] ^ t;
    }
    /* Step 2: apply InvMixColumns to round key words 4..55 so that the
       TD-table XOR in each middle round produces the correct result.
       InvMixColumns(w) = TD0[SBOX[b0]] ^ TD1[SBOX[b1]] ^ TD2[SBOX[b2]] ^ TD3[SBOX[b3]] */
    for (int i = 4; i < 56; i++) {
        uint w = rk[i];
        rk[i] = TD0[SBOX[(w>>24)&0xff]] ^ TD1[SBOX[(w>>16)&0xff]]
               ^ TD2[SBOX[(w>>8)&0xff]]  ^ TD3[SBOX[w&0xff]];
    }
}


__constant uchar SBOX_INV[256] = {
    0x52,0x09,0x6a,0xd5,0x30,0x36,0xa5,0x38,0xbf,0x40,0xa3,0x9e,0x81,0xf3,0xd7,0xfb,
    0x7c,0xe3,0x39,0x82,0x9b,0x2f,0xff,0x87,0x34,0x8e,0x43,0x44,0xc4,0xde,0xe9,0xcb,
    0x54,0x7b,0x94,0x32,0xa6,0xc2,0x23,0x3d,0xee,0x4c,0x95,0x0b,0x42,0xfa,0xc3,0x4e,
    0x08,0x2e,0xa1,0x66,0x28,0xd9,0x24,0xb2,0x76,0x5b,0xa2,0x49,0x6d,0x8b,0xd1,0x25,
    0x72,0xf8,0xf6,0x64,0x86,0x68,0x98,0x16,0xd4,0xa4,0x5c,0xcc,0x5d,0x65,0xb6,0x92,
    0x6c,0x70,0x48,0x50,0xfd,0xed,0xb9,0xda,0x5e,0x15,0x46,0x57,0xa7,0x8d,0x9d,0x84,
    0x90,0xd8,0xab,0x00,0x8c,0xbc,0xd3,0x0a,0xf7,0xe4,0x58,0x05,0xb8,0xb3,0x45,0x06,
    0xd0,0x2c,0x1e,0x8f,0xca,0x3f,0x0f,0x02,0xc1,0xaf,0xbd,0x03,0x01,0x13,0x8a,0x6b,
    0x3a,0x91,0x11,0x41,0x4f,0x67,0xdc,0xea,0x97,0xf2,0xcf,0xce,0xf0,0xb4,0xe6,0x73,
    0x96,0xac,0x74,0x22,0xe7,0xad,0x35,0x85,0xe2,0xf9,0x37,0xe8,0x1c,0x75,0xdf,0x6e,
    0x47,0xf1,0x1a,0x71,0x1d,0x29,0xc5,0x89,0x6f,0xb7,0x62,0x0e,0xaa,0x18,0xbe,0x1b,
    0xfc,0x56,0x3e,0x4b,0xc6,0xd2,0x79,0x20,0x9a,0xdb,0xc0,0xfe,0x78,0xcd,0x5a,0xf4,
    0x1f,0xdd,0xa8,0x33,0x88,0x07,0xc7,0x31,0xb1,0x12,0x10,0x59,0x27,0x80,0xec,0x5f,
    0x60,0x51,0x7f,0xa9,0x19,0xb5,0x4a,0x0d,0x2d,0xe5,0x7a,0x9f,0x93,0xc9,0x9c,0xef,
    0xa0,0xe0,0x3b,0x4d,0xae,0x2a,0xf5,0xb0,0xc8,0xeb,0xbb,0x3c,0x83,0x53,0x99,0x61,
    0x17,0x2b,0x04,0x7e,0xba,0x77,0xd6,0x26,0xe1,0x69,0x14,0x63,0x55,0x21,0x0c,0x7d
};

/*
 * AES-256 CBC decrypt one 16-byte block given a pre-expanded key schedule.
 *
 * Key expansion is separated from block decryption so we expand once and reuse
 * rk[] for both ciphertext blocks.  This saves ~60 operations per password.
 *
 * Each middle round does 4 TD-table lookups + XOR per column (4 columns = 16
 * lookups per round, 13 middle rounds = 208 lookups total per block).  The
 * final round uses SBOX_INV directly because InvMixColumns is not applied there.
 *
 * Note the InvShiftRows pattern in the column index offsets: s0 uses row 0 of
 * each column un-shifted, s1 uses row 3, s2 uses row 2, s3 uses row 1 — this
 * is the inverse of AES's ShiftRows baked into the column selection.
 */
void aes256_block_decrypt(__private uint *rk, const uchar *xor_block, const uchar *ct, uchar *pt) {
    uint s0 = ((uint)ct[ 0]<<24)|((uint)ct[ 1]<<16)|((uint)ct[ 2]<<8)|ct[ 3];
    uint s1 = ((uint)ct[ 4]<<24)|((uint)ct[ 5]<<16)|((uint)ct[ 6]<<8)|ct[ 7];
    uint s2 = ((uint)ct[ 8]<<24)|((uint)ct[ 9]<<16)|((uint)ct[10]<<8)|ct[11];
    uint s3 = ((uint)ct[12]<<24)|((uint)ct[13]<<16)|((uint)ct[14]<<8)|ct[15];

    s0^=rk[56]; s1^=rk[57]; s2^=rk[58]; s3^=rk[59];

    uint t0,t1,t2,t3;
    for (int r = 13; r >= 1; r--) {
        t0 = TD0[(s0>>24)&0xff]^TD1[(s3>>16)&0xff]^TD2[(s2>>8)&0xff]^TD3[s1&0xff]^rk[r*4+0];
        t1 = TD0[(s1>>24)&0xff]^TD1[(s0>>16)&0xff]^TD2[(s3>>8)&0xff]^TD3[s2&0xff]^rk[r*4+1];
        t2 = TD0[(s2>>24)&0xff]^TD1[(s1>>16)&0xff]^TD2[(s0>>8)&0xff]^TD3[s3&0xff]^rk[r*4+2];
        t3 = TD0[(s3>>24)&0xff]^TD1[(s2>>16)&0xff]^TD2[(s1>>8)&0xff]^TD3[s0&0xff]^rk[r*4+3];
        s0=t0; s1=t1; s2=t2; s3=t3;
    }
    /* Final round: InvShiftRows + InvSubBytes (no InvMixColumns) */
    t0 = ((uint)SBOX_INV[(s0>>24)&0xff]<<24)|((uint)SBOX_INV[(s3>>16)&0xff]<<16)|((uint)SBOX_INV[(s2>>8)&0xff]<<8)|SBOX_INV[s1&0xff];
    t1 = ((uint)SBOX_INV[(s1>>24)&0xff]<<24)|((uint)SBOX_INV[(s0>>16)&0xff]<<16)|((uint)SBOX_INV[(s3>>8)&0xff]<<8)|SBOX_INV[s2&0xff];
    t2 = ((uint)SBOX_INV[(s2>>24)&0xff]<<24)|((uint)SBOX_INV[(s1>>16)&0xff]<<16)|((uint)SBOX_INV[(s0>>8)&0xff]<<8)|SBOX_INV[s3&0xff];
    t3 = ((uint)SBOX_INV[(s3>>24)&0xff]<<24)|((uint)SBOX_INV[(s2>>16)&0xff]<<16)|((uint)SBOX_INV[(s1>>8)&0xff]<<8)|SBOX_INV[s0&0xff];

    /* AddRoundKey (round 0) */
    t0^=rk[0]; t1^=rk[1]; t2^=rk[2]; t3^=rk[3];

    /* XOR with IV / previous ciphertext block (CBC) */
    uint x0 = ((uint)xor_block[ 0]<<24)|((uint)xor_block[ 1]<<16)|((uint)xor_block[ 2]<<8)|xor_block[ 3];
    uint x1 = ((uint)xor_block[ 4]<<24)|((uint)xor_block[ 5]<<16)|((uint)xor_block[ 6]<<8)|xor_block[ 7];
    uint x2 = ((uint)xor_block[ 8]<<24)|((uint)xor_block[ 9]<<16)|((uint)xor_block[10]<<8)|xor_block[11];
    uint x3 = ((uint)xor_block[12]<<24)|((uint)xor_block[13]<<16)|((uint)xor_block[14]<<8)|xor_block[15];
    t0^=x0; t1^=x1; t2^=x2; t3^=x3;

    pt[ 0]=(uchar)(t0>>24); pt[ 1]=(uchar)(t0>>16); pt[ 2]=(uchar)(t0>>8); pt[ 3]=(uchar)t0;
    pt[ 4]=(uchar)(t1>>24); pt[ 5]=(uchar)(t1>>16); pt[ 6]=(uchar)(t1>>8); pt[ 7]=(uchar)t1;
    pt[ 8]=(uchar)(t2>>24); pt[ 9]=(uchar)(t2>>16); pt[10]=(uchar)(t2>>8); pt[11]=(uchar)t2;
    pt[12]=(uchar)(t3>>24); pt[13]=(uchar)(t3>>16); pt[14]=(uchar)(t3>>8); pt[15]=(uchar)t3;
}

/* ---- Base58 validation ---- */
/*
 * Bitcoin private keys in WIF format are base58-encoded, using the alphabet
 * 123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz (58 chars).
 * Note the deliberate omissions: 0 (zero), O, I, l — visually ambiguous chars.
 *
 * Checking base58 validity is much cheaper than a full Bitcoin key parse, so we
 * use it as the rejection filter.  A wrong password decrypts to random bytes;
 * the probability that all 32 decrypted bytes happen to be valid base58 chars
 * purely by chance is (58/94)^32 ≈ 1 in 10^8 — effectively zero false positives.
 */
/* Returns 1 if ALL bytes in buf[0..len-1] are valid base58 characters */
int all_b58(const uchar *buf, uint len) {
    for (uint i = 0; i < len; i++) {
        uchar c = buf[i];
        if (c < '1' || c > 'z') return 0;
        if (c > '9' && c < 'A') return 0;
        if (c > 'Z' && c < 'a') return 0;
        if (c == 'I' || c == 'O' || c == 'l') return 0;
    }
    return 1;
}

/* ---- Main kernel ---- */
/*
 * One work item = one password.  We chose this 1:1 mapping because:
 *   - Each password requires independent state (salted[], key1/key2/iv, rk[60]).
 *   - The RTX 2060 has 1920 CUDA cores; with batch sizes of 1M we launch far
 *     more work items than cores, so the GPU scheduler keeps all cores busy.
 *   - Mapping multiple passwords per work item would complicate register usage
 *     without improving occupancy (we're already thread-count limited, not
 *     memory-limited).
 *
 * Two-stage early rejection minimises wasted AES work:
 *   Stage 1: after decrypting block 1, check the first byte (must be L/K/5/Q).
 *            Only 4 of 256 values pass → 98.4% of wrong passwords exit here.
 *   Stage 2: check remaining 15 bytes of block 1 for base58 validity.
 *   Stage 3: decrypt block 2 and check all 16 bytes for base58 validity.
 *
 * Parameters:
 *   pw_data:   flat array of passwords, each padded to pw_stride bytes
 *   pw_lens:   byte length of each password (0 = padding slot, skip it)
 *   salt:      8-byte MultiBit salt (same for all passwords in a batch)
 *   enc:       32-byte ciphertext from the wallet file (same for all passwords)
 *   found_idx: output — set atomically to (gid+1) of the first matching password
 */
__kernel void multibit_check(
    __global const uchar *pw_data,
    __global const uint  *pw_lens,
    __constant     uchar *salt,
    __constant     uchar *enc,
    __global       uint  *found_idx,
    const          uint   pw_stride
) {
    uint gid = get_global_id(0);

    uint pw_len = pw_lens[gid];
    if (pw_len == 0) return;

    /* Build salted = password + salt (max 128 bytes) */
    uchar salted[136];
    __global const uchar *pw = pw_data + gid * pw_stride;
    for (uint i = 0; i < pw_len; i++) salted[i] = pw[i];
    for (uint i = 0; i < 8; i++)     salted[pw_len + i] = salt[i];
    uint salted_len = pw_len + 8;

    /* key1 = MD5(salted) */
    uchar key1[16];
    md5(salted, salted_len, key1);

    /* key2 = MD5(key1 + salted) */
    uchar tmp[152];
    for (uint i = 0; i < 16; i++)         tmp[i]    = key1[i];
    for (uint i = 0; i < salted_len; i++) tmp[16+i] = salted[i];
    uchar key2[16];
    md5(tmp, 16 + salted_len, key2);

    /* iv = MD5(key2 + salted) */
    for (uint i = 0; i < 16; i++)         tmp[i]    = key2[i];
    /* salted already in tmp[16..] from above */
    uchar iv[16];
    md5(tmp, 16 + salted_len, iv);

    /* aes_key = key1 + key2 */
    uchar aes_key[32];
    for (uint i = 0; i < 16; i++) aes_key[i]    = key1[i];
    for (uint i = 0; i < 16; i++) aes_key[16+i] = key2[i];

    /* Copy enc to private memory.
     * NVIDIA's OpenCL compiler enforces strict address space rules: passing a
     * __constant pointer where a generic pointer is expected is a compile error.
     * Copying to a private array resolves this without any runtime cost. */
    uchar enc_local[32];
    for (uint i = 0; i < 32; i++) enc_local[i] = enc[i];

    /* Expand key once; reuse for both AES blocks */
    uint rk[60];
    aes256_key_expand(aes_key, rk);

    /* Decrypt first AES block (IV = iv, CT = enc_local[0..15]) */
    uchar pt1[16];
    aes256_block_decrypt(rk, iv, enc_local, pt1);

    /* Quick check: first byte must be L, K, 5, or Q */
    uchar b0 = pt1[0];
    if (b0 != 'L' && b0 != 'K' && b0 != '5' && b0 != 'Q') return;

    /* Remaining 15 bytes of first block must all be valid base58 */
    if (!all_b58(pt1 + 1, 15)) return;

    /* Decrypt second AES block (IV = first ciphertext block = enc_local[0..15]) */
    uchar pt2[16];
    aes256_block_decrypt(rk, enc_local, enc_local + 16, pt2);
    if (!all_b58(pt2, 16)) return;

    /* Found! Record this work item's index (1-based) */
    atomic_cmpxchg(found_idx, 0u, gid + 1u);
}
"""

# ---------------------------------------------------------------------------
# MultiBit wallet loader
#
# A MultiBit Classic .key file is a text file whose first 64 characters are
# base64-encoded data in OpenSSL "Salted__" format:
#   bytes  0- 7: magic "Salted__"
#   bytes  8-15: 8-byte random salt
#   bytes 16-47: 32 bytes of AES-256-CBC ciphertext (two 16-byte blocks)
# We only need the salt and the first 32 ciphertext bytes; everything else
# in the file can be ignored.
# ---------------------------------------------------------------------------

class WalletMultiBit:
    def __init__(self, encrypted_block, salt):
        self._enc   = encrypted_block  # 32 bytes
        self._salt  = salt             # 8 bytes

    @classmethod
    def load(cls, filename: str) -> "WalletMultiBit":
        with open(filename, "r") as f:
            raw = f.read(70)
        data = b"".join(raw.encode("ascii").split())
        if len(data) < 64:
            raise ValueError("MultiBit key file too short")
        data = base64.b64decode(data[:64])
        # assert replaced with explicit check: assert is silently disabled
        # by the Python -O flag, making it unreliable as an error guard.
        if data[:8] != b"Salted__":
            raise ValueError("Not a MultiBit key file (missing 'Salted__' header)")
        if len(data) < 48:
            raise ValueError("MultiBit key file decodes to less than 48 bytes")
        return cls(encrypted_block=data[16:48], salt=data[8:16])

    def verify_cpu(self, password: str) -> bool:
        """CPU re-verification of a GPU hit before reporting it as found.
        The GPU uses atomic_cmpxchg which can in theory store a false positive
        (e.g. a base58 collision); this CPU check is the authoritative test."""
        pw_bytes = password.encode("utf-8")
        salted = pw_bytes + self._salt
        key1 = hashlib.md5(salted).digest()
        key2 = hashlib.md5(key1 + salted).digest()
        iv   = hashlib.md5(key2 + salted).digest()
        aes_key = key1 + key2
        pt = _cpu_aes_decrypt(aes_key, iv, self._enc[:16])
        if pt[0:1] not in (b"L", b"K", b"5", b"Q"):
            return False
        b58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        if pt[1:].translate(None, b58):
            return False
        pt2 = _cpu_aes_decrypt(aes_key, self._enc[:16], self._enc[16:32])
        return not pt2.translate(None, b58)

# ---------------------------------------------------------------------------
# OpenCL engine
#
# BATCH_SIZE: 1M passwords per kernel launch.  Larger batches amortise kernel
# launch overhead and give the GPU more work to pipeline internally.  We chose
# 1M after testing; at ~500k passwords/s generation rate one batch takes ~2 s,
# which is long enough to keep the GPU fed between launches.
#
# PW_STRIDE: all passwords padded to 128 bytes so each work item can find its
# password with a simple pointer offset (gid * stride).  Variable-length storage
# would require a separate offset table and non-coalesced memory access.
# ---------------------------------------------------------------------------

BATCH_SIZE   = 1048576  # passwords per GPU kernel launch
PW_STRIDE    = 128      # fixed stride per password (max supported length)

class GPUEngine:
    def __init__(self, wallet, batch_size=BATCH_SIZE):
        self._wallet     = wallet
        self._batch_size = batch_size
        platforms = cl.get_platforms()
        gpu_devices = []
        for p in platforms:
            gpu_devices += p.get_devices(cl.device_type.GPU)
        if not gpu_devices:
            # Raise instead of sys.exit: constructors should not terminate
            # the process; let the caller decide how to handle missing hardware.
            raise RuntimeError("No OpenCL GPU devices found.")
        self._device  = gpu_devices[0]
        print(f"Using GPU: {self._device.name.strip()}")
        self._ctx     = cl.Context([self._device])
        self._queue   = cl.CommandQueue(self._ctx)
        # PyOpenCL caches compiled kernels on disk keyed by source hash, so
        # subsequent runs skip recompilation (~5 s saved per run).
        self._program = cl.Program(self._ctx, KERNEL_SOURCE).build()
        self._kernel  = self._program.multibit_check

        mf = cl.mem_flags
        enc_np   = np.frombuffer(wallet._enc,  dtype=np.uint8)
        salt_np  = np.frombuffer(wallet._salt, dtype=np.uint8)
        # Salt and ciphertext are the same for every password in every batch,
        # so we upload them once at init and keep them in GPU memory permanently.
        self._enc_buf  = cl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=enc_np)
        self._salt_buf = cl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=salt_np)

        # Password and length buffers are reused each batch to avoid repeated
        # allocation of 128 MB (batch_size * PW_STRIDE).
        self._pw_buf    = cl.Buffer(self._ctx, mf.READ_ONLY,  batch_size * PW_STRIDE)
        self._len_buf   = cl.Buffer(self._ctx, mf.READ_ONLY,  batch_size * 4)
        # Single uint output: 0 = not found; N = (gid+1) of the matching password.
        self._found_buf = cl.Buffer(self._ctx, mf.READ_WRITE, 4)
        self._found_np  = np.zeros(1, dtype=np.uint32)

    # Context manager support (RAII equivalent): ensures GPU buffers are
    # released even if an exception occurs, rather than relying on GC.
    def __enter__(self) -> "GPUEngine":
        return self

    def __exit__(self, *_) -> None:
        for buf in (self._pw_buf, self._len_buf, self._found_buf,
                    self._enc_buf, self._salt_buf):
            buf.release()

    def check_batch(self, passwords: List[str]) -> Optional[str]:
        """Check a list of password strings on the GPU.
        Returns the found password string, or None."""
        n = len(passwords)
        stride = PW_STRIDE

        # Pack passwords into a flat byte buffer.  We use a bytearray (not a 2D
        # numpy array) because numpy's per-row frombuffer calls add Python overhead
        # for each password.  A single bytearray write followed by one np.frombuffer
        # call is ~2x faster for large batches.
        raw = bytearray(n * stride)
        len_arr = np.zeros(n, dtype=np.uint32)
        for i, pw in enumerate(passwords):
            # ascii not utf-8: passwords are always ASCII; utf-8 adds
            # unnecessary overhead and can produce multi-byte sequences
            # for non-ASCII chars that the kernel doesn't expect.
            pb = pw.encode("ascii", "ignore")
            pw_len = len(pb)
            if pw_len > stride:
                pw_len = stride
                pb = pb[:stride]
            raw[i*stride : i*stride+pw_len] = pb
            len_arr[i] = pw_len

        pw_np = np.frombuffer(raw, dtype=np.uint8)
        self._found_np[0] = 0

        cl.enqueue_copy(self._queue, self._pw_buf,    pw_np)
        cl.enqueue_copy(self._queue, self._len_buf,   len_arr)
        cl.enqueue_copy(self._queue, self._found_buf, self._found_np)

        self._kernel(
            self._queue, (n,), None,
            self._pw_buf, self._len_buf,
            self._salt_buf, self._enc_buf,
            self._found_buf,
            np.uint32(stride)
        )
        cl.enqueue_copy(self._queue, self._found_np, self._found_buf)
        self._queue.finish()

        if self._found_np[0]:
            candidate = passwords[self._found_np[0] - 1]
            if self._wallet.verify_cpu(candidate):
                return candidate
        return None

# ---------------------------------------------------------------------------
# Token list parser and password generator
#
# Ported from btcrecover's btcrpass.py so we can read the same .txt token list
# files without modification.  The format is:
#   - One line per "slot" in the password.
#   - Tokens on the same line are mutually exclusive (pick one per combination).
#   - Lines prefixed with "+" are required; all others are optional (a None
#     placeholder is prepended so they can be skipped).
#   - Tokens prefixed with "^" or suffixed with "$" are positional anchors.
#   - "%" introduces a wildcard that expands to many concrete strings at parse time.
#
# All combinations of one token per line are generated via itertools.product,
# then each combination is expanded into all valid permutations of its tokens.
# Positional anchors override permutation order for specific tokens.
# ---------------------------------------------------------------------------

class AnchoredToken:
    POSITIONAL = 1
    RELATIVE   = 2
    MIDDLE     = 3

    def __init__(self, token, line_num: int | str = "?"):
        if token.startswith("^"):
            m = re.match(r"\^(?:(?P<begin>\d+)?(?P<middle>,)(?P<end>\d+)?|(?P<rel>[rR])?(?P<pos>\d+))[\^$]", token)
            if m:
                if m.group("middle"):
                    begin = int(m.group("begin")) if m.group("begin") else 2
                    end   = int(m.group("end"))   if m.group("end")   else sys.maxsize
                    if begin > end or begin < 2:
                        raise ValueError(f"line {line_num}: invalid anchor range")
                    self.type  = self.MIDDLE
                    self.begin = begin - 1
                    self.end   = end   - 1 if end != sys.maxsize else end
                else:
                    pos = int(m.group("pos"))
                    if m.group("rel"):
                        self.type = self.RELATIVE
                        self.pos  = pos
                    else:
                        self.type = self.POSITIONAL
                        self.pos  = pos - 1
                self.text = token[m.end():]
            else:
                self.type = self.POSITIONAL
                self.pos  = 0
                self.text = token[1:]
            if self.text.endswith("$"):
                raise ValueError(f"line {line_num}: token has both ^ and $ anchors")
        elif token.endswith("$"):
            self.type = self.POSITIONAL
            self.pos  = -1  # sentinel for end-anchor; resolved to n-1 at use
            self.text = token[:-1]
        else:
            raise ValueError("Not an anchored token")
        self._str  = token
        self._hash = hash(self._str)

    def __hash__(self):      return self._hash
    def __eq__(self, o):     return isinstance(o, AnchoredToken) and self._str == o._str
    def __str__(self):       return self._str
    def __repr__(self):      return f"AnchoredToken({self._str!r})"


def _expand_wildcard(wc_str):
    """Expand a single wildcard spec like '%d', '%0,4d', '%3,4[0-9]' into a list of strings."""
    # %[N[,M]]<type>  or  %[N[,M]][<charset>]
    m = re.match(r"%(?:(\d+)(?:,(\d+))?)?(\[.+?\]|[dansANpPyYwWsltTrRbS%^]|i[dansAN])", wc_str)
    if not m:
        return [wc_str]  # not a recognized wildcard, return as-is

    min_r = int(m.group(1)) if m.group(1) is not None else 1
    max_r = int(m.group(2)) if m.group(2) is not None else min_r
    wtype = m.group(3)

    sets = {
        "d": string.digits,
        "a": string.ascii_lowercase,
        "A": string.ascii_uppercase,
        "n": string.ascii_lowercase + string.digits,
        "N": string.ascii_uppercase + string.digits,
        "s": " ",
        "p": "".join(chr(c) for c in range(33, 127)),
        "y": string.punctuation,
        "Y": string.digits + string.punctuation,
    }

    if wtype.startswith("[") and wtype.endswith("]"):
        charset = wtype[1:-1]
        # Handle ranges like 0-9, a-z
        expanded = ""
        i = 0
        while i < len(charset):
            if i+2 < len(charset) and charset[i+1] == "-":
                for c in range(ord(charset[i]), ord(charset[i+2])+1):
                    expanded += chr(c)
                i += 3
            else:
                expanded += charset[i]
                i += 1
        chars = expanded
    elif wtype.startswith("i"):
        base = sets.get(wtype[1], "")
        chars = base.upper() + base.lower() if wtype[1].islower() else base.lower() + base.upper()
    else:
        chars = sets.get(wtype, wtype)

    results = []
    for length in range(min_r, max_r + 1):
        for combo in itertools.product(chars, repeat=length):
            results.append("".join(combo))
    return results


def _token_to_strings(token):
    """Expand a token string (possibly containing wildcards) into a list of concrete strings."""
    if "%" not in token:
        return [token]
    # Find wildcards and expand them
    parts = re.split(r"(%(?:\d+(?:,\d+)?)?(?:\[.+?\]|[a-zA-Z%^]))", token)
    options = [[]]
    for part in parts:
        if part.startswith("%"):
            expanded = _expand_wildcard(part)
            options = [prev + [e] for prev in options for e in expanded]
        else:
            options = [prev + [part] for prev in options]
    return ["".join(p) for p in options]


def parse_tokenlist(filepath):
    """Parse a btcrecover token list file. Returns token_lists structure."""
    token_lists = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if not parts:
                continue

            required = False
            if parts[0] == "+":
                required = True
                parts = parts[1:]
                if not parts:
                    continue

            tokens = []
            for raw in parts:
                if raw.startswith("^") or raw.endswith("$"):
                    try:
                        tok = AnchoredToken(raw, line_num)
                        # Expand wildcards in the token's text
                        expanded_texts = _token_to_strings(tok.text)
                        for et in expanded_texts:
                            at = AnchoredToken.__new__(AnchoredToken)
                            at.type = tok.type
                            at.pos  = tok.pos if hasattr(tok, "pos") else 0
                            if hasattr(tok, "begin"):
                                at.begin = tok.begin
                            if hasattr(tok, "end"):
                                at.end = tok.end
                            at.text  = et
                            at._str  = raw
                            at._hash = hash(raw)
                            tokens.append(at)
                    except ValueError:
                        tokens.extend(_token_to_strings(raw))
                else:
                    tokens.extend(_token_to_strings(raw))

            if not tokens:
                continue

            if required:
                token_lists.append(tokens)          # required: no leading None
            else:
                token_lists.append([None] + tokens) # optional: leading None means "skip"

    # Reverse so that itertools.product cycles the last file line fastest,
    # matching btcrecover's iteration order (last line = innermost loop).
    token_lists.reverse()
    return token_lists


def _assemble(ordered_tokens):
    """Assemble an ordered list of tokens (strings + AnchoredTokens) into a password string."""
    n = len(ordered_tokens)
    result = [None] * n
    free   = []

    for tok in ordered_tokens:
        if isinstance(tok, AnchoredToken):
            if tok.type == AnchoredToken.POSITIONAL:
                pos = (n - 1) if tok.pos == -1 else tok.pos
                if pos >= n or result[pos] is not None:
                    return None  # conflict
                result[pos] = tok.text
            else:
                free.append(tok)
        else:
            free.append(tok)

    # Fill free slots in order
    slot = 0
    for tok in free:
        while slot < n and result[slot] is not None:
            slot += 1
        if slot >= n:
            return None
        if isinstance(tok, AnchoredToken):
            if tok.type == AnchoredToken.MIDDLE:
                actual_pos = slot
                if not (tok.begin <= actual_pos <= tok.end):
                    return None
            result[slot] = tok.text
        else:
            result[slot] = tok

        slot += 1

    if None in result:
        return None
    return "".join(r for r in result if r is not None)


def _count_combo_passwords(tokens):
    """Count passwords a combo yields without generating them (O(n) per combo).
    Used by find_combo_position to locate a save-file position without building
    password strings.  For non-anchored combos this is simply n!; for anchored
    combos it is (number of free tokens)! since positional tokens don't permute."""
    n = len(tokens)
    has_anchors = any(isinstance(t, AnchoredToken) for t in tokens)
    if not has_anchors:
        return factorial(n)
    positional = {}
    free_tokens = []
    for tok in tokens:
        if isinstance(tok, AnchoredToken) and tok.type == AnchoredToken.POSITIONAL:
            pos = (n - 1) if tok.pos == -1 else tok.pos
            if pos in positional:
                return 0  # conflict — combo produces nothing
            positional[pos] = tok.text
        else:
            free_tokens.append(tok)
    return factorial(len(free_tokens))


def find_combo_position(token_lists, target_skip):
    """Return (combo_idx, skip_in_combo) for a target password count.

    Old save files stored only a linear password count (skip).  To restore
    quickly we need to convert that count into a (combo_idx, perm_offset) pair
    so password_generator can use itertools.islice to jump to the right product
    combo at C speed rather than iterating through millions of passwords in Python.

    This function does that conversion: it iterates the product in C (fast),
    calling _count_combo_passwords() for each combo (O(n), no string building),
    and stops when the cumulative count reaches target_skip."""
    count = 0
    last_idx = 0
    for combo_idx, combo in enumerate(itertools.product(*token_lists)):
        last_idx = combo_idx
        tokens = [t for t in combo if t is not None]
        if not tokens:
            continue
        pw_count = _count_combo_passwords(tokens)
        if count + pw_count > target_skip:
            return combo_idx, target_skip - count
        count += pw_count
        if count == target_skip:
            return combo_idx + 1, 0
    return last_idx + 1, 0


def password_generator(token_lists, start_combo=0, skip_in_combo=0):
    """Yields (combo_idx, pw_in_combo, password).

    combo_idx and pw_in_combo are tracked so save_state() can record the exact
    position without counting passwords; on restore, start_combo allows us to
    jump to that position using itertools.islice (pure C, much faster than
    replaying all the Python generator logic from the start).

    start_combo: product iterator is advanced to this index at C speed via islice.
    skip_in_combo: passwords to skip within the first resumed combo (handles the
                   case where a save happened mid-combo)."""
    product_iter = itertools.product(*token_lists)
    if start_combo > 0:
        # islice advances the C-level product iterator without executing any
        # Python password-generation code — this is the key performance win
        # vs. the old approach of iterating through skip_count passwords.
        for _ in itertools.islice(product_iter, start_combo):
            pass

    for combo_idx, combo in enumerate(product_iter, start=start_combo):
        tokens = [t for t in combo if t is not None]
        if not tokens:
            continue

        n = len(tokens)
        has_anchors = any(isinstance(t, AnchoredToken) for t in tokens)
        first_combo = (combo_idx == start_combo)

        if not has_anchors:
            pw_in_combo = 0
            for perm in itertools.permutations(tokens):
                if first_combo and pw_in_combo < skip_in_combo:
                    pw_in_combo += 1
                    continue
                yield combo_idx, pw_in_combo, "".join(perm)
                pw_in_combo += 1
            continue

        positional = {}
        free_tokens = []
        skip = False
        for tok in tokens:
            if isinstance(tok, AnchoredToken) and tok.type == AnchoredToken.POSITIONAL:
                pos = (n - 1) if tok.pos == -1 else tok.pos
                if pos in positional:
                    skip = True
                    break
                positional[pos] = tok.text
            else:
                free_tokens.append(tok)

        if skip:
            continue

        pw_in_combo = 0
        for perm in itertools.permutations(free_tokens):
            result = [None] * n
            for pos, text in positional.items():
                result[pos] = text
            free_idx = 0
            for i in range(n):
                if result[i] is None:
                    tok = perm[free_idx]
                    result[i] = tok.text if isinstance(tok, AnchoredToken) else tok
                    free_idx += 1
            if None not in result:
                if first_combo and pw_in_combo < skip_in_combo:
                    pw_in_combo += 1
                    continue
                yield combo_idx, pw_in_combo, "".join(r for r in result if r is not None)
                pw_in_combo += 1

# ---------------------------------------------------------------------------
# Save / restore
#
# SaveState is a dataclass rather than a raw dict so that:
#   - Fields are named and typed (interface clarity, C++ Core Guidelines I.4)
#   - Accidental typos in key names are caught at definition time
#   - The structure is self-documenting
#
# We save combo_idx + perm_idx alongside the plain password count (skip_count).
# skip_count is kept for display purposes and backward compatibility with old
# saves that lack combo_idx.  combo_idx + perm_idx is what makes restoration
# fast: islice to the right product combo at C speed, then skip perm_idx
# permutations within that combo.
#
# Autosave fires every 30 seconds so at most 30 seconds of progress can be lost
# on an ungraceful exit (Ctrl+C during a kernel call, power loss, etc.).
# ---------------------------------------------------------------------------

@dataclass
class SaveState:
    skip:      int
    combo_idx: int
    perm_idx:  int
    tokenlist: str
    wallet:    str

def save_state(path: str, skip_count: int, combo_idx: int, perm_idx: int,
               tokenlist_path: str, wallet_path: str) -> None:
    state = SaveState(
        skip      = skip_count,
        combo_idx = combo_idx,
        perm_idx  = perm_idx,
        tokenlist = tokenlist_path,
        wallet    = wallet_path,
    )
    with open(path, "wb") as f:
        pickle.dump(state, f)

def load_state(path: str) -> SaveState:
    with open(path, "rb") as f:
        raw = pickle.load(f)
    # Handle old dict-format saves (before SaveState dataclass was introduced)
    if isinstance(raw, dict):
        return SaveState(
            skip      = raw.get("skip", 0),
            combo_idx = raw.get("combo_idx", 0),
            perm_idx  = raw.get("perm_idx",  0),
            tokenlist = raw.get("tokenlist", ""),
            wallet    = raw.get("wallet",    ""),
        )
    return raw

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _batch_producer(gen, batch_size, q):
    """Background thread: pulls passwords from the generator and queues batches.

    Running generation in a separate thread lets the CPU build batch N+1 while
    the main thread is transferring and processing batch N on the GPU.  Without
    this overlap, the GPU would be idle during the entire generation phase.

    Queue maxsize=2 means at most one prefetched batch sits in memory waiting.
    Larger values would pre-generate more but waste memory and don't help since
    the GPU processes each batch faster than the CPU generates the next one."""
    batch_pws  = []
    last_combo = 0
    last_perm  = 0
    for combo_idx, pw_in_combo, pw in gen:
        batch_pws.append(pw)
        last_combo = combo_idx
        last_perm  = pw_in_combo
        if len(batch_pws) >= batch_size:
            q.put((batch_pws, last_combo, last_perm))
            batch_pws = []
    if batch_pws:
        q.put((batch_pws, last_combo, last_perm))
    q.put(None)  # sentinel


def main():
    parser = argparse.ArgumentParser(description="MultiBit GPU password recovery")
    parser.add_argument("--wallet",    help="Path to MultiBit .key file")
    parser.add_argument("--tokenlist", help="Path to btcrecover token list file")
    parser.add_argument("--skip",      type=int, default=0, help="Skip first N passwords")
    parser.add_argument("--autosave",  help="Save progress to this file every batch")
    parser.add_argument("--restore",   help="Restore progress from a save file")
    parser.add_argument("--batch-size",type=int, default=BATCH_SIZE, help="GPU batch size")
    args = parser.parse_args()

    skip_count     = args.skip
    tokenlist_path = args.tokenlist
    wallet_path    = args.wallet
    start_combo    = 0
    start_perm     = 0

    if args.restore:
        state          = load_state(args.restore)  # returns SaveState dataclass
        skip_count     = state.skip
        tokenlist_path = state.tokenlist
        wallet_path    = state.wallet
        start_combo    = state.combo_idx or None   # 0 treated as "not set" for migration
        start_perm     = state.perm_idx
        print(f"Restored from {args.restore}, resuming at password #{skip_count:,}")

    if not wallet_path or not tokenlist_path:
        parser.error("--wallet and --tokenlist are required (or --restore)")

    wallet      = WalletMultiBit.load(wallet_path)
    token_lists = parse_tokenlist(tokenlist_path)
    batch_size  = args.batch_size

    # 'with' ensures GPU buffers are released even if an exception occurs,
    # rather than relying on the garbage collector to eventually call __del__.
    with GPUEngine(wallet, batch_size) as engine:
      _run_search(engine, args, token_lists, wallet_path, tokenlist_path,
                  skip_count, start_combo, start_perm)


def _run_search(engine, args, token_lists, wallet_path, tokenlist_path,
                skip_count, start_combo, start_perm):
    """Inner search loop, separated so GPUEngine 'with' block is clean."""
    batch_size = engine._batch_size

    # Old save files only store skip_count, not combo_idx.
    # Run a fast counting pass (no string building) to locate the combo position.
    if args.restore and start_combo is None and skip_count > 0:
        print(f"One-time migration: locating password #{skip_count:,} in token list...", end=" ", flush=True)
        t0 = time.time()
        start_combo, start_perm = find_combo_position(token_lists, skip_count)
        print(f"done in {time.time()-t0:.1f}s  (combo {start_combo:,}, perm {start_perm})")
        save_state(args.restore, skip_count, start_combo, start_perm, tokenlist_path, wallet_path)
    elif start_combo is None:
        start_combo = 0
        start_perm  = 0

    prefetch_q = _queue.Queue(maxsize=2)
    gen = password_generator(token_lists, start_combo=start_combo, skip_in_combo=start_perm)
    producer = threading.Thread(
        target=_batch_producer,
        args=(gen, batch_size, prefetch_q),
        daemon=True,
    )
    producer.start()

    total          = skip_count
    last_combo_idx = start_combo
    last_perm_idx  = start_perm
    start_time     = time.time()
    last_save      = time.time()

    print(f"Starting at password #{skip_count:,}  (batch size {batch_size:,})")

    while True:
        item = prefetch_q.get()
        if item is None:
            break

        batch, last_combo_idx, last_perm_idx = item
        result = engine.check_batch(batch)
        total += len(batch)

        if result:
            print(f"\n*** PASSWORD FOUND: '{result}' ***")
            if args.autosave:
                save_state(args.autosave, total, last_combo_idx, last_perm_idx, tokenlist_path, wallet_path)
            return

        elapsed = time.time() - start_time
        rate    = (total - skip_count) / elapsed if elapsed > 0 else 0
        print(f"\r{total:>16,} passwords  {rate:>10,.0f}/s", end="", flush=True)

        if args.autosave and time.time() - last_save > 30:
            save_state(args.autosave, total, last_combo_idx, last_perm_idx, tokenlist_path, wallet_path)
            last_save = time.time()

    elapsed = time.time() - start_time
    rate    = (total - skip_count) / elapsed if elapsed > 0 else 0
    print(f"\nSearch complete. {total:,} passwords checked in {elapsed:.1f}s ({rate:,.0f}/s). Password not found.")

if __name__ == "__main__":
    main()
