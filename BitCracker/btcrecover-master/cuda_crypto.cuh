// CUDA verification optimized for independent MultiBit candidate passwords.
#pragma once

#define MULTIBIT_CRYPTO_OPTIMIZED 1

struct SharedAesTables {
    uint32_t td0[256], td1[256], td2[256], td3[256], sbox[256];
    uint8_t sbox_inv[256];
};

template <bool Shared>
struct AesReader {
    const SharedAesTables* shared;
    __device__ __forceinline__ uint32_t td0(uint32_t i) const {
        if constexpr (Shared) return shared->td0[i];
        else return c_TD0[i];
    }
    __device__ __forceinline__ uint32_t td1(uint32_t i) const {
        if constexpr (Shared) return shared->td1[i];
        else return c_TD1[i];
    }
    __device__ __forceinline__ uint32_t td2(uint32_t i) const {
        if constexpr (Shared) return shared->td2[i];
        else return c_TD2[i];
    }
    __device__ __forceinline__ uint32_t td3(uint32_t i) const {
        if constexpr (Shared) return shared->td3[i];
        else return c_TD3[i];
    }
    __device__ __forceinline__ uint32_t sbox(uint32_t i) const {
        if constexpr (Shared) return shared->sbox[i];
        else return c_SBOX[i];
    }
    __device__ __forceinline__ uint8_t sbox_inv(uint32_t i) const {
        if constexpr (Shared) return shared->sbox_inv[i];
        else return c_SBOX_INV[i];
    }
};

__device__ inline void load_shared_aes(SharedAesTables& tables) {
    for (int i = threadIdx.x; i < 256; i += blockDim.x) {
        tables.td0[i] = c_TD0[i];
        tables.td1[i] = c_TD1[i];
        tables.td2[i] = c_TD2[i];
        tables.td3[i] = c_TD3[i];
        tables.sbox[i] = c_SBOX[i];
        tables.sbox_inv[i] = c_SBOX_INV[i];
    }
    __syncthreads();
}

// All message and padding words have fixed indexes after loop unrolling.
// This path accepts at most 31 password bytes, plus salt and a 16-byte prefix.
HD inline void short_md5(const uint8_t* password, int length,
                         const uint8_t* salt, const uint8_t* prefix,
                         uint8_t* digest) {
    uint32_t words[16];
    const int prefix_length = prefix ? 16 : 0;
    const int message_length = prefix_length + length + 8;
    #pragma unroll
    for (int word = 0; word < 14; ++word) {
        uint32_t value = 0;
        #pragma unroll
        for (int byte = 0; byte < 4; ++byte) {
            const int index = word * 4 + byte;
            uint32_t item = 0;
            if (index < prefix_length) item = prefix[index];
            else if (index < prefix_length + length) {
                item = password[index - prefix_length];
            } else if (index < message_length) {
                item = salt[index - prefix_length - length];
            } else if (index == message_length) item = 0x80;
            value |= item << (byte * 8);
        }
        words[word] = value;
    }
    words[14] = static_cast<uint32_t>(message_length * 8);
    words[15] = 0;
    uint32_t state[4] = {0x67452301u, 0xefcdab89u, 0x98badcfeu,
                         0x10325476u};
    md5_compress(state, words);
    #pragma unroll
    for (int i = 0; i < 16; ++i) {
        digest[i] = static_cast<uint8_t>(state[i / 4] >> ((i % 4) * 8));
    }
}

