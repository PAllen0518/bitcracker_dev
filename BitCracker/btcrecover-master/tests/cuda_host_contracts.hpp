// Host and pipeline acceptance tests, compiled only into the native harness.
#pragma once
#include <set>
#include <future>
#include <random>

static void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

struct ProducerGuard {
    ProducerState& state;
    std::thread thread;
    explicit ProducerGuard(ProducerState& value, uint64_t base = 0)
        : state(value), thread(producer_thread, &value, base) {}
    ~ProducerGuard() {
        state.request_stop();
        if (thread.joinable()) thread.join();
    }
};

static std::vector<TokenLine> small_lines() {
    return {{{"a", "b"}, true, false, 0},
            {{"X", "Y"}, false, false, 0},
            {{"head"}, true, true, 0},
            {{"tail"}, true, true, -1}};
}

static uint64_t combo_count(const std::vector<TokenLine>& lines) {
    uint64_t count = 1;
    for (const auto& line : lines) {
        count *= line.tokens.size() + (line.required ? 0 : 1);
    }
    return count;
}

struct Capture {
    std::vector<std::string> passwords;
    std::vector<SaveState> checkpoints;
    uint64_t chunk_allocations = 0;
    uint64_t block_copies = 0;
};

static Capture collect_candidates(
    const std::vector<TokenLine>& lines, const TypoConfig& config,
    const SaveState& start = {}, int capacity = 7, int producers = 1) {
    ProducerState state(capacity, false);
    state.lines = &lines;
    state.total_combos = combo_count(lines);
    state.start_combo = start.combo_idx;
    state.start_perm = start.perm_idx;
    state.start_typo = start.typo_idx;
    state.typo_cfg = &config;
    state.producers = producers;
    ProducerGuard guard(state, start.passwords_checked);
    Capture result;
    while (auto batch = state.take_ready(true)) {
        for (int i = 0; i < batch->count; ++i) {
            result.passwords.emplace_back(
                reinterpret_cast<const char*>(batch->pw_data.data())
                    + i * batch->stride, batch->pw_lens.data()[i]);
        }
        SaveState point{};
        point.combo_idx = batch->next_combo_idx;
        point.perm_idx = batch->next_perm_idx;
        point.typo_idx = batch->next_typo_idx;
        point.passwords_checked = batch->passwords_total + batch->count;
        result.checkpoints.push_back(point);
        state.recycle(std::move(batch));
    }
    guard.thread.join();
    state.rethrow_failure();
    result.chunk_allocations = state.chunk_allocations.load();
    result.block_copies = state.block_copies;
    require(state.allocated_batches() == 3, "pool must remain bounded");
    return result;
}

static std::vector<std::string> reference_candidates(
    const std::vector<TokenLine>& lines, const TypoConfig& config) {
    std::vector<std::string> output;
    for (uint64_t index = 0; index < combo_count(lines); ++index) {
        uint64_t remainder = index;
        const char* pointers[MAX_FREE];
        int lengths[MAX_FREE], permutation[MAX_FREE];
        AnchorSlot anchors[MAX_ANCHORED];
        int free_count = 0, anchor_count = 0;
        for (const auto& line : lines) {
            int radix = static_cast<int>(line.tokens.size())
                + (line.required ? 0 : 1);
            int choice = static_cast<int>(remainder % radix);
            remainder /= radix;
            if (!line.required && choice-- == 0) continue;
            const auto& token = line.tokens[choice];
            if (line.has_anchor) {
                anchors[anchor_count++] = {
                    line.anchor_pos, token.data(), static_cast<int>(token.size())};
            } else {
                pointers[free_count] = token.data();
                lengths[free_count] = static_cast<int>(token.size());
                permutation[free_count] = free_count;
                free_count++;
            }
        }
        if (free_count == 0) continue;
        do {
            char buffer[PW_MAX_LEN];
            int length = 0;
            assemble_password_fast(pointers, lengths, free_count, anchors,
                                   anchor_count, permutation, buffer, &length);
            if (config.any()) {
                for (const auto& variant : generate_typo_variants(
                         std::string(buffer, length), config)) {
                    if (!variant.pw.empty() && variant.pw.size() <= 128) {
                        output.push_back(variant.pw);
                    }
                }
            } else if (length > 0) output.emplace_back(buffer, length);
        } while (std::next_permutation(permutation, permutation + free_count));
    }
    return output;
}

static void assembly_contract() {
    TypoConfig config;
    auto lines = small_lines();
    require(collect_candidates(lines, config).passwords
            == reference_candidates(lines, config), "assembly sequence changed");
    for (int position : {0, 1, 2, -1}) {
        lines[2].anchor_pos = position;
        require(collect_candidates(lines, config).passwords
                == reference_candidates(lines, config), "anchor sequence changed");
    }
    lines = {{{std::string(32, 'a')}, true, false, 0},
             {{std::string(32, 'b')}, true, false, 0},
             {{std::string(32, 'c')}, true, false, 0},
             {{std::string(32, 'd')}, true, false, 0}};
    require(collect_candidates(lines, config).passwords
            == reference_candidates(lines, config), "long assembly changed");
}

static void resume_contract() {
    auto lines = small_lines();
    TypoConfig config;
    config.max_typos = 1;
    config.repeat = true;
    config.del = true;
    const auto full = collect_candidates(lines, config);
    require(full.passwords == reference_candidates(lines, config),
            "typo producer differs from reference");
    for (const auto& checkpoint : full.checkpoints) {
        auto resumed = collect_candidates(lines, config, checkpoint);
        std::vector<std::string> suffix(
            full.passwords.begin() + checkpoint.passwords_checked,
            full.passwords.end());
        require(resumed.passwords == suffix, "resume suffix differs");
    }
}

// Parallel generation must be byte-for-byte identical to the trusted single
// producer (which the assembly/resume contracts already tie to the reference
// oracle) across permutation-heavy, typo-heavy, optional-line, and anchored
// fixtures, and must resume from arbitrary checkpoints without loss or repeat.
static void parallel_contract() {
    struct Case { std::vector<TokenLine> lines; TypoConfig config; };
    std::vector<Case> cases;
    cases.push_back({small_lines(), TypoConfig{}});
    cases.push_back({{{{"a", "b", "c"}, true, false, 0},
                      {{"d", "e", "f"}, true, false, 0},
                      {{"g", "h", "i"}, true, false, 0},
                      {{"j", "k", "l"}, true, false, 0}},
                     TypoConfig{}});
    {
        TypoConfig heavy;
        heavy.max_typos = 2;
        heavy.capslock = heavy.swap = heavy.repeat = true;
        heavy.del = heavy.closecase = heavy.insert = true;
        heavy.insert_charset = "xy";
        cases.push_back({small_lines(), heavy});
    }
    for (auto& item : cases) {
        const auto serial = collect_candidates(item.lines, item.config, {}, 7, 1);
        require(serial.passwords == reference_candidates(item.lines, item.config),
                "serial producer differs from reference");
        for (int workers : {2, 3, 4, 8}) {
            auto parallel =
                collect_candidates(item.lines, item.config, {}, 7, workers);
            require(parallel.passwords == serial.passwords,
                    "parallel output differs from serial");
        }
    }
    auto lines = small_lines();
    TypoConfig config;
    config.max_typos = 1;
    config.repeat = true;
    config.del = true;
    const auto full = collect_candidates(lines, config, {}, 7, 1);
    for (const auto& checkpoint : full.checkpoints) {
        auto resumed = collect_candidates(lines, config, checkpoint, 7, 4);
        std::vector<std::string> suffix(
            full.passwords.begin() + checkpoint.passwords_checked,
            full.passwords.end());
        require(resumed.passwords == suffix, "parallel resume suffix differs");
    }
}

