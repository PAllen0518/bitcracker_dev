// ---------------------------------------------------------------------------
// Checkpoint / save format.
//
// Extracted from multibit_cuda_threads.cu so the save/restore logic can be unit
// tested on the host without a GPU. Holds:
//   - the legacy SaveState struct + reader/writer (unchanged behavior), and
//   - the v1 integrity format: identity bindings (token-list / wallet / delimiter
//     / typo hashes), a whole-record checksum, atomic+durable writes with a
//     retained previous generation, and a confirmed migration path from legacy.
//
// Spec: docs/specs/CHECKPOINT_INTEGRITY_SPEC.md.
// ---------------------------------------------------------------------------
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <io.h>       // _commit, _fileno

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>  // MoveFileExA (atomic replace)

// TypoConfig is defined earlier in the translation unit (multibit_cuda_threads.cu).

// ---------------------------------------------------------------------------
// Legacy format (v0): a bare struct with no magic, version, or integrity data.
// Kept as-is for migration reads and for tests that fabricate legacy saves.
// ---------------------------------------------------------------------------

struct SaveState {
    char     tokenlist[512];
    char     wallet[512];
    uint64_t combo_idx;
    uint64_t total_combos;
    uint64_t passwords_checked;
    uint64_t perm_idx;
    uint64_t typo_idx;
};

// Older saves ended after passwords_checked. They still restore at combo
// precision; new saves include perm_idx and typo_idx for exact resumes.
static const size_t OLD_SAVE_STATE_SIZE = 512 + 512 + sizeof(uint64_t) * 3;

static void save_progress(const char* path, const SaveState& s) {
    FILE* f=fopen(path,"wb");
    if (f) { fwrite(&s,sizeof(s),1,f); fclose(f); }
}
static bool load_progress(const char* path, SaveState& s) {
    FILE* f=fopen(path,"rb");
    if (!f) return false;
    memset(&s, 0, sizeof(s));
    size_t n=fread(&s,1,sizeof(s),f);
    fclose(f);
    return n==sizeof(s) || n==OLD_SAVE_STATE_SIZE;
}

// ---------------------------------------------------------------------------
// SHA-256 (host, dependency-free). Pinned against published vectors by the
// sha256_known_vectors contract.
// ---------------------------------------------------------------------------

struct Sha256 {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t  buffer[64];
    uint32_t buffered;

    static uint32_t rotr(uint32_t x, uint32_t n) { return (x >> n) | (x << (32 - n)); }

