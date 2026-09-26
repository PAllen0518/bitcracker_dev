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
    bool append_block(const uint8_t* data, const uint32_t* lengths,
                      const NextCandidate* next, size_t count,
                      uint32_t source_stride) {
        size_t offset = 0;
        while (offset < count) {
            if (state_.stopped()) return false;
            if (current_ && current_->full() && !flush()) return false;
            if (!current_) {
                const auto wait_start = hrclock::now();
                current_ = state_.acquire(total_);
                waiting_ += secs_since(wait_start);
                if (!current_) return false;
            }
            const size_t take = std::min(count - offset,
                static_cast<size_t>(current_->capacity - current_->count));
            const auto maximum = *std::max_element(
                lengths + offset, lengths + offset + take);
            current_->ensure_stride(static_cast<int>(maximum));
            auto* destination = current_->pw_data.data()
                + static_cast<size_t>(current_->count) * current_->stride;
            const auto* source = data + offset * source_stride;
            if (current_->stride == source_stride) {
                memcpy(destination, source, take * source_stride);
                ++state_.block_copies;
            } else {
                for (size_t i = 0; i < take; ++i) {
                    memcpy(destination + i * current_->stride,
                           source + i * source_stride, lengths[offset + i]);
                }
            }
            memcpy(current_->pw_lens.data() + current_->count,
                   lengths + offset, take * sizeof(uint32_t));
            current_->count += static_cast<int>(take);
            total_ += take;
            offset += take;
            set_next(next[offset - 1]);
        }
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

constexpr size_t PARALLEL_CHUNK_CANDIDATES = 8192;
constexpr uint64_t PARALLEL_UNIT_COMBOS = 16;
constexpr size_t CHUNKS_PER_WORKER = 4;

struct ChunkPosition {
    uint64_t unit = 0;
    uint32_t sequence = 0;
    bool last = false;
};

class GenChunk {
public:
    GenChunk(size_t owner, ProducerState& state)
        : owner_(owner), state_(state), bytes_(PARALLEL_CHUNK_CANDIDATES * 32),
          lengths_(PARALLEL_CHUNK_CANDIDATES),
          next_(PARALLEL_CHUNK_CANDIDATES) {
        // Three payload vectors (bytes, lengths, next) allocate per new chunk;
        // this counter tracks pool growth, not every process allocation.
        state_.chunk_allocations.fetch_add(3);
        record_growth();
    }
    ~GenChunk() noexcept { state_.chunk_live_bytes.fetch_sub(storage_); }
    GenChunk(const GenChunk&) = delete;
    GenChunk& operator=(const GenChunk&) = delete;
    size_t owner() const noexcept { return owner_; }
    size_t count() const noexcept { return count_; }
    uint32_t stride() const noexcept { return stride_; }
    const uint8_t* data() const noexcept { return bytes_.data(); }
    const uint32_t* lengths() const noexcept { return lengths_.data(); }
    const NextCandidate* next() const noexcept { return next_.data(); }
    const ChunkPosition& position() const noexcept { return position_; }
    bool has_prefix() const noexcept { return has_prefix_; }
    NextCandidate prefix() const noexcept { return prefix_; }
    void set_last(bool last) noexcept { position_.last = last; }
    void reset(uint64_t unit, uint32_t sequence) noexcept {
        position_ = {unit, sequence, false};
        count_ = 0;
        stride_ = 32;
        has_prefix_ = false;
    }
    // The next-candidate metadata trails the last appended candidate. Before the
    // first append it belongs to the previous chunk's final slot, so carry it as
    // a prefix and apply it there during replay.
    void set_next(NextCandidate next) noexcept {
        if (count_) next_[count_ - 1] = next;
        else {
            prefix_ = next;
            has_prefix_ = true;
        }
    }
    template <class Write>
    void append(int length, NextCandidate next, Write write) {
        if (length < 1 || length > PW_MAX_LEN
            || count_ == PARALLEL_CHUNK_CANDIDATES) {
            throw std::logic_error("invalid chunk append");
        }
        const uint32_t required = length <= 32
            ? 32 : (length <= 64 ? 64 : 128);
        if (required > stride_) widen(required);
        write(bytes_.data() + count_ * stride_);
        lengths_[count_] = static_cast<uint32_t>(length);
        next_[count_++] = next;
    }
private:
    void record_growth() {
        const uint64_t bytes = bytes_.capacity()
            + lengths_.capacity() * sizeof(uint32_t)
            + next_.capacity() * sizeof(NextCandidate);
        const auto live = state_.chunk_live_bytes.fetch_add(bytes - storage_)
            + bytes - storage_;
        storage_ = bytes;
        auto peak = state_.chunk_storage_bytes.load();
        while (peak < live
               && !state_.chunk_storage_bytes.compare_exchange_weak(
                   peak, live)) {}
    }
    void widen(uint32_t required) {
        const auto capacity = bytes_.capacity();
        if (bytes_.size() < PARALLEL_CHUNK_CANDIDATES * required) {
            bytes_.resize(PARALLEL_CHUNK_CANDIDATES * required);
        }
        if (bytes_.capacity() != capacity) {
            state_.chunk_allocations.fetch_add(1);
            record_growth();
        }
        // Restride existing candidates from the top down so an in-place move
        // never overwrites a slot we have not copied yet.
        for (size_t i = count_; i > 0; --i) {
            memmove(bytes_.data() + (i - 1) * required,
                    bytes_.data() + (i - 1) * stride_, lengths_[i - 1]);
        }
        stride_ = required;
    }
    size_t owner_;
    ProducerState& state_;
    std::vector<uint8_t> bytes_;
    std::vector<uint32_t> lengths_;
    std::vector<NextCandidate> next_;
    ChunkPosition position_{};
    size_t count_ = 0;
    uint32_t stride_ = 32;
    uint64_t storage_ = 0;
    bool has_prefix_ = false;
    NextCandidate prefix_{};
};

class ParallelMerge {
public:
    ParallelMerge(ProducerState& state, uint64_t base, uint64_t num_units)
        : state_(state), writer_(state, base), num_units_(num_units),
          available_(std::max(1, state.producers)),
          allocated_(available_.size(), 0) {
        for (auto& bucket : available_) bucket.reserve(CHUNKS_PER_WORKER);
    }
    bool stopped() const { return state_.stopped() || aborted_.load(); }

    std::unique_ptr<GenChunk> acquire(size_t owner) {
        if (owner >= available_.size()) {
            throw std::logic_error("invalid owner");
        }
#ifdef MULTIBIT_DISABLE_CHUNK_POOL
        if (stopped()) return nullptr;
        return std::make_unique<GenChunk>(owner, state_);
#else
        std::unique_lock<std::mutex> lock(mutex_);
        // Each owner keeps its own reservation of CHUNKS_PER_WORKER, so a burst
        // of later units cannot consume the chunks the next in-order producer
        // needs to keep making progress.
        produce_.wait(lock, [&] {
            return stopped() || !available_[owner].empty()
                || allocated_[owner] < CHUNKS_PER_WORKER;
        });
        if (stopped()) return nullptr;
        auto& bucket = available_[owner];
        if (!bucket.empty()) {
            auto chunk = std::move(bucket.back());
            bucket.pop_back();
            return chunk;
        }
        auto chunk = std::make_unique<GenChunk>(owner, state_);
        ++allocated_[owner];
        return chunk;
#endif
    }
    void publish(std::unique_ptr<GenChunk> chunk) {
        std::unique_lock<std::mutex> lock(mutex_);
        const auto position = chunk->position();
        // Wait for buffer room, but always admit the chunk the merge is waiting
        // for next -- even over budget -- so the earliest ordered work never deadlocks.
        produce_.wait(lock, [&] {
            return stopped()
                || buffered_candidates_ + chunk->count() <= budget_
                || (position.unit == expected_unit_
                    && position.sequence == expected_sequence_);
        });
        if (stopped()) return;
        buffered_candidates_ += chunk->count();
        auto key = std::make_pair(position.unit, position.sequence);
        if (!buffered_.emplace(key, std::move(chunk)).second) {
            throw std::logic_error("duplicate generation chunk");
        }
        consume_.notify_one();
    }
    void run() {
        while (true) {
            std::unique_lock<std::mutex> lock(mutex_);
            if (stopped() || expected_unit_ >= num_units_) return;
            auto key = std::make_pair(expected_unit_, expected_sequence_);
            consume_.wait(lock, [&] {
                return stopped() || buffered_.count(key);
            });
            if (stopped()) return;
            auto it = buffered_.find(key);
            auto chunk = std::move(it->second);
            buffered_.erase(it);
            buffered_candidates_ -= chunk->count();
            if (chunk->position().last) {
                ++expected_unit_;
                expected_sequence_ = 0;
            } else ++expected_sequence_;
            produce_.notify_all();
            lock.unlock();
            const auto start = hrclock::now();
            replay(*chunk);
            state_.merge_seconds += secs_since(start);
            recycle(std::move(chunk));
        }
    }
    void abort() {
        std::lock_guard<std::mutex> lock(mutex_);
        aborted_.store(true);
        produce_.notify_all();
        consume_.notify_all();
    }
    void finish() {
        writer_.flush();
        state_.generation_seconds = writer_.generation_seconds();
    }
private:
    void recycle(std::unique_ptr<GenChunk> chunk) {
#ifndef MULTIBIT_DISABLE_CHUNK_POOL
        std::lock_guard<std::mutex> lock(mutex_);
        available_[chunk->owner()].push_back(std::move(chunk));
        produce_.notify_all();
#endif
    }
    void replay(const GenChunk& chunk) {
        if (chunk.has_prefix()) writer_.set_next(chunk.prefix());
#ifdef MULTIBIT_SCALAR_CHUNK_MERGE
        for (size_t i = 0; i < chunk.count(); ++i) {
            if (!writer_.append(static_cast<int>(chunk.lengths()[i]),
                    chunk.next()[i], [&](uint8_t* output) {
                        memcpy(output, chunk.data() + i * chunk.stride(),
                               chunk.lengths()[i]);
                    })) return;
        }
#else
        writer_.append_block(chunk.data(), chunk.lengths(), chunk.next(),
                             chunk.count(), chunk.stride());
#endif
    }
    ProducerState& state_;
    BatchWriter writer_;
    std::mutex mutex_;
    std::condition_variable produce_, consume_;
    std::map<std::pair<uint64_t, uint32_t>,
             std::unique_ptr<GenChunk>> buffered_;
    uint64_t expected_unit_ = 0;
    uint32_t expected_sequence_ = 0;
    uint64_t num_units_;
    size_t buffered_candidates_ = 0;
    const size_t budget_ = 4 * static_cast<size_t>(BATCH_SIZE);
    std::vector<std::vector<std::unique_ptr<GenChunk>>> available_;
    std::vector<size_t> allocated_;
    std::atomic<bool> aborted_{false};
};

class ChunkSink {
public:
    ChunkSink(ParallelMerge& merge, uint64_t unit, size_t owner = 0)
        : merge_(merge), unit_(unit), owner_(owner) { acquire(); }
    bool stopped() const { return merge_.stopped(); }
    template <class Write>
    bool append(int length, NextCandidate next, Write write) {
        if (stopped() || !current_) return false;
        if (length == 0 || length > PW_MAX_LEN) return true;
        // Defer publishing a full chunk until final metadata is available.
        if (current_->count() == PARALLEL_CHUNK_CANDIDATES) {
            flush(false);
            if (!current_) return false;
        }
        current_->append(length, next, write);
        return true;
    }
    void set_next(NextCandidate next) {
        if (current_) current_->set_next(next);
    }
    void finish() { flush(true); }
private:
    void acquire() {
        current_ = merge_.acquire(owner_);
        if (current_) current_->reset(unit_, sequence_);
    }
    void flush(bool last) {
        if (!current_) return;
        current_->set_last(last);
        merge_.publish(std::move(current_));
        ++sequence_;
        if (!last) acquire();
    }
    ParallelMerge& merge_;
    uint64_t unit_;
    size_t owner_;
    uint32_t sequence_ = 0;
    std::unique_ptr<GenChunk> current_;
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
    const uint64_t unit_combos = state.typo_cfg && state.typo_cfg->any()
        ? 1 : PARALLEL_UNIT_COMBOS;
    const uint64_t num_units =
        (remaining + unit_combos - 1) / unit_combos;
    ParallelMerge merge(state, base_count, num_units);
    std::atomic<uint64_t> next_start{start_combo};
    std::exception_ptr worker_error;
    std::mutex error_mutex;
    auto worker = [&](size_t worker_index) {
        try {
            const char* free_tokens[MAX_FREE];
            int free_lengths[MAX_FREE], permutation[MAX_FREE];
            AnchorSlot anchors[MAX_ANCHORED];
            while (!merge.stopped()) {
                uint64_t start = next_start.fetch_add(unit_combos);
                if (start >= total) break;
                uint64_t unit = (start - start_combo) / unit_combos;
                uint64_t end = std::min(start + unit_combos, total);
                ChunkSink sink(merge, unit, worker_index);
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
            workers.emplace_back(worker, static_cast<size_t>(i));
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
