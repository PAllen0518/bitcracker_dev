/*
 * multibit_cuda.cu  -  GPU-side password generation + MultiBit Classic check
 *
 * Architecture:
 *   CPU enumerates product combinations from the token list (integer indices only,
 *   no string building).  For each batch of combos, GPU blocks each handle one
 *   combo and threads within the block enumerate all permutations of that combo's
 *   tokens, generating and checking passwords entirely on-device.
 *
 *   This eliminates the Python string-generation CPU bottleneck.  The GPU now
 *   both generates and checks passwords in the same kernel.
 *
 * Compile:
 *   nvcc multibit_cuda.cu -o multibit_cuda.exe -O3 -arch=sm_75
 *   (sm_75 = compute capability 7.5 = Turing = RTX 2060)
 *
 * Usage:
 *   multibit_cuda.exe --wallet multi.key --tokenlist search46.txt --autosave save.bin
 *   multibit_cuda.exe --restore save.bin
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <time.h>
#include <cuda_runtime.h>

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#define THREADS_PER_BLOCK   256      // threads per block (permutation workers)
#define COMBO_BATCH_SIZE    65536    // combos per kernel launch
#define MAX_LINES           16       // max token list lines
#define MAX_TOKENS_PER_LINE 11200    // max tokens on one line (digit wildcard = 11111)
#define MAX_LITERAL_TOKENS  64       // max non-wildcard tokens per line
#define MAX_TOKEN_LEN       32       // max length of a single token string
#define MAX_FREE_TOKENS     10       // max free (permutable) tokens in one combo
#define MAX_ANCHORED        4        // max anchored tokens in one combo
#define PW_MAX_LEN          128      // max password length in bytes

// ---------------------------------------------------------------------------
// AES / MD5 precomputed tables (computed at startup, uploaded to constant mem)
// ---------------------------------------------------------------------------

__constant__ uint32_t c_TD0[256];
__constant__ uint32_t c_TD1[256];
__constant__ uint32_t c_TD2[256];
__constant__ uint32_t c_TD3[256];
__constant__ uint32_t c_SBOX[256];
__constant__ uint8_t  c_SBOX_INV[256];
__constant__ uint8_t  c_salt[8];
__constant__ uint8_t  c_enc[32];

// ---------------------------------------------------------------------------
// Combo descriptor (one per block; threads within the block enumerate perms)
// ---------------------------------------------------------------------------

struct ComboTask {
    // Free tokens to be permuted
    int    n_free;
    char   free_tok[MAX_FREE_TOKENS][MAX_TOKEN_LEN];

    // Anchored tokens: placed at a fixed position, not permuted
    int    n_anchored;
    char   anch_tok[MAX_ANCHORED][MAX_TOKEN_LEN];
    int    anch_pos[MAX_ANCHORED];   // position in the final password (0 = first)
    int    anch_after_free;          // total free slots before anchor matters
};

// ---------------------------------------------------------------------------
// Device: MD5
// ---------------------------------------------------------------------------

#define MD5_F(x,y,z) (((x)&(y))|(~(x)&(z)))
#define MD5_G(x,y,z) (((x)&(z))|((y)&~(z)))
#define MD5_H(x,y,z) ((x)^(y)^(z))
#define MD5_I(x,y,z) ((y)^((x)|~(z)))
#define MD5_ROTL(x,n) (((x)<<(n))|((x)>>(32-(n))))
#define MD5_STEP(f,a,b,c,d,x,t,s) \
    (a) += f((b),(c),(d)) + (x) + (uint32_t)(t); \
    (a)  = MD5_ROTL((a),(s)) + (b);

__device__ void md5_compress(uint32_t* st, uint32_t* blk) {
    uint32_t a=st[0], b=st[1], c=st[2], d=st[3];
    MD5_STEP(MD5_F,a,b,c,d,blk[ 0],0xd76aa478u, 7) MD5_STEP(MD5_F,d,a,b,c,blk[ 1],0xe8c7b756u,12)
    MD5_STEP(MD5_F,c,d,a,b,blk[ 2],0x242070dbu,17) MD5_STEP(MD5_F,b,c,d,a,blk[ 3],0xc1bdceeeu,22)
    MD5_STEP(MD5_F,a,b,c,d,blk[ 4],0xf57c0fafu, 7) MD5_STEP(MD5_F,d,a,b,c,blk[ 5],0x4787c62au,12)
    MD5_STEP(MD5_F,c,d,a,b,blk[ 6],0xa8304613u,17) MD5_STEP(MD5_F,b,c,d,a,blk[ 7],0xfd469501u,22)
    MD5_STEP(MD5_F,a,b,c,d,blk[ 8],0x698098d8u, 7) MD5_STEP(MD5_F,d,a,b,c,blk[ 9],0x8b44f7afu,12)
    MD5_STEP(MD5_F,c,d,a,b,blk[10],0xffff5bb1u,17) MD5_STEP(MD5_F,b,c,d,a,blk[11],0x895cd7beu,22)
    MD5_STEP(MD5_F,a,b,c,d,blk[12],0x6b901122u, 7) MD5_STEP(MD5_F,d,a,b,c,blk[13],0xfd987193u,12)
    MD5_STEP(MD5_F,c,d,a,b,blk[14],0xa679438eu,17) MD5_STEP(MD5_F,b,c,d,a,blk[15],0x49b40821u,22)
    MD5_STEP(MD5_G,a,b,c,d,blk[ 1],0xf61e2562u, 5) MD5_STEP(MD5_G,d,a,b,c,blk[ 6],0xc040b340u, 9)
    MD5_STEP(MD5_G,c,d,a,b,blk[11],0x265e5a51u,14) MD5_STEP(MD5_G,b,c,d,a,blk[ 0],0xe9b6c7aau,20)
    MD5_STEP(MD5_G,a,b,c,d,blk[ 5],0xd62f105du, 5) MD5_STEP(MD5_G,d,a,b,c,blk[10],0x02441453u, 9)
    MD5_STEP(MD5_G,c,d,a,b,blk[15],0xd8a1e681u,14) MD5_STEP(MD5_G,b,c,d,a,blk[ 4],0xe7d3fbc8u,20)
    MD5_STEP(MD5_G,a,b,c,d,blk[ 9],0x21e1cde6u, 5) MD5_STEP(MD5_G,d,a,b,c,blk[14],0xc33707d6u, 9)
    MD5_STEP(MD5_G,c,d,a,b,blk[ 3],0xf4d50d87u,14) MD5_STEP(MD5_G,b,c,d,a,blk[ 8],0x455a14edu,20)
    MD5_STEP(MD5_G,a,b,c,d,blk[13],0xa9e3e905u, 5) MD5_STEP(MD5_G,d,a,b,c,blk[ 2],0xfcefa3f8u, 9)
    MD5_STEP(MD5_G,c,d,a,b,blk[ 7],0x676f02d9u,14) MD5_STEP(MD5_G,b,c,d,a,blk[12],0x8d2a4c8au,20)
    MD5_STEP(MD5_H,a,b,c,d,blk[ 5],0xfffa3942u, 4) MD5_STEP(MD5_H,d,a,b,c,blk[ 8],0x8771f681u,11)
    MD5_STEP(MD5_H,c,d,a,b,blk[11],0x6d9d6122u,16) MD5_STEP(MD5_H,b,c,d,a,blk[14],0xfde5380cu,23)
    MD5_STEP(MD5_H,a,b,c,d,blk[ 1],0xa4beea44u, 4) MD5_STEP(MD5_H,d,a,b,c,blk[ 4],0x4bdecfa9u,11)
    MD5_STEP(MD5_H,c,d,a,b,blk[ 7],0xf6bb4b60u,16) MD5_STEP(MD5_H,b,c,d,a,blk[10],0xbebfbc70u,23)
    MD5_STEP(MD5_H,a,b,c,d,blk[13],0x289b7ec6u, 4) MD5_STEP(MD5_H,d,a,b,c,blk[ 0],0xeaa127fau,11)
    MD5_STEP(MD5_H,c,d,a,b,blk[ 3],0xd4ef3085u,16) MD5_STEP(MD5_H,b,c,d,a,blk[ 6],0x04881d05u,23)
    MD5_STEP(MD5_H,a,b,c,d,blk[ 9],0xd9d4d039u, 4) MD5_STEP(MD5_H,d,a,b,c,blk[12],0xe6db99e5u,11)
    MD5_STEP(MD5_H,c,d,a,b,blk[15],0x1fa27cf8u,16) MD5_STEP(MD5_H,b,c,d,a,blk[ 2],0xc4ac5665u,23)
    MD5_STEP(MD5_I,a,b,c,d,blk[ 0],0xf4292244u, 6) MD5_STEP(MD5_I,d,a,b,c,blk[ 7],0x432aff97u,10)
    MD5_STEP(MD5_I,c,d,a,b,blk[14],0xab9423a7u,15) MD5_STEP(MD5_I,b,c,d,a,blk[ 5],0xfc93a039u,21)
    MD5_STEP(MD5_I,a,b,c,d,blk[12],0x655b59c3u, 6) MD5_STEP(MD5_I,d,a,b,c,blk[ 3],0x8f0ccc92u,10)
    MD5_STEP(MD5_I,c,d,a,b,blk[10],0xffeff47du,15) MD5_STEP(MD5_I,b,c,d,a,blk[ 1],0x85845dd1u,21)
    MD5_STEP(MD5_I,a,b,c,d,blk[ 8],0x6fa87e4fu, 6) MD5_STEP(MD5_I,d,a,b,c,blk[15],0xfe2ce6e0u,10)
    MD5_STEP(MD5_I,c,d,a,b,blk[ 6],0xa3014314u,15) MD5_STEP(MD5_I,b,c,d,a,blk[13],0x4e0811a1u,21)
    MD5_STEP(MD5_I,a,b,c,d,blk[ 4],0xf7537e82u, 6) MD5_STEP(MD5_I,d,a,b,c,blk[11],0xbd3af235u,10)
    MD5_STEP(MD5_I,c,d,a,b,blk[ 2],0x2ad7d2bbu,15) MD5_STEP(MD5_I,b,c,d,a,blk[ 9],0xeb86d391u,21)
    st[0]+=a; st[1]+=b; st[2]+=c; st[3]+=d;
}

__device__ void md5(const uint8_t* data, uint32_t len, uint8_t* digest) {
    uint32_t st[4] = {0x67452301u, 0xefcdab89u, 0x98badcfeu, 0x10325476u};
    uint32_t blk[16];
    uint32_t pos = 0;

    while (pos + 64 <= len) {
        for (int i = 0; i < 16; i++) {
            uint32_t j = pos + i*4;
            blk[i] = (uint32_t)data[j] | ((uint32_t)data[j+1]<<8)
                   | ((uint32_t)data[j+2]<<16) | ((uint32_t)data[j+3]<<24);
        }
        md5_compress(st, blk);
        pos += 64;
    }

    uint32_t rem = len - pos;
    uint8_t buf[128];
    for (uint32_t i = 0; i < rem; i++) buf[i] = data[pos+i];
    buf[rem] = 0x80;
    for (uint32_t i = rem+1; i < 128; i++) buf[i] = 0;
    uint64_t bits = (uint64_t)len * 8;
    uint32_t off = (rem < 56) ? 56 : 120;
    for (int i = 0; i < 8; i++) buf[off+i] = (uint8_t)(bits >> (i*8));

    uint32_t nblocks = (rem < 56) ? 1 : 2;
    for (uint32_t b = 0; b < nblocks; b++) {
        for (int i = 0; i < 16; i++) {
            uint32_t j = b*64 + i*4;
            blk[i] = (uint32_t)buf[j] | ((uint32_t)buf[j+1]<<8)
                   | ((uint32_t)buf[j+2]<<16) | ((uint32_t)buf[j+3]<<24);
        }
        md5_compress(st, blk);
    }

    for (int i = 0; i < 4; i++) {
        digest[i*4+0] = (uint8_t)(st[i]    );
        digest[i*4+1] = (uint8_t)(st[i]>> 8);
        digest[i*4+2] = (uint8_t)(st[i]>>16);
        digest[i*4+3] = (uint8_t)(st[i]>>24);
    }
}

// ---------------------------------------------------------------------------
// Device: AES-256 key expand + block decrypt (equivalent inverse cipher)
// ---------------------------------------------------------------------------

__device__ void aes256_key_expand(const uint8_t* key, uint32_t* rk) {
    const uint32_t RCON[7] = {
        0x01000000u,0x02000000u,0x04000000u,0x08000000u,
        0x10000000u,0x20000000u,0x40000000u
    };
    for (int i = 0; i < 8; i++)
        rk[i] = ((uint32_t)key[i*4]<<24)|((uint32_t)key[i*4+1]<<16)
               |((uint32_t)key[i*4+2]<<8)|key[i*4+3];
    for (int i = 8; i < 60; i++) {
        uint32_t t = rk[i-1];
        if (i % 8 == 0) {
            t = (c_SBOX[(t>>16)&0xff]<<24)|(c_SBOX[(t>>8)&0xff]<<16)
               |(c_SBOX[t&0xff]<<8)|c_SBOX[(t>>24)&0xff];
            t ^= RCON[i/8 - 1];
        } else if (i % 8 == 4) {
            t = (c_SBOX[(t>>24)&0xff]<<24)|(c_SBOX[(t>>16)&0xff]<<16)
               |(c_SBOX[(t>>8)&0xff]<<8)|c_SBOX[t&0xff];
        }
        rk[i] = rk[i-8] ^ t;
    }
    // Apply InvMixColumns to middle round keys (equivalent inverse cipher)
    for (int i = 4; i < 56; i++) {
        uint32_t w = rk[i];
        rk[i] = c_TD0[c_SBOX[(w>>24)&0xff]] ^ c_TD1[c_SBOX[(w>>16)&0xff]]
               ^ c_TD2[c_SBOX[(w>>8)&0xff]]  ^ c_TD3[c_SBOX[w&0xff]];
    }
}

__device__ void aes256_block_decrypt(const uint32_t* rk,
                                     const uint8_t* xor_block,
                                     const uint8_t* ct, uint8_t* pt) {
    uint32_t s0=((uint32_t)ct[ 0]<<24)|((uint32_t)ct[ 1]<<16)|((uint32_t)ct[ 2]<<8)|ct[ 3];
    uint32_t s1=((uint32_t)ct[ 4]<<24)|((uint32_t)ct[ 5]<<16)|((uint32_t)ct[ 6]<<8)|ct[ 7];
    uint32_t s2=((uint32_t)ct[ 8]<<24)|((uint32_t)ct[ 9]<<16)|((uint32_t)ct[10]<<8)|ct[11];
    uint32_t s3=((uint32_t)ct[12]<<24)|((uint32_t)ct[13]<<16)|((uint32_t)ct[14]<<8)|ct[15];
    s0^=rk[56]; s1^=rk[57]; s2^=rk[58]; s3^=rk[59];
    uint32_t t0,t1,t2,t3;
    for (int r = 13; r >= 1; r--) {
        t0=c_TD0[(s0>>24)&0xff]^c_TD1[(s3>>16)&0xff]^c_TD2[(s2>>8)&0xff]^c_TD3[s1&0xff]^rk[r*4+0];
        t1=c_TD0[(s1>>24)&0xff]^c_TD1[(s0>>16)&0xff]^c_TD2[(s3>>8)&0xff]^c_TD3[s2&0xff]^rk[r*4+1];
        t2=c_TD0[(s2>>24)&0xff]^c_TD1[(s1>>16)&0xff]^c_TD2[(s0>>8)&0xff]^c_TD3[s3&0xff]^rk[r*4+2];
        t3=c_TD0[(s3>>24)&0xff]^c_TD1[(s2>>16)&0xff]^c_TD2[(s1>>8)&0xff]^c_TD3[s0&0xff]^rk[r*4+3];
        s0=t0; s1=t1; s2=t2; s3=t3;
    }
    t0=((uint32_t)c_SBOX_INV[(s0>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s3>>16)&0xff]<<16)
      |((uint32_t)c_SBOX_INV[(s2>>8)&0xff]<<8)|c_SBOX_INV[s1&0xff];
    t1=((uint32_t)c_SBOX_INV[(s1>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s0>>16)&0xff]<<16)
      |((uint32_t)c_SBOX_INV[(s3>>8)&0xff]<<8)|c_SBOX_INV[s2&0xff];
    t2=((uint32_t)c_SBOX_INV[(s2>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s1>>16)&0xff]<<16)
      |((uint32_t)c_SBOX_INV[(s0>>8)&0xff]<<8)|c_SBOX_INV[s3&0xff];
    t3=((uint32_t)c_SBOX_INV[(s3>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s2>>16)&0xff]<<16)
      |((uint32_t)c_SBOX_INV[(s1>>8)&0xff]<<8)|c_SBOX_INV[s0&0xff];
    t0^=rk[0]; t1^=rk[1]; t2^=rk[2]; t3^=rk[3];
    uint32_t x0=((uint32_t)xor_block[0]<<24)|((uint32_t)xor_block[1]<<16)
               |((uint32_t)xor_block[2]<<8)|xor_block[3];
    uint32_t x1=((uint32_t)xor_block[4]<<24)|((uint32_t)xor_block[5]<<16)
               |((uint32_t)xor_block[6]<<8)|xor_block[7];
    uint32_t x2=((uint32_t)xor_block[8]<<24)|((uint32_t)xor_block[9]<<16)
               |((uint32_t)xor_block[10]<<8)|xor_block[11];
    uint32_t x3=((uint32_t)xor_block[12]<<24)|((uint32_t)xor_block[13]<<16)
               |((uint32_t)xor_block[14]<<8)|xor_block[15];
    t0^=x0; t1^=x1; t2^=x2; t3^=x3;
    pt[0]=(uint8_t)(t0>>24); pt[1]=(uint8_t)(t0>>16); pt[2]=(uint8_t)(t0>>8); pt[3]=(uint8_t)t0;
    pt[4]=(uint8_t)(t1>>24); pt[5]=(uint8_t)(t1>>16); pt[6]=(uint8_t)(t1>>8); pt[7]=(uint8_t)t1;
    pt[8]=(uint8_t)(t2>>24); pt[9]=(uint8_t)(t2>>16); pt[10]=(uint8_t)(t2>>8); pt[11]=(uint8_t)t2;
    pt[12]=(uint8_t)(t3>>24); pt[13]=(uint8_t)(t3>>16); pt[14]=(uint8_t)(t3>>8); pt[15]=(uint8_t)t3;
}

// ---------------------------------------------------------------------------
// Device: base58 validation
// ---------------------------------------------------------------------------

__device__ bool all_b58(const uint8_t* buf, int len) {
    for (int i = 0; i < len; i++) {
        uint8_t c = buf[i];
        if (c < '1' || c > 'z') return false;
        if (c > '9' && c < 'A') return false;
        if (c > 'Z' && c < 'a') return false;
        if (c == 'I' || c == 'O' || c == 'l') return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Device: MultiBit Classic password check
// ---------------------------------------------------------------------------

__device__ bool check_multibit(const uint8_t* pw, int pw_len) {
    // Build salted = password + salt
    uint8_t salted[144];
    for (int i = 0; i < pw_len; i++) salted[i] = pw[i];
    for (int i = 0; i < 8; i++)      salted[pw_len+i] = c_salt[i];
    int salted_len = pw_len + 8;

    uint8_t key1[16], key2[16], iv[16];
    md5(salted, salted_len, key1);

    uint8_t tmp[160];
    for (int i = 0; i < 16; i++)           tmp[i]    = key1[i];
    for (int i = 0; i < salted_len; i++)   tmp[16+i] = salted[i];
    md5(tmp, 16 + salted_len, key2);

    for (int i = 0; i < 16; i++) tmp[i] = key2[i];
    md5(tmp, 16 + salted_len, iv);

    uint8_t aes_key[32];
    for (int i = 0; i < 16; i++) aes_key[i]    = key1[i];
    for (int i = 0; i < 16; i++) aes_key[16+i] = key2[i];

    uint32_t rk[60];
    aes256_key_expand(aes_key, rk);

    // Stage 1: decrypt first block, check first byte and remaining 15 for b58
    uint8_t pt1[16];
    aes256_block_decrypt(rk, iv, c_enc, pt1);
    uint8_t b0 = pt1[0];
    if (b0 != 'L' && b0 != 'K' && b0 != '5' && b0 != 'Q') return false;
    if (!all_b58(pt1+1, 15)) return false;

    // Stage 2: decrypt second block
    uint8_t pt2[16];
    aes256_block_decrypt(rk, c_enc, c_enc+16, pt2);
    return all_b58(pt2, 16);
}

// ---------------------------------------------------------------------------
// Device: N-th permutation via factoradic number system
//   Maps integer perm_idx in [0, n!) to a unique ordering of [0..n-1].
// ---------------------------------------------------------------------------

/*
 * nth_permutation: maps a 64-bit index to the unique ordering of [0..n-1]
 * using the factoradic (factorial number system).
 *
 * idx=0 gives [0,1,2,...,n-1], idx=n!-1 gives [n-1,...,1,0].
 * This lets each GPU thread compute its permutation directly from its
 * thread index without communicating with other threads.
 *
 * Algorithm: decompose idx in the factorial number system — each digit
 * selects which remaining element to place next.  O(n^2) per call.
 * For n <= 9 (our max) this is 45 inner iterations: negligible vs AES.
 */