static void require_same_capture(const Capture& actual,
                                 const Capture& expected) {
    require(actual.passwords == expected.passwords,
            "candidate bytes/order differ");
    require(actual.checkpoints.size() == expected.checkpoints.size(),
            "batch boundaries differ");
    for (size_t i = 0; i < actual.checkpoints.size(); ++i) {
        const auto& a = actual.checkpoints[i];
        const auto& b = expected.checkpoints[i];
        require(a.combo_idx == b.combo_idx && a.perm_idx == b.perm_idx
                && a.typo_idx == b.typo_idx
                && a.passwords_checked == b.passwords_checked,
                "checkpoint differs at batch boundary");
    }
}

static void throughput_contract(bool reuse) {
    std::vector<TokenLine> lines(6, {{"aa", "bb", "cc"}, true, false, 0});
    const TypoConfig config;
    const auto serial = collect_candidates(lines, config, {}, 8193, 1);
    const auto parallel = collect_candidates(lines, config, {}, 8193, 4);
    require_same_capture(parallel, serial);
    if (reuse) {
        require(parallel.chunk_allocations <= 4 * 4 * 3,
                "chunk storage allocations grow with emitted chunks");
    } else {
        require(parallel.block_copies > 0,
                "parallel merge did not copy candidate blocks");
        require(parallel.block_copies < parallel.passwords.size() / 100,
                "merge still copies one candidate at a time");
    }
}

static void block_boundaries_contract() {
    const std::vector<std::vector<TokenLine>> small_cases{
        {}, {{{""}, true, false, 0}}, {{{"a"}, true, false, 0}},
        {{{"anchor"}, true, true, 0}}
    };
    for (const auto& lines : small_cases) {
        const auto serial = collect_candidates(lines, TypoConfig{}, {}, 1, 1);
        require_same_capture(
            collect_candidates(lines, TypoConfig{}, {}, 1, 4), serial);
    }
    std::vector<TokenLine> lines(6, {{"a", "B"}, true, false, 0});
    const TypoConfig config;
    for (int capacity : {1, 7, 8191, 8192, 8193, 16385}) {
        const auto serial = collect_candidates(lines, config, {}, capacity, 1);
        for (int workers : {2, 4, 8}) {
            require_same_capture(
                collect_candidates(lines, config, {}, capacity, workers),
                serial);
        }
    }
}

static Capture raw_block_capture(bool blocks, int capacity) {
    ProducerState state(capacity, false);
    ParallelMerge merge(state, 0, 1);
    auto emit = [](auto& sink) {
        const std::array<int, 10> lengths{
            {0, 31, 32, 33, 63, 64, 65, 127, 128, 129}};
        for (uint64_t i = 0; i < 22000 && !sink.stopped(); ++i) {
            const int length = lengths[i % lengths.size()];
            if (!sink.append(
                    length, {i + 1, i % 17, i % 5}, [&](uint8_t* out) {
                    memset(out, static_cast<int>(i % 251), length);
                })) return;
            if (i % 1024 == 0) sink.set_next({i + 10, 99, 7});
        }
        sink.set_next({99999, 0, 0});
    };
    auto producer = std::async(std::launch::async, [&] {
        if (blocks) {
            auto worker = std::async(std::launch::async, [&] {
                try {
                    ChunkSink sink(merge, 0);
                    emit(sink);
                    sink.finish();
                } catch (...) {
                    state.request_stop();
                    merge.abort();
                    throw;
                }
            });
            try { merge.run(); }
            catch (...) { state.request_stop(); merge.abort(); throw; }
            merge.abort();
            worker.get();
            merge.finish();
        } else {
            BatchWriter writer(state, 0);
            emit(writer);
            writer.flush();
        }
        state.finish();
    });
    Capture result;
    while (auto batch = state.take_ready(true)) {
        for (int i = 0; i < batch->count; ++i) {
            result.passwords.emplace_back(reinterpret_cast<const char*>(
                batch->pw_data.data()
                    + static_cast<size_t>(i) * batch->stride),
                batch->pw_lens[i]);
        }
        SaveState point{};
        point.combo_idx = batch->next_combo_idx;
        point.perm_idx = batch->next_perm_idx;
        point.typo_idx = batch->next_typo_idx;
        point.passwords_checked = batch->passwords_total + batch->count;
        result.checkpoints.push_back(point);
        state.recycle(std::move(batch));
    }
    producer.get();
    return result;
}

static void raw_blocks_contract() {
    for (int capacity : {1, 3, 7, 8192, 8193, 32768}) {
        require_same_capture(raw_block_capture(true, capacity),
                             raw_block_capture(false, capacity));
    }
}

static void pool_ownership_contract() {
    ProducerState state(8, false);
    ParallelMerge merge(state, 0, 1);
    for (uint32_t i = 0; i < 4; ++i) {
        auto chunk = merge.acquire(0);
        chunk->reset(0, i);
        chunk->append(1, {i + 1, 0, 0}, [&](uint8_t* out) {
            *out = static_cast<uint8_t>(i + 1);
        });
        chunk->set_last(i == 3);
        merge.publish(std::move(chunk));
    }
    std::promise<void> entered;
    auto blocked = std::async(std::launch::async, [&] {
        entered.set_value();
        auto chunk = merge.acquire(0);
        if (chunk) {
            chunk->reset(42, 0);
            chunk->append(1, {42, 0, 0}, [](uint8_t* out) { *out = 255; });
        }
        return chunk != nullptr;
    });
    entered.get_future().wait();
    const bool waited = blocked.wait_for(std::chrono::milliseconds(30))
        == std::future_status::timeout;
    if (!waited) {
        state.request_stop();
        merge.abort();
        blocked.get();
        require(false, "pool exceeded the per-worker ownership limit");
    }
    merge.run();
    require(blocked.get(), "pool did not return released storage");
    merge.finish();
    auto batch = state.take_ready(false);
    require(batch && batch->count == 4, "pool lost queued candidates");
    for (int i = 0; i < 4; ++i) {
        const auto value = batch->pw_data[
            static_cast<size_t>(i) * batch->stride];
        require(value == i + 1,
                "worker overwrote a chunk before merge completed");
    }
    require(state.chunk_allocations.load() == 12, "released chunk not reused");
}