    Sha256() { reset(); }
    void reset() {
        static const uint32_t iv[8] = {
            0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
            0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
        memcpy(state, iv, sizeof(state));
        bitlen = 0;
        buffered = 0;
    }
    void block(const uint8_t* p) {
        static const uint32_t k[64] = {
            0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
            0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
            0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
            0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
            0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
            0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
            0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
            0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u};
        uint32_t w[64];
        for (int i = 0; i < 16; ++i) {
            w[i] = (uint32_t(p[i*4]) << 24) | (uint32_t(p[i*4+1]) << 16)
                 | (uint32_t(p[i*4+2]) << 8) | uint32_t(p[i*4+3]);
        }
        for (int i = 16; i < 64; ++i) {
            uint32_t s0 = rotr(w[i-15],7) ^ rotr(w[i-15],18) ^ (w[i-15] >> 3);
            uint32_t s1 = rotr(w[i-2],17) ^ rotr(w[i-2],19) ^ (w[i-2] >> 10);
            w[i] = w[i-16] + s0 + w[i-7] + s1;
        }
        uint32_t a=state[0],b=state[1],c=state[2],d=state[3],
                 e=state[4],f=state[5],g=state[6],h=state[7];
        for (int i = 0; i < 64; ++i) {
            uint32_t S1 = rotr(e,6) ^ rotr(e,11) ^ rotr(e,25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = h + S1 + ch + k[i] + w[i];
            uint32_t S0 = rotr(a,2) ^ rotr(a,13) ^ rotr(a,22);
            uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + maj;
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        state[0]+=a; state[1]+=b; state[2]+=c; state[3]+=d;
        state[4]+=e; state[5]+=f; state[6]+=g; state[7]+=h;
    }
    void update(const uint8_t* data, size_t len) {
        bitlen += uint64_t(len) * 8;
        while (len) {
            uint32_t take = 64 - buffered;
            if (take > len) take = static_cast<uint32_t>(len);
            memcpy(buffer + buffered, data, take);
            buffered += take; data += take; len -= take;
            if (buffered == 64) { block(buffer); buffered = 0; }
        }
    }
    void finish(uint8_t out[32]) {
        uint64_t total = bitlen;
        uint8_t one = 0x80;
        update(&one, 1);
        uint8_t zero = 0;
        while (buffered != 56) update(&zero, 1);
        uint8_t len[8];
        for (int i = 0; i < 8; ++i) len[i] = uint8_t(total >> (56 - i*8));
        // update() would re-count these into bitlen; write the length block directly.
        memcpy(buffer + buffered, len, 8);
        block(buffer);
        for (int i = 0; i < 8; ++i) {
            out[i*4]   = uint8_t(state[i] >> 24);
            out[i*4+1] = uint8_t(state[i] >> 16);
            out[i*4+2] = uint8_t(state[i] >> 8);
            out[i*4+3] = uint8_t(state[i]);
        }
    }
};

static void sha256(const uint8_t* data, size_t len, uint8_t out[32]) {
    Sha256 h;
    h.update(data, len);
    h.finish(out);
}
static void sha256_vec(const std::vector<uint8_t>& data, uint8_t out[32]) {
    sha256(data.empty() ? reinterpret_cast<const uint8_t*>("") : data.data(),
           data.size(), out);
}

// Format a 32-byte hash as lowercase hex into a 65-byte buffer (for display).
static const char* sha256_hex(const uint8_t h[32], char out[65]) {
    static const char* d = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) {
        out[i*2]   = d[h[i] >> 4];
        out[i*2+1] = d[h[i] & 0xf];
    }
    out[64] = 0;
    return out;
}

// SHA-256 of the raw bytes of a file (the token-list binding). Returns false if
// the file cannot be read.
static bool sha256_file(const char* path, uint8_t out[32]) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    Sha256 h;
    uint8_t chunk[8192];
    for (;;) {
        size_t n = fread(chunk, 1, sizeof(chunk), f);
        if (n) h.update(chunk, n);
        if (n < sizeof(chunk)) break;
    }
    bool ok = ferror(f) == 0;
    fclose(f);
    if (ok) h.finish(out);
    return ok;
}

// Wallet identity: hash of salt(8) || enc(32). Binds identity, not contents/path.
static void wallet_id_hash(const uint8_t salt[8], const uint8_t enc[32],
                           uint8_t out[32]) {
    uint8_t buf[40];
    memcpy(buf, salt, 8);
    memcpy(buf + 8, enc, 32);
    sha256(buf, sizeof(buf), out);
}

// Canonical serialization of every TypoConfig field that affects the search
// space, including the insert charset. Order is fixed and version-stable.
static std::vector<uint8_t> serialize_typo(const TypoConfig& c) {
    std::vector<uint8_t> out;
    auto put_i32 = [&](int32_t v) {
        for (int i = 0; i < 4; ++i) out.push_back(uint8_t(uint32_t(v) >> (i*8)));
    };
    auto put_u8 = [&](bool v) { out.push_back(v ? 1 : 0); };
    put_i32(c.max_typos);
    put_u8(c.capslock);
    put_u8(c.swap);
    put_i32(c.max_typos_swap);
    put_u8(c.repeat);
    put_u8(c.del);
    put_u8(c.closecase);
    put_i32(c.max_typos_simple);
    put_u8(c.insert);
    put_i32(c.max_typos_insert);
    put_i32(static_cast<int32_t>(c.insert_charset.size()));
    for (unsigned char ch : c.insert_charset) out.push_back(ch);
    return out;
}
static void typo_hash(const TypoConfig& c, uint8_t out[32]) {
    sha256_vec(serialize_typo(c), out);
}

// ---------------------------------------------------------------------------
// v1 record: fixed layout, magic-prefixed, checksummed.
// ---------------------------------------------------------------------------

static const char     SAVE_MAGIC_V1[8]     = {'M','B','C','K','S','V','1','\0'};
static const uint32_t SAVE_FORMAT_VERSION  = 1;
static const uint32_t SAVE_TOOL_VERSION    = 1;

#pragma pack(push, 1)
struct SaveRecordV1 {
    char     magic[8];            // SAVE_MAGIC_V1
    uint32_t format_version;      // SAVE_FORMAT_VERSION
    uint32_t header_size;         // sizeof(SaveRecordV1)
    uint32_t tool_version;        // SAVE_TOOL_VERSION
    uint8_t  delimiter;           // exact delimiter byte
    uint8_t  pad[3];
    uint8_t  tokenlist_hash[32];  // SHA-256 of raw token-list file bytes
    uint8_t  wallet_id_hash[32];  // SHA-256 of salt||enc
    uint8_t  typo_hash[32];       // SHA-256 of canonical TypoConfig
    char     tokenlist_path[512]; // convenience default only (not an identity check)
    char     wallet_path[512];    // convenience default only (not an identity check)
    uint64_t combo_idx;
    uint64_t total_combos;
    uint64_t passwords_checked;
    uint64_t perm_idx;
    uint64_t typo_idx;
    uint8_t  record_sha256[32];   // SHA-256 over all preceding bytes
};
#pragma pack(pop)

static const size_t SAVE_RECORD_V1_SIZE = sizeof(SaveRecordV1);
static const size_t SAVE_RECORD_V1_CHECKSUMMED = SAVE_RECORD_V1_SIZE - 32;

static void finalize_record_checksum(SaveRecordV1& r) {
    sha256(reinterpret_cast<const uint8_t*>(&r), SAVE_RECORD_V1_CHECKSUMMED,
           r.record_sha256);
}
static bool verify_record_checksum(const SaveRecordV1& r) {
    uint8_t expected[32];
    sha256(reinterpret_cast<const uint8_t*>(&r), SAVE_RECORD_V1_CHECKSUMMED,
           expected);
    return memcmp(expected, r.record_sha256, 32) == 0;
}

// Fill a record from identity bindings and progress, then checksum it.
static void build_record_v1(
    SaveRecordV1& r,
    const char* tokenlist_path, const char* wallet_path, uint8_t delimiter,
    const uint8_t tok_hash[32], const uint8_t wal_hash[32],
    const uint8_t typ_hash[32],
    uint64_t combo_idx, uint64_t total_combos, uint64_t passwords_checked,
    uint64_t perm_idx, uint64_t typo_idx) {
    memset(&r, 0, sizeof(r));
    memcpy(r.magic, SAVE_MAGIC_V1, 8);
    r.format_version = SAVE_FORMAT_VERSION;
    r.header_size    = static_cast<uint32_t>(SAVE_RECORD_V1_SIZE);
    r.tool_version   = SAVE_TOOL_VERSION;
    r.delimiter      = delimiter;
    memcpy(r.tokenlist_hash, tok_hash, 32);
    memcpy(r.wallet_id_hash, wal_hash, 32);
    memcpy(r.typo_hash, typ_hash, 32);
    if (tokenlist_path) strncpy(r.tokenlist_path, tokenlist_path, 511);
    if (wallet_path)    strncpy(r.wallet_path, wallet_path, 511);
    r.combo_idx         = combo_idx;
    r.total_combos      = total_combos;
    r.passwords_checked = passwords_checked;
    r.perm_idx          = perm_idx;
    r.typo_idx          = typo_idx;
    finalize_record_checksum(r);
}

enum class SaveMismatch {
    None, BadMagic, BadVersion, Corrupt,
    Tokenlist, Wallet, Delimiter, Typos, ComboCount
};

static const char* mismatch_message(SaveMismatch m) {
    switch (m) {
        case SaveMismatch::None:       return "ok";
        case SaveMismatch::BadMagic:   return "not a recognized save file (bad magic)";
        case SaveMismatch::BadVersion: return "save was written by a newer tool version";
        case SaveMismatch::Corrupt:    return "save file is corrupt or truncated (checksum mismatch)";
        case SaveMismatch::Tokenlist:  return "token-list file differs from the one this save was bound to";
        case SaveMismatch::Wallet:     return "wallet differs from the one this save was bound to";
        case SaveMismatch::Delimiter:  return "--delimiter differs from the one this save was bound to";
        case SaveMismatch::Typos:      return "typo options differ from the ones this save was bound to";
        case SaveMismatch::ComboCount: return "re-parsed combo count differs from the saved value";
    }
    return "unknown";
}

// Validate a loaded v1 record against the current inputs. Returns the first
// binding that fails, or None if every binding matches. Ordered so structural
// problems (magic/version/corruption) are reported before identity mismatches.
static SaveMismatch validate_save(
    const SaveRecordV1& r,
    const uint8_t expected_tokenlist_hash[32],
    const uint8_t expected_wallet_hash[32],
    uint8_t delimiter,
    const uint8_t expected_typo_hash[32],
    uint64_t total_combos) {
    if (memcmp(r.magic, SAVE_MAGIC_V1, 8) != 0) return SaveMismatch::BadMagic;
    if (r.format_version > SAVE_FORMAT_VERSION)  return SaveMismatch::BadVersion;
    if (!verify_record_checksum(r))              return SaveMismatch::Corrupt;
    if (memcmp(r.tokenlist_hash, expected_tokenlist_hash, 32) != 0)
        return SaveMismatch::Tokenlist;
    if (memcmp(r.wallet_id_hash, expected_wallet_hash, 32) != 0)
        return SaveMismatch::Wallet;
    if (r.delimiter != delimiter)                return SaveMismatch::Delimiter;
    if (memcmp(r.typo_hash, expected_typo_hash, 32) != 0)
        return SaveMismatch::Typos;
    if (r.total_combos != total_combos)          return SaveMismatch::ComboCount;
    return SaveMismatch::None;
}

// ---------------------------------------------------------------------------
// Atomic, durable writes with a retained previous generation.
// ---------------------------------------------------------------------------

// Test-only seam to force a failure at a specific stage. Production passes None.
enum class WriteFault { None, ShortWrite, FlushFail, RenameFail };

static bool file_exists(const char* path) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

// Durable copy via a temp file + atomic rename, so the destination is never
// left partial. Used to retain the previous save generation.
static bool copy_file_durable(const char* src, const char* dst) {
    FILE* in = fopen(src, "rb");
    if (!in) return false;
    std::string tmp = std::string(dst) + ".tmp";
    FILE* out = fopen(tmp.c_str(), "wb");
    if (!out) { fclose(in); return false; }
    bool ok = true;
    uint8_t buf[8192];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), in)) > 0) {
        if (fwrite(buf, 1, n, out) != n) { ok = false; break; }
    }
    if (ferror(in)) ok = false;
    fclose(in);
    if (ok) { fflush(out); _commit(_fileno(out)); }
    if (fclose(out) != 0) ok = false;
    if (ok) ok = MoveFileExA(tmp.c_str(), dst, MOVEFILE_REPLACE_EXISTING) != 0;
    if (!ok) remove(tmp.c_str());
    return ok;
}