__device__ void nth_permutation(int* perm, int n, uint64_t idx) {
    bool used[MAX_FREE_TOKENS] = {false};
    uint64_t f = 1;
    for (int i = 2; i <= n; i++) f *= i;   // f = n!

    for (int i = 0; i < n; i++) {
        f /= (n - i);                        // f = (n-i-1)!
        int digit = (int)(idx / f);
        idx %= f;
        int count = 0;
        for (int j = 0; j < n; j++) {
            if (!used[j]) {
                if (count == digit) { perm[i] = j; used[j] = true; break; }
                count++;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Device: string append helper
// ---------------------------------------------------------------------------

__device__ void append_str(uint8_t* pw, int* len, const char* tok) {
    for (int i = 0; tok[i] != '\0' && *len < PW_MAX_LEN-1; i++)
        pw[(*len)++] = (uint8_t)tok[i];
    pw[*len] = 0;
}

// ---------------------------------------------------------------------------
// Main CUDA kernel
//
// Each block processes one ComboTask.  Threads within a block enumerate all
// permutations of the combo's free tokens using the factoradic number system,
// assemble the password, and check it against the wallet.
// ---------------------------------------------------------------------------

__global__ void multibit_check_kernel(
    const ComboTask* __restrict__ tasks,
    int              n_tasks,
    int*             found_combo,   // output: -1 or combo index
    int*             found_perm     // output: -1 or perm index within combo
) {
    int combo_id = blockIdx.x;
    if (combo_id >= n_tasks) return;

    // Load combo into registers/local (avoids repeated global memory reads)
    ComboTask task = tasks[combo_id];

    if (task.n_free == 0) return;

    // Compute n! for the free tokens
    uint64_t n_perms = 1;
    for (int i = 2; i <= task.n_free; i++) n_perms *= i;

    // Threads stride through permutations
    for (uint64_t perm_idx = threadIdx.x; perm_idx < n_perms; perm_idx += blockDim.x) {

        // Get the ordering of free tokens for this permutation
        int perm[MAX_FREE_TOKENS];
        nth_permutation(perm, task.n_free, perm_idx);

        // Assemble password: interleave free tokens with anchored tokens
        uint8_t pw[PW_MAX_LEN];
        int     pw_len = 0;
        int     free_slot = 0;

        // Total slots in the password = n_free + n_anchored
        int total_slots = task.n_free + task.n_anchored;
        // Use a small fixed array to track which slot is anchored
        bool slot_is_anchored[MAX_FREE_TOKENS + MAX_ANCHORED] = {false};
        int  slot_anch_which[MAX_FREE_TOKENS + MAX_ANCHORED]  = {-1};
        for (int a = 0; a < task.n_anchored; a++) {
            int pos = task.anch_pos[a];
            if (pos >= 0 && pos < total_slots) {
                slot_is_anchored[pos] = true;
                slot_anch_which[pos]  = a;
            }
        }

        for (int slot = 0; slot < total_slots; slot++) {
            if (slot_is_anchored[slot]) {
                int a = slot_anch_which[slot];
                append_str(pw, &pw_len, task.anch_tok[a]);
            } else {
                append_str(pw, &pw_len, task.free_tok[perm[free_slot]]);
                free_slot++;
            }
        }

        if (check_multibit(pw, pw_len)) {
            atomicCAS(found_combo, -1, combo_id);
            atomicCAS(found_perm,  -1, (int)perm_idx);
        }
    }
}

// ---------------------------------------------------------------------------
// Host: AES/MD5 table computation and GPU upload
// ---------------------------------------------------------------------------

static uint32_t h_SBOX[256] = {
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

static uint8_t h_SBOX_INV[256] = {
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

static uint8_t gmul(uint8_t a, uint8_t b) {
    uint8_t p = 0;
    for (int i = 0; i < 8; i++) {
        if (b & 1) p ^= a;
        uint8_t hi = a & 0x80;
        a <<= 1;
        if (hi) a ^= 0x1b;
        b >>= 1;
    }
    return p;
}

static void build_td_tables(uint32_t* td0, uint32_t* td1,
                             uint32_t* td2, uint32_t* td3) {
    for (int x = 0; x < 256; x++) {
        uint8_t s = h_SBOX_INV[x];
        td0[x] = ((uint32_t)gmul(14,s)<<24)|((uint32_t)gmul(9,s)<<16)
                |((uint32_t)gmul(13,s)<<8)|gmul(11,s);
        td1[x] = (td0[x]>>8)|(td0[x]<<24);
        td2[x] = (td1[x]>>8)|(td1[x]<<24);
        td3[x] = (td2[x]>>8)|(td2[x]<<24);
    }
}

static void upload_tables() {
    uint32_t td0[256], td1[256], td2[256], td3[256];
    build_td_tables(td0, td1, td2, td3);
    cudaMemcpyToSymbol(c_TD0, td0, 256*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_TD1, td1, 256*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_TD2, td2, 256*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_TD3, td3, 256*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_SBOX,    h_SBOX,    256*sizeof(uint32_t));
    cudaMemcpyToSymbol(c_SBOX_INV,h_SBOX_INV,256*sizeof(uint8_t));
}

// ---------------------------------------------------------------------------
// Host: MultiBit wallet loader
// ---------------------------------------------------------------------------

#include <stdexcept>
#include <string>

static void load_wallet(const char* path, uint8_t* enc_out, uint8_t* salt_out) {
    FILE* f = fopen(path, "r");
    if (!f) { fprintf(stderr, "Cannot open wallet: %s\n", path); exit(1); }
    char buf[80] = {0};
    fread(buf, 1, 70, f);
    fclose(f);

    // Strip whitespace from base64
    char b64[70]; int b64len = 0;
    for (int i = 0; buf[i] && b64len < 64; i++)
        if (buf[i] != '\r' && buf[i] != '\n' && buf[i] != ' ')
            b64[b64len++] = buf[i];
    b64[b64len] = 0;

    // Decode base64
    static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    uint8_t decoded[48]; int dlen = 0;
    for (int i = 0; i < b64len - 3 && dlen < 48; i += 4) {
        int v = 0;
        for (int j = 0; j < 4; j++) {
            const char* p = strchr(B64, b64[i+j]);
            v = (v<<6) | (p ? (int)(p-B64) : 0);
        }
        decoded[dlen++] = (v>>16)&0xff;
        if (dlen < 48) decoded[dlen++] = (v>>8)&0xff;
        if (dlen < 48) decoded[dlen++] = v&0xff;
    }

    if (dlen < 48 || memcmp(decoded, "Salted__", 8) != 0) {
        fprintf(stderr, "Not a valid MultiBit key file\n"); exit(1);
    }
    memcpy(salt_out, decoded+8,  8);
    memcpy(enc_out,  decoded+16, 32);
}

// ---------------------------------------------------------------------------
// Host: token list parser
// ---------------------------------------------------------------------------

struct TokenListLine {
    char   tokens[MAX_LITERAL_TOKENS][MAX_TOKEN_LEN];
    int    n_tokens;
    bool   required;
    bool   is_digit_wildcard;  // %0,4d style
    int    digit_min_len;
    int    digit_max_len;
    bool   has_anchor;
    int    anchor_pos;         // 0 = ^token (first)
};

static void strip(char* s) {
    // Remove leading/trailing whitespace
    int len = strlen(s);
    while (len > 0 && (s[len-1]=='\r'||s[len-1]=='\n'||s[len-1]==' ')) s[--len]=0;
}

static void parse_tokenlist(const char* path, TokenListLine* lines, int* n_lines) {
    FILE* f = fopen(path, "r");
    if (!f) { fprintf(stderr, "Cannot open tokenlist: %s\n", path); exit(1); }
    *n_lines = 0;
    char buf[4096];
    while (fgets(buf, sizeof(buf), f) && *n_lines < MAX_LINES) {
        strip(buf);
        if (buf[0] == '#' || buf[0] == 0) continue;

        TokenListLine* line = &lines[(*n_lines)++];
        memset(line, 0, sizeof(*line));

        char* p = buf;
        if (*p == '+') { line->required = true; while (*p == '+' || *p == ' ') p++; }
        else while (*p == ' ') p++;  // skip leading space (optional line marker)

        // Tokenise by spaces
        char* tok = strtok(p, " \t");
        while (tok && line->n_tokens < MAX_LITERAL_TOKENS) {
            // Detect digit wildcard
            if (strncmp(tok, "%0,4d", 5)==0 || strcmp(tok,"%4d")==0 || strcmp(tok,"%d")==0) {
                line->is_digit_wildcard = true;
                if (strncmp(tok,"%0,4d",5)==0) { line->digit_min_len=0; line->digit_max_len=4; }
                else if (strcmp(tok,"%4d")==0)  { line->digit_min_len=4; line->digit_max_len=4; }
                else                             { line->digit_min_len=1; line->digit_max_len=1; }
            } else if (tok[0]=='^' || tok[strlen(tok)-1]=='$') {
                // Anchored token
                line->has_anchor = true;
                line->anchor_pos = 0;  // ^ = first position
                const char* text = tok + (tok[0]=='^' ? 1 : 0);
                if (line->n_tokens < MAX_LITERAL_TOKENS)
                    strncpy(line->tokens[line->n_tokens++], text, MAX_TOKEN_LEN-1);
            } else {
                strncpy(line->tokens[line->n_tokens++], tok, MAX_TOKEN_LEN-1);
            }
            tok = strtok(NULL, " \t");
        }
    }
    fclose(f);

    // Reverse (btcrecover convention: last line iterated fastest)
    for (int i = 0, j = *n_lines-1; i < j; i++, j--) {
        TokenListLine tmp = lines[i]; lines[i] = lines[j]; lines[j] = tmp;
    }
}

// ---------------------------------------------------------------------------
// Host: count total product combos (for progress reporting)
// ---------------------------------------------------------------------------

/*
 * count_combos: total number of entries in the Cartesian product of all
 * token list lines.  This is the outer loop count (combos, not passwords) —
 * each combo may produce multiple passwords via permutation.
 * Used only for progress display and save/restore.
 */
static uint64_t count_combos(const TokenListLine* lines, int n_lines) {
    uint64_t total = 1;
    for (int i = 0; i < n_lines; i++) {
        const TokenListLine* L = &lines[i];
        int choices;
        if (L->is_digit_wildcard) {
            int n = 0;
            for (int d = L->digit_min_len; d <= L->digit_max_len; d++) {
                int c = 1; for (int k=0;k<d;k++) c *= 10;
                n += c;
            }
            choices = n + (L->required ? 0 : 1);  // +1 for None if optional
        } else {
            choices = L->n_tokens + (L->required ? 0 : 1);
        }
        total *= choices;
    }
    return total;
}

// ---------------------------------------------------------------------------
// Host: decode combo_idx (mixed-radix) into per-line choices
// ---------------------------------------------------------------------------

// choice[i] = -1 means None (skip), else index into tokens[]
// For digit wildcard: choice = digit string index (0=None,1="",2="0",...,11112="9999")
/*
 * decode_combo: mixed-radix decode of combo_idx into per-line token choices.
 *
 * The combo_idx is like a number in a mixed-radix system where each "digit"
 * selects one token from one line.  Repeatedly dividing by the line size
 * and taking the remainder extracts each digit in O(n_lines) time — the
 * same O(1)-per-digit property that lets you decode a decimal number.
 *
 * choices[i] = -1 means the optional line i was skipped (None selected).
 * choices[i] >= 0 is the index into lines[i].tokens[].
 * For digit wildcard lines: choices[i] is the digit string index passed to
 * digit_idx_to_str().
 */
static void decode_combo(uint64_t combo_idx,
                         const TokenListLine* lines, int n_lines,
                         int* choices) {
    uint64_t idx = combo_idx;
    for (int i = 0; i < n_lines; i++) {
        const TokenListLine* L = &lines[i];
        int sz;
        if (L->is_digit_wildcard) {
            int n = 0;
            for (int d=L->digit_min_len; d<=L->digit_max_len; d++) {
                int c=1; for(int k=0;k<d;k++) c*=10; n+=c;
            }
            sz = n + (L->required ? 0 : 1);
        } else {
            sz = L->n_tokens + (L->required ? 0 : 1);
        }
        choices[i] = (int)(idx % sz);
        if (!L->required) {
            choices[i]--;  // -1 = None, 0..n_tokens-1 = token index
        }
        idx /= sz;
    }
}

// ---------------------------------------------------------------------------
// Host: digit index to string
//   idx 0: "" (0 digits)
//   idx 1-10: "0"-"9"
//   idx 11-110: "00"-"99"
//   idx 111-1110: "000"-"999"
//   idx 1111-11110: "0000"-"9999"
// ---------------------------------------------------------------------------

/*
 * digit_idx_to_str: converts a digit-line choice index to the actual string.
 *
 * The %0,4d wildcard expands to 11,111 strings (empty + 1-4 digit numbers).
 * We encode them as a single integer rather than storing 11,111 strings in
 * the ComboTask struct.  Encoding:
 *   0        -> None (skip this line — handled before calling this)
 *   1        -> "" (zero-digit, empty string)
 *   2-11     -> "0"-"9"  (1 digit)
 *   12-111   -> "00"-"99" (2 digits)
 *   112-1111 -> "000"-"999" (3 digits)
 *   1112-11111 -> "0000"-"9999" (4 digits)
 */
static void digit_idx_to_str(int idx, char* out) {
    if (idx <= 0) { out[0]=0; return; }
    idx--;  // now 0-based into the actual strings
    if (idx < 1)          { out[0]=0; return; }  // empty string (min_len=0 case)
    // Determine number of digits
    int base=1, ndigits=0;
    while (idx >= base*10 && ndigits < 4) { base*=10; ndigits++; }
    // Actually: idx 0=empty, idx 1-10="0"-"9", idx 11-110="00"-"99", etc.
    // Recompute properly:
    idx = (idx==0) ? -1 : idx; // -1 means empty
    if (idx < 0) { out[0]=0; return; }
    // Count which range idx falls in
    int lo=0, nd=1, range=10;
    while (lo+range <= idx && nd < 5) { lo+=range; range*=10; nd++; }
    int val = (int)(idx - lo);
    // Write nd digits
    out[nd]=0;
    for (int i=nd-1; i>=0; i--) { out[i]='0'+(val%10); val/=10; }
}

// ---------------------------------------------------------------------------
// Host: build ComboTask from decoded choices
// ---------------------------------------------------------------------------

static bool build_task(const TokenListLine* lines, int n_lines,
                       const int* choices, ComboTask* task) {
    memset(task, 0, sizeof(*task));
    task->n_free = 0;
    task->n_anchored = 0;

    for (int i = 0; i < n_lines; i++) {
        const TokenListLine* L = &lines[i];
        if (choices[i] == -1) continue;  // skipped optional line

        char tok[MAX_TOKEN_LEN] = {0};
        if (L->is_digit_wildcard) {
            digit_idx_to_str(choices[i], tok);
            if (tok[0] == 0) continue;  // empty string = effectively skip
        } else {
            int ci = choices[i];
            if (ci < 0 || ci >= L->n_tokens) continue;
            strncpy(tok, L->tokens[ci], MAX_TOKEN_LEN-1);
        }

        if (L->has_anchor && task->n_anchored < MAX_ANCHORED) {
            // Place this token at the anchored position
            int a = task->n_anchored++;
            strncpy(task->anch_tok[a], tok, MAX_TOKEN_LEN-1);
            task->anch_pos[a] = L->anchor_pos;  // 0 = first
        } else if (task->n_free < MAX_FREE_TOKENS) {
            strncpy(task->free_tok[task->n_free++], tok, MAX_TOKEN_LEN-1);
        }
    }

    return (task->n_free + task->n_anchored) > 0;
}

// ---------------------------------------------------------------------------
// Save / restore
// ---------------------------------------------------------------------------

struct SaveState {
    char     tokenlist[512];
    char     wallet[512];
    uint64_t combo_idx;        // next combo to process
    uint64_t total_combos;
    uint64_t passwords_checked;
};

static void save_progress(const char* path, const SaveState* s) {
    FILE* f = fopen(path, "wb");
    if (f) { fwrite(s, sizeof(*s), 1, f); fclose(f); }
}

static bool load_progress(const char* path, SaveState* s) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    bool ok = (fread(s, sizeof(*s), 1, f) == 1);
    fclose(f);
    return ok;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
    const char* wallet_path    = NULL;
    const char* tokenlist_path = NULL;
    const char* autosave_path  = NULL;
    const char* restore_path   = NULL;

    for (int i = 1; i < argc; i++) {
        if      (!strcmp(argv[i],"--wallet")    && i+1<argc) wallet_path    = argv[++i];
        else if (!strcmp(argv[i],"--tokenlist") && i+1<argc) tokenlist_path = argv[++i];
        else if (!strcmp(argv[i],"--autosave")  && i+1<argc) autosave_path  = argv[++i];
        else if (!strcmp(argv[i],"--restore")   && i+1<argc) restore_path   = argv[++i];
    }

    SaveState state = {0};
    if (restore_path) {
        if (!load_progress(restore_path, &state)) {
            fprintf(stderr, "Cannot load save file: %s\n", restore_path); return 1;
        }
        if (!wallet_path)    wallet_path    = state.wallet;
        if (!tokenlist_path) tokenlist_path = state.tokenlist;
        if (!autosave_path)  autosave_path  = restore_path;
        printf("Restored: resuming at combo #%llu / %llu\n",
               (unsigned long long)state.combo_idx,
               (unsigned long long)state.total_combos);
    }

    if (!wallet_path || !tokenlist_path) {
        fprintf(stderr, "Usage: multibit_cuda.exe --wallet <f> --tokenlist <f> [--autosave <f>]\n"
                        "       multibit_cuda.exe --restore <save.bin>\n");
        return 1;
    }

    // Load wallet
    uint8_t h_enc[32], h_salt[8];
    load_wallet(wallet_path, h_enc, h_salt);

    // Upload tables and wallet data
    upload_tables();
    cudaMemcpyToSymbol(c_enc,  h_enc,  32);
    cudaMemcpyToSymbol(c_salt, h_salt, 8);

    // Parse token list
    TokenListLine lines[MAX_LINES];
    int n_lines = 0;
    parse_tokenlist(tokenlist_path, lines, &n_lines);
    printf("Token list: %s (%d lines)\n", tokenlist_path, n_lines);

    uint64_t total_combos = count_combos(lines, n_lines);
    printf("Total product combos: %llu\n", (unsigned long long)total_combos);

    // Populate save state
    if (!restore_path) {
        strncpy(state.tokenlist, tokenlist_path, 511);
        strncpy(state.wallet,    wallet_path,    511);
        state.total_combos     = total_combos;
        state.combo_idx        = 0;
        state.passwords_checked = 0;
    }

    // Allocate GPU combo task buffer
    ComboTask* d_tasks = NULL;
    cudaMalloc(&d_tasks, COMBO_BATCH_SIZE * sizeof(ComboTask));

    int* d_found_combo = NULL;
    int* d_found_perm  = NULL;
    cudaMalloc(&d_found_combo, sizeof(int));
    cudaMalloc(&d_found_perm,  sizeof(int));

    ComboTask* h_tasks = (ComboTask*)malloc(COMBO_BATCH_SIZE * sizeof(ComboTask));
    if (!h_tasks) { fprintf(stderr, "OOM\n"); return 1; }

    int h_found_combo = -1, h_found_perm = -1;

    time_t start_time = time(NULL);
    time_t last_save  = start_time;

    printf("Starting at combo #%llu\n", (unsigned long long)state.combo_idx);
    printf("Batch size: %d combos/launch, %d threads/block\n",
           COMBO_BATCH_SIZE, THREADS_PER_BLOCK);

    for (uint64_t combo_base = state.combo_idx;
         combo_base < total_combos;
         combo_base += COMBO_BATCH_SIZE)
    {
        uint64_t batch_end = combo_base + COMBO_BATCH_SIZE;
        if (batch_end > total_combos) batch_end = total_combos;
        int batch_size = (int)(batch_end - combo_base);

        // Build combo tasks on CPU (fast: just integer decode, no strings across PCIe)
        int valid_tasks = 0;
        for (int b = 0; b < batch_size; b++) {
            int choices[MAX_LINES];
            decode_combo(combo_base + b, lines, n_lines, choices);
            if (build_task(lines, n_lines, choices, &h_tasks[valid_tasks]))
                valid_tasks++;
        }

        if (valid_tasks == 0) { state.combo_idx = batch_end; continue; }

        // Upload tasks and reset found flags
        cudaMemcpy(d_tasks, h_tasks, valid_tasks * sizeof(ComboTask), cudaMemcpyHostToDevice);
        h_found_combo = -1; h_found_perm = -1;
        cudaMemcpy(d_found_combo, &h_found_combo, sizeof(int), cudaMemcpyHostToDevice);
        cudaMemcpy(d_found_perm,  &h_found_perm,  sizeof(int), cudaMemcpyHostToDevice);

        // Launch: one block per combo, THREADS_PER_BLOCK threads per block
        multibit_check_kernel<<<valid_tasks, THREADS_PER_BLOCK>>>(
            d_tasks, valid_tasks, d_found_combo, d_found_perm);
        cudaDeviceSynchronize();

        // Check result
        cudaMemcpy(&h_found_combo, d_found_combo, sizeof(int), cudaMemcpyDeviceToHost);
        cudaMemcpy(&h_found_perm,  d_found_perm,  sizeof(int), cudaMemcpyDeviceToHost);

        if (h_found_combo >= 0) {
            printf("\n*** PASSWORD FOUND in combo %d, perm %d ***\n",
                   h_found_combo, h_found_perm);
            // TODO: reconstruct and print the actual password string
            // (decode combo and apply nth_permutation on CPU)
            return 0;
        }

        state.combo_idx = batch_end;

        // Progress
        time_t now = time(NULL);
        double elapsed = difftime(now, start_time);
        double frac = (double)state.combo_idx / total_combos;
        if (elapsed > 0)
            printf("\rCombo %llu / %llu (%.1f%%)  %.1f combos/s  ",
                   (unsigned long long)state.combo_idx,
                   (unsigned long long)total_combos,
                   frac * 100.0,
                   state.combo_idx / elapsed);
        fflush(stdout);

        // Autosave every 30 seconds
        if (autosave_path && difftime(now, last_save) >= 30.0) {
            save_progress(autosave_path, &state);
            last_save = now;
        }
    }

    printf("\nSearch complete. Password not found.\n");
    free(h_tasks);
    cudaFree(d_tasks); cudaFree(d_found_combo); cudaFree(d_found_perm);
    return 0;
}
