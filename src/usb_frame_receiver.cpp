#include "usb_frame_receiver.h"
#include <cstdlib>
#include <cstring>
#include "pico/stdlib.h"

static constexpr uint8_t MAGIC[4] = {'M', 'W', 'F', '1'};

USBFrameReceiver::USBFrameReceiver(uint32_t expected_len)
    : expected_len_(expected_len)
{
    // Allocate once (fixed size)
    buf_ = (uint8_t *)malloc(expected_len_);
    // If malloc fails, you’ll crash later; expected_len_ is small (~27KB), should be fine.
    state_ = State::MAGIC;
}

static inline int read_byte_nonblocking()
{
    // getchar_timeout_us returns PICO_ERROR_TIMEOUT (-1) if no data
    return getchar_timeout_us(0);
}

bool USBFrameReceiver::poll(USBFrame &out)
{
    while (true)
    {
        int v = read_byte_nonblocking();
        if (v < 0)
            return false; // nothing available

        uint8_t b = (uint8_t)v;

        switch (state_)
        {
        case State::MAGIC:
            magic_[magic_pos_++] = b;
            if (magic_pos_ == 4)
            {
                if (memcmp(magic_, MAGIC, 4) == 0)
                {
                    state_ = State::LEN;
                    len_pos_ = 0;
                }
                else
                {
                    // shift window by 1 and keep searching
                    magic_[0] = magic_[1];
                    magic_[1] = magic_[2];
                    magic_[2] = magic_[3];
                    magic_pos_ = 3;
                }
            }
            break;

        case State::LEN:
            len_bytes_[len_pos_++] = b;
            if (len_pos_ == 4)
            {
                frame_len_ = (uint32_t)len_bytes_[0] | ((uint32_t)len_bytes_[1] << 8) | ((uint32_t)len_bytes_[2] << 16) | ((uint32_t)len_bytes_[3] << 24);

                if (frame_len_ != expected_len_)
                {
                    send_ack_err(0x01); // bad len
                    state_ = State::MAGIC;
                    magic_pos_ = 0;
                    break;
                }

                payload_pos_ = 0;
                crc_calc_ = 0xFFFFFFFFu;
                state_ = State::PAYLOAD;
            }
            break;

        case State::PAYLOAD:
            buf_[payload_pos_++] = b;
            crc_calc_ = crc32_update(crc_calc_, b);
            if (payload_pos_ == frame_len_)
            {
                state_ = State::CRC;
                crc_pos_ = 0;
            }
            break;

        case State::CRC:
            crc_bytes_[crc_pos_++] = b;
            if (crc_pos_ == 4)
            {
                crc_rx_ = (uint32_t)crc_bytes_[0] | ((uint32_t)crc_bytes_[1] << 8) | ((uint32_t)crc_bytes_[2] << 16) | ((uint32_t)crc_bytes_[3] << 24);

                uint32_t crc_ok = crc32_finalize(crc_calc_);

                if (crc_ok != crc_rx_)
                {
                    send_ack_err(0x02); // bad crc
                    state_ = State::MAGIC;
                    magic_pos_ = 0;
                    break;
                }

                out.payload = buf_;
                out.payload_len = frame_len_;

                // Prepare for next frame
                state_ = State::MAGIC;
                magic_pos_ = 0;

                return true;
            }
            break;
        }
    }
}

void USBFrameReceiver::send_ack_ok()
{
    // binary-safe 2-byte ACK
    putchar_raw('O');
    putchar_raw('K');
}

void USBFrameReceiver::send_ack_err(uint8_t code)
{
    putchar_raw('E');
    putchar_raw('R');
    putchar_raw((char)code);
}

uint32_t USBFrameReceiver::crc32_update(uint32_t crc, uint8_t data)
{
    crc ^= data;
    for (int i = 0; i < 8; i++)
    {
        uint32_t mask = -(crc & 1u);
        crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
    return crc;
}

uint32_t USBFrameReceiver::crc32_finalize(uint32_t crc)
{
    return ~crc;
}
