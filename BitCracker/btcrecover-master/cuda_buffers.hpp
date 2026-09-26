// Bounded host buffer ownership for the CPU/CUDA pipeline.
#pragma once
#include <atomic>
#include <array>
#include <deque>
#include <exception>
#include <stdexcept>

class CudaFailure : public std::runtime_error {
public:
    CudaFailure(cudaError_t code, const char* operation)
        : std::runtime_error(std::string(operation) + ": "
                             + cudaGetErrorString(code)), code_(code) {}
    cudaError_t code() const noexcept { return code_; }
private:
    cudaError_t code_;
};

static void checked_cuda(cudaError_t error, const char* operation) {
    if (error != cudaSuccess) {
        throw CudaFailure(error, operation);
    }
}

template <class T>
class HostBuffer {
public:
    explicit HostBuffer(size_t count, bool pin = true) {
        if (pin) {
            cudaError_t error = cudaHostAlloc(
                reinterpret_cast<void**>(&pinned_), count * sizeof(T),
                cudaHostAllocDefault);
            if (error != cudaSuccess && error != cudaErrorMemoryAllocation) {
                checked_cuda(error, "allocate pinned host memory");
            }
            if (error == cudaErrorMemoryAllocation) cudaGetLastError();
        }
        if (!pinned_) ordinary_.resize(count);
    }
    ~HostBuffer() noexcept { if (pinned_) cudaFreeHost(pinned_); }
    HostBuffer(const HostBuffer&) = delete;
    HostBuffer& operator=(const HostBuffer&) = delete;
    T* data() noexcept { return pinned_ ? pinned_ : ordinary_.data(); }
    const T* data() const noexcept {
        return pinned_ ? pinned_ : ordinary_.data();
    }
    bool pinned() const noexcept { return pinned_ != nullptr; }
    T& operator[](size_t index) noexcept { return data()[index]; }
private:
    T* pinned_ = nullptr;
    std::vector<T> ordinary_;
};

class Batch {
public:
    HostBuffer<uint8_t> pw_data;
    HostBuffer<uint32_t> pw_lens;
    const int capacity;
    int count = 0;
    uint32_t stride = 32;
    int max_length = 0;
    int found_index = -1;
    uint64_t next_combo_idx = 0, next_perm_idx = 0, next_typo_idx = 0;
    uint64_t passwords_total = 0;
    double copy_ms = 0, kernel_ms = 0;

    explicit Batch(int size = BATCH_SIZE, bool pinned = true)
        : pw_data(valid_capacity(size) * PW_STRIDE, pinned),
          pw_lens(valid_capacity(size), pinned), capacity(size) {}
    Batch(const Batch&) = delete;
    Batch& operator=(const Batch&) = delete;
    Batch& info() noexcept { return *this; }
    const Batch& info() const noexcept { return *this; }
    uint8_t* passwords() noexcept { return pw_data.data(); }
    const uint8_t* passwords() const noexcept { return pw_data.data(); }
    uint32_t* lengths() noexcept { return pw_lens.data(); }
    const uint32_t* lengths() const noexcept { return pw_lens.data(); }

    void reset(uint64_t total) {
        writable();
        count = 0;
        stride = 32;
        max_length = 0;
        found_index = -1;
        next_combo_idx = next_perm_idx = next_typo_idx = 0;
        passwords_total = total;
        copy_ms = kernel_ms = 0;
    }
    bool full() const noexcept { return count == capacity; }
    bool pinned() const noexcept { return pw_data.pinned() && pw_lens.pinned(); }

