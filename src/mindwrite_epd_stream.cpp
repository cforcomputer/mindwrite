#include <cstdio>
#include <cstring>
#include <cstdint>

#include "pico/stdlib.h"
#include "pico/stdio.h"

#include "hardware/gpio.h"
#include "hardware/spi.h"

#include "epd/ssd1683_gdey0579t93.h"

// Panel: 792x272, 1bpp
static constexpr int EPD_W = 792;
static constexpr int EPD_H = 272;
static constexpr int BYTES_PER_ROW = (EPD_W + 7) / 8;     // 99
static constexpr int FRAME_BYTES = BYTES_PER_ROW * EPD_H; // 26928

// ========= PIN MAP (edit to match your wiring) =========
static constexpr uint PIN_CS = 17;
static constexpr uint PIN_DC = 20;
static constexpr uint PIN_RST = 21;
static constexpr uint PIN_BUSY = 22;

static constexpr uint PIN_SCK = 18;  // SPI0 SCK
static constexpr uint PIN_MOSI = 19; // SPI0 TX (MOSI)
// ======================================================

static constexpr uint32_t SPI_HZ = 20'000'000;

// --- CRC32 (IEEE, bitwise) ---
static uint32_t crc32_ieee(const uint8_t *data, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
    {
        crc ^= data[i];
        for (int k = 0; k < 8; k++)
        {
            uint32_t mask = -(crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

static inline uint32_t u32le(const uint8_t b[4])
{
    return (uint32_t)b[0] |
           ((uint32_t)b[1] << 8) |
           ((uint32_t)b[2] << 16) |
           ((uint32_t)b[3] << 24);
}

// Read exactly n bytes from USB CDC via getchar_timeout_us.
// Returns false if it times out.
static bool read_exact(uint8_t *dst, size_t n, uint32_t timeout_ms)
{
    absolute_time_t deadline = make_timeout_time_ms(timeout_ms);
    size_t got = 0;

    while (got < n)
    {
        int c = getchar_timeout_us(1000); // 1ms poll
        if (c >= 0)
        {
            dst[got++] = (uint8_t)c;
            continue;
        }

        if (absolute_time_diff_us(get_absolute_time(), deadline) <= 0)
            return false;
    }

    return true;
}

static inline void send_ok()
{
    putchar_raw('O');
    putchar_raw('K');
    stdio_flush();
}

static void make_test_pattern(uint8_t *fb)
{
    memset(fb, 0xFF, FRAME_BYTES); // white

    // chunky checkerboard
    for (int y = 0; y < EPD_H; ++y)
    {
        for (int x = 0; x < EPD_W; ++x)
        {
            bool black = (((x / 24) + (y / 24)) % 2) == 0;
            if (black)
            {
                int byte_i = y * BYTES_PER_ROW + (x / 8);
                int bit = 7 - (x % 8);
                fb[byte_i] &= ~(1u << bit);
            }
        }
    }
}

int main()
{
    stdio_init_all();
    sleep_ms(1200); // let USB enumerate

    const uint LED_PIN = 25;
    gpio_init(LED_PIN);
    gpio_set_dir(LED_PIN, GPIO_OUT);
    gpio_put(LED_PIN, 0);

    printf("mindwrite_epd_stream boot\n");
    stdio_flush();

    SSD1683_GDEY0579T93 epd(
        spi0,
        PIN_CS, PIN_DC, PIN_RST, PIN_BUSY,
        PIN_SCK, PIN_MOSI,
        true);

    epd.init(SPI_HZ);

    // Boot pattern (full refresh) once
    static uint8_t prev_frame[FRAME_BYTES];
    make_test_pattern(prev_frame);
    epd.show_full_fullscreen(prev_frame);

    // Streaming buffer
    static uint8_t frame[FRAME_BYTES];

    // Parser state: sync on "MWF1"
    uint8_t sync[4] = {0, 0, 0, 0};

    while (true)
    {
        int c = getchar_timeout_us(1000);
        if (c < 0)
        {
            tight_loop_contents();
            continue;
        }

        sync[0] = sync[1];
        sync[1] = sync[2];
        sync[2] = sync[3];
        sync[3] = (uint8_t)c;

        if (!(sync[0] == 'M' && sync[1] == 'W' && sync[2] == 'F' && sync[3] == '1'))
            continue;

        // length
        uint8_t len_b[4];
        if (!read_exact(len_b, 4, 2000))
            continue;

        uint32_t len = u32le(len_b);

        // We support:
        // - legacy: len == FRAME_BYTES (payload = frame)
        // - new:    len == FRAME_BYTES + 1 (payload = flags + frame)
        uint8_t flags = 0;

        if (len != (uint32_t)FRAME_BYTES && len != (uint32_t)(FRAME_BYTES + 1))
        {
            continue; // resync
        }

        static uint8_t payload_buf[FRAME_BYTES + 1];
        if (!read_exact(payload_buf, len, 8000))
            continue;

        uint8_t crc_b[4];
        if (!read_exact(crc_b, 4, 2000))
            continue;

        uint32_t want_crc = u32le(crc_b);
        uint32_t got_crc = crc32_ieee(payload_buf, len);
        if (want_crc != got_crc)
            continue;

        const uint8_t *frame_ptr = nullptr;
        if (len == (uint32_t)FRAME_BYTES)
        {
            flags = 0; // legacy defaults to partial
            frame_ptr = payload_buf;
        }
        else
        {
            flags = payload_buf[0];
            frame_ptr = payload_buf + 1;
        }

        memcpy(frame, frame_ptr, FRAME_BYTES);

        const bool force_full = (flags & 0x01u) != 0;

        if (force_full)
        {
            epd.show_full_fullscreen(frame);
        }
        else
        {
            epd.show_partial_fullscreen(frame, prev_frame);
        }

        memcpy(prev_frame, frame, FRAME_BYTES);

        send_ok();
        gpio_put(LED_PIN, !gpio_get(LED_PIN));
    }
}
