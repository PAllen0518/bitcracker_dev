// Bounded comparison of the preserved reference and production CUDA kernels.
#define MULTIBIT_CUDA_TESTING 1
#define main multibit_application_main
#include "../multibit_cuda_threads.cu"
#undef main
#include <iostream>

static int candidate_length(const std::string& workload, int index) {
    if (workload == "short") return 16;
    if (workload == "medium") return 48;
    if (workload == "long") return 112;
    if (workload == "mixed") return index % 128 + 1;
    throw std::invalid_argument("unknown workload");
}

static void measure_kernel(const std::string& workload, int mode, int count) {
    uint8_t salt[8], encrypted[32];
    load_wallet("btcrecover/test/test-wallets/multibit-wallet.key", encrypted, salt);
    build_and_upload_tables();
    checked_cuda(cudaMemcpyToSymbol(c_salt, salt, 8), "upload salt");
    checked_cuda(cudaMemcpyToSymbol(c_enc, encrypted, 32), "upload ciphertext");
    int maximum = workload == "mixed" ? 128 : candidate_length(workload, 0);
    const int stride = mode == 0 ? 128
        : (maximum <= 32 ? 32 : (maximum <= 64 ? 64 : 128));
    HostBuffer<uint8_t> passwords(static_cast<size_t>(count) * stride, mode != 0);
    HostBuffer<uint32_t> lengths(count, mode != 0);
    uint32_t random = 20260920;
    for (int i = 0; i < count; ++i) {
        lengths[i] = candidate_length(workload, i);
        for (uint32_t j = 0; j < lengths[i]; ++j) {
            random = random * 1664525u + 1013904223u;
            passwords[static_cast<size_t>(i) * stride + j] = random >> 24;
        }
    }
    DeviceBuffer<uint8_t> device_passwords(static_cast<size_t>(count) * stride);
    DeviceBuffer<uint32_t> device_lengths(count);
    DeviceBuffer<int> device_result(1);
    CudaStream stream;
    CudaEvent begin, copied, computed, finish;
    HostBuffer<int> result(1);
    for (int trial = -2; trial < 5; ++trial) {
        const auto started = hrclock::now();
        begin.record(stream.get());
        checked_cuda(cudaMemcpyAsync(device_passwords.data(), passwords.data(),
            static_cast<size_t>(count) * stride, cudaMemcpyHostToDevice,
            stream.get()), "benchmark candidate copy");
        checked_cuda(cudaMemcpyAsync(device_lengths.data(), lengths.data(),
            count * sizeof(uint32_t), cudaMemcpyHostToDevice, stream.get()),
            "benchmark length copy");
        checked_cuda(cudaMemsetAsync(device_result.data(), 0xff, sizeof(int),
                                    stream.get()), "benchmark result reset");
        copied.record(stream.get());
        const int blocks = (count + 255) / 256;
        if (mode == 0) {
            check_kernel<<<blocks, 256, 0, stream.get()>>>(
                device_passwords.data(), device_lengths.data(), count,
                stride, device_result.data());
        } else if (mode == 2) {
            optimized_check_kernel<false, true, false>
                <<<blocks, 256, 0, stream.get()>>>(device_passwords.data(),
                    device_lengths.data(), count, stride, device_result.data());
        } else if (mode == 3) {
            optimized_check_kernel<true, false, false>
                <<<blocks, 256, 0, stream.get()>>>(device_passwords.data(),
                    device_lengths.data(), count, stride, device_result.data());
        } else {
            optimized_check_kernel<true, true, false>
                <<<blocks, 256, 0, stream.get()>>>(device_passwords.data(),
                    device_lengths.data(), count, stride, device_result.data());
        }
        checked_cuda(cudaGetLastError(), "benchmark kernel launch");
        computed.record(stream.get());
        checked_cuda(cudaMemcpyAsync(result.data(), device_result.data(),
            sizeof(int), cudaMemcpyDeviceToHost, stream.get()), "benchmark result");
        finish.record(stream.get());
        finish.wait();
        if (result[0] != -1) throw std::runtime_error("unexpected benchmark hit");
        if (trial >= 0) {
            printf("{\"kind\":\"gpu\",\"workload\":\"%s\",\"mode\":%d,"
                   "\"count\":%d,\"trial\":%d,\"copy_ms\":%.6f,"
                   "\"kernel_ms\":%.6f,\"wall_s\":%.9f,"
                   "\"transfer_bytes\":%llu}\n", workload.c_str(), mode,
                   count, trial, copied.since(begin), computed.since(copied),
                   secs_since(started),
                   static_cast<unsigned long long>(count) * (stride + 4));
        }
    }
}

