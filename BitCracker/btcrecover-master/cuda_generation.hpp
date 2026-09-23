// Ordered generation into reusable, adaptively sized password slots.
#pragma once
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <map>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

class AssemblyPlan {
public:
    AssemblyPlan(const int* free_lengths, int free_count,
                 const AnchorSlot* anchors, int anchor_count)
        : slots_(free_count + anchor_count) {
        for (int i = 0; i < anchor_count; ++i) {
            int position = anchors[i].pos == -1 ? slots_ - 1 : anchors[i].pos;
            if (position >= 0 && position < slots_ && !fixed_[position]) {
                fixed_[position] = anchors[i].text;
                lengths_[position] = anchors[i].len;
            }
        }
        int next_free = 0;
        for (int i = 0; i < slots_; ++i) {
            if (fixed_[i]) length_ += lengths_[i];
            else if (next_free < free_count) free_order_[i] = next_free++;
        }
        for (int i = 0; i < next_free; ++i) length_ += free_lengths[i];
        length_ = std::min(length_, PW_MAX_LEN - 1);
    }
    int length() const noexcept { return length_; }
    void write(const char* const* free_tokens, const int* free_lengths,
               const int* permutation, uint8_t* destination) const {
        int written = 0;
        for (int i = 0; i < slots_ && written < length_; ++i) {
            const char* token = fixed_[i];
            int length = lengths_[i];
            if (!token && free_order_[i] >= 0) {
                int index = permutation[free_order_[i]];
                token = free_tokens[index];
                length = free_lengths[index];
            }
            if (token) {
                length = std::min(length, length_ - written);
                memcpy(destination + written, token, length);
                written += length;
            }
        }
    }
private:
    int slots_;
    int length_ = 0;
    std::array<const char*, MAX_FREE + MAX_ANCHORED> fixed_{};
    std::array<int, MAX_FREE + MAX_ANCHORED> lengths_{};
    std::array<int, MAX_FREE + MAX_ANCHORED> free_order_ = [] {
        std::array<int, MAX_FREE + MAX_ANCHORED> result{};
        result.fill(-1);
        return result;
    }();
};

struct NextCandidate {
    uint64_t combo, permutation, typo;
};

class BatchWriter {
public:
    BatchWriter(ProducerState& state, uint64_t base)
        : state_(state), total_(base), started_(hrclock::now()) {}

    template <class Write>
    bool append(int length, NextCandidate next, Write write) {
        if (state_.stopped()) return false;
        if (length == 0 || length > PW_MAX_LEN) return true;
        if (current_ && current_->full() && !flush()) return false;
        if (!current_) {
            const auto wait_start = hrclock::now();
            current_ = state_.acquire(total_);
            waiting_ += secs_since(wait_start);
            if (!current_) return false;
        }
        current_->ensure_stride(length);
        write(current_->pw_data.data()
              + static_cast<size_t>(current_->count) * current_->stride);
        current_->pw_lens[current_->count++] = static_cast<uint32_t>(length);
        set_next(next);
        ++total_;
        return true;
    }
    void set_next(NextCandidate next) {
        if (!current_) return;
        current_->next_combo_idx = next.combo;
        current_->next_perm_idx = next.permutation;
        current_->next_typo_idx = next.typo;
    }
    bool stopped() const { return state_.stopped(); }
    bool flush() {
        if (!current_ || !current_->count) return !state_.stopped();
        return state_.publish(std::move(current_));
    }
    double generation_seconds() const {
        return std::max(0.0, secs_since(started_) - waiting_);
    }
private:
    ProducerState& state_;
    std::unique_ptr<Batch> current_;
    uint64_t total_;
    hrtimepoint started_;
    double waiting_ = 0;
};