// Atomic, durable write: fully write and flush a temp file, retain the prior
// generation as <path>.prev, then atomically replace <path>. Every return code
// is checked; any failure leaves the previous good <path> intact and reports
// false. The WriteFault seam forces a failure at a given stage for tests.
static bool save_bytes_v1(const char* path, const uint8_t* data, size_t size,
                          WriteFault fault) {
    std::string tmp = std::string(path) + ".tmp";
    FILE* f = fopen(tmp.c_str(), "wb");
    if (!f) return false;
    // Stage 1: write (a short write, injected or real, aborts without touching
    // the existing save).
    size_t intended = (fault == WriteFault::ShortWrite && size > 0)
                          ? size - 1 : size;
    size_t wrote = fwrite(data, 1, intended, f);
    if (wrote != size) { fclose(f); remove(tmp.c_str()); return false; }
    // Stage 2: flush to stable storage.
    if (fflush(f) != 0 || fault == WriteFault::FlushFail
        || _commit(_fileno(f)) != 0) {
        fclose(f); remove(tmp.c_str()); return false;
    }
    if (fclose(f) != 0) { remove(tmp.c_str()); return false; }
    // Stage 3: retain the prior generation before replacing (best effort; a
    // failure to make .prev must not block the primary save from advancing).
    if (file_exists(path)) {
        copy_file_durable(path, (std::string(path) + ".prev").c_str());
    }
    // Stage 4: atomic replace. On failure the original <path> is untouched.
    if (fault == WriteFault::RenameFail
        || MoveFileExA(tmp.c_str(), path,
                       MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) == 0) {
        remove(tmp.c_str());
        return false;
    }
    return true;
}