template <bool Shared>
__device__ __forceinline__ void fast_aes256_key_expand(const uint8_t* key, uint32_t* rk, AesReader<Shared> tables) {
    const uint32_t RCON[7]={0x01000000u,0x02000000u,0x04000000u,0x08000000u,0x10000000u,0x20000000u,0x40000000u};
    for (int i=0;i<8;i++)
        rk[i]=((uint32_t)key[i*4]<<24)|((uint32_t)key[i*4+1]<<16)|((uint32_t)key[i*4+2]<<8)|key[i*4+3];
    for (int i=8;i<60;i++) {
        uint32_t t=rk[i-1];
        if (i%8==0) {
            t=(tables.sbox((t>>16)&0xff)<<24)|(tables.sbox((t>>8)&0xff)<<16)|(tables.sbox(t&0xff)<<8)|tables.sbox((t>>24)&0xff);
            t^=RCON[i/8-1];
        } else if (i%8==4) {
            t=(tables.sbox((t>>24)&0xff)<<24)|(tables.sbox((t>>16)&0xff)<<16)|(tables.sbox((t>>8)&0xff)<<8)|tables.sbox(t&0xff);
        }
        rk[i]=rk[i-8]^t;
    }
    for (int i=4;i<56;i++) {
        uint32_t w=rk[i];
        rk[i]=tables.td0(tables.sbox((w>>24)&0xff))^tables.td1(tables.sbox((w>>16)&0xff))
              ^tables.td2(tables.sbox((w>>8)&0xff)) ^tables.td3(tables.sbox(w&0xff));
    }
}