// Emit every candidate for one already-decoded combination, in exact CUDA
// order, through a sink exposing append()/set_next()/stopped(). This is the
// single source of truth for per-combo generation; both the serial writer and
// the parallel workers drive it, so their output is identical by construction.
// `permutation` must be identity (0..free_count-1) on entry.
template <class Sink>
static void generate_variants(
    const char* const* free_tokens, const int* free_lengths, int free_count,
    const AnchorSlot* anchors, int anchor_count, int* permutation,
    uint64_t combo, uint64_t perm_start, uint64_t typo_start,
    const TypoConfig* typo_cfg, Sink& sink) {
    if (!free_count) return;
    const uint64_t permutations = factorial_u64(free_count);
    if (perm_start >= permutations) return;
    AssemblyPlan plan(free_lengths, free_count, anchors, anchor_count);
    for (uint64_t skipped = 0; skipped < perm_start; ++skipped) {
        if (sink.stopped()) return;
        std::next_permutation(permutation, permutation + free_count);
    }
    uint64_t perm_index = perm_start;
    do {
        if (sink.stopped()) return;
        NextCandidate next = perm_index + 1 == permutations
            ? NextCandidate{combo + 1, 0, 0}
            : NextCandidate{combo, perm_index + 1, 0};
        if (typo_cfg && typo_cfg->any()) {
            char buffer[PW_MAX_LEN];
            plan.write(free_tokens, free_lengths, permutation,
                       reinterpret_cast<uint8_t*>(buffer));
            std::string password(buffer, plan.length());
            uint64_t skip = perm_index == perm_start ? typo_start : 0;
            uint64_t index = 0, last_added = UINT64_MAX;
            auto emit = [&](const std::string& value) {
                if (sink.stopped()) return false;
                uint64_t current = index++;
                if (current < skip) return true;
                bool valid = !value.empty() && value.size() <= PW_MAX_LEN;
                bool accepted = sink.append(
                    static_cast<int>(value.size()),
                    {combo, perm_index, index}, [&](uint8_t* output) {
                        memcpy(output, value.data(), value.size());
                    });
                if (valid && accepted) last_added = current;
                return accepted;
            };
            auto stats = stream_typo_variants(password, *typo_cfg, emit);
            if (stats.completed && index > 0 && last_added == index - 1) {
                sink.set_next(next);
            }
        } else {
            if (!sink.append(plan.length(), next, [&](uint8_t* output) {
                plan.write(free_tokens, free_lengths, permutation, output);
            })) return;
        }
        ++perm_index;
    } while (std::next_permutation(permutation, permutation + free_count));
}

