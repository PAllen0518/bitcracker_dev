/*
 * multibit_cuda_threads.cu - password generation on the CPU, crypto check on the GPU.
 *
 * The CPU walks the tokenlist and assembles candidate passwords; the GPU does the
 * MultiBit check (3x MD5 + AES + base58) one thread per password. A producer thread
 * fills the next batch while the GPU works on the current one. About 11M pw/s on a
 * 2060.
 *
 * The earlier approach (multibit_cuda.cu) generated passwords on the GPU with
 * nth_permutation and was slower than Python thanks to the O(n^2) indexing and
 * thread divergence. Moving generation back to the CPU and only shipping finished
 * strings to the GPU was the fix. The other big win was dropping std::string in the
 * assembly loop for stack char arrays - that alone took it from ~3M to ~11M.
 *
 * Build:  build_cuda.bat
 * Run:    multibit_cuda_threads.exe --wallet multi.key --tokenlist search46.txt --autosave save.bin
 *         multibit_cuda_threads.exe --restore save.bin
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <cuda_runtime.h>
#include <io.h>
#include <fcntl.h>
#include <sys/stat.h>

#include <string>
#include <vector>
#include <algorithm>
#include <memory>
#include <chrono>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <functional>

using hrclock     = std::chrono::steady_clock;
using hrtimepoint = hrclock::time_point;
static inline double secs_since(hrtimepoint t0) {
    return std::chrono::duration<double>(hrclock::now() - t0).count();
}

// Wrap every CUDA call so a failed malloc or bad launch aborts loudly instead
// of silently corrupting results. Kernel launch errors are async, so the launch
// sites also check cudaGetLastError() explicitly.
#define CUDA_CHECK(call) do {                                                  \
    cudaError_t _e = (call);                                                   \
    if (_e != cudaSuccess) {                                                   \
        fprintf(stderr, "\nCUDA error at %s:%d: %s\n",                         \
                __FILE__, __LINE__, cudaGetErrorString(_e));                   \
        exit(1);                                                               \
    }                                                                          \
} while(0)

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#define BATCH_SIZE     (1 << 20)   // 1M passwords per GPU launch
#define PW_STRIDE      128         // fixed bytes per password slot

// PW_STRIDE must be a power of two, otherwise gid * PW_STRIDE can land on a
// misaligned address in the kernel.
static_assert((PW_STRIDE & (PW_STRIDE - 1)) == 0,
              "PW_STRIDE must be a power of two");
#define MAX_LINES      16
#define MAX_TOKENS     11200
#define MAX_TOKEN_LEN  32
#define MAX_FREE       10
#define MAX_ANCHORED   4
#define PW_MAX_LEN     128

// ---------------------------------------------------------------------------
// Constant memory (crypto tables + wallet data)
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
// Device: MD5
// ---------------------------------------------------------------------------

#define MD5_F(x,y,z) (((x)&(y))|(~(x)&(z)))
#define MD5_G(x,y,z) (((x)&(z))|((y)&~(z)))
#define MD5_H(x,y,z) ((x)^(y)^(z))
#define MD5_I(x,y,z) ((y)^((x)|~(z)))
#define MD5_ROTL(x,n) (((x)<<(n))|((x)>>(32-(n))))
#define MD5_STEP(f,a,b,c,d,x,t,s) \
    (a)+=f((b),(c),(d))+(x)+(uint32_t)(t); (a)=MD5_ROTL((a),(s))+(b);

__device__ void md5_compress(uint32_t* st, uint32_t* blk) {
    uint32_t a=st[0],b=st[1],c=st[2],d=st[3];
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
    uint32_t st[4]={0x67452301u,0xefcdab89u,0x98badcfeu,0x10325476u};
    uint32_t blk[16];
    uint32_t pos=0;
    while (pos+64<=len) {
        for (int i=0;i<16;i++) {
            uint32_t j=pos+i*4;
            blk[i]=(uint32_t)data[j]|((uint32_t)data[j+1]<<8)|((uint32_t)data[j+2]<<16)|((uint32_t)data[j+3]<<24);
        }
        md5_compress(st,blk); pos+=64;
    }
    uint32_t rem=len-pos;
    uint8_t buf[128];
    for (uint32_t i=0;i<rem;i++) buf[i]=data[pos+i];
    buf[rem]=0x80;
    for (uint32_t i=rem+1;i<128;i++) buf[i]=0;
    uint64_t bits=(uint64_t)len*8;
    uint32_t off=(rem<56)?56:120;
    for (int i=0;i<8;i++) buf[off+i]=(uint8_t)(bits>>(i*8));
    uint32_t nb=(rem<56)?1:2;
    for (uint32_t b=0;b<nb;b++) {
        for (int i=0;i<16;i++) {
            uint32_t j=b*64+i*4;
            blk[i]=(uint32_t)buf[j]|((uint32_t)buf[j+1]<<8)|((uint32_t)buf[j+2]<<16)|((uint32_t)buf[j+3]<<24);
        }
        md5_compress(st,blk);
    }
    for (int i=0;i<4;i++) {
        digest[i*4+0]=(uint8_t)(st[i]);    digest[i*4+1]=(uint8_t)(st[i]>>8);
        digest[i*4+2]=(uint8_t)(st[i]>>16);digest[i*4+3]=(uint8_t)(st[i]>>24);
    }
}

// ---------------------------------------------------------------------------
// Device: AES-256 (equivalent inverse cipher)
// ---------------------------------------------------------------------------

__device__ void aes256_key_expand(const uint8_t* key, uint32_t* rk) {
    const uint32_t RCON[7]={0x01000000u,0x02000000u,0x04000000u,0x08000000u,0x10000000u,0x20000000u,0x40000000u};
    for (int i=0;i<8;i++)
        rk[i]=((uint32_t)key[i*4]<<24)|((uint32_t)key[i*4+1]<<16)|((uint32_t)key[i*4+2]<<8)|key[i*4+3];
    for (int i=8;i<60;i++) {
        uint32_t t=rk[i-1];
        if (i%8==0) {
            t=(c_SBOX[(t>>16)&0xff]<<24)|(c_SBOX[(t>>8)&0xff]<<16)|(c_SBOX[t&0xff]<<8)|c_SBOX[(t>>24)&0xff];
            t^=RCON[i/8-1];
        } else if (i%8==4) {
            t=(c_SBOX[(t>>24)&0xff]<<24)|(c_SBOX[(t>>16)&0xff]<<16)|(c_SBOX[(t>>8)&0xff]<<8)|c_SBOX[t&0xff];
        }
        rk[i]=rk[i-8]^t;
    }
    for (int i=4;i<56;i++) {
        uint32_t w=rk[i];
        rk[i]=c_TD0[c_SBOX[(w>>24)&0xff]]^c_TD1[c_SBOX[(w>>16)&0xff]]
              ^c_TD2[c_SBOX[(w>>8)&0xff]] ^c_TD3[c_SBOX[w&0xff]];
    }
}

__device__ void aes256_block_decrypt(const uint32_t* rk, const uint8_t* xb, const uint8_t* ct, uint8_t* pt) {
    uint32_t s0=((uint32_t)ct[0]<<24)|((uint32_t)ct[1]<<16)|((uint32_t)ct[2]<<8)|ct[3];
    uint32_t s1=((uint32_t)ct[4]<<24)|((uint32_t)ct[5]<<16)|((uint32_t)ct[6]<<8)|ct[7];
    uint32_t s2=((uint32_t)ct[8]<<24)|((uint32_t)ct[9]<<16)|((uint32_t)ct[10]<<8)|ct[11];
    uint32_t s3=((uint32_t)ct[12]<<24)|((uint32_t)ct[13]<<16)|((uint32_t)ct[14]<<8)|ct[15];
    s0^=rk[56];s1^=rk[57];s2^=rk[58];s3^=rk[59];
    uint32_t t0,t1,t2,t3;
    for (int r=13;r>=1;r--) {
        t0=c_TD0[(s0>>24)&0xff]^c_TD1[(s3>>16)&0xff]^c_TD2[(s2>>8)&0xff]^c_TD3[s1&0xff]^rk[r*4+0];
        t1=c_TD0[(s1>>24)&0xff]^c_TD1[(s0>>16)&0xff]^c_TD2[(s3>>8)&0xff]^c_TD3[s2&0xff]^rk[r*4+1];
        t2=c_TD0[(s2>>24)&0xff]^c_TD1[(s1>>16)&0xff]^c_TD2[(s0>>8)&0xff]^c_TD3[s3&0xff]^rk[r*4+2];
        t3=c_TD0[(s3>>24)&0xff]^c_TD1[(s2>>16)&0xff]^c_TD2[(s1>>8)&0xff]^c_TD3[s0&0xff]^rk[r*4+3];
        s0=t0;s1=t1;s2=t2;s3=t3;
    }
    t0=((uint32_t)c_SBOX_INV[(s0>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s3>>16)&0xff]<<16)|((uint32_t)c_SBOX_INV[(s2>>8)&0xff]<<8)|c_SBOX_INV[s1&0xff];
    t1=((uint32_t)c_SBOX_INV[(s1>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s0>>16)&0xff]<<16)|((uint32_t)c_SBOX_INV[(s3>>8)&0xff]<<8)|c_SBOX_INV[s2&0xff];
    t2=((uint32_t)c_SBOX_INV[(s2>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s1>>16)&0xff]<<16)|((uint32_t)c_SBOX_INV[(s0>>8)&0xff]<<8)|c_SBOX_INV[s3&0xff];
    t3=((uint32_t)c_SBOX_INV[(s3>>24)&0xff]<<24)|((uint32_t)c_SBOX_INV[(s2>>16)&0xff]<<16)|((uint32_t)c_SBOX_INV[(s1>>8)&0xff]<<8)|c_SBOX_INV[s0&0xff];
    t0^=rk[0];t1^=rk[1];t2^=rk[2];t3^=rk[3];
    uint32_t x0=((uint32_t)xb[0]<<24)|((uint32_t)xb[1]<<16)|((uint32_t)xb[2]<<8)|xb[3];
    uint32_t x1=((uint32_t)xb[4]<<24)|((uint32_t)xb[5]<<16)|((uint32_t)xb[6]<<8)|xb[7];
    uint32_t x2=((uint32_t)xb[8]<<24)|((uint32_t)xb[9]<<16)|((uint32_t)xb[10]<<8)|xb[11];
    uint32_t x3=((uint32_t)xb[12]<<24)|((uint32_t)xb[13]<<16)|((uint32_t)xb[14]<<8)|xb[15];
    t0^=x0;t1^=x1;t2^=x2;t3^=x3;
    pt[0]=(uint8_t)(t0>>24);pt[1]=(uint8_t)(t0>>16);pt[2]=(uint8_t)(t0>>8);pt[3]=(uint8_t)t0;
    pt[4]=(uint8_t)(t1>>24);pt[5]=(uint8_t)(t1>>16);pt[6]=(uint8_t)(t1>>8);pt[7]=(uint8_t)t1;
    pt[8]=(uint8_t)(t2>>24);pt[9]=(uint8_t)(t2>>16);pt[10]=(uint8_t)(t2>>8);pt[11]=(uint8_t)t2;
    pt[12]=(uint8_t)(t3>>24);pt[13]=(uint8_t)(t3>>16);pt[14]=(uint8_t)(t3>>8);pt[15]=(uint8_t)t3;
}

__device__ bool all_b58(const uint8_t* buf, int len) {
    for (int i=0;i<len;i++) {
        uint8_t c=buf[i];
        if (c<'1'||c>'z') return false;
        if (c>'9'&&c<'A') return false;
        if (c>'Z'&&c<'a') return false;
        if (c=='I'||c=='O'||c=='l') return false;
    }
    return true;
}

__device__ bool check_multibit(const uint8_t* pw, int pw_len) {
    uint8_t salted[144];
    for (int i=0;i<pw_len;i++) salted[i]=pw[i];
    for (int i=0;i<8;i++)      salted[pw_len+i]=c_salt[i];
    int slen=pw_len+8;
    uint8_t key1[16],key2[16],iv[16];
    md5(salted,slen,key1);
    uint8_t tmp[160];
    for (int i=0;i<16;i++) tmp[i]=key1[i];
    for (int i=0;i<slen;i++) tmp[16+i]=salted[i];
    md5(tmp,16+slen,key2);
    for (int i=0;i<16;i++) tmp[i]=key2[i];
    md5(tmp,16+slen,iv);
    uint8_t aes_key[32];
    for (int i=0;i<16;i++) aes_key[i]=key1[i];
    for (int i=0;i<16;i++) aes_key[16+i]=key2[i];
    uint32_t rk[60];
    aes256_key_expand(aes_key,rk);
    uint8_t pt1[16];
    aes256_block_decrypt(rk,iv,c_enc,pt1);
    uint8_t b0=pt1[0];
    if (b0!='L'&&b0!='K'&&b0!='5'&&b0!='Q') return false;
    if (!all_b58(pt1+1,15)) return false;
    uint8_t pt2[16];
    aes256_block_decrypt(rk,c_enc,c_enc+16,pt2);
    return all_b58(pt2,16);
}

// ---------------------------------------------------------------------------
// GPU kernel: one thread per password, simple check only.
// Passwords are pre-built on the CPU; no generation logic on GPU.
// ---------------------------------------------------------------------------

__global__ void check_kernel(
    const uint8_t* __restrict__ pw_data,
    const uint32_t* __restrict__ pw_lens,
    int n,
    uint32_t stride,
    int* found_idx
) {
    int gid = blockIdx.x * blockDim.x + threadIdx.x;
    if (gid >= n) return;
    uint32_t len = pw_lens[gid];
    if (len == 0) return;
    if (check_multibit(pw_data + gid * stride, (int)len))
        atomicCAS(found_idx, -1, gid);
}

// ---------------------------------------------------------------------------
// Host: AES/MD5 table setup
// ---------------------------------------------------------------------------

static uint32_t h_SBOX[256]={
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
static uint8_t h_SBOX_INV[256]={
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
    uint8_t p=0;
    for (int i=0;i<8;i++) { if (b&1) p^=a; uint8_t hi=a&0x80; a<<=1; if (hi) a^=0x1b; b>>=1; }
    return p;
}
static void build_and_upload_tables() {
    uint32_t td0[256],td1[256],td2[256],td3[256];
    for (int x=0;x<256;x++) {
        uint8_t s=h_SBOX_INV[x];
        td0[x]=((uint32_t)gmul(14,s)<<24)|((uint32_t)gmul(9,s)<<16)|((uint32_t)gmul(13,s)<<8)|gmul(11,s);
        td1[x]=(td0[x]>>8)|(td0[x]<<24);
        td2[x]=(td1[x]>>8)|(td1[x]<<24);
        td3[x]=(td2[x]>>8)|(td2[x]<<24);
    }
    CUDA_CHECK(cudaMemcpyToSymbol(c_TD0,     td0,       1024));
    CUDA_CHECK(cudaMemcpyToSymbol(c_TD1,     td1,       1024));
    CUDA_CHECK(cudaMemcpyToSymbol(c_TD2,     td2,       1024));
    CUDA_CHECK(cudaMemcpyToSymbol(c_TD3,     td3,       1024));
    CUDA_CHECK(cudaMemcpyToSymbol(c_SBOX,    h_SBOX,    1024));
    CUDA_CHECK(cudaMemcpyToSymbol(c_SBOX_INV,h_SBOX_INV,256));
}

// ---------------------------------------------------------------------------
// Host: wallet loader
// ---------------------------------------------------------------------------

static void load_wallet(const char* path, uint8_t* enc_out, uint8_t* salt_out) {
    FILE* f=fopen(path,"r");
    if (!f) { fprintf(stderr,"Cannot open wallet: %s\n",path); exit(1); }
    char buf[80]={0}; fread(buf,1,70,f); fclose(f);
    char b64[70]; int b64len=0;
    for (int i=0;buf[i]&&b64len<64;i++)
        if (buf[i]!='\r'&&buf[i]!='\n'&&buf[i]!=' ') b64[b64len++]=buf[i];
    b64[b64len]=0;
    static const char* B64="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    uint8_t dec[48]; int dlen=0;
    for (int i=0;i<b64len-3&&dlen<48;i+=4) {
        int v=0;
        for (int j=0;j<4;j++) { const char* p=strchr(B64,b64[i+j]); v=(v<<6)|(p?(int)(p-B64):0); }
        dec[dlen++]=(v>>16)&0xff;
        if (dlen<48) dec[dlen++]=(v>>8)&0xff;
        if (dlen<48) dec[dlen++]=v&0xff;
    }
    if (dlen<48||memcmp(dec,"Salted__",8)) { fprintf(stderr,"Bad wallet\n"); exit(1); }
    memcpy(salt_out,dec+8,8); memcpy(enc_out,dec+16,32);
}

// ---------------------------------------------------------------------------
// Host: token list parser (same format as btcrecover)
// ---------------------------------------------------------------------------

struct TokenLine {
    std::vector<std::string> tokens; // all concrete token strings for this line
    bool required;
    bool has_anchor;
    int  anchor_pos;   // 0 = first position (^token)
};

static std::string strip_nl(const std::string& s) {
    std::string r=s;
    while (!r.empty()&&(r.back()=='\r'||r.back()=='\n'||r.back()==' ')) r.pop_back();
    return r;
}

// Expand a %0,4d style wildcard into all concrete strings
static std::vector<std::string> expand_digit_wildcard(int min_len, int max_len) {
    std::vector<std::string> out;
    out.push_back("");  // 0 digits
    for (int len=std::max(1,min_len);len<=max_len;len++) {
        int total=1; for (int i=0;i<len;i++) total*=10;
        for (int v=0;v<total;v++) {
            char buf[8]={0};
            for (int i=len-1;i>=0;i--) { buf[i]='0'+(v%10); v/=10; v*=10; v+=buf[i]-'0'; }
            // simpler: sprintf with zero-padding
            char fmt[8]; sprintf(fmt,"%%0%dd",len);
            char s[8]; sprintf(s,fmt,v*0+v); // just format v with len digits... actually:
            // reset v properly:
            out.push_back(""); // placeholder, fix below
        }
        out.pop_back(); // remove last placeholder
        // redo correctly:
        for (int v=0;v<total;v++) {
            char s[8]; char fmt[8]; sprintf(fmt,"%%0%dd",len); sprintf(s,fmt,v);
            out.push_back(std::string(s));
        }
    }
    // Remove duplicates from the simple loop above
    // Actually let's redo this cleanly:
    out.clear();
    if (min_len==0) out.push_back("");
    for (int len=std::max(1,min_len);len<=max_len;len++) {
        int total=1; for (int i=0;i<len;i++) total*=10;
        for (int v=0;v<total;v++) {
            char s[8],fmt[8]; sprintf(fmt,"%%0%dd",len); sprintf(s,fmt,v);
            out.push_back(std::string(s));
        }
    }
    return out;
}

static std::vector<TokenLine> parse_tokenlist(const char* path, char delimiter) {
    std::vector<TokenLine> lines;
    FILE* f=fopen(path,"r");
    if (!f) { fprintf(stderr,"Cannot open tokenlist: %s\n",path); exit(1); }
    char buf[8192];
    while (fgets(buf,sizeof(buf),f)) {
        std::string line=strip_nl(buf);
        if (line.empty()||line[0]=='#') continue;

        TokenLine tl; tl.required=false; tl.has_anchor=false; tl.anchor_pos=0;
        const char* p=line.c_str();
        if (*p=='+') { tl.required=true; while (*p=='+'||*p==delimiter) p++; }
        else while (*p==delimiter) p++;  // optional line marker (leading delimiter)

        // Split remaining by the configured delimiter (default: space). A
        // non-space delimiter frees up literal spaces to live inside a
        // token's own text, e.g. "I love Freedom" as one atomic value.
        std::string rest(p);
        std::vector<std::string> parts;
        size_t pos=0;
        while (pos<rest.size()) {
            size_t end=rest.find(delimiter,pos);
            if (end==std::string::npos) end=rest.size();
            if (end>pos) parts.push_back(rest.substr(pos,end-pos));
            pos=end+1;
        }

        for (auto& tok : parts) {
            // Digit wildcard
            if (tok=="%0,4d"||tok=="%d"||tok=="%4d") {
                int mn=(tok=="%4d")?4:0, mx=(tok=="%d")?1:4;
                auto ds=expand_digit_wildcard(mn,mx);
                for (auto& d:ds) tl.tokens.push_back(d);
            }
            // Anchored token: ^text (pos 0), ^N^text (pos N-1, 0-indexed), text$ (last)
            else if (!tok.empty()&&(tok[0]=='^'||tok.back()=='$')) {
                tl.has_anchor=true;
                std::string text=tok;
                if (!text.empty()&&text.back()=='$') {
                    tl.anchor_pos=-1;  // sentinel: last position, resolved at assembly
                    text=text.substr(0,text.size()-1);
                } else {
                    text=text.substr(1);  // strip leading ^
                    // ^N^text: N is 1-indexed position
                    size_t inner=text.find('^');
                    if (inner!=std::string::npos && inner>0) {
                        std::string num=text.substr(0,inner);
                        bool is_num=!num.empty();
                        for (char c:num) if (!isdigit(c)) { is_num=false; break; }
                        if (is_num) {
                            tl.anchor_pos=std::stoi(num)-1;  // convert to 0-indexed
                            text=text.substr(inner+1);
                        } else {
                            tl.anchor_pos=0;
                        }
                    } else {
                        tl.anchor_pos=0;  // plain ^text = first position
                    }
                }
                tl.tokens.push_back(text);
            }
            else {
                tl.tokens.push_back(tok);
            }
        }
        if (!tl.tokens.empty()) lines.push_back(tl);
    }
    fclose(f);

    // btcrecover reverses token list order so last line iterates fastest
    std::reverse(lines.begin(),lines.end());
    return lines;
}

// ---------------------------------------------------------------------------
// Host: password batch (double-buffered)
// The producer thread fills one buffer while the GPU checks the other.
// ---------------------------------------------------------------------------

struct Batch {
    std::vector<uint8_t>  pw_data;   // flat: n * PW_STRIDE bytes
    std::vector<uint32_t> pw_lens;
    int      count;
    uint64_t combo_idx;              // last combo processed (for save)
    uint64_t passwords_total;        // cumulative passwords checked so far

    Batch() : count(0), combo_idx(0), passwords_total(0) {
        pw_data.resize(BATCH_SIZE * PW_STRIDE, 0);
        pw_lens.resize(BATCH_SIZE, 0);
    }
    void reset() { count=0; memset(pw_data.data(),0,pw_data.size()); }
    bool full() const { return count>=BATCH_SIZE; }
};

// Password assembly. For each combination we collect the free tokens and run
// std::next_permutation over their indices (not the strings), which keeps the
// inner loop working on ints. Everything below stays on the stack: the slot
// tables are fixed-size arrays of pointers into the token strings that already
// live in TokenLine.tokens, and lengths travel alongside so there's no strlen.
// Keeping this allocation-free in the hot path is what gets us to ~11M/s;
// the std::string version sat around 3M.

struct AnchorSlot {
    int         pos;   // 0-indexed slot position; -1 = last
    const char* text;
    int         len;
};

// Writes the assembled password into 'out' (char[PW_MAX_LEN]), sets *out_len.
// All token pointers are stable (point into TokenLine.tokens strings).
static inline void assemble_password_fast(
    const char** free_ptrs, const int* free_lens, int n_free,
    const AnchorSlot* anchors, int n_anchored,
    const int* perm,   // permutation of [0..n_free-1]
    char* out, int* out_len)
{
    int total = n_free + n_anchored;
    if (total == 0) { out[0]=0; *out_len=0; return; }

    // Stack slot table, no heap allocation
    const char* slots[MAX_FREE + MAX_ANCHORED] = {};
    int         slens[MAX_FREE + MAX_ANCHORED] = {};
    bool        taken[MAX_FREE + MAX_ANCHORED] = {};

    for (int i = 0; i < n_anchored; i++) {
        int pos = (anchors[i].pos == -1) ? total - 1 : anchors[i].pos;
        if (pos >= 0 && pos < total && !taken[pos]) {
            slots[pos] = anchors[i].text;
            slens[pos] = anchors[i].len;
            taken[pos] = true;
        }
    }

    int fi = 0;
    for (int i = 0; i < total && fi < n_free; i++) {
        if (!taken[i]) {
            slots[i] = free_ptrs[perm[fi]];
            slens[i] = free_lens[perm[fi]];
            taken[i] = true;
            fi++;
        }
    }

    int pos = 0;
    for (int i = 0; i < total; i++) {
        if (!slots[i]) continue;
        int l = slens[i];
        if (pos + l > PW_MAX_LEN - 1) l = PW_MAX_LEN - 1 - pos;
        memcpy(out + pos, slots[i], l);
        pos += l;
    }
    out[pos] = 0;
    *out_len = pos;
}

// Typo generation, ported from btcrecover (btcrpass.py's capslock/swap/simple/
// insert typo generators). Stages run in btcrecover's order - capslock, swap,
// simple (repeat/delete/closecase), insert - and share one --typos N budget, so
// the output is every way of spreading up to N typos across the enabled stages.
// btcrecover threads the budget through chained Python generators; this does the
// same thing with plain recursion. The variant set matches.
//
// This path uses std::string/vector/function and allocates freely, unlike the
// base assembly loop. That's fine: typos are for hammering on a small set of
// near-miss candidates, not for driving the full tokenlist search.

struct TypoConfig {
    int  max_typos        = 0;
    bool capslock         = false;
    bool swap             = false;
    int  max_typos_swap   = 1000000;
    bool repeat           = false;
    bool del              = false;
    bool closecase        = false;
    int  max_typos_simple = 1000000;
    bool insert           = false;
    int  max_typos_insert = 1000000;
    std::string insert_charset;

    bool any() const { return capslock || swap || repeat || del || closecase || insert; }
};

struct TypoCandidate {
    std::string pw;
    int typos_used;
};

static inline char swap_case_ch(char c) {
    if (c>='A'&&c<='Z') return char(c - 'A' + 'a');
    if (c>='a'&&c<='z') return char(c - 'a' + 'A');
    return c;
}
static std::string swapcase_str(const std::string& s) {
    std::string r = s;
    for (auto& c : r) c = swap_case_ch(c);
    return r;
}
// 0 = uncased, 1 = upper, 2 = lower
static inline int case_id_ch(char c) {
    if (c>='A'&&c<='Z') return 1;
    if (c>='a'&&c<='z') return 2;
    return 0;
}
// closecase only flips a letter that sits next to a case change (or at either
// end): the shift-held-a-beat-too-long typo, rather than a case flip anywhere.
static bool is_case_transition(const std::string& pw, int i) {
    int cur = case_id_ch(pw[i]);
    if (cur == 0) return false;
    if (i == 0 || i+1 == (int)pw.size()) return true;
    int prev = case_id_ch(pw[i-1]);
    int next = case_id_ch(pw[i+1]);
    return (prev != 0 && prev != cur) || (next != 0 && next != cur);
}

static void typo_stage_capslock(const std::vector<TypoCandidate>& in, const TypoConfig& cfg,
                                 std::vector<TypoCandidate>& out) {
    for (auto& c : in) {
        out.push_back(c);
        if (cfg.capslock && c.typos_used < cfg.max_typos) {
            std::string sw = swapcase_str(c.pw);
            if (sw != c.pw) out.push_back({sw, c.typos_used + 1});
        }
    }
}

// Choose `remaining` more non-overlapping adjacent-pair indexes from
// [start, len-2], applying each complete selection to `base` and emitting it.
static void swap_combinations(const std::string& base, int len, int start, int remaining,
                               std::vector<int>& chosen, int typos_used,
                               std::vector<TypoCandidate>& out) {
    if (remaining == 0) {
        std::string pw = base;
        for (size_t k = 0; k < chosen.size(); k++) {
            int i = chosen[k];
            if (pw[i] == pw[i+1]) return;  // no-op swap, matches btcrecover's dup avoidance
            std::swap(pw[i], pw[i+1]);
        }
        out.push_back({pw, typos_used + (int)chosen.size()});
        return;
    }
    for (int i = start; i <= len - 2; i++) {
        if (!chosen.empty() && i == chosen.back() + 1) continue;  // would re-swap a char
        chosen.push_back(i);
        swap_combinations(base, len, i + 1, remaining - 1, chosen, typos_used, out);
        chosen.pop_back();
    }
}

static void typo_stage_swap(const std::vector<TypoCandidate>& in, const TypoConfig& cfg,
                             std::vector<TypoCandidate>& out) {
    for (auto& c : in) {
        out.push_back(c);
        if (!cfg.swap) continue;
        int len = (int)c.pw.size();
        int budget = std::min({cfg.max_typos - c.typos_used, cfg.max_typos_swap, len / 2});
        for (int k = 1; k <= budget; k++) {
            std::vector<int> chosen;
            swap_combinations(c.pw, len, 0, k, chosen, c.typos_used, out);
        }
    }
}

// Choose `remaining` more distinct positions from [start, len), then cross
// every enabled simple-typo option at each chosen position. If any position
// has no valid option (e.g. closecase away from a case boundary), the whole
// combination produces nothing.
static void simple_positions(const std::string& base, int len, int start, int remaining,
                              std::vector<int>& chosen, const TypoConfig& cfg,
                              int typos_used, std::vector<TypoCandidate>& out) {
    if (remaining == 0) {
        int k = (int)chosen.size();
        std::vector<std::vector<std::string>> options(k);
        for (int j = 0; j < k; j++) {
            int i = chosen[j];
            auto& opts = options[j];
            if (cfg.repeat) opts.push_back(std::string(2, base[i]));
            if (cfg.del)    opts.push_back(std::string());
            if (cfg.closecase && is_case_transition(base, i)) {
                char sc = swap_case_ch(base[i]);
                if (sc != base[i]) opts.push_back(std::string(1, sc));
            }
            if (opts.empty()) return;
        }
        std::vector<int> sel(k, 0);
        std::function<void(int)> cross = [&](int pos) {
            if (pos == k) {
                std::string pw;
                pw.reserve(len + k);
                int prev = 0;
                for (int j = 0; j < k; j++) {
                    pw += base.substr(prev, chosen[j] - prev);
                    pw += options[j][sel[j]];
                    prev = chosen[j] + 1;
                }
                pw += base.substr(prev);
                out.push_back({pw, typos_used + k});
                return;
            }
            for (int o = 0; o < (int)options[pos].size(); o++) { sel[pos] = o; cross(pos + 1); }
        };
        cross(0);
        return;
    }
    for (int i = start; i < len; i++) {
        chosen.push_back(i);
        simple_positions(base, len, i + 1, remaining - 1, chosen, cfg, typos_used, out);
        chosen.pop_back();
    }
}

static void typo_stage_simple(const std::vector<TypoCandidate>& in, const TypoConfig& cfg,
                               std::vector<TypoCandidate>& out) {
    bool any_simple = cfg.repeat || cfg.del || cfg.closecase;
    for (auto& c : in) {
        out.push_back(c);
        if (!any_simple) continue;
        int len = (int)c.pw.size();
        int budget = std::min({cfg.max_typos - c.typos_used, cfg.max_typos_simple, len});
        for (int k = 1; k <= budget; k++) {
            std::vector<int> chosen;
            simple_positions(c.pw, len, 0, k, chosen, cfg, c.typos_used, out);
        }
    }
}

// Choose `remaining` more insertion points from [start, len] (positions may
// repeat, i.e. more than one character can be inserted at the same point),
// then cross every charset character at each chosen point and emit the
// resulting password.
static void insert_positions(const std::string& base, int len, int start, int remaining,
                              std::vector<int>& chosen, const TypoConfig& cfg,
                              int typos_used, std::vector<TypoCandidate>& out) {
    if (remaining == 0) {
        int k = (int)chosen.size();
        int csn = (int)cfg.insert_charset.size();
        std::vector<int> sel(k, 0);
        std::function<void(int)> cross = [&](int pos) {
            if (pos == k) {
                std::string pw;
                pw.reserve(len + k);
                int prev = 0;
                for (int j = 0; j < k; j++) {
                    pw += base.substr(prev, chosen[j] - prev);
                    pw += cfg.insert_charset[sel[j]];
                    prev = chosen[j];
                }
                pw += base.substr(prev);
                out.push_back({pw, typos_used + k});
                return;
            }
            for (int o = 0; o < csn; o++) { sel[pos] = o; cross(pos + 1); }
        };
        cross(0);
        return;
    }
    for (int i = start; i <= len; i++) {
        chosen.push_back(i);
        insert_positions(base, len, i, remaining - 1, chosen, cfg, typos_used, out);
        chosen.pop_back();
    }
}

static void typo_stage_insert(const std::vector<TypoCandidate>& in, const TypoConfig& cfg,
                               std::vector<TypoCandidate>& out) {
    for (auto& c : in) {
        out.push_back(c);
        if (!cfg.insert || cfg.insert_charset.empty()) continue;
        int len = (int)c.pw.size();
        int budget = std::min({cfg.max_typos - c.typos_used, cfg.max_typos_insert, len + 1});
        for (int k = 1; k <= budget; k++) {
            std::vector<int> chosen;
            insert_positions(c.pw, len, 0, k, chosen, cfg, c.typos_used, out);
        }
    }
}

// Top-level: applies all enabled typo stages, in btcrecover's fixed order,
// to a single base password, sharing the --typos N budget across all of
// them. Returns every variant, including the unmodified original (0 typos).
static std::vector<TypoCandidate> generate_typo_variants(const std::string& base_pw,
                                                           const TypoConfig& cfg) {
    std::vector<TypoCandidate> stage{ {base_pw, 0} };
    if (cfg.capslock) {
        std::vector<TypoCandidate> next;
        typo_stage_capslock(stage, cfg, next);
        stage = std::move(next);
    }
    if (cfg.swap) {
        std::vector<TypoCandidate> next;
        typo_stage_swap(stage, cfg, next);
        stage = std::move(next);
    }
    if (cfg.repeat || cfg.del || cfg.closecase) {
        std::vector<TypoCandidate> next;
        typo_stage_simple(stage, cfg, next);
        stage = std::move(next);
    }
    if (cfg.insert) {
        std::vector<TypoCandidate> next;
        typo_stage_insert(stage, cfg, next);
        stage = std::move(next);
    }
    return stage;
}

// ---------------------------------------------------------------------------
// Producer thread: generates passwords and fills batch buffers
// ---------------------------------------------------------------------------

struct ProducerState {
    const std::vector<TokenLine>* lines;
    uint64_t start_combo;
    uint64_t total_combos;
    const TypoConfig*       typo_cfg = nullptr;  // nullptr or !any() => no typo generation

    std::mutex              mtx;
    std::condition_variable cv_ready;    // signals main thread: batch is ready
    std::condition_variable cv_consumed; // signals producer: batch was consumed
    std::unique_ptr<Batch>  ready_batch; // owned; transferred via std::move
    bool                    done;

    ProducerState() : ready_batch(nullptr), done(false) {}
};

static void producer_thread(ProducerState* state, uint64_t pw_count_base) {
    const auto& lines = *state->lines;
    int n_lines = (int)lines.size();

    // Compute line sizes (number of options including None for optional)
    std::vector<int> line_sizes(n_lines);
    for (int i=0;i<n_lines;i++)
        line_sizes[i] = (int)lines[i].tokens.size() + (lines[i].required ? 0 : 1);

    // Initialise combo to start_combo using mixed-radix decode
    std::vector<int> combo(n_lines,0);
    {
        uint64_t idx=state->start_combo;
        for (int i=0;i<n_lines;i++) { combo[i]=(int)(idx%line_sizes[i]); idx/=line_sizes[i]; }
    }

    // unique_ptr ensures Batch is freed even on early exit (exception or done signal).
    auto cur = std::make_unique<Batch>();
    cur->passwords_total = pw_count_base;
    uint64_t combo_idx = state->start_combo;

    auto push_batch = [&]() {
        if (cur->count == 0) return;
        std::unique_lock<std::mutex> lk(state->mtx);
        state->cv_consumed.wait(lk,[&]{ return state->ready_batch==nullptr||state->done; });
        if (state->done) return;
        uint64_t next_base = cur->passwords_total + cur->count;
        state->ready_batch = std::move(cur);   // transfer ownership to main thread
        state->cv_ready.notify_one();
        cur = std::make_unique<Batch>();
        cur->passwords_total = next_base;
    };

    // Hot-path buffers, reused across every combo and permutation.
    const char* free_ptrs[MAX_FREE];
    int         free_lens[MAX_FREE];
    AnchorSlot  anchors[MAX_ANCHORED];
    int         perm[MAX_FREE];
    char        pw_buf[PW_MAX_LEN];
    int         pw_len;

    auto add_password = [&](uint64_t cidx) {
        if (pw_len == 0 || pw_len > PW_MAX_LEN) return;
        int slot = cur->count;
        uint8_t* dst = cur->pw_data.data() + slot * PW_STRIDE;
        memcpy(dst, pw_buf, pw_len);
        cur->pw_lens[slot] = (uint32_t)pw_len;
        cur->combo_idx = cidx;
        cur->count++;
        if (cur->full()) push_batch();
    };

    // Sibling of add_password() for typo variants, which are std::strings
    // (heap-allocated during generation) rather than the stack pw_buf.
    auto add_password_str = [&](const std::string& s, uint64_t cidx) {
        if (s.empty() || (int)s.size() > PW_MAX_LEN) return;
        int slot = cur->count;
        uint8_t* dst = cur->pw_data.data() + slot * PW_STRIDE;
        memcpy(dst, s.data(), s.size());
        cur->pw_lens[slot] = (uint32_t)s.size();
        cur->combo_idx = cidx;
        cur->count++;
        if (cur->full()) push_batch();
    };

    // Iterate all combos from start_combo to total_combos
    for (; combo_idx < state->total_combos; combo_idx++) {

        // Decode this combo into free and anchored tokens. Pointers reference
        // the token strings directly, nothing is copied here.
        int n_free = 0, n_anchored = 0;
        bool valid = true;

        for (int i=0;i<n_lines;i++) {
            int ch = combo[i];
            if (!lines[i].required) {
                if (ch == 0) continue;  // None: skip this line
                ch--;                   // 0=None, 1..n = tokens[0..n-1]
            }
            if (ch < 0 || ch >= (int)lines[i].tokens.size()) { valid=false; break; }
            const std::string& tok = lines[i].tokens[ch];
            if (lines[i].has_anchor) {
                if (n_anchored < MAX_ANCHORED) {
                    anchors[n_anchored++] = { lines[i].anchor_pos,
                                              tok.c_str(), (int)tok.size() };
                }
            } else {
                if (n_free < MAX_FREE) {
                    free_ptrs[n_free] = tok.c_str();
                    free_lens[n_free] = (int)tok.size();
                    n_free++;
                }
            }
        }

        if (valid && n_free > 0) {
            // Walk all n_free! orderings, one per next_permutation step.
            for (int i=0;i<n_free;i++) perm[i]=i;
            do {
                assemble_password_fast(free_ptrs, free_lens, n_free,
                                       anchors, n_anchored, perm,
                                       pw_buf, &pw_len);
                if (state->typo_cfg && state->typo_cfg->any()) {
                    for (auto& v : generate_typo_variants(std::string(pw_buf, pw_len), *state->typo_cfg))
                        add_password_str(v.pw, combo_idx);
                } else {
                    add_password(combo_idx);
                }
            } while (std::next_permutation(perm, perm + n_free));
        }

        // Advance combo (mixed-radix increment)
        for (int i=0;i<n_lines;i++) {
            combo[i]++;
            if (combo[i] < line_sizes[i]) break;
            combo[i] = 0;
        }
    }

    // Push any remaining partial batch
    if (cur->count > 0) push_batch();

    // Signal done
    {
        std::unique_lock<std::mutex> lk(state->mtx);
        state->done = true;
        state->cv_ready.notify_all();
    }
    // cur is a unique_ptr, freed automatically here
}


// ---------------------------------------------------------------------------
// Save / restore
// ---------------------------------------------------------------------------

struct SaveState {
    char     tokenlist[512];
    char     wallet[512];
    uint64_t combo_idx;
    uint64_t total_combos;
    uint64_t passwords_checked;
};

static void save_progress(const char* path, const SaveState& s) {
    FILE* f=fopen(path,"wb");
    if (f) { fwrite(&s,sizeof(s),1,f); fclose(f); }
}
static bool load_progress(const char* path, SaveState& s) {
    FILE* f=fopen(path,"rb");
    if (!f) return false;
    bool ok=(fread(&s,sizeof(s),1,f)==1);
    fclose(f); return ok;
}

// Write the recovered password to a file instead of the terminal, so it
// doesn't persist in scrollback, tmux/screen logs, or redirected output.
// The file is created owner-read/write only (_S_IREAD|_S_IWRITE).
static void write_found_password(const uint8_t* pw, int len) {
    const char* path = "RECOVERED_PASSWORD.txt";
    int fd = _open(path, _O_CREAT | _O_WRONLY | _O_TRUNC | _O_BINARY,
                    _S_IREAD | _S_IWRITE);
    if (fd < 0) {
        fprintf(stderr, "\nWARNING: could not create %s; printing password below instead.\n", path);
        printf("\n*** PASSWORD FOUND: '");
        fwrite(pw, 1, len, stdout);
        printf("' ***\n");
        return;
    }
    _write(fd, pw, len);
    _close(fd);
    printf("\n*** PASSWORD FOUND - written to %s ***\n", path);
}

// ---------------------------------------------------------------------------
// Progress display helpers
// ---------------------------------------------------------------------------

// Format a raw count with adaptive units so the display is readable at any scale.
// 0.000T is useless for a 76M-password search; this shows "76.00M" instead.
static const char* fmt_count(double n, char* buf) {
    if      (n >= 1e12) sprintf(buf, "%.6fT", n/1e12);  // 6 dp = 1M resolution at T scale
    else if (n >= 1e9)  sprintf(buf, "%.3fB", n/1e9);   // 3 dp = 1M resolution at B scale
    else if (n >= 1e6)  sprintf(buf, "%.2fM", n/1e6);
    else if (n >= 1e3)  sprintf(buf, "%.1fK", n/1e3);
    else                sprintf(buf, "%.0f",  n);
    return buf;
}

// Format seconds as a human-readable duration (5s / 3.2m / 1.4h / 23.1d).
static const char* fmt_eta(double secs, char* buf) {
    if (secs <= 0)           strcpy(buf, "---");
    else if (secs < 60)      sprintf(buf, "%.0fs",  secs);
    else if (secs < 3600)    sprintf(buf, "%.1fm",  secs/60);
    else if (secs < 86400)   sprintf(buf, "%.1fh",  secs/3600);
    else                     sprintf(buf, "%.1fd",  secs/86400);
    return buf;
}

// ---------------------------------------------------------------------------
// GPU engine (GPU buffers + kernel launch)
// ---------------------------------------------------------------------------

struct GPUEngine {
    uint8_t*  d_pw;
    uint32_t* d_lens;
    int*      d_found;
    int       h_found;

    GPUEngine() {
        CUDA_CHECK(cudaMalloc(&d_pw,    (size_t)BATCH_SIZE * PW_STRIDE));
        CUDA_CHECK(cudaMalloc(&d_lens,  (size_t)BATCH_SIZE * 4));
        CUDA_CHECK(cudaMalloc(&d_found, 4));
        h_found = -1;
    }
    ~GPUEngine() { cudaFree(d_pw); cudaFree(d_lens); cudaFree(d_found); }

    // Returns found password index (into the batch) or -1.
    // cudaGetLastError() is called after the kernel launch to catch
    // configuration errors (bad block size, insufficient resources) that
    // are reported asynchronously and would otherwise go undetected.
    int check(const Batch& b) {
        h_found = -1;
        CUDA_CHECK(cudaMemcpy(d_pw,    b.pw_data.data(), (size_t)b.count * PW_STRIDE, cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_lens,  b.pw_lens.data(), (size_t)b.count * 4,          cudaMemcpyHostToDevice));
        CUDA_CHECK(cudaMemcpy(d_found, &h_found, 4, cudaMemcpyHostToDevice));
        int threads = 256;
        int blocks  = (b.count + threads - 1) / threads;
        check_kernel<<<blocks, threads>>>(d_pw, d_lens, b.count, PW_STRIDE, d_found);
        CUDA_CHECK(cudaGetLastError());        // catch async launch errors
        CUDA_CHECK(cudaDeviceSynchronize());   // wait and catch execution errors
        CUDA_CHECK(cudaMemcpy(&h_found, d_found, 4, cudaMemcpyDeviceToHost));
        return h_found;
    }
};

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
    const char* wallet_path    = nullptr;
    const char* tokenlist_path = nullptr;
    const char* autosave_path  = nullptr;
    const char* restore_path   = nullptr;
    char        delimiter      = ' ';
    TypoConfig  typo_cfg;

    for (int i=1;i<argc;i++) {
        if      (!strcmp(argv[i],"--wallet")           && i+1<argc) wallet_path           = argv[++i];
        else if (!strcmp(argv[i],"--tokenlist")        && i+1<argc) tokenlist_path        = argv[++i];
        else if (!strcmp(argv[i],"--autosave")         && i+1<argc) autosave_path         = argv[++i];
        else if (!strcmp(argv[i],"--restore")          && i+1<argc) restore_path          = argv[++i];
        else if (!strcmp(argv[i],"--delimiter")        && i+1<argc) delimiter             = argv[++i][0];
        else if (!strcmp(argv[i],"--typos")            && i+1<argc) typo_cfg.max_typos    = atoi(argv[++i]);
        else if (!strcmp(argv[i],"--typos-capslock"))                typo_cfg.capslock    = true;
        else if (!strcmp(argv[i],"--typos-swap"))                    typo_cfg.swap        = true;
        else if (!strcmp(argv[i],"--max-typos-swap")   && i+1<argc) typo_cfg.max_typos_swap   = atoi(argv[++i]);
        else if (!strcmp(argv[i],"--typos-repeat"))                  typo_cfg.repeat      = true;
        else if (!strcmp(argv[i],"--typos-delete"))                  typo_cfg.del         = true;
        else if (!strcmp(argv[i],"--typos-closecase"))               typo_cfg.closecase   = true;
        else if (!strcmp(argv[i],"--typos-insert")     && i+1<argc) { typo_cfg.insert = true; typo_cfg.insert_charset = argv[++i]; }
        else if (!strcmp(argv[i],"--max-typos-insert") && i+1<argc) typo_cfg.max_typos_insert = atoi(argv[++i]);
    }
    if (typo_cfg.any() && typo_cfg.max_typos <= 0) {
        fprintf(stderr, "--typos-* flags given without --typos N (N > 0); no typos will be generated.\n");
    }

    SaveState state = {};
    if (restore_path) {
        if (!load_progress(restore_path, state)) {
            fprintf(stderr,"Cannot load save: %s\n",restore_path); return 1;
        }
        if (!wallet_path)    wallet_path    = state.wallet;
        if (!tokenlist_path) tokenlist_path = state.tokenlist;
        if (!autosave_path)  autosave_path  = restore_path;
        printf("Restored: resuming at combo #%llu (%.3fT passwords done)\n",
               (unsigned long long)state.combo_idx,
               state.passwords_checked / 1e12);
    }
    if (!wallet_path || !tokenlist_path) {
        fprintf(stderr,"Usage: multibit_cuda_threads.exe --wallet <f> --tokenlist <f> [--autosave <f>]\n"
                       "                                  [--delimiter <c>] [--typos N [--typos-capslock]\n"
                       "                                  [--typos-swap] [--typos-repeat] [--typos-delete]\n"
                       "                                  [--typos-closecase] [--typos-insert <charset>]]\n"
                       "       multibit_cuda_threads.exe --restore <save.bin>\n");
        return 1;
    }

    uint8_t h_enc[32], h_salt[8];
    load_wallet(wallet_path, h_enc, h_salt);
    build_and_upload_tables();
    CUDA_CHECK(cudaMemcpyToSymbol(c_enc,  h_enc,  32));
    CUDA_CHECK(cudaMemcpyToSymbol(c_salt, h_salt, 8));

    auto lines = parse_tokenlist(tokenlist_path, delimiter);
    printf("Token list: %s (%d lines, delimiter '%c')\n", tokenlist_path, (int)lines.size(), delimiter);

    // Count total combos for progress display
    uint64_t total_combos = 1;
    for (auto& l : lines)
        total_combos *= (uint64_t)(l.tokens.size() + (l.required ? 0 : 1));
    printf("Total combos: %llu\n", (unsigned long long)total_combos);

    if (!restore_path) {
        strncpy(state.tokenlist, tokenlist_path, 511);
        strncpy(state.wallet,    wallet_path,    511);
        state.total_combos      = total_combos;
        state.combo_idx         = 0;
        state.passwords_checked = 0;
    } else if (total_combos != state.total_combos) {
        // The tokenlist re-parsed to a different combo count than the save file
        // expects - either the tokenlist was edited, or --delimiter doesn't match
        // what the original run used. Resuming anyway would silently check the
        // wrong passwords against a stale combo_idx, so refuse instead.
        fprintf(stderr,
            "\nFATAL: re-parsed tokenlist yields %llu combos but the save file expects %llu.\n"
            "The tokenlist file or --delimiter must have changed since this save was written.\n"
            "Pass the same --delimiter used originally, and don't edit the tokenlist between\n"
            "save and restore.\n",
            (unsigned long long)total_combos, (unsigned long long)state.total_combos);
        return 1;
    }

    // Start producer thread
    ProducerState ps;
    ps.lines        = &lines;
    ps.start_combo  = state.combo_idx;
    ps.total_combos = total_combos;
    ps.typo_cfg     = &typo_cfg;
    if (typo_cfg.any() && typo_cfg.max_typos > 0)
        printf("Typos enabled: budget %d (capslock=%d swap=%d repeat=%d delete=%d closecase=%d insert=%d)\n",
               typo_cfg.max_typos, typo_cfg.capslock, typo_cfg.swap, typo_cfg.repeat,
               typo_cfg.del, typo_cfg.closecase, typo_cfg.insert);

    GPUEngine gpu;
    std::thread producer(producer_thread, &ps, state.passwords_checked);

    hrtimepoint start_time    = hrclock::now();
    hrtimepoint last_save_tp  = start_time;
    uint64_t pw_checked       = state.passwords_checked;
    uint64_t pw_session_base  = state.passwords_checked;   // historical total at session start
    uint64_t start_combo_idx  = state.combo_idx;

    printf("Starting at combo #%llu  (batch size %d)\n",
           (unsigned long long)state.combo_idx, BATCH_SIZE);

    while (true) {
        std::unique_ptr<Batch> batch;
        {
            std::unique_lock<std::mutex> lk(ps.mtx);
            ps.cv_ready.wait(lk, [&]{ return ps.ready_batch != nullptr || ps.done; });
            if (ps.ready_batch) {
                batch = std::move(ps.ready_batch);  // take ownership; no delete needed
                ps.cv_consumed.notify_one();
            } else {
                break; // done and no pending batch
            }
        }

        int found = gpu.check(*batch);
        pw_checked = batch->passwords_total + batch->count;

        if (found >= 0) {
            // Password found: read directly from the batch buffer.
            // The buffer contains the exact bytes the producer assembled,
            // so no index-to-string reconstruction is needed.
            const uint8_t* pw = batch->pw_data.data() + found * PW_STRIDE;
            int len = batch->pw_lens[found];
            write_found_password(pw, len);
            if (autosave_path) {
                state.combo_idx = batch->combo_idx;
                state.passwords_checked = pw_checked;
                save_progress(autosave_path, state);
            }
            ps.done = true;
            ps.cv_consumed.notify_all();
            break;  // batch freed automatically by unique_ptr destructor
        }

        state.combo_idx         = batch->combo_idx + 1;
        state.passwords_checked = pw_checked;
        // batch freed automatically here by unique_ptr destructor

        // Progress: rate and ETA based on THIS SESSION only so a restored
        // run shows true current throughput, not inflated by historical totals.
        double elapsed        = secs_since(start_time);   // high-res, sub-second
        uint64_t session_pw   = pw_checked - pw_session_base;
        double rate           = elapsed > 1.0 ? (double)session_pw / elapsed : 0.0;
        uint64_t session_cb   = state.combo_idx - start_combo_idx;
        double combo_rate     = (elapsed > 1.0 && session_cb > 0)
                                    ? (double)session_cb / elapsed : 0.0;
        double remaining_cb   = (double)(total_combos - state.combo_idx);
        double eta_secs       = combo_rate > 0 ? remaining_cb / combo_rate : 0.0;
        double frac           = total_combos > 0
                                    ? (double)state.combo_idx / total_combos : 0.0;
        char pw_buf[32], rate_buf[32], eta_buf[32];
        printf("\r%16s passwords  %9s/s  %.1f%%  ETA %s",
               fmt_count((double)pw_checked, pw_buf),
               fmt_count(rate, rate_buf),
               frac * 100.0,
               fmt_eta(eta_secs, eta_buf));
        fflush(stdout);

        if (autosave_path && secs_since(last_save_tp) >= 30.0) {
            save_progress(autosave_path, state);
            last_save_tp = hrclock::now();
        }
    }

    producer.join();

    if (state.combo_idx >= total_combos) {
        double elapsed = secs_since(start_time);
        char pw_buf[32], rate_buf[32];
        printf("\nSearch complete. %s passwords in %.0fs (%s/s). Not found.\n",
               fmt_count((double)pw_checked, pw_buf),
               elapsed,
               fmt_count(elapsed > 0 ? (double)pw_checked / elapsed : 0, rate_buf));
    }
    return 0;
}
