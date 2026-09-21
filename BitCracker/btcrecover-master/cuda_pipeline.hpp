// Two ordered streams with persistent host and device allocations.
#pragma once

template <class T>
class DeviceBuffer {
public:
    explicit DeviceBuffer(size_t count) {
        checked_cuda(cudaMalloc(reinterpret_cast<void**>(&data_),
                                count * sizeof(T)), "allocate device buffer");
    }
    ~DeviceBuffer() noexcept { if (data_) cudaFree(data_); }
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;
    T* data() noexcept { return data_; }
private:
    T* data_ = nullptr;
};

class CudaStream {
public:
    CudaStream() {
        checked_cuda(cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking),
                     "create stream");
    }
    ~CudaStream() noexcept { if (stream_) cudaStreamDestroy(stream_); }
    CudaStream(const CudaStream&) = delete;
    CudaStream& operator=(const CudaStream&) = delete;
    cudaStream_t get() const noexcept { return stream_; }
private:
    cudaStream_t stream_ = nullptr;
};

class CudaEvent {
public:
    CudaEvent() { checked_cuda(cudaEventCreate(&event_), "create event"); }
    ~CudaEvent() noexcept { if (event_) cudaEventDestroy(event_); }
    CudaEvent(const CudaEvent&) = delete;
    CudaEvent& operator=(const CudaEvent&) = delete;
    cudaEvent_t get() const noexcept { return event_; }
    void record(cudaStream_t stream) {
        checked_cuda(cudaEventRecord(event_, stream), "record event");
    }
    void wait() { checked_cuda(cudaEventSynchronize(event_), "wait for batch"); }
    float since(const CudaEvent& previous) const {
        float milliseconds = 0;
        checked_cuda(cudaEventElapsedTime(&milliseconds, previous.get(), event_),
                     "measure GPU stage");
        return milliseconds;
    }
private:
    cudaEvent_t event_ = nullptr;
};

class GPUWorkSlot {
public:
    DeviceBuffer<uint8_t> passwords;
    DeviceBuffer<uint32_t> lengths;
    DeviceBuffer<int> found;
    HostBuffer<int> host_found;
    CudaStream stream;
    CudaEvent begin, copied, computed, finished;
    std::unique_ptr<Batch> batch;
    int delay_ms = 0;

    explicit GPUWorkSlot(int capacity)
        : passwords(static_cast<size_t>(capacity) * PW_STRIDE),
          lengths(capacity), found(1), host_found(1) {}
    ~GPUWorkSlot() noexcept {
        if (batch) {
            cudaStreamSynchronize(stream.get());
            batch->mark_completed();
        }
    }
    GPUWorkSlot(const GPUWorkSlot&) = delete;
    GPUWorkSlot& operator=(const GPUWorkSlot&) = delete;
};

class GPUEngine {
public:
    explicit GPUEngine(int capacity = BATCH_SIZE, bool asynchronous = true)
        : capacity_(capacity) {
        slots_.push_back(std::make_unique<GPUWorkSlot>(capacity));
        if (asynchronous && slots_[0]->host_found.pinned()) {
            try {
                slots_.push_back(std::make_unique<GPUWorkSlot>(capacity));
            } catch (const CudaFailure& error) {
                // Fall back only when the second slot cannot fit in memory.
                if (error.code() != cudaErrorMemoryAllocation) throw;
                cudaGetLastError();
                fprintf(stderr, "Using one GPU buffer due to memory limits.\n");
            }
        }
    }
    GPUEngine(const GPUEngine&) = delete;
    GPUEngine& operator=(const GPUEngine&) = delete;
    bool can_submit() const noexcept { return pending_.size() < slots_.size(); }
    size_t pending() const noexcept { return pending_.size(); }
    int device_allocations() const noexcept {
        return static_cast<int>(slots_.size());
    }

    void submit(std::unique_ptr<Batch> batch) {
        if (!can_submit() || batch->count < 1 || batch->count > capacity_) {
            throw std::logic_error("invalid GPU submission");
        }
        const size_t index = next_slot_;
        auto& slot = *slots_[index];
        if (slot.batch) throw std::logic_error("GPU slot is still in use");
        slot.batch = std::move(batch);
        slot.batch->mark_submitted();
        const auto stream = slot.stream.get();
#ifdef MULTIBIT_CUDA_TESTING
        if (delay_ms_) {
            slot.delay_ms = delay_ms_;
            checked_cuda(cudaLaunchHostFunc(stream, [](void* value) {
                std::this_thread::sleep_for(std::chrono::milliseconds(
                    *static_cast<int*>(value)));
            }, &slot.delay_ms), "queue test delay");
            delay_ms_ = 0;
        }
#endif
        slot.begin.record(stream);
        const auto& work = *slot.batch;
        checked_cuda(cudaMemcpyAsync(slot.passwords.data(), work.pw_data.data(),
            static_cast<size_t>(work.count) * work.stride,
            cudaMemcpyHostToDevice, stream), "copy candidates");
        checked_cuda(cudaMemcpyAsync(slot.lengths.data(), work.pw_lens.data(),
            work.count * sizeof(uint32_t), cudaMemcpyHostToDevice, stream),
            "copy candidate lengths");
        checked_cuda(cudaMemsetAsync(slot.found.data(), 0xff, sizeof(int), stream),
                     "clear result");
        slot.copied.record(stream);
        optimized_check_kernel<true, true, false>
            <<<(work.count + 255) / 256, 256, 0, stream>>>(
                slot.passwords.data(), slot.lengths.data(), work.count,
                work.stride, slot.found.data());
        checked_cuda(cudaGetLastError(), "launch candidate kernel");
        slot.computed.record(stream);
        checked_cuda(cudaMemcpyAsync(slot.host_found.data(), slot.found.data(),
            sizeof(int), cudaMemcpyDeviceToHost, stream), "copy result");
        slot.finished.record(stream);
        pending_.push_back(index);
        next_slot_ = (index + 1) % slots_.size();
        if (slots_.size() == 1 || !work.pinned() || !slot.host_found.pinned()) {
            slot.finished.wait();
        }
    }

    std::unique_ptr<Batch> complete() {
        if (pending_.empty()) throw std::logic_error("no GPU batch pending");
        auto& slot = *slots_[pending_.front()];
        slot.finished.wait();
        slot.batch->copy_ms = slot.copied.since(slot.begin);
        slot.batch->kernel_ms = slot.computed.since(slot.copied);
        slot.batch->found_index = slot.host_found[0];
        slot.batch->mark_completed();
        pending_.pop_front();
        return std::move(slot.batch);
    }

#ifdef MULTIBIT_CUDA_TESTING
    void delay_next_for_test(int milliseconds) noexcept { delay_ms_ = milliseconds; }
#endif

private:
    int capacity_;
    std::vector<std::unique_ptr<GPUWorkSlot>> slots_;
    std::deque<size_t> pending_;
    size_t next_slot_ = 0;
#ifdef MULTIBIT_CUDA_TESTING
    int delay_ms_ = 0;
#endif
};