static void pool_progress_contract() {
    ProducerState state(16, false);
    state.producers = 2;
    ParallelMerge merge(state, 0, 2);
    for (uint32_t i = 0; i < 4; ++i) {
        auto chunk = merge.acquire(1);
        chunk->reset(1, i);
        chunk->append(1, {i + 1, 0, 0}, [&](uint8_t* out) {
            *out = static_cast<uint8_t>(i + 1);
        });
        merge.publish(std::move(chunk));
    }
    std::promise<void> entered;
    auto later = std::async(std::launch::async, [&] {
        entered.set_value();
        auto chunk = merge.acquire(1);
        if (!chunk) return;
        chunk->reset(1, 4);
        chunk->append(1, {100, 0, 0}, [](uint8_t* out) { *out = 99; });
        chunk->set_last(true);
        merge.publish(std::move(chunk));
    });
    entered.get_future().wait();
    // Owner zero retains a reservation when later work exhausts owner one.
    auto first = merge.acquire(0);
    first->reset(0, 0);
    first->append(1, {1, 0, 0}, [](uint8_t* out) { *out = 42; });
    first->set_last(true);
    merge.publish(std::move(first));
    merge.run();
    later.get();
    merge.finish();
    auto batch = state.take_ready(false);
    require(batch && batch->count == 6, "pool progress lost work");
    const std::array<int, 6> expected{{42, 1, 2, 3, 4, 99}};
    for (size_t i = 0; i < expected.size(); ++i) {
        require(batch->pw_data[i * batch->stride] == expected[i],
                "out-of-order chunk escaped merge");
    }
}

static void seeded_blocks_contract() {
    uint32_t seed = 20260923;
    auto draw = [&] { seed = seed * 1664525u + 1013904223u; return seed; };
    for (int trial = 0; trial < 16; ++trial) {
        std::vector<TokenLine> lines;
        for (int i = 0; i < 4; ++i) {
            TokenLine line{{}, (draw() % 2) != 0, false, 0};
            const int alternatives = 1 + draw() % 3;
            for (int j = 0; j < alternatives; ++j) {
                const std::array<int, 6> lengths{{1, 15, 16, 17, 31, 32}};
                line.tokens.emplace_back(lengths[draw() % lengths.size()],
                                         static_cast<char>('a' + i + j));
            }
            lines.push_back(std::move(line));
        }
        lines.push_back({{"S"}, true, true, 0});
        lines.push_back({{"E"}, true, true, -1});
        TypoConfig config;
        config.max_typos = 1;
        config.del = trial % 2 == 0;
        config.repeat = trial % 3 == 0;
        const int capacity = trial % 2 == 0 ? 7 : 8193;
        const auto serial = collect_candidates(lines, config, {}, capacity, 1);
        for (int workers : {2, 4, 8}) {
            require_same_capture(
                collect_candidates(lines, config, {}, capacity, workers),
                serial);
        }
        if (!serial.checkpoints.empty()) {
            const auto& point =
                serial.checkpoints[serial.checkpoints.size() / 2];
            const auto resumed = collect_candidates(lines, config, point,
                                                    capacity, 4);
            const std::vector<std::string> suffix(
                serial.passwords.begin() + point.passwords_checked,
                serial.passwords.end());
            require(resumed.passwords == suffix, "seeded resume lost suffix");
        }
    }
}

static void pool_cancel_contract() {
    ProducerState state(8, false);
    ParallelMerge merge(state, 0, 1);
    std::vector<std::unique_ptr<GenChunk>> held;
    for (int i = 0; i < 4; ++i) held.push_back(merge.acquire(0));
    std::promise<void> entered;
    auto blocked = std::async(std::launch::async, [&] {
        entered.set_value();
        return merge.acquire(0) == nullptr;
    });
    auto coordinator = std::async(std::launch::async, [&] { merge.run(); });
    entered.get_future().wait();
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    state.request_stop();
    merge.abort();
    require(blocked.get(), "stopped pool handed out a chunk");
    coordinator.get();
}

static void mixed_contract() {
    Batch batch(12, false);
    const std::vector<int> lengths = {31, 32, 33, 64, 65, 128, 1, 32};
    for (int length : lengths) {
        batch.ensure_stride(length);
        memset(batch.pw_data.data() + batch.count * batch.stride,
               length, length);
        batch.pw_lens.data()[batch.count++] = length;
    }
    require(batch.stride == 128, "incorrect final stride");
    for (size_t i = 0; i < lengths.size(); ++i) {
        for (int j = 0; j < lengths[i]; ++j) {
            require(batch.pw_data.data()[i * batch.stride + j] == lengths[i],
                    "stride expansion damaged candidate bytes");
        }
    }
}

static void pool_contract() {
    std::vector<TokenLine> lines = {
        {{"a", "b", "c"}, true, false, 0},
        {{"d", "e", "f"}, true, false, 0},
        {{"g", "h", "i"}, true, false, 0},
        {{"j", "k", "l"}, true, false, 0}};
    ProducerState state(7, false);
    state.lines = &lines;
    state.total_combos = combo_count(lines);
    TypoConfig config;
    state.typo_cfg = &config;
    ProducerGuard guard(state);
    std::set<const void*> allocations;
    size_t seen = 0;
    while (auto batch = state.take_ready(true)) {
        allocations.insert(batch->pw_data.data());
        seen += batch->count;
        require(allocations.size() <= 3, "buffer allocated during generation");
        state.recycle(std::move(batch));
    }
    require(seen == 81 * 24, "producer lost candidates");
    require(state.allocated_batches() == 3, "unexpected pool size");
    state.rethrow_failure();
}

static void bounded_contract() {
    TypoConfig config;
    config.max_typos = 3;
    config.insert = true;
    config.insert_charset = "xy";
    size_t previous_capacity = 0;
    for (size_t limit : {20000u, 40000u}) {
        size_t count = 0;
        auto emit = [&](const std::string& value) {
            require(value.size() <= 35, "insert budget exceeded");
            return ++count < limit;
        };
        auto stats = stream_typo_variants(std::string(32, 'a'), config, emit);
        require(count == limit && !stats.completed, "stream cancellation failed");
        require(stats.scratch_bytes < 8192, "unbounded scratch storage");
        if (previous_capacity) require(previous_capacity == stats.scratch_bytes,
                                       "scratch grows with total variants");
        previous_capacity = stats.scratch_bytes;
    }
}

static void cancel_contract() {
    std::vector<TokenLine> lines(8, {{"a", "b"}, true, false, 0});
    ProducerState state(2, false);
    state.lines = &lines;
    state.total_combos = combo_count(lines);
    ProducerGuard guard(state);
    auto held = state.take_ready(true);
    require(held != nullptr, "producer produced no batch");
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
    state.request_stop();
}

static void parallel_cancel_contract() {
    std::vector<TokenLine> lines(8, {{"a", "b"}, true, false, 0});
    ProducerState state(2, false);
    state.lines = &lines;
    state.total_combos = combo_count(lines);
    state.producers = 4;
    ProducerGuard guard(state);
    auto held = state.take_ready(true);
    require(held != nullptr, "parallel producer produced no batch");
    // Hold the consumer so later units fill the merge queue and block.
    std::this_thread::sleep_for(std::chrono::seconds(8));
    state.request_stop();
}

static void legacy_contract() {
    SaveState original{};
    original.combo_idx = 123;
    original.total_combos = 1000;
    original.passwords_checked = 456;
    original.perm_idx = 7;
    original.typo_idx = 8;
    const char* path = ".cuda-build/test-checkpoint.bin";
    save_progress(path, original);
    SaveState restored{};
    require(load_progress(path, restored), "new checkpoint did not load");
    require(restored.perm_idx == 7 && restored.typo_idx == 8,
            "new checkpoint offsets lost");
    {
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file.write(reinterpret_cast<const char*>(&original), OLD_SAVE_STATE_SIZE);
    }
    require(load_progress(path, restored), "legacy checkpoint did not load");
    require(restored.combo_idx == 123 && restored.passwords_checked == 456
            && restored.perm_idx == 0 && restored.typo_idx == 0,
            "legacy checkpoint defaults changed");
}