    void ensure_stride(int length) {
        writable();
        if (length < 0 || length > PW_MAX_LEN) {
            throw std::invalid_argument("invalid password length");
        }
        const uint32_t required = length <= 32 ? 32 : (length <= 64 ? 64 : 128);
        if (required > stride) {
            for (int i = count - 1; i >= 0; --i) {
                memmove(pw_data.data() + static_cast<size_t>(i) * required,
                        pw_data.data() + static_cast<size_t>(i) * stride,
                        pw_lens[i]);
            }
            stride = required;
        }
        max_length = std::max(max_length, length);
    }
    void mark_submitted() {
        writable();
        in_flight_ = true;
    }
    void mark_completed() noexcept { in_flight_ = false; }

private:
    bool in_flight_ = false;
    static size_t valid_capacity(int size) {
        if (size < 1 || size > BATCH_SIZE) {
            throw std::invalid_argument("batch size must be from 1 to 1048576");
        }
        return static_cast<size_t>(size);
    }
    void writable() const {
        if (in_flight_) throw std::logic_error("batch is still in flight");
    }
};

struct TypoConfig;

class ProducerState {
public:
    const std::vector<TokenLine>* lines = nullptr;
    const TypoConfig* typo_cfg = nullptr;
    uint64_t start_combo = 0, start_perm = 0, start_typo = 0;
    uint64_t total_combos = 0;
    double generation_seconds = 0;
    std::atomic<uint64_t> chunk_allocations{0};
    uint64_t block_copies = 0;
    std::atomic<uint64_t> chunk_storage_bytes{0};
    std::atomic<uint64_t> chunk_live_bytes{0};
    double merge_seconds = 0;
    int producers = 1;  // >1 enables the parallel generate + ordered merge

    explicit ProducerState(int capacity = BATCH_SIZE, bool pin = true) {
        for (int i = 0; i < 3; ++i) {
            available_.push_back(std::make_unique<Batch>(capacity, pin));
            ++allocated_batches_;
            all_pinned_ = all_pinned_ && available_.back()->pinned();
        }
    }
    ProducerState(const ProducerState&) = delete;
    ProducerState& operator=(const ProducerState&) = delete;
    ProducerState& details() noexcept { return *this; }
    const ProducerState& details() const noexcept { return *this; }
    bool stopped() const noexcept { return stopped_.load(); }
    void request_stop() noexcept {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopped_.store(true);
        }
        available_condition_.notify_all();
        ready_condition_.notify_all();
    }
    std::unique_ptr<Batch> acquire(uint64_t total) {
        std::unique_lock<std::mutex> lock(mutex_);
        available_condition_.wait(lock, [&] {
            return stopped() || !available_.empty();
        });
        if (stopped()) return nullptr;
        auto batch = std::move(available_.front());
        available_.pop_front();
        lock.unlock();
        batch->reset(total);
        return batch;
    }
    bool publish(std::unique_ptr<Batch> batch) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopped()) return false;
        ready_.push_back(std::move(batch));
        ready_condition_.notify_one();
        return true;
    }
    std::unique_ptr<Batch> take_ready(bool wait) {
        std::unique_lock<std::mutex> lock(mutex_);
        if (wait) ready_condition_.wait(lock, [&] {
            return !ready_.empty() || finished_ || stopped();
        });
        if (ready_.empty()) return nullptr;
        auto batch = std::move(ready_.front());
        ready_.pop_front();
        return batch;
    }
    void recycle(std::unique_ptr<Batch> batch) {
        std::lock_guard<std::mutex> lock(mutex_);
        available_.push_back(std::move(batch));
        available_condition_.notify_one();
    }
    void finish(std::exception_ptr failure = nullptr) {
        std::lock_guard<std::mutex> lock(mutex_);
        failure_ = failure;
        finished_ = true;
        ready_condition_.notify_all();
    }
    void rethrow_failure() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (failure_) std::rethrow_exception(failure_);
    }
    int allocated_batches() const noexcept { return allocated_batches_; }
    bool all_pinned() const noexcept { return all_pinned_; }

private:
    std::mutex mutex_;
    std::condition_variable available_condition_, ready_condition_;
    std::deque<std::unique_ptr<Batch>> available_, ready_;
    std::atomic<bool> stopped_{false};
    bool finished_ = false;
    std::exception_ptr failure_;
    bool all_pinned_ = true;
    int allocated_batches_ = 0;
};