static void measure_assembly(int mode, int count) {
    const char* tokens[] = {"a1", "b2", "c3", "d4", "e5", "f6", "g7", "h8", "i9"};
    int lengths[] = {2, 2, 2, 2, 2, 2, 2, 2, 2};
    int permutation[] = {0, 1, 2, 3, 4, 5, 6, 7, 8};
    AnchorSlot anchor[] = {{0, "HEAD", 4}, {-1, "TAIL", 4}};
    AssemblyPlan plan(lengths, 9, anchor, 2);
    const int stride = mode == 0 ? 128 : 32;
    std::vector<uint8_t> output(static_cast<size_t>(count) * stride);
    const auto started = hrclock::now();
    for (int i = 0; i < count; ++i) {
        uint8_t* destination = output.data() + static_cast<size_t>(i) * stride;
        if (mode == 0) {
            char buffer[128];
            int length = 0;
            assemble_password_fast(tokens, lengths, 9, anchor, 2, permutation,
                                   buffer, &length);
            memcpy(destination, buffer, length);
        } else plan.write(tokens, lengths, permutation, destination);
        std::next_permutation(permutation, permutation + 9);
    }
    const double seconds = secs_since(started);
    uint64_t checksum = 0;
    for (int i = 0; i < count; ++i) {
        for (int j = 0; j < 26; ++j) checksum = checksum * 31
            + output[static_cast<size_t>(i) * stride + j];
    }
    printf("{\"kind\":\"assembly\",\"mode\":%d,\"count\":%d,"
           "\"wall_s\":%.9f,\"checksum\":%llu}\n", mode, count, seconds,
           static_cast<unsigned long long>(checksum));
}

static void measure_typos(int mode, int repetitions) {
    TypoConfig config;
    config.max_typos = 2;
    config.capslock = config.swap = config.repeat = config.del = true;
    config.closecase = config.insert = true;
    config.insert_charset = "0123456789";
    uint64_t count = 0, checksum = 0;
    auto consume = [&](const std::string& value) {
        ++count;
        for (uint8_t byte : value) checksum = checksum * 31 + byte;
        checksum = checksum * 31 + 255;
        return true;
    };
    const auto started = hrclock::now();
    for (int i = 0; i < repetitions; ++i) {
        if (mode == 0) {
            for (const auto& candidate : generate_typo_variants("aBcDeFgHiJ", config)) {
                consume(candidate.pw);
            }
        } else stream_typo_variants(std::string("aBcDeFgHiJ"), config, consume);
    }
    printf("{\"kind\":\"typos\",\"mode\":%d,\"count\":%llu,"
           "\"wall_s\":%.9f,\"checksum\":%llu}\n", mode,
           static_cast<unsigned long long>(count), secs_since(started),
           static_cast<unsigned long long>(checksum));
}

int main(int argc, char** argv) {
    try {
        if (argc != 4) throw std::invalid_argument("workload mode count required");
        std::string workload(argv[1]);
        int mode = std::stoi(argv[2]), count = std::stoi(argv[3]);
        if (mode < 0 || mode > 3 || count < 1 || count > (1 << 20)) {
            throw std::invalid_argument("invalid benchmark bounds");
        }
        if (workload == "assembly") measure_assembly(mode, count);
        else if (workload == "typos") measure_typos(mode, std::min(count, 100));
        else measure_kernel(workload, mode, count);
        return 0;
    } catch (const std::exception& error) {
        fprintf(stderr, "Benchmark failed: %s\n", error.what());
        return 2;
    }
}