static void md5_contract() {
    const uint8_t salt[8] = {0, 1, 2, 3, 4, 5, 6, 7};
    for (int length : {1, 15, 16, 30, 31}) {
        uint8_t password[31], message[55], first[16], expected[16], actual[16];
        for (int i = 0; i < length; ++i) password[i] = i * 17;
        memcpy(message, password, length);
        memcpy(message + length, salt, 8);
        md5(message, length + 8, first);
        short_md5(password, length, salt, nullptr, actual);
        require(memcmp(first, actual, 16) == 0, "short first MD5 differs");
        memcpy(message, first, 16);
        memcpy(message + 16, password, length);
        memcpy(message + 16 + length, salt, 8);
        md5(message, length + 24, expected);
        short_md5(password, length, salt, first, actual);
        require(memcmp(expected, actual, 16) == 0, "short prefixed MD5 differs");
    }
}

static void pipeline_contract(bool checkpoint_only) {
    uint8_t encrypted[32], salt[8];
    load_wallet("btcrecover/test/test-wallets/multibit-wallet.key", encrypted, salt);
    build_and_upload_tables();
    CUDA_CHECK(cudaMemcpyToSymbol(c_salt, salt, 8));
    CUDA_CHECK(cudaMemcpyToSymbol(c_enc, encrypted, 32));
    for (bool asynchronous : {false, true}) {
        GPUEngine engine(8, asynchronous);
        uint64_t committed = 0;
        for (int wave = 0; wave < 5; ++wave) {
            const int submissions = asynchronous ? 2 : 1;
            std::vector<const void*> active_buffers;
            for (int i = 0; i < submissions; ++i) {
                auto batch = std::make_unique<Batch>(8, true);
                batch->reset(committed + i * 3);
                batch->ensure_stride(18);
                for (int j = 0; j < 3; ++j) {
                    const char* value = j == 2 ? "btcr-test-password" : "wrong";
                    size_t length = strlen(value);
                    memcpy(batch->pw_data.data() + j * batch->stride, value, length);
                    batch->pw_lens.data()[j] = static_cast<uint32_t>(length);
                }
                batch->count = 3;
                batch->next_combo_idx = batch->passwords_total + 3;
                auto* owned = batch.get();
                engine.delay_next_for_test(5);
                engine.submit(std::move(batch));
                bool protected_buffer = false;
                try { owned->reset(0); }
                catch (const std::logic_error& error) {
                    static_cast<void>(error);
                    protected_buffer = true;
                }
                require(protected_buffer, "inflight host buffer was writable");
            }
            require(committed == static_cast<uint64_t>(wave * submissions * 3),
                    "checkpoint advanced on submission");
            while (engine.pending()) {
                auto completed = engine.complete();
                require(completed->passwords_total == committed,
                        "pipeline returned batches out of order");
                require(completed->found_index == 2, "pipeline lost valid password");
                committed += completed->count;
                completed->reset(committed);
            }
        }
        require(engine.device_allocations() == (asynchronous ? 2 : 1),
                "device buffers were not reused");
    }
}

// ---------------------------------------------------------------------------
// Checkpoint / save-integrity contracts (save_format.hpp). Host-only, no GPU.
// ---------------------------------------------------------------------------

static std::string save_hex(const uint8_t* p, size_t n) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    for (size_t i = 0; i < n; ++i) {
        out.push_back(digits[p[i] >> 4]);
        out.push_back(digits[p[i] & 0xf]);
    }
    return out;
}
static void label_hash(const char* s, uint8_t out[32]) {
    sha256(reinterpret_cast<const uint8_t*>(s), strlen(s), out);
}

static void save_sha256_contract() {
    uint8_t d[32];
    sha256(reinterpret_cast<const uint8_t*>(""), 0, d);
    require(save_hex(d, 32) ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "sha256 empty-string vector differs");
    sha256(reinterpret_cast<const uint8_t*>("abc"), 3, d);
    require(save_hex(d, 32) ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            "sha256 \"abc\" vector differs");
    const char* m =
        "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    sha256(reinterpret_cast<const uint8_t*>(m), strlen(m), d);
    require(save_hex(d, 32) ==
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
            "sha256 448-bit vector differs");
}

static void save_roundtrip_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "tokens.txt", "wallet.key", '|', th, wh, yh,
                    111, 222, 333, 4, 5);
    const char* path = ".cuda-build/save-roundtrip.bin";
    require(save_record_v1(path, r), "v1 save failed");
    SaveRecordV1 got;
    require(load_record_v1(path, got), "v1 load failed");
    require(memcmp(&r, &got, sizeof(r)) == 0, "v1 roundtrip differs");
    require(got.combo_idx == 111 && got.perm_idx == 4 && got.typo_idx == 5,
            "v1 progress fields lost");
    require(verify_record_checksum(got), "v1 checksum invalid");
}

static void save_identity_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 10, 0, 0, 0);
    require(memcmp(r.magic, SAVE_MAGIC_V1, 8) == 0, "magic not written");
    require(r.format_version == SAVE_FORMAT_VERSION, "format version not written");
    require(r.tool_version == SAVE_TOOL_VERSION, "tool version not written");
    require(r.delimiter == ' ', "delimiter not bound");
    require(memcmp(r.tokenlist_hash, th, 32) == 0, "token-list hash not bound");
    require(memcmp(r.wallet_id_hash, wh, 32) == 0, "wallet hash not bound");
    require(memcmp(r.typo_hash, yh, 32) == 0, "typo hash not bound");
}

static void save_reject_tokenlist_contract() {
    uint8_t tA[32], tB[32], wh[32], yh[32];
    label_hash("tokA", tA); label_hash("tokB", tB);
    label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', tA, wh, yh, 0, 100, 0, 0, 0);
    require(validate_save(r, tB, wh, ' ', yh, 100) == SaveMismatch::Tokenlist,
            "different token list with equal combo count was not rejected");
    require(validate_save(r, tA, wh, ' ', yh, 100) == SaveMismatch::None,
            "matching token list was rejected");
}

static void save_reject_wallet_contract() {
    uint8_t th[32], wA[32], wB[32], yh[32];
    label_hash("tok", th); label_hash("walA", wA); label_hash("walB", wB);
    label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wA, yh, 0, 100, 0, 0, 0);
    require(validate_save(r, th, wB, ' ', yh, 100) == SaveMismatch::Wallet,
            "different wallet was not rejected");
    require(validate_save(r, th, wA, ' ', yh, 100) == SaveMismatch::None,
            "matching wallet was rejected");
}

static void save_reject_delimiter_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    require(validate_save(r, th, wh, '|', yh, 100) == SaveMismatch::Delimiter,
            "different delimiter was not rejected");
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::None,
            "matching delimiter was rejected");
}

static void save_reject_typos_contract() {
    uint8_t th[32], wh[32], yA[32], yB[32];
    label_hash("tok", th); label_hash("wal", wh);
    TypoConfig a; a.max_typos = 2; a.swap = true; a.insert_charset = "xy";
    TypoConfig b = a; b.insert_charset = "xz";  // charset affects the space
    typo_hash(a, yA); typo_hash(b, yB);
    require(memcmp(yA, yB, 32) != 0, "typo serialization ignores insert charset");
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yA, 0, 100, 0, 0, 0);
    require(validate_save(r, th, wh, ' ', yB, 100) == SaveMismatch::Typos,
            "different typo options were not rejected");
    require(validate_save(r, th, wh, ' ', yA, 100) == SaveMismatch::None,
            "matching typo options were rejected");
}