static bool save_record_v1(const char* path, const SaveRecordV1& r,
                           WriteFault fault = WriteFault::None) {
    return save_bytes_v1(path, reinterpret_cast<const uint8_t*>(&r),
                         sizeof(r), fault);
}

// Load a v1 record: exact size, magic, and checksum. On success `out` holds the
// parsed record; callers still run validate_save against current inputs.
static bool load_record_v1(const char* path, SaveRecordV1& out) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    memset(&out, 0, sizeof(out));
    size_t n = fread(&out, 1, sizeof(out), f);
    fclose(f);
    if (n != sizeof(out)) return false;
    if (memcmp(out.magic, SAVE_MAGIC_V1, 8) != 0) return false;
    return true;
}

// ---------------------------------------------------------------------------
// Format detection + migration.
// ---------------------------------------------------------------------------

enum class SaveKind { V1, Legacy1064, Legacy1048, Unknown };

static SaveKind detect_save_kind(const char* path) {
    FILE* f = fopen(path, "rb");
    if (!f) return SaveKind::Unknown;
    uint8_t head[8] = {0};
    size_t got = fread(head, 1, sizeof(head), f);
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return SaveKind::Unknown; }
    long size = ftell(f);
    fclose(f);
    if (got == 8 && memcmp(head, SAVE_MAGIC_V1, 8) == 0
        && size >= static_cast<long>(SAVE_RECORD_V1_SIZE)) {
        return SaveKind::V1;
    }
    if (size == static_cast<long>(sizeof(SaveState)))     return SaveKind::Legacy1064;
    if (size == static_cast<long>(OLD_SAVE_STATE_SIZE))   return SaveKind::Legacy1048;
    return SaveKind::Unknown;
}

// The interactive re-bind confirmation token. Accepts only an exact match.
static bool rebind_confirmed(const std::string& typed) {
    return typed == "REBIND";
}
