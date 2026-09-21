// Native acceptance harness. Includes the actual application implementation.
#define main multibit_application_main
#include CUDA_SOURCE
#undef main

#include <fstream>
#include <iostream>
#include <stdexcept>
#include <sstream>
#include <array>

#ifdef MULTIBIT_OPTIMIZED
#include "cuda_host_contracts.hpp"
#endif

static std::vector<uint8_t> unhex(const std::string& text) {
    if (text == "-") return {};
    if (text.size() % 2 != 0) throw std::invalid_argument("odd hex length");
    std::vector<uint8_t> bytes;
    for (size_t i = 0; i < text.size(); i += 2) {
        bytes.push_back(static_cast<uint8_t>(
            std::stoul(text.substr(i, 2), nullptr, 16)));
    }
    return bytes;
}

static void print_hex(const uint8_t* bytes, size_t size) {
    if (size == 0) std::cout << '-';
    for (size_t i = 0; i < size; ++i) printf("%02x", bytes[i]);
}

__global__ void reference_audit(
    const uint8_t* passwords, const uint32_t* lengths, int count,
    int* results, int* iv_calls, int* short_calls) {
    int id = blockIdx.x * blockDim.x + threadIdx.x;
    if (id >= count) return;
    int length = lengths[id];
    iv_calls[id] = length > 0 ? 1 : 0;
    short_calls[id] = 0;
    results[id] = length > 0 && check_multibit(
        passwords + id * 128, length);
}

static int crypto_file(const char* filename, int mode) {
    std::ifstream input(filename);
    if (!input) throw std::runtime_error("cannot read crypto fixture");
    std::string salt_hex, encrypted_hex;
    int count = 0;
    input >> salt_hex >> encrypted_hex >> count;
    auto salt = unhex(salt_hex);
    auto encrypted = unhex(encrypted_hex);
    if (salt.size() != 8 || encrypted.size() != 32 || count < 1) {
        throw std::invalid_argument("invalid crypto fixture");
    }
    std::vector<uint8_t> bytes(static_cast<size_t>(count) * 128, 0);
    std::vector<uint32_t> lengths(count);
    for (int i = 0; i < count; ++i) {
        std::string encoded;
        if (!(input >> encoded)) throw std::invalid_argument("missing candidate");
        auto candidate = unhex(encoded);
        if (candidate.size() > 128) {
            throw std::invalid_argument("candidate exceeds 128 bytes");
        }
        lengths[i] = static_cast<uint32_t>(candidate.size());
        std::copy(candidate.begin(), candidate.end(), bytes.begin() + i * 128);
    }
    build_and_upload_tables();
    CUDA_CHECK(cudaMemcpyToSymbol(c_salt, salt.data(), 8));
    CUDA_CHECK(cudaMemcpyToSymbol(c_enc, encrypted.data(), 32));
    uint8_t* device_bytes = nullptr;
    uint32_t* device_lengths = nullptr;
    int* device_results = nullptr;
    CUDA_CHECK(cudaMalloc(&device_bytes, bytes.size()));
    CUDA_CHECK(cudaMalloc(&device_lengths, count * sizeof(uint32_t)));
    CUDA_CHECK(cudaMalloc(&device_results, count * 3 * sizeof(int)));
    CUDA_CHECK(cudaMemcpy(device_bytes, bytes.data(), bytes.size(),
                          cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(device_lengths, lengths.data(),
                          count * sizeof(uint32_t), cudaMemcpyHostToDevice));
    int blocks = (count + 255) / 256;
    cudaFuncAttributes attributes{};
#ifdef MULTIBIT_CRYPTO_OPTIMIZED
    if (mode != 0) {
        launch_audit_kernel(device_bytes, device_lengths, count, 128,
                            device_results, mode);
        CUDA_CHECK(cudaFuncGetAttributes(
            &attributes, optimized_check_kernel<true, true, true>));
    } else
#endif
    {
        reference_audit<<<blocks, 256>>>(
            device_bytes, device_lengths, count, device_results,
            device_results + count, device_results + count * 2);
        CUDA_CHECK(cudaFuncGetAttributes(&attributes, reference_audit));
    }
    CUDA_CHECK(cudaGetLastError());
    CUDA_CHECK(cudaDeviceSynchronize());
    std::vector<int> results(count * 3);
    CUDA_CHECK(cudaMemcpy(results.data(), device_results,
                          results.size() * sizeof(int), cudaMemcpyDeviceToHost));
    std::cout << "{\"shared\":" << attributes.sharedSizeBytes << ",\"rows\":[";
    for (int i = 0; i < count; ++i) {
        if (i) std::cout << ',';
        std::cout << '[' << results[i] << ',' << results[i + count]
                  << ',' << results[i + 2 * count] << ']';
    }
    std::cout << "]}\n";
    CUDA_CHECK(cudaFree(device_results));
    CUDA_CHECK(cudaFree(device_lengths));
    CUDA_CHECK(cudaFree(device_bytes));
    return 0;
}

static TypoConfig typo_config(int budget, int flags) {
    TypoConfig config;
    config.max_typos = budget;
    config.capslock = (flags & 1) != 0;
    config.swap = (flags & 2) != 0;
    config.repeat = (flags & 4) != 0;
    config.del = (flags & 8) != 0;
    config.closecase = (flags & 16) != 0;
    config.insert = (flags & 32) != 0;
    config.insert_charset = "xy";
    return config;
}

static int typo_test(const char* encoded, int budget, int flags, int mode) {
    auto bytes = unhex(encoded);
    std::string base(bytes.begin(), bytes.end());
    auto config = typo_config(budget, flags);
    auto emit = [](const std::string& candidate) {
        print_hex(reinterpret_cast<const uint8_t*>(candidate.data()),
                  candidate.size());
        std::cout << '\n';
        return true;
    };
#ifdef MULTIBIT_OPTIMIZED
    if (mode) {
        stream_typo_variants(base, config, emit);
        return 0;
    }
#endif
    for (const auto& candidate : generate_typo_variants(base, config)) {
        emit(candidate.pw);
    }
    return 0;
}

static int compact_test() {
#ifdef MULTIBIT_OPTIMIZED
    Batch batch(8, false);
    for (int length : {32, 33, 64, 65, 128}) {
        batch.reset(0);
        batch.ensure_stride(length);
        std::cout << batch.stride << ' ';
    }
#else
    Batch batch;
    for (int length : {32, 33, 64, 65, 128}) std::cout << PW_STRIDE << ' ';
#endif
    std::cout << '\n';
    return 0;
}

static int optimized_contract(const std::string& name) {
#ifdef MULTIBIT_OPTIMIZED
    return run_host_contract(name);
#else
    throw std::runtime_error("optimized contract unavailable: " + name);
#endif
}

int main(int argc, char** argv) {
    try {
        if (argc >= 4 && std::string(argv[1]) == "crypto") {
            return crypto_file(argv[2], std::stoi(argv[3]));
        }
        if (argc == 6 && std::string(argv[1]) == "typos") {
            return typo_test(argv[2], std::stoi(argv[3]),
                             std::stoi(argv[4]), std::stoi(argv[5]));
        }
        if (argc == 2 && std::string(argv[1]) == "compact") {
            return compact_test();
        }
        if (argc == 3 && std::string(argv[1]) == "contract") {
            return optimized_contract(argv[2]);
        }
        throw std::invalid_argument("unknown harness command");
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 2;
    }
}
