#include "crc32.h"

static inline uint32_t crc32_update(uint32_t crc, uint8_t data)
{
    crc ^= data;
    for (int i = 0; i < 8; i++)
    {
        crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320UL : (crc >> 1);
    }
    return crc;
}

uint32_t crc32_compute(const uint8_t *data, size_t len)
{
    uint32_t crc = 0xFFFFFFFFUL;
    for (size_t i = 0; i < len; i++)
    {
        crc = crc32_update(crc, data[i]);
    }
    return crc ^ 0xFFFFFFFFUL;
}
