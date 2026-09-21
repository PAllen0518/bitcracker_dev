// Ordered, bounded-storage replacement for the staged typo vectors.
#pragma once
#include <array>

struct TypoStreamStats {
    size_t scratch_bytes;
    uint64_t emitted;
    bool completed;
};

template <class Emit>
class TypoStream {
public:
    TypoStream(const TypoConfig& config, Emit& emit)
        : config_(config), emit_(emit) {
        for (auto& buffer : buffers_) buffer.reserve(4 * PW_MAX_LEN + 4);
    }

    TypoStreamStats run(const std::string& base) {
        if (base.size() > PW_MAX_LEN) {
            throw std::invalid_argument("typo base exceeds 128 bytes");
        }
        swaps(base, 0);
        if (running_ && config_.capslock && config_.max_typos > 0) {
            auto& changed = buffers_[0];
            changed.assign(base);
            for (auto& character : changed) character = swap_case_ch(character);
            if (changed != base) swaps(changed, 1);
        }
        size_t bytes = sizeof(swap_positions_) + sizeof(simple_positions_)
            + sizeof(simple_options_) + sizeof(insert_positions_)
            + sizeof(insert_letters_);
        for (const auto& buffer : buffers_) bytes += buffer.capacity();
        return {bytes, emitted_, running_};
    }

private:
    const TypoConfig& config_;
    Emit& emit_;
    std::array<std::string, 4> buffers_;
    std::array<int, PW_MAX_LEN> swap_positions_{};
    std::array<int, PW_MAX_LEN> simple_positions_{};
    std::array<uint8_t, PW_MAX_LEN> simple_options_{};
    std::array<int, PW_MAX_LEN * 2 + 1> insert_positions_{};
    std::array<char, PW_MAX_LEN * 2 + 1> insert_letters_{};
    uint64_t emitted_ = 0;
    bool running_ = true;

    void output(const std::string& value) {
        if (running_) {
            ++emitted_;
            running_ = emit_(value);
        }
    }

    void swaps(const std::string& base, int used) {
        if (!running_) return;
        simple(base, used);
        if (!config_.swap) return;
        const int maximum = std::min({config_.max_typos - used,
            config_.max_typos_swap, static_cast<int>(base.size()) / 2});
        for (int count = 1; count <= maximum && running_; ++count) {
            choose_swaps(base, used, count, 0, 0);
        }
    }

    void choose_swaps(const std::string& base, int used, int count,
                      int depth, int first) {
        if (!running_) return;
        if (depth == count) {
            for (int i = 0; i < count; ++i) {
                int position = swap_positions_[i];
                if (base[position] == base[position + 1]) return;
            }
            auto& changed = buffers_[1];
            changed.assign(base);
            for (int i = 0; i < count; ++i) {
                int position = swap_positions_[i];
                std::swap(changed[position], changed[position + 1]);
            }
            simple(changed, used + count);
            return;
        }
        for (int position = first;
             position + 1 < static_cast<int>(base.size()) && running_;
             ++position) {
            if (base[position] == base[position + 1]) continue;
            swap_positions_[depth] = position;
            choose_swaps(base, used, count, depth + 1, position + 2);
        }
    }

    void simple(const std::string& base, int used) {
        if (!running_) return;
        insertions(base, used);
        if (!(config_.repeat || config_.del || config_.closecase)) return;
        const int maximum = std::min({config_.max_typos - used,
            config_.max_typos_simple, static_cast<int>(base.size())});
        for (int count = 1; count <= maximum && running_; ++count) {
            choose_simple(base, used, count, 0, 0);
        }
    }

    void choose_simple(const std::string& base, int used, int count,
                       int depth, int first) {
        if (!running_) return;
        if (depth == count) {
            simple_options(base, used, count, 0);
            return;
        }
        const int last = static_cast<int>(base.size()) - (count - depth);
        for (int position = first; position <= last && running_; ++position) {
            if (!config_.repeat && !config_.del
                && !is_case_transition(base, position)) continue;
            simple_positions_[depth] = position;
            choose_simple(base, used, count, depth + 1, position + 1);
        }
    }

    void simple_options(const std::string& base, int used, int count,
                        int depth) {
        if (!running_) return;
        if (depth == count) {
            auto& changed = buffers_[2];
            changed.clear();
            int choice = 0;
            for (int i = 0; i < static_cast<int>(base.size()); ++i) {
                if (choice < count && simple_positions_[choice] == i) {
                    uint8_t option = simple_options_[choice++];
                    if (option == 0) changed.append(2, base[i]);
                    else if (option == 2) changed.push_back(swap_case_ch(base[i]));
                } else changed.push_back(base[i]);
            }
            insertions(changed, used + count);
            return;
        }
        int position = simple_positions_[depth];
        if (config_.repeat) {
            simple_options_[depth] = 0;
            simple_options(base, used, count, depth + 1);
        }
        if (config_.del) {
            simple_options_[depth] = 1;
            simple_options(base, used, count, depth + 1);
        }
        if (config_.closecase && is_case_transition(base, position)) {
            simple_options_[depth] = 2;
            simple_options(base, used, count, depth + 1);
        }
    }

    void insertions(const std::string& base, int used) {
        output(base);
        if (!config_.insert || config_.insert_charset.empty()) return;
        const int maximum = std::min({config_.max_typos - used,
            config_.max_typos_insert, static_cast<int>(base.size()) + 1});
        for (int count = 1; count <= maximum && running_; ++count) {
            choose_insertions(base, count, 0, 0);
        }
    }

    void choose_insertions(const std::string& base, int count,
                           int depth, int first) {
        if (!running_) return;
        if (depth == count) {
            insertion_letters(base, count, 0);
            return;
        }
        for (int position = first;
             position <= static_cast<int>(base.size()) && running_;
             ++position) {
            insert_positions_[depth] = position;
            choose_insertions(base, count, depth + 1, position);
        }
    }

    void insertion_letters(const std::string& base, int count, int depth) {
        if (!running_) return;
        if (depth == count) {
            auto& changed = buffers_[3];
            changed.clear();
            int choice = 0;
            for (int position = 0;
                 position <= static_cast<int>(base.size()); ++position) {
                while (choice < count && insert_positions_[choice] == position) {
                    changed.push_back(insert_letters_[choice++]);
                }
                if (position < static_cast<int>(base.size())) {
                    changed.push_back(base[position]);
                }
            }
            output(changed);
            return;
        }
        for (char letter : config_.insert_charset) {
            if (!running_) return;
            insert_letters_[depth] = letter;
            insertion_letters(base, count, depth + 1);
        }
    }
};

template <class Emit>
static TypoStreamStats stream_typo_variants(
    const std::string& base, const TypoConfig& config, Emit& emit) {
    return TypoStream<Emit>(config, emit).run(base);
}
