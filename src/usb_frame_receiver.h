#pragma once
#include <cstdint>

struct USBFrame
{
    const uint8_t *payload = nullptr;
    uint32_t payload_len = 0;
};

class USBFrameReceiver
{
public:
    explicit USBFrameReceiver(uint32_t expected_len);

    // Non-blocking; returns true when a full validated frame is ready in out
    bool poll(USBFrame &out);

    void send_ack_ok();
    void send_ack_err(uint8_t code);

private:
    enum class State : uint8_t
    {
        MAGIC,
        LEN,
        PAYLOAD,
        CRC
    };

    uint32_t expected_len_;
    State state_ = State::MAGIC;

    uint8_t magic_[4]{};
    uint32_t magic_pos_ = 0;

    uint8_t len_bytes_[4]{};
    uint32_t len_pos_ = 0;
    uint32_t frame_len_ = 0;

    uint32_t payload_pos_ = 0;
    uint32_t crc_rx_ = 0;
    uint8_t crc_bytes_[4]{};
    uint32_t crc_pos_ = 0;

    uint32_t crc_calc_ = 0;

    // buffer storage
    uint8_t *buf_ = nullptr;

    static uint32_t crc32_update(uint32_t crc, uint8_t data);
    static uint32_t crc32_finalize(uint32_t crc);
};