template <bool Shared>
__device__ __forceinline__ void fast_aes256_block_decrypt(const uint32_t* rk, const uint8_t* xb, const uint8_t* ct, uint8_t* pt, AesReader<Shared> tables) {
    uint32_t s0=((uint32_t)ct[0]<<24)|((uint32_t)ct[1]<<16)|((uint32_t)ct[2]<<8)|ct[3];
    uint32_t s1=((uint32_t)ct[4]<<24)|((uint32_t)ct[5]<<16)|((uint32_t)ct[6]<<8)|ct[7];
    uint32_t s2=((uint32_t)ct[8]<<24)|((uint32_t)ct[9]<<16)|((uint32_t)ct[10]<<8)|ct[11];
    uint32_t s3=((uint32_t)ct[12]<<24)|((uint32_t)ct[13]<<16)|((uint32_t)ct[14]<<8)|ct[15];
    s0^=rk[56];s1^=rk[57];s2^=rk[58];s3^=rk[59];
    uint32_t t0,t1,t2,t3;
    for (int r=13;r>=1;r--) {
        t0=tables.td0((s0>>24)&0xff)^tables.td1((s3>>16)&0xff)^tables.td2((s2>>8)&0xff)^tables.td3(s1&0xff)^rk[r*4+0];
        t1=tables.td0((s1>>24)&0xff)^tables.td1((s0>>16)&0xff)^tables.td2((s3>>8)&0xff)^tables.td3(s2&0xff)^rk[r*4+1];
        t2=tables.td0((s2>>24)&0xff)^tables.td1((s1>>16)&0xff)^tables.td2((s0>>8)&0xff)^tables.td3(s3&0xff)^rk[r*4+2];
        t3=tables.td0((s3>>24)&0xff)^tables.td1((s2>>16)&0xff)^tables.td2((s1>>8)&0xff)^tables.td3(s0&0xff)^rk[r*4+3];
        s0=t0;s1=t1;s2=t2;s3=t3;
    }
    t0=((uint32_t)tables.sbox_inv((s0>>24)&0xff)<<24)|((uint32_t)tables.sbox_inv((s3>>16)&0xff)<<16)|((uint32_t)tables.sbox_inv((s2>>8)&0xff)<<8)|tables.sbox_inv(s1&0xff);
    t1=((uint32_t)tables.sbox_inv((s1>>24)&0xff)<<24)|((uint32_t)tables.sbox_inv((s0>>16)&0xff)<<16)|((uint32_t)tables.sbox_inv((s3>>8)&0xff)<<8)|tables.sbox_inv(s2&0xff);
    t2=((uint32_t)tables.sbox_inv((s2>>24)&0xff)<<24)|((uint32_t)tables.sbox_inv((s1>>16)&0xff)<<16)|((uint32_t)tables.sbox_inv((s0>>8)&0xff)<<8)|tables.sbox_inv(s3&0xff);
    t3=((uint32_t)tables.sbox_inv((s3>>24)&0xff)<<24)|((uint32_t)tables.sbox_inv((s2>>16)&0xff)<<16)|((uint32_t)tables.sbox_inv((s1>>8)&0xff)<<8)|tables.sbox_inv(s0&0xff);
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



template <bool Shared, bool Short>
__device__ __forceinline__ bool optimized_multibit(
    const uint8_t* password, int length, AesReader<Shared> tables,
    int& iv_calls, int& short_calls) {
    uint8_t first[16], second[16], iv[16];
    uint8_t message[152];
    const bool short_password = Short && length <= 31;
    if (short_password) {
        short_md5(password, length, c_salt, nullptr, first);
        short_md5(password, length, c_salt, first, second);
        short_calls = 2;
    } else {
        for (int i = 0; i < length; ++i) message[16 + i] = password[i];
        for (int i = 0; i < 8; ++i) message[16 + length + i] = c_salt[i];
        md5(message + 16, length + 8, first);
        for (int i = 0; i < 16; ++i) message[i] = first[i];
        md5(message, length + 24, second);
    }
    uint8_t key[32];
    #pragma unroll
    for (int i = 0; i < 16; ++i) {
        key[i] = first[i];
        key[16 + i] = second[i];
    }
    uint32_t round_keys[60];
    fast_aes256_key_expand(key, round_keys, tables);
    uint8_t plaintext[16];
    fast_aes256_block_decrypt(round_keys, c_enc, c_enc + 16,
                               plaintext, tables);
    if (!all_b58(plaintext, 16)) return false;
    iv_calls = 1;
    if (short_password) {
        short_md5(password, length, c_salt, second, iv);
        short_calls++;
    } else {
        for (int i = 0; i < 16; ++i) message[i] = second[i];
        md5(message, length + 24, iv);
    }
    fast_aes256_block_decrypt(round_keys, iv, c_enc, plaintext, tables);
    const uint8_t first_byte = plaintext[0];
    if (first_byte != 'L' && first_byte != 'K' && first_byte != '5'
        && first_byte != 'Q') return false;
    return all_b58(plaintext + 1, 15);
}

template <bool Shared, bool Short, bool Audit>
__global__ void optimized_check_kernel(
    const uint8_t* __restrict__ passwords,
    const uint32_t* __restrict__ lengths, int count, uint32_t stride,
    int* results) {
    __shared__ SharedAesTables tables;
    if constexpr (Shared) load_shared_aes(tables);
    const int id = blockIdx.x * blockDim.x + threadIdx.x;
    if (id >= count) return;
    const int length = lengths[id];
    int iv_calls = 0, short_calls = 0;
    const bool accepted = length > 0 && length <= 128
        && optimized_multibit<Shared, Short>(
            passwords + static_cast<size_t>(id) * stride, length,
            AesReader<Shared>{&tables}, iv_calls, short_calls);
    if constexpr (Audit) {
        results[id] = accepted;
        results[count + id] = iv_calls;
        results[count * 2 + id] = short_calls;
    } else if (accepted) atomicCAS(results, -1, id);
}

#ifdef MULTIBIT_CUDA_TESTING
static void launch_audit_kernel(
    const uint8_t* data, const uint32_t* lengths, int count,
    uint32_t stride, int* results, int mode) {
    const int blocks = (count + 255) / 256;
    if (mode == 2) {
        optimized_check_kernel<false, true, true><<<blocks, 256>>>(
            data, lengths, count, stride, results);
    } else if (mode == 3) {
        optimized_check_kernel<true, false, true><<<blocks, 256>>>(
            data, lengths, count, stride, results);
    } else {
        optimized_check_kernel<true, true, true><<<blocks, 256>>>(
            data, lengths, count, stride, results);
    }
}
#endif