static void save_detect_corruption_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 7, 100, 9, 1, 2);
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::None,
            "valid record rejected");
    r.combo_idx ^= 0x1;  // flip a bit without recomputing the checksum
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::Corrupt,
            "corrupted record was not detected");
}

static void save_atomic_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 a, b;
    build_record_v1(a, "t", "w", ' ', th, wh, yh, 1, 100, 1, 0, 0);
    build_record_v1(b, "t", "w", ' ', th, wh, yh, 2, 100, 2, 0, 0);
    const char* path = ".cuda-build/save-atomic.bin";
    std::string prev = std::string(path) + ".prev";
    remove(path); remove(prev.c_str());
    require(save_record_v1(path, a), "initial save failed");
    for (WriteFault fault : {WriteFault::ShortWrite, WriteFault::FlushFail,
                             WriteFault::RenameFail}) {
        require(!save_record_v1(path, b, fault),
                "a faulted save falsely reported success");
        SaveRecordV1 got;
        require(load_record_v1(path, got),
                "primary save missing after a faulted write");
        require(got.combo_idx == 1,
                "a faulted write corrupted the primary save");
    }
    require(save_record_v1(path, b), "replacement save failed");
    SaveRecordV1 got;
    require(load_record_v1(path, got) && got.combo_idx == 2,
            "atomic replace did not take effect");
}

static void save_prev_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 a, b;
    build_record_v1(a, "t", "w", ' ', th, wh, yh, 10, 100, 10, 0, 0);
    build_record_v1(b, "t", "w", ' ', th, wh, yh, 20, 100, 20, 0, 0);
    const char* path = ".cuda-build/save-prev.bin";
    std::string prev = std::string(path) + ".prev";
    remove(path); remove(prev.c_str());
    require(save_record_v1(path, a), "save A failed");
    require(save_record_v1(path, b), "save B failed");
    SaveRecordV1 cur, old;
    require(load_record_v1(path, cur) && cur.combo_idx == 20,
            "current save is not the latest generation");
    require(load_record_v1(prev.c_str(), old) && old.combo_idx == 10,
            "previous generation was not retained");
}

static std::string save_slurp(const char* path);

// F1: with three generations present, a failed primary publish must leave both
// the current and previous generations byte-for-byte unchanged.
static void save_three_generation_rename_failure_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 gen1, gen2, gen3;
    build_record_v1(gen1, "t", "w", ' ', th, wh, yh, 1, 100, 1, 0, 0);
    build_record_v1(gen2, "t", "w", ' ', th, wh, yh, 2, 100, 2, 0, 0);
    build_record_v1(gen3, "t", "w", ' ', th, wh, yh, 3, 100, 3, 0, 0);
    const char* path = ".cuda-build/save-three-generation.bin";
    const std::string prev = std::string(path) + ".prev";
    remove(path);
    remove(prev.c_str());
    require(save_record_v1(path, gen1), "generation 1 save failed");
    require(save_record_v1(path, gen2), "generation 2 save failed");
    const std::string primary_before = save_slurp(path);
    const std::string prev_before = save_slurp(prev.c_str());
    require(!save_record_v1(path, gen3, WriteFault::RenameFail),
            "injected primary rename failure reported success");
    require(save_slurp(path) == primary_before,
            "primary changed after its rename failed");
    require(save_slurp(prev.c_str()) == prev_before,
            ".prev changed before the primary rename committed");
}

static void save_file_state_error_contract() {
    const char* path = ".cuda-build/save-file-state.bin";
    const std::string prev = std::string(path) + ".prev";
    remove(path);
    remove(prev.c_str());
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 gen1, gen2;
    build_record_v1(gen1, "t", "w", ' ', th, wh, yh, 1, 100, 1, 0, 0);
    build_record_v1(gen2, "t", "w", ' ', th, wh, yh, 2, 100, 2, 0, 0);
    require(file_state(path) == FileState::Absent,
            "missing path was not classified absent");
    require(file_state(path, nullptr, true) == FileState::Error,
            "attribute failure was classified absent");
    require(save_record_v1(path, gen1), "initial state save failed");
    const std::string before = save_slurp(path);
    require(!save_record_v1(path, gen2, WriteFault::AttributeFail),
            "save advanced after an existence-query error");
    require(save_slurp(path) == before,
            "primary changed after an existence-query error");
    require(file_state(prev.c_str()) == FileState::Absent,
            ".prev was created after an existence-query error");
}

static void save_commit_failure_contract() {
    const char* src = ".cuda-build/save-commit-src.bin";
    const char* dst = ".cuda-build/save-commit-dst.bin";
    { std::ofstream f(src, std::ios::binary | std::ios::trunc); f << "new"; }
    { std::ofstream f(dst, std::ios::binary | std::ios::trunc); f << "old"; }
    require(!copy_file_durable(src, dst, WriteFault::CommitFail),
            "backup reported success after injected _commit failure");
    require(save_slurp(dst) == "old",
            "backup destination changed after _commit failure");

    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 gen1, gen2;
    build_record_v1(gen1, "t", "w", ' ', th, wh, yh, 1, 100, 1, 0, 0);
    build_record_v1(gen2, "t", "w", ' ', th, wh, yh, 2, 100, 2, 0, 0);
    const char* path = ".cuda-build/save-commit.bin";
    const std::string prev = std::string(path) + ".prev";
    remove(path);
    remove(prev.c_str());
    require(save_record_v1(path, gen1), "initial commit save failed");
    const std::string before = save_slurp(path);
    require(!save_record_v1(path, gen2, WriteFault::CommitFail),
            "save reported success after injected _commit failure");
    require(save_slurp(path) == before,
            "primary changed after injected _commit failure");
    require(file_state(prev.c_str()) == FileState::Absent,
            ".prev changed after injected _commit failure");
}

static void save_fuzz_contract() {
    std::mt19937_64 generator(20260926);
    const char* path = ".cuda-build/save-fuzz-roundtrip.bin";
    remove(path);
    remove((std::string(path) + ".prev").c_str());
    for (int example = 0; example < 32; ++example) {
        uint8_t th[32], wh[32], yh[32];
        for (size_t index = 0; index < 32; ++index) {
            th[index] = static_cast<uint8_t>(generator());
            wh[index] = static_cast<uint8_t>(generator());
            yh[index] = static_cast<uint8_t>(generator());
        }
        SaveRecordV1 original;
        build_record_v1(
            original, "synthetic-token-list", "synthetic-wallet",
            static_cast<uint8_t>(generator()), th, wh, yh,
            generator(), generator(), generator(), generator(), generator());
        require(save_record_v1(path, original),
                "random valid record could not be saved");
        SaveRecordV1 loaded;
        require(load_record_v1(path, loaded),
                "random valid record could not be loaded");
        require(memcmp(&loaded, &original, sizeof(original)) == 0,
                "random valid record changed during round-trip");
        for (size_t offset = 0; offset < sizeof(original); ++offset) {
            SaveRecordV1 mutated = original;
            auto* bytes = reinterpret_cast<uint8_t*>(&mutated);
            bytes[offset] ^= 0x01;
            require(!verify_record_checksum(mutated),
                    "single-byte mutation passed checksum verification");
        }
    }
}

