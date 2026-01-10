#pragma once
#include <cstdint>
#include <cstddef>

// Simple binary frame protocol (PC -> Pico)
//
// Header:
//   magic[4]   = 'M','P','F','B'
//   flags      = uint8   (bit0=normal, bit1=force_full)
//   reserved   = uint8   (0)
//   payload_len= uint32  (bytes)
//   crc32      = uint32  (of payload)
// Payload:
//   packed 1bpp frame bytes (row-major, MSB=left pixel)
//
// Notes:
// - We keep it minimal: full-frame only for now (26928 bytes).
// - Later you can add x/y/w/h for changed-rectangle streaming.

static constexpr uint8_t MPFB_MAGIC[4] = {'M', 'P', 'F', 'B'};

#pragma pack(push, 1)
struct MPFBHeader
{
    uint8_t magic[4];
    uint8_t flags;
    uint8_t reserved;
    uint32_t payload_len;
    uint32_t crc32;
};
#pragma pack(pop)

static constexpr uint8_t MPFB_FLAG_FORCE_FULL = 0x02;