// Decode a combination index into its free tokens and anchors (same ordering as
// the serial walk and the reference oracle). Returns false to skip the combo.
static bool decode_combo(
    const std::vector<TokenLine>& lines, const std::vector<int>& radices,
    uint64_t combo, const char** free_tokens, int* free_lengths,
    int* permutation, AnchorSlot* anchors, int& free_count, int& anchor_count) {
    uint64_t remainder = combo;
    free_count = 0;
    anchor_count = 0;
    for (size_t i = 0; i < lines.size(); ++i) {
        int choice = static_cast<int>(remainder % radices[i]);
        remainder /= radices[i];
        if (!lines[i].required && choice-- == 0) continue;
        const auto& token = lines[i].tokens.at(choice);
        if (lines[i].has_anchor) {
            if (anchor_count == MAX_ANCHORED) {
                throw std::invalid_argument("too many anchored tokens");
            }
            anchors[anchor_count++] = {lines[i].anchor_pos, token.data(),
                                       static_cast<int>(token.size())};
        } else {
            if (free_count == MAX_FREE) {
                throw std::invalid_argument("too many free tokens");
            }
            free_tokens[free_count] = token.data();
            free_lengths[free_count] = static_cast<int>(token.size());
            permutation[free_count] = free_count;
            ++free_count;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// Parallel generation: many workers produce combos concurrently; a single
// merge thread consumes their output strictly in generation order, so the
// GPU-visible sequence, batch boundaries, and checkpoints are byte-for-byte
// identical to the single-producer path.
// ---------------------------------------------------------------------------

// A bounded slice of recorded generation. A work unit is a contiguous run of
// combinations (see PARALLEL_UNIT_COMBOS); its candidates are packed into chunks
// of ~PARALLEL_CHUNK_CANDIDATES so that, no matter how small each combination
// is, the merge's per-chunk locking and allocation are amortized over thousands
// of candidates rather than paid per combination. Bytes for stored candidates
// are concatenated; ops replay the exact append()/set_next() calls in order.
struct GenChunk {
    uint64_t unit = 0;
    uint32_t seq = 0;
    bool last = false;
    size_t candidates = 0;
    std::vector<uint8_t> bytes;
    struct Op {
        bool is_append;
        int length;
        NextCandidate next;
    };
    std::vector<Op> ops;
};

static const size_t PARALLEL_CHUNK_CANDIDATES = 8192;
// Combinations per work unit. Chosen so tiny combinations (e.g. search60's
// ~720 candidates each) still pack into full chunks, while common workloads
// still yield far more units than worker threads.
static const uint64_t PARALLEL_UNIT_COMBOS = 16;

class ParallelMerge {
public:
    ParallelMerge(ProducerState& state, uint64_t base, uint64_t num_units)
        : state_(state), writer_(state, base), num_units_(num_units) {}

    bool stopped() const { return state_.stopped() || aborted_.load(); }

    // Called by workers. Blocks under back-pressure, except the chunk the merge
    // is currently waiting for is always admitted to guarantee progress.
    void publish(GenChunk chunk) {
        std::unique_lock<std::mutex> lock(mutex_);
        produce_.wait(lock, [&] {
            return state_.stopped() || aborted_.load()
                || buffered_candidates_ + chunk.candidates <= budget_
                || (chunk.unit == expected_unit_ && chunk.seq == expected_seq_);
        });
        if (state_.stopped() || aborted_.load()) return;
        buffered_candidates_ += chunk.candidates;
        auto key = std::make_pair(chunk.unit, chunk.seq);
        buffered_.emplace(key, std::move(chunk));
        consume_.notify_one();
    }

    // Runs on the coordinator thread until every unit is merged or a stop.
    void run() {
        while (true) {
            std::unique_lock<std::mutex> lock(mutex_);
            if (state_.stopped() || aborted_.load()) return;
            if (expected_unit_ >= num_units_) return;
            auto key = std::make_pair(expected_unit_, expected_seq_);
            consume_.wait(lock, [&] {
                return state_.stopped() || aborted_.load()
                    || buffered_.count(key) != 0;
            });
            if (state_.stopped() || aborted_.load()) return;
            auto it = buffered_.find(key);
            GenChunk chunk = std::move(it->second);
            buffered_.erase(it);
            buffered_candidates_ -= chunk.candidates;
            if (chunk.last) {
                ++expected_unit_;
                expected_seq_ = 0;
            } else {
                ++expected_seq_;
            }
            produce_.notify_all();
            lock.unlock();
            replay(chunk);
        }
    }

    void abort() {
        aborted_.store(true);
        std::lock_guard<std::mutex> lock(mutex_);
        produce_.notify_all();
        consume_.notify_all();
    }

    void finish() {
        writer_.flush();
        state_.generation_seconds = writer_.generation_seconds();
    }

private:
    void replay(GenChunk& chunk) {
        size_t offset = 0;
        for (const auto& op : chunk.ops) {
            if (op.is_append) {
                const uint8_t* source = nullptr;
                int length = op.length;
                if (length > 0 && length <= PW_MAX_LEN) {
                    source = chunk.bytes.data() + offset;
                    offset += static_cast<size_t>(length);
                }
                writer_.append(length, op.next, [&](uint8_t* out) {
                    if (source) memcpy(out, source, static_cast<size_t>(length));
                });
            } else {
                writer_.set_next(op.next);
            }
        }
    }

    ProducerState& state_;
    BatchWriter writer_;
    std::mutex mutex_;
    std::condition_variable produce_, consume_;
    std::map<std::pair<uint64_t, uint32_t>, GenChunk> buffered_;
    uint64_t expected_unit_ = 0;
    uint32_t expected_seq_ = 0;
    uint64_t num_units_;
    size_t buffered_candidates_ = 0;
    const size_t budget_ = 4 * static_cast<size_t>(BATCH_SIZE);
    std::atomic<bool> aborted_{false};
};

// Per-worker, per-unit sink: packs a unit's candidates (spanning many small
// combinations) into bounded chunks. Mirrors BatchWriter's byte-storage guard
// so replay is exact.
class ChunkSink {
public:
    ChunkSink(ParallelMerge& merge, uint64_t unit)
        : merge_(merge), unit_(unit) {
        current_.unit = unit;
        reserve();
    }
    bool stopped() const { return merge_.stopped(); }

    template <class Write>
    bool append(int length, NextCandidate next, Write write) {
        if (merge_.stopped()) return false;
        if (length > 0 && length <= PW_MAX_LEN) {
            size_t offset = current_.bytes.size();
            current_.bytes.resize(offset + static_cast<size_t>(length));
            write(current_.bytes.data() + offset);
        }
        current_.ops.push_back({true, length, next});
        ++current_.candidates;
        if (current_.candidates >= PARALLEL_CHUNK_CANDIDATES) flush(false);
        return !merge_.stopped();
    }
    void set_next(NextCandidate next) {
        current_.ops.push_back({false, 0, next});
    }
    void finish() { flush(true); }

private:
    void reserve() {
        current_.ops.reserve(PARALLEL_CHUNK_CANDIDATES + PARALLEL_CHUNK_CANDIDATES / 4);
        current_.bytes.reserve(PARALLEL_CHUNK_CANDIDATES * 16);
    }
    void flush(bool last) {
        uint32_t seq = current_.seq;
        current_.last = last;
        merge_.publish(std::move(current_));
        current_ = GenChunk{};
        current_.unit = unit_;
        current_.seq = seq + 1;
        reserve();
    }
    ParallelMerge& merge_;
    uint64_t unit_;
    GenChunk current_;
};

static void generate_parallel(ProducerState& state, uint64_t base_count) {
    const auto& lines = *state.lines;
    std::vector<int> radices(lines.size());
    for (size_t i = 0; i < lines.size(); ++i) {
        radices[i] = static_cast<int>(lines[i].tokens.size())
            + (lines[i].required ? 0 : 1);
        if (radices[i] == 0) throw std::invalid_argument("empty required line");
    }
    const uint64_t start_combo = state.start_combo;
    const uint64_t total = state.total_combos;
    const uint64_t remaining = total > start_combo ? total - start_combo : 0;
    const uint64_t num_units =
        (remaining + PARALLEL_UNIT_COMBOS - 1) / PARALLEL_UNIT_COMBOS;
    ParallelMerge merge(state, base_count, num_units);
    std::atomic<uint64_t> next_start{start_combo};
    std::exception_ptr worker_error;
    std::mutex error_mutex;
    auto worker = [&]() {
        try {
            const char* free_tokens[MAX_FREE];
            int free_lengths[MAX_FREE], permutation[MAX_FREE];
            AnchorSlot anchors[MAX_ANCHORED];
            while (!merge.stopped()) {
                uint64_t start = next_start.fetch_add(PARALLEL_UNIT_COMBOS);
                if (start >= total) break;
                uint64_t unit = (start - start_combo) / PARALLEL_UNIT_COMBOS;
                uint64_t end = std::min(start + PARALLEL_UNIT_COMBOS, total);
                ChunkSink sink(merge, unit);
                for (uint64_t combo = start; combo < end && !merge.stopped();
                     ++combo) {
                    int free_count = 0, anchor_count = 0;
                    decode_combo(lines, radices, combo, free_tokens, free_lengths,
                                 permutation, anchors, free_count, anchor_count);
                    uint64_t perm_start =
                        combo == start_combo ? state.start_perm : 0;
                    uint64_t typo_start =
                        combo == start_combo ? state.start_typo : 0;
                    generate_variants(free_tokens, free_lengths, free_count,
                                      anchors, anchor_count, permutation, combo,
                                      perm_start, typo_start, state.typo_cfg,
                                      sink);
                }
                sink.finish();
            }
            if (state.stopped()) merge.abort();
        } catch (...) {
            {
                std::lock_guard<std::mutex> lock(error_mutex);
                if (!worker_error) worker_error = std::current_exception();
            }
            state.request_stop();
            merge.abort();
        }
    };
    std::vector<std::thread> workers;
    try {
        for (int i = 0; i < state.producers; ++i) {
            workers.emplace_back(worker);
        }
        merge.run();
    } catch (...) {
        state.request_stop();
        merge.abort();
        for (auto& thread : workers) thread.join();
        throw;
    }
    // Cancellation can leave workers waiting for space in the merge queue.
    merge.abort();
    for (auto& thread : workers) thread.join();
    if (worker_error) std::rethrow_exception(worker_error);
    merge.finish();
}

static void generate_ordered(ProducerState& state, uint64_t base_count) {
    if (state.producers > 1) {
        generate_parallel(state, base_count);
        return;
    }
    const auto& lines = *state.lines;
    std::vector<int> radices(lines.size()), choices(lines.size());
    uint64_t remainder = state.start_combo;
    for (size_t i = 0; i < lines.size(); ++i) {
        radices[i] = static_cast<int>(lines[i].tokens.size())
            + (lines[i].required ? 0 : 1);
        if (radices[i] == 0) throw std::invalid_argument("empty required line");
        choices[i] = static_cast<int>(remainder % radices[i]);
        remainder /= radices[i];
    }
    BatchWriter writer(state, base_count);
    const char* free_tokens[MAX_FREE];
    int free_lengths[MAX_FREE], permutation[MAX_FREE];
    AnchorSlot anchors[MAX_ANCHORED];
    for (uint64_t combo = state.start_combo;
         combo < state.total_combos && !state.stopped(); ++combo) {
        int free_count = 0, anchor_count = 0;
        for (size_t i = 0; i < lines.size(); ++i) {
            int choice = choices[i];
            if (!lines[i].required && choice-- == 0) continue;
            const auto& token = lines[i].tokens.at(choice);
            if (lines[i].has_anchor) {
                if (anchor_count == MAX_ANCHORED) {
                    throw std::invalid_argument("too many anchored tokens");
                }
                anchors[anchor_count++] = {lines[i].anchor_pos, token.data(),
                                           static_cast<int>(token.size())};
            } else {
                if (free_count == MAX_FREE) {
                    throw std::invalid_argument("too many free tokens");
                }
                free_tokens[free_count] = token.data();
                free_lengths[free_count] = static_cast<int>(token.size());
                permutation[free_count] = free_count;
                ++free_count;
            }
        }
        uint64_t perm_start = combo == state.start_combo ? state.start_perm : 0;
        uint64_t typo_start = combo == state.start_combo ? state.start_typo : 0;
        generate_variants(free_tokens, free_lengths, free_count, anchors,
                          anchor_count, permutation, combo, perm_start,
                          typo_start, state.typo_cfg, writer);
        for (size_t i = 0; i < choices.size(); ++i) {
            if (++choices[i] < radices[i]) break;
            choices[i] = 0;
        }
    }
    writer.flush();
    state.generation_seconds = writer.generation_seconds();
}

static void producer_thread(ProducerState* state, uint64_t base) noexcept {
    try {
        generate_ordered(*state, base);
        state->finish();
    } catch (const std::exception& error) {
        static_cast<void>(error);
        state->finish(std::current_exception());
    }
}

class ProducerWorker {
public:
    ProducerWorker(ProducerState& state, uint64_t base)
        : state_(state), thread_(producer_thread, &state, base) {}
    ~ProducerWorker() noexcept {
        state_.request_stop();
        if (thread_.joinable()) thread_.join();
    }
    void join() { if (thread_.joinable()) thread_.join(); }
    ProducerWorker(const ProducerWorker&) = delete;
    ProducerWorker& operator=(const ProducerWorker&) = delete;
private:
    ProducerState& state_;
    std::thread thread_;
};