static void save_migrate_1064_contract() {
    SaveState s{};
    s.combo_idx = 123; s.total_combos = 1000; s.passwords_checked = 456;
    s.perm_idx = 7; s.typo_idx = 8;
    strncpy(s.tokenlist, "t", 511); strncpy(s.wallet, "w", 511);
    const char* path = ".cuda-build/save-legacy-1064.bin";
    save_progress(path, s);
    require(detect_save_kind(path) == SaveKind::Legacy1064,
            "1064-byte save not detected as legacy");
    SaveState r{};
    require(load_progress(path, r), "legacy 1064 load failed");
    require(r.combo_idx == 123 && r.perm_idx == 7 && r.typo_idx == 8
            && r.passwords_checked == 456,
            "legacy 1064 indices not preserved");
}

static void save_migrate_1048_contract() {
    SaveState s{};
    s.combo_idx = 321; s.total_combos = 2000; s.passwords_checked = 654;
    s.perm_idx = 9; s.typo_idx = 9;
    const char* path = ".cuda-build/save-legacy-1048.bin";
    {
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file.write(reinterpret_cast<const char*>(&s), OLD_SAVE_STATE_SIZE);
    }
    require(detect_save_kind(path) == SaveKind::Legacy1048,
            "1048-byte save not detected as legacy");
    SaveState r{};
    require(load_progress(path, r), "legacy 1048 load failed");
    require(r.combo_idx == 321 && r.passwords_checked == 654
            && r.perm_idx == 0 && r.typo_idx == 0,
            "legacy 1048 defaults changed");
}

static void save_rebind_confirm_contract() {
    require(rebind_confirmed("REBIND"), "exact confirmation token not accepted");
    require(!rebind_confirmed(""), "empty input accepted as confirmation");
    require(!rebind_confirmed("rebind"), "lowercase accepted as confirmation");
    require(!rebind_confirmed("YES"), "unrelated token accepted as confirmation");
    require(!rebind_confirmed("REBIND "), "padded token accepted as confirmation");
}

static void save_reject_badmagic_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    r.magic[0] = 'X';  // magic is checked before anything else
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::BadMagic,
            "record with wrong magic was not rejected");
}

static void save_reject_badversion_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    r.format_version = SAVE_FORMAT_VERSION + 1;  // a newer format we cannot read
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::BadVersion,
            "record from a newer tool version was not rejected");
}

static void save_reject_combocount_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    require(validate_save(r, th, wh, ' ', yh, 200) == SaveMismatch::ComboCount,
            "identity match with a different combo count was not rejected");
}

static void save_detect_unknown_contract() {
    const char* path = ".cuda-build/save-unknown.bin";
    {
        std::ofstream f(path, std::ios::binary | std::ios::trunc);
        f << "garbage";  // wrong size, no magic
    }
    require(detect_save_kind(path) == SaveKind::Unknown,
            "short garbage file not classified as unknown");
    { std::ofstream f(path, std::ios::binary | std::ios::trunc); }  // zero length
    require(detect_save_kind(path) == SaveKind::Unknown,
            "zero-length file not classified as unknown");
    require(detect_save_kind(".cuda-build/save-missing.bin") == SaveKind::Unknown,
            "missing file not classified as unknown");
}

static void save_rebind_roundtrip_contract() {
    SaveState legacy{};
    legacy.combo_idx = 999; legacy.total_combos = 5000;
    legacy.passwords_checked = 7777; legacy.perm_idx = 3; legacy.typo_idx = 4;
    const char* lpath = ".cuda-build/save-rebind-legacy.bin";
    save_progress(lpath, legacy);
    SaveState read{};
    require(load_progress(lpath, read), "legacy read failed");
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, read.combo_idx,
                    read.total_combos, read.passwords_checked,
                    read.perm_idx, read.typo_idx);
    const char* vpath = ".cuda-build/save-rebind-v1.bin";
    require(save_record_v1(vpath, r), "v1 save after rebind failed");
    SaveRecordV1 got;
    require(load_record_v1(vpath, got), "v1 load after rebind failed");
    require(validate_save(got, th, wh, ' ', yh, read.total_combos)
            == SaveMismatch::None, "rebound record failed its own validation");
    require(got.combo_idx == 999 && got.perm_idx == 3 && got.typo_idx == 4
            && got.passwords_checked == 7777,
            "rebind lost the resume indices");
}

static std::string save_slurp(const char* path) {
    std::string out;
    FILE* f = fopen(path, "rb");
    if (!f) return out;
    char buf[512];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    fclose(f);
    return out;
}
static void put_u64_le(std::vector<uint8_t>& b, size_t off, uint64_t v) {
    for (int i = 0; i < 8; ++i) b[off + i] = static_cast<uint8_t>(v >> (i * 8));
}

// Defect 4: a v1 file with valid content plus trailing bytes must be rejected.
static void save_reject_oversized_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 1, 100, 1, 0, 0);
    const char* path = ".cuda-build/save-oversized.bin";
    require(save_record_v1(path, r), "base save failed");
    { std::ofstream f(path, std::ios::binary | std::ios::app); f << "TRAILING"; }
    require(detect_save_kind(path) != SaveKind::V1,
            "oversized magic-prefixed file still detected as v1");
    SaveRecordV1 got;
    require(!load_record_v1(path, got),
            "v1 record with trailing bytes was accepted");
}

// Defect 3: if the prior generation cannot be retained, the primary save must
// NOT advance. The .prev path is occupied by a directory so the durable copy
// cannot publish there.
static void save_prev_failure_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 a, b;
    build_record_v1(a, "t", "w", ' ', th, wh, yh, 1, 100, 1, 0, 0);
    build_record_v1(b, "t", "w", ' ', th, wh, yh, 2, 100, 2, 0, 0);
    const char* path = ".cuda-build/save-prevfail.bin";
    std::string prev = std::string(path) + ".prev";
    remove(path); remove(prev.c_str()); RemoveDirectoryA(prev.c_str());
    require(save_record_v1(path, a), "initial save failed");
    require(CreateDirectoryA(prev.c_str(), nullptr) != 0,
            "could not occupy the .prev path with a directory");
    require(!save_record_v1(path, b),
            "save advanced despite a .prev retention failure");
    SaveRecordV1 got;
    require(load_record_v1(path, got) && got.combo_idx == 1,
            "primary was overwritten when .prev retention failed");
    RemoveDirectoryA(prev.c_str());
    require(save_record_v1(path, b), "save failed after .prev path cleared");
    require(load_record_v1(path, got) && got.combo_idx == 2,
            "save did not advance once .prev could be retained");
}

// Defect 2: a backup reports success only after a durable flush and rename;
// injected flush/rename failures must be reported and must not touch the dst.
static void save_backup_durable_contract() {
    const char* src = ".cuda-build/save-bk-src.bin";
    const char* dst = ".cuda-build/save-bk-dst.bin";
    { std::ofstream f(src, std::ios::binary | std::ios::trunc); f << "NEWCONTENT"; }
    { std::ofstream f(dst, std::ios::binary | std::ios::trunc); f << "OLDCONTENT"; }
    remove((std::string(dst) + ".tmp").c_str());
    require(!copy_file_durable(src, dst, WriteFault::FlushFail),
            "backup reported success despite a flush failure");
    require(save_slurp(dst) == "OLDCONTENT", "dst changed after a flush failure");
    require(!copy_file_durable(src, dst, WriteFault::RenameFail),
            "backup reported success despite a rename failure");
    require(save_slurp(dst) == "OLDCONTENT", "dst changed after a rename failure");
    require(file_state((std::string(dst) + ".tmp").c_str())
                == FileState::Absent,
            "backup left a temp file behind on failure");
    require(copy_file_durable(src, dst), "clean backup failed");
    require(save_slurp(dst) == "NEWCONTENT",
            "clean backup did not publish the new content");
}

