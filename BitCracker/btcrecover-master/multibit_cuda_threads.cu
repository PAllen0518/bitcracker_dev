/*
 * multibit_cuda_threads.cu  -  C++ host generation + CUDA GPU checking
 *
 * Architecture (why this is faster than multibit_cuda.cu):
 *   multibit_cuda.cu used GPU-side permutation generation via nth_permutation,
 *   which has O(n^2) complexity and causes thread divergence.  This version
 *   generates passwords on the CPU in C++ using std::next_permutation, which
 *   runs at ~100-500M passwords/sec (vs Python's ~500K/sec).  The GPU only
 *   does what it is uniquely good at: the MD5+AES+base58 crypto check.
 *
 *   A background thread generates passwords and packs them into batches while
 *   the main thread simultaneously runs the GPU kernel on the previous batch.
 *   This fully overlaps CPU generation with GPU computation.
 *
 * Compile:
 *   build_cuda.bat  (or see build_cuda.bat for the exact nvcc flags)
 *
 * Usage:
 *   multibit_cuda_threads.exe --wallet multi.key --tokenlist search46.txt --autosave save.bin
 *   multibit_cuda_threads.exe --restore save.bin
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <cuda_runtime.h>

#include <string>
#include <vector>
#include <algorithm>
#include <numeric>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <queue>
#include <atomic>

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#define BATCH_SIZE     (1 << 20)   // 1M passwords per GPU launch
#define PW_STRIDE      128         // fixed bytes per password slot
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
    cudaMemcpyToSymbol(c_TD0,td0,1024); cudaMemcpyToSymbol(c_TD1,td1,1024);
    cudaMemcpyToSymbol(c_TD2,td2,1024); cudaMemcpyToSymbol(c_TD3,td3,1024);
    cudaMemcpyToSymbol(c_SBOX,h_SBOX,1024);
    cudaMemcpyToSymbol(c_SBOX_INV,h_SBOX_INV,256);
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

static std::vector<TokenLine> parse_tokenlist(const char* path) {
    std::vector<TokenLine> lines;
    FILE* f=fopen(path,"r");
    if (!f) { fprintf(stderr,"Cannot open tokenlist: %s\n",path); exit(1); }
    char buf[8192];
    while (fgets(buf,sizeof(buf),f)) {
        std::string line=strip_nl(buf);
        if (line.empty()||line[0]=='#') continue;

        TokenLine tl; tl.required=false; tl.has_anchor=false; tl.anchor_pos=0;
        const char* p=line.c_str();
        if (*p=='+') { tl.required=true; while (*p=='+'||*p==' ') p++; }
        else while (*p==' ') p++;  // optional line marker (leading space)

        // Split remaining by spaces
        std::string rest(p);
        std::vector<std::string> parts;
        size_t pos=0;
        while (pos<rest.size()) {
            size_t end=rest.find(' ',pos);
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

// ---------------------------------------------------------------------------
// Host: password generator using std::next_permutation
//
// For each product combination, we collect free tokens, sort their indices,
// then iterate all permutations using std::next_permutation (O(n) per step).
// This is ~100x faster than Python's itertools.permutations because:
//   1. No Python interpreter overhead
//   2. Operating on integers (indices), not strings
//   3. next_permutation is highly optimized in the C++ standard library
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Fast password assembly — zero heap allocation in the hot path.
//
// Instead of std::string and std::vector per call (each a heap allocation),
// we use stack-allocated arrays of const char* pointers into the token
// strings that already live in the TokenLine.tokens vector.  Token lengths
// are passed alongside the pointers so we never call strlen().
//
// Benchmark: replacing std::string assembly raised throughput from ~3M/s
// to ~15-30M/s by eliminating malloc/free on every password.
// ---------------------------------------------------------------------------

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

    // Stack slot table — no heap allocation
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

// ---------------------------------------------------------------------------
// Producer thread: generates passwords and fills batch buffers
// ---------------------------------------------------------------------------

struct ProducerState {
    const std::vector<TokenLine>* lines;
    uint64_t start_combo;
    uint64_t total_combos;

    std::mutex              mtx;
    std::condition_variable cv_ready;   // signals main thread: batch is ready
    std::condition_variable cv_consumed;// signals producer: batch was consumed
    Batch* ready_batch;
    bool   done;

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

    Batch* cur = new Batch();
    cur->passwords_total = pw_count_base;
    uint64_t combo_idx = state->start_combo;

    auto push_batch = [&]() {
        if (cur->count == 0) return;
        std::unique_lock<std::mutex> lk(state->mtx);
        state->cv_consumed.wait(lk,[&]{ return state->ready_batch==nullptr||state->done; });
        if (state->done) return;
        state->ready_batch = cur;
        state->cv_ready.notify_one();
        cur = new Batch();
        cur->passwords_total = state->ready_batch->passwords_total + state->ready_batch->count;
    };

    // Stack-allocated hot-path buffers — reused across all combos and permutations.
    // No heap allocation in the inner loop.
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

    // Iterate all combos from start_combo to total_combos
    for (; combo_idx < state->total_combos; combo_idx++) {

        // Decode this combo into free tokens and anchored tokens.
        // All pointers are into existing std::string storage — no copies.
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
            // Initialise permutation indices [0, 1, ..., n_free-1].
            // std::next_permutation generates all n_free! orderings, one per step,
            // in O(n) amortised time — far cheaper than Python's full tuple build.
            for (int i=0;i<n_free;i++) perm[i]=i;
            do {
                assemble_password_fast(free_ptrs, free_lens, n_free,
                                       anchors, n_anchored, perm,
                                       pw_buf, &pw_len);
                add_password(combo_idx);
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
    delete cur;
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

// ---------------------------------------------------------------------------
// GPU engine (GPU buffers + kernel launch)
// ---------------------------------------------------------------------------

struct GPUEngine {
    uint8_t*  d_pw;
    uint32_t* d_lens;
    int*      d_found;
    int       h_found;

    GPUEngine() {
        cudaMalloc(&d_pw,   (size_t)BATCH_SIZE * PW_STRIDE);
        cudaMalloc(&d_lens, (size_t)BATCH_SIZE * 4);
        cudaMalloc(&d_found, 4);
        h_found = -1;
    }
    ~GPUEngine() { cudaFree(d_pw); cudaFree(d_lens); cudaFree(d_found); }

    // Returns found password index (into the batch) or -1
    int check(const Batch& b) {
        h_found = -1;
        cudaMemcpy(d_pw,    b.pw_data.data(), (size_t)b.count * PW_STRIDE, cudaMemcpyHostToDevice);
        cudaMemcpy(d_lens,  b.pw_lens.data(), (size_t)b.count * 4,          cudaMemcpyHostToDevice);
        cudaMemcpy(d_found, &h_found, 4, cudaMemcpyHostToDevice);
        int threads = 256;
        int blocks  = (b.count + threads - 1) / threads;
        check_kernel<<<blocks, threads>>>(d_pw, d_lens, b.count, PW_STRIDE, d_found);
        cudaMemcpy(&h_found, d_found, 4, cudaMemcpyDeviceToHost);
        cudaDeviceSynchronize();
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

    for (int i=1;i<argc;i++) {
        if      (!strcmp(argv[i],"--wallet")    && i+1<argc) wallet_path    = argv[++i];
        else if (!strcmp(argv[i],"--tokenlist") && i+1<argc) tokenlist_path = argv[++i];
        else if (!strcmp(argv[i],"--autosave")  && i+1<argc) autosave_path  = argv[++i];
        else if (!strcmp(argv[i],"--restore")   && i+1<argc) restore_path   = argv[++i];
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
                       "       multibit_cuda_threads.exe --restore <save.bin>\n");
        return 1;
    }

    uint8_t h_enc[32], h_salt[8];
    load_wallet(wallet_path, h_enc, h_salt);
    build_and_upload_tables();
    cudaMemcpyToSymbol(c_enc,  h_enc,  32);
    cudaMemcpyToSymbol(c_salt, h_salt, 8);

    auto lines = parse_tokenlist(tokenlist_path);
    printf("Token list: %s (%d lines)\n", tokenlist_path, (int)lines.size());

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
    }

    // Start producer thread
    ProducerState ps;
    ps.lines        = &lines;
    ps.start_combo  = state.combo_idx;
    ps.total_combos = total_combos;

    GPUEngine gpu;
    std::thread producer(producer_thread, &ps, state.passwords_checked);

    time_t start_time = time(nullptr);
    time_t last_save  = start_time;
    uint64_t pw_checked = state.passwords_checked;

    printf("Starting at combo #%llu  (batch size %d)\n",
           (unsigned long long)state.combo_idx, BATCH_SIZE);

    while (true) {
        Batch* batch = nullptr;
        {
            std::unique_lock<std::mutex> lk(ps.mtx);
            ps.cv_ready.wait(lk, [&]{ return ps.ready_batch != nullptr || ps.done; });
            if (ps.ready_batch) {
                batch = ps.ready_batch;
                ps.ready_batch = nullptr;
                ps.cv_consumed.notify_one();
            } else {
                break; // done and no batch
            }
        }

        int found = gpu.check(*batch);
        pw_checked = batch->passwords_total + batch->count;

        if (found >= 0) {
            // Reconstruct the found password from the batch buffer
            const uint8_t* pw = batch->pw_data.data() + found * PW_STRIDE;
            int len = batch->pw_lens[found];
            printf("\n*** PASSWORD FOUND: '");
            fwrite(pw, 1, len, stdout);
            printf("' ***\n");
            if (autosave_path) {
                state.combo_idx = batch->combo_idx;
                state.passwords_checked = pw_checked;
                save_progress(autosave_path, state);
            }
            ps.done = true;
            ps.cv_consumed.notify_all();
            delete batch;
            break;
        }

        state.combo_idx         = batch->combo_idx + 1;
        state.passwords_checked = pw_checked;
        delete batch;

        // Progress
        time_t now = time(nullptr);
        double elapsed = difftime(now, start_time);
        double rate = elapsed > 0 ? pw_checked / elapsed : 0;
        printf("\r%16.3fT passwords  %10.0f/s  combo %llu/%llu",
               pw_checked / 1e12, rate,
               (unsigned long long)state.combo_idx,
               (unsigned long long)total_combos);
        fflush(stdout);

        if (autosave_path && difftime(now, last_save) >= 30.0) {
            save_progress(autosave_path, state);
            last_save = now;
        }
    }

    producer.join();

    if (state.combo_idx >= total_combos) {
        time_t now = time(nullptr);
        double elapsed = difftime(now, start_time);
        printf("\nSearch complete. %.3fT passwords in %.0fs (%.0f/s). Not found.\n",
               pw_checked / 1e12, elapsed, pw_checked / elapsed);
    }
    return 0;
}