// Defect 5: build the legacy fixture from an explicit documented byte layout
// (not the native struct) with distinct nonzero indices matching the real
// save's shape, and prove the reader returns them exactly.
static void save_migrate_1064_raw_contract() {
    std::vector<uint8_t> raw(1064, 0);
    const uint64_t combo = 111222333444ull, total = 555666777888ull,
                   checked = 999888777ull, perm = 41ull, typo = 17ull;
    put_u64_le(raw, 1024, combo);
    put_u64_le(raw, 1032, total);
    put_u64_le(raw, 1040, checked);
    put_u64_le(raw, 1048, perm);
    put_u64_le(raw, 1056, typo);
    const char* path = ".cuda-build/save-legacy-raw.bin";
    {
        std::ofstream f(path, std::ios::binary | std::ios::trunc);
        f.write(reinterpret_cast<const char*>(raw.data()),
                static_cast<std::streamsize>(raw.size()));
    }
    require(detect_save_kind(path) == SaveKind::Legacy1064,
            "raw 1064 fixture not detected as legacy");
    SaveState s{};
    require(load_progress(path, s), "raw legacy load failed");
    require(s.combo_idx == combo && s.total_combos == total
            && s.passwords_checked == checked && s.perm_idx == perm
            && s.typo_idx == typo,
            "reader misread the documented legacy offsets (ABI mismatch)");
}

// -----------------------------------------------------------------------------
// Generation-version binding (GENERATION_BINDING_SPEC.md). A v1 save records the
// candidate-generation version of the build that wrote it; --restore must refuse
// to resume a save produced by a different generation order, which would silently
// skip or repeat candidates.
// -----------------------------------------------------------------------------

// F1: a v1 save whose recorded generation differs from this build's must be
// refused even when every other binding matches. The checksum is re-finalized
// after tampering so the record passes the Corrupt gate and reaches Generation.
static void save_reject_generation_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    r.tool_version = SAVE_GENERATION_VERSION + 1;   // a different generation order
    finalize_record_checksum(r);                    // otherwise-valid record
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::Generation,
            "save from a different candidate-generation order was not rejected");
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::None,
            "matching generation was rejected");
}

// F5: generation is a build property and must be reported before input-identity
// mismatches. A record that mismatches BOTH generation and token list reports
// Generation first, so the user is told to use the matching build.
static void save_generation_order_contract() {
    uint8_t tA[32], tB[32], wh[32], yh[32];
    label_hash("tokA", tA); label_hash("tokB", tB);
    label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', tA, wh, yh, 0, 100, 0, 0, 0);
    r.tool_version = SAVE_GENERATION_VERSION + 1;
    finalize_record_checksum(r);
    require(validate_save(r, tB, wh, ' ', yh, 100) == SaveMismatch::Generation,
            "generation mismatch was not reported before the token-list mismatch");
}

// F5: the generation field is only trustworthy once the bytes are intact. A
// corrupt record that also carries another generation reports Corrupt.
static void save_generation_after_checksum_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
    r.tool_version = SAVE_GENERATION_VERSION + 2;
    finalize_record_checksum(r);
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::Generation,
            "sealed record with another generation was not refused as Generation");
    r.combo_idx ^= 1;                 // corrupt the body; checksum now stale
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::Corrupt,
            "corruption was not reported before the generation mismatch");
}

// F3: every existing on-disk v1 save carries tool_version == 1. Shipping with
// SAVE_GENERATION_VERSION == 1 must let those saves resume with no migration.
static void save_generation_migrate_v1_contract() {
    require(SAVE_GENERATION_VERSION == 1,
            "shipping generation != 1 would refuse every existing v1 save (F3)");
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    SaveRecordV1 r;
    build_record_v1(r, "t", "w", ' ', th, wh, yh, 7, 100, 9, 1, 2);
    r.tool_version = 1;               // the value every historical v1 save carries
    finalize_record_checksum(r);
    require(validate_save(r, th, wh, ' ', yh, 100) == SaveMismatch::None,
            "an existing tool_version==1 v1 save was refused under generation 1");
}

// F4: fail closed. No wildcard/sentinel generation value bypasses the gate:
// every value other than the build's own refuses; only the exact value resumes.
static void save_generation_no_override_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    const uint32_t probes[] = {0u, 1u, 2u, 5u, 0x7fffffffu, 0xffffffffu};
    for (uint32_t v : probes) {
        SaveRecordV1 r;
        build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
        r.tool_version = v;
        finalize_record_checksum(r);
        SaveMismatch got = validate_save(r, th, wh, ' ', yh, 100);
        if (v == SAVE_GENERATION_VERSION) {
            require(got == SaveMismatch::None,
                    "the build's own generation value was refused");
        } else {
            require(got == SaveMismatch::Generation,
                    "a non-matching generation value was accepted (no fail-closed gate)");
        }
    }
}

// F4 property (Amendment 1, A3): every value up to 65535, where real version
// numbers live, then boundary values and seeded random ones across the rest.
// Only the build's own value resumes; every other value refuses.
static void save_generation_property_contract() {
    uint8_t th[32], wh[32], yh[32];
    label_hash("tok", th); label_hash("wal", wh); label_hash("typ", yh);
    std::vector<uint32_t> values;
    for (uint32_t v = 0; v <= 0xffffu; ++v) values.push_back(v);
    values.insert(values.end(), {
        SAVE_GENERATION_VERSION - 1u, SAVE_GENERATION_VERSION + 1u,
        0x7fffffffu, 0x80000000u, 0xffffffffu});
    std::mt19937_64 generator(20261003);
    for (int i = 0; i < 4096; ++i) {
        values.push_back(static_cast<uint32_t>(generator()));
    }
    bool saw_equal = false, saw_unequal = false;
    for (uint32_t v : values) {
        SaveRecordV1 r;
        build_record_v1(r, "t", "w", ' ', th, wh, yh, 0, 100, 0, 0, 0);
        r.tool_version = v;
        finalize_record_checksum(r);
        bool equal = v == SAVE_GENERATION_VERSION;
        require(validate_save(r, th, wh, ' ', yh, 100)
                    == (equal ? SaveMismatch::None : SaveMismatch::Generation),
                "a generation value was not accepted iff it equals the build's own");
        (equal ? saw_equal : saw_unequal) = true;
    }
    require(saw_equal && saw_unequal,
            "generation property did not exercise both outcomes");
}

// The refusal text is user-facing behavior (spec 4.1, Amendment 1 A4).
static void save_generation_message_contract() {
    require(strcmp(mismatch_message(SaveMismatch::Generation),
                   "save was written by a build with a different "
                   "candidate-generation order; resume it with the matching "
                   "build or start a new search") == 0,
            "the generation-mismatch message differs from the approved text");
}

// Synthetic tokens next to case changes, so capslock and closecase have
// letters to act on.
static std::vector<TokenLine> case_lines() {
    return {{{"aB", "c"}, true, false, 0},
            {{"De"}, false, false, 0}};
}

// F2 support: fingerprint the candidate-generation ORDER by hashing the trusted
// reference oracle's output over a fixed battery of shapes (plain, typo-heavy,
// optional + anchored). Deterministic, so a stable build yields a stable hex.
static void generation_fingerprint(char out_hex[65]) {
    struct Case { std::vector<TokenLine> lines; TypoConfig cfg; };
    std::vector<Case> battery;
    battery.push_back({small_lines(), TypoConfig{}});
    {
        TypoConfig t;
        t.max_typos = 2; t.swap = true; t.repeat = true;
        t.insert = true; t.insert_charset = "12";
        battery.push_back({small_lines(), t});
    }
    battery.push_back({{{{"a", "b", "c"}, true, false, 0},
                        {{"d", "e"}, false, false, 0},
                        {{"Z"}, true, true, -1}},
                       TypoConfig{}});
    // One case per remaining typo mode, each alone, then every mode together
    // so the order between options at one position is pinned too.
    {
        TypoConfig t;
        t.max_typos = 1; t.capslock = true;
        battery.push_back({case_lines(), t});
    }
    {
        TypoConfig t;
        t.max_typos = 2; t.del = true;
        battery.push_back({case_lines(), t});
    }
    {
        TypoConfig t;
        t.max_typos = 2; t.closecase = true;
        battery.push_back({case_lines(), t});
    }
    {
        TypoConfig t;
        t.max_typos = 2; t.capslock = true; t.swap = true; t.repeat = true;
        t.del = true; t.closecase = true; t.insert = true;
        t.insert_charset = "1";
        battery.push_back({case_lines(), t});
    }
    std::string blob;
    for (const auto& c : battery) {
        for (const auto& pw : reference_candidates(c.lines, c.cfg)) {
            blob += pw;
            blob.push_back('\n');
        }
        blob.push_back('\x1e');  // record separator between cases
    }
    uint8_t digest[32];
    sha256_vec(std::vector<uint8_t>(blob.begin(), blob.end()), digest);
    sha256_hex(digest, out_hex);
}

// F2: lock the candidate-generation ORDER to SAVE_GENERATION_VERSION. If the
// order changes, the fingerprint drifts and this fails. The pin rows are
// append-only: tests/test_generation_pins.py fails if a row is edited, removed
// or reordered, so the only green path is a version bump plus a new row.
static void save_generation_canary_contract() {
    struct Pin { uint32_t version; const char* hex; };
    static const Pin pins[] = {
        // GENERATION-PINS-BEGIN
        {1, "9c66679756f0a61c6c269f13ab37464340bc5d9a104009d734e2dc2af44b967f"},
        // GENERATION-PINS-END
    };
    char hex[65];
    generation_fingerprint(hex);
    const char* expected = nullptr;
    for (const auto& p : pins) {
        if (p.version == SAVE_GENERATION_VERSION) expected = p.hex;
    }
    if (!expected) {
        fprintf(stderr, "generation fingerprint for version %u = %s\n",
                (unsigned)SAVE_GENERATION_VERSION, hex);
        require(false,
                "no pinned generation fingerprint for the current "
                "SAVE_GENERATION_VERSION - append a row with the printed "
                "fingerprint; never edit an existing row");
    }
    if (std::string(hex) != expected) {
        fprintf(stderr, "generation fingerprint expected %s got %s\n",
                expected, hex);
        require(false,
                "candidate-generation ordering changed - never edit an "
                "existing pin; bump SAVE_GENERATION_VERSION and append a row "
                "with the printed fingerprint");
    }
}

static int run_host_contract(const std::string& name) {
    if (name == "assembly") assembly_contract();
    else if (name == "resume") resume_contract();
    else if (name == "parallel") parallel_contract();
    else if (name == "block_merge") throughput_contract(false);
    else if (name == "chunk_reuse") throughput_contract(true);
    else if (name == "block_boundaries") block_boundaries_contract();
    else if (name == "raw_blocks") raw_blocks_contract();
    else if (name == "pool_ownership") pool_ownership_contract();
    else if (name == "pool_progress") pool_progress_contract();
    else if (name == "pool_cancel") pool_cancel_contract();
    else if (name == "seeded_blocks") seeded_blocks_contract();
    else if (name == "pool") pool_contract();
    else if (name == "mixed_lengths") mixed_contract();
    else if (name == "bounded_typos") bounded_contract();
    else if (name == "cancel") cancel_contract();
    else if (name == "parallel_cancel") parallel_cancel_contract();
    else if (name == "legacy") legacy_contract();
    else if (name == "save_sha256") save_sha256_contract();
    else if (name == "save_roundtrip") save_roundtrip_contract();
    else if (name == "save_identity") save_identity_contract();
    else if (name == "save_reject_tokenlist") save_reject_tokenlist_contract();
    else if (name == "save_reject_wallet") save_reject_wallet_contract();
    else if (name == "save_reject_delimiter") save_reject_delimiter_contract();
    else if (name == "save_reject_typos") save_reject_typos_contract();
    else if (name == "save_detect_corruption") save_detect_corruption_contract();
    else if (name == "save_reject_badmagic") save_reject_badmagic_contract();
    else if (name == "save_reject_badversion") save_reject_badversion_contract();
    else if (name == "save_reject_combocount") save_reject_combocount_contract();
    else if (name == "save_detect_unknown") save_detect_unknown_contract();
    else if (name == "save_reject_oversized") save_reject_oversized_contract();
    else if (name == "save_prev_failure") save_prev_failure_contract();
    else if (name == "save_backup_durable") save_backup_durable_contract();
    else if (name == "save_migrate_1064_raw") save_migrate_1064_raw_contract();
    else if (name == "save_atomic") save_atomic_contract();
    else if (name == "save_prev") save_prev_contract();
    else if (name == "save_three_generation_rename_failure")
        save_three_generation_rename_failure_contract();
    else if (name == "save_file_state_error")
        save_file_state_error_contract();
    else if (name == "save_commit_failure")
        save_commit_failure_contract();
    else if (name == "save_fuzz") save_fuzz_contract();
    else if (name == "save_migrate_1064") save_migrate_1064_contract();
    else if (name == "save_migrate_1048") save_migrate_1048_contract();
    else if (name == "save_rebind_confirm") save_rebind_confirm_contract();
    else if (name == "save_rebind_roundtrip") save_rebind_roundtrip_contract();
    else if (name == "save_reject_generation") save_reject_generation_contract();
    else if (name == "save_generation_order") save_generation_order_contract();
    else if (name == "save_generation_after_checksum")
        save_generation_after_checksum_contract();
    else if (name == "save_generation_migrate_v1")
        save_generation_migrate_v1_contract();
    else if (name == "save_generation_no_override")
        save_generation_no_override_contract();
    else if (name == "save_generation_canary") save_generation_canary_contract();
    else if (name == "save_generation_property")
        save_generation_property_contract();
    else if (name == "save_generation_message")
        save_generation_message_contract();
    else if (name == "md5") md5_contract();
    else if (name == "pipeline") pipeline_contract(false);
    else if (name == "checkpoint") pipeline_contract(true);
    else throw std::invalid_argument("unknown contract");
    std::cout << "PASS\n";
    return 0;
}
