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

// Flags
static constexpr uint8_t FLAG_FORCE_FULL = 0x01;
static constexpr uint8_t FLAG_RECT = 0x02;

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

static inline uint16_t u16le(const uint8_t b[2])
{
    return (uint16_t)b[0] | ((uint16_t)b[1] << 8);
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

    // Start clean
    static uint8_t prev_frame[FRAME_BYTES];
    memset(prev_frame, 0xFF, FRAME_BYTES);
    epd.clear_to_white();

    // Parser state: sync on "MWF1"
    uint8_t sync[4] = {0, 0, 0, 0};

    // Max payload we accept:
    // - full: 1 + FRAME_BYTES
    // - rect: 1 + 8 + FRAME_BYTES (worst case)
    static uint8_t payload_buf[FRAME_BYTES + 9];

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
        if (len == 0 || len > (uint32_t)(FRAME_BYTES + 9))
            continue;

        if (!read_exact(payload_buf, len, 8000))
            continue;

        // CRC
        uint8_t crc_b[4];
        if (!read_exact(crc_b, 4, 2000))
            continue;

        uint32_t want_crc = u32le(crc_b);
        uint32_t got_crc = crc32_ieee(payload_buf, len);
        if (want_crc != got_crc)
            continue;

        uint8_t flags = payload_buf[0];
        const bool force_full = (flags & FLAG_FORCE_FULL) != 0;
        const bool is_rect = (flags & FLAG_RECT) != 0;

        if (!is_rect)
        {
            // Full-frame payloads:
            // legacy: len == FRAME_BYTES  (no flags) -> not supported here (we always send flags now)
            // new:    len == 1 + FRAME_BYTES
            if (len != (uint32_t)(1 + FRAME_BYTES))
                continue;

            const uint8_t *frame_ptr = payload_buf + 1;

            if (force_full)
            {
                epd.clear_to_white();
                epd.show_full_fullscreen(frame_ptr);
            }
            else
            {
                epd.show_partial_fullscreen(frame_ptr, prev_frame);
            }

            memcpy(prev_frame, frame_ptr, FRAME_BYTES);
        }
        else
        {
            // Rect payload: [flags][x:u16][y:u16][w:u16][h:u16][rect_bytes]
            if (len < 1 + 8)
                continue;

            const uint8_t *p = payload_buf + 1;
            uint16_t x = u16le(p + 0);
            uint16_t y = u16le(p + 2);
            uint16_t w = u16le(p + 4);
            uint16_t h = u16le(p + 6);
            const uint8_t *rect = payload_buf + 1 + 8;

            if ((x & 7u) != 0 || (w & 7u) != 0)
                continue; // must be byte aligned

            if (w == 0 || h == 0)
                continue;

            if (x >= EPD_W || y >= EPD_H)
                continue;

            if ((uint32_t)x + (uint32_t)w > (uint32_t)EPD_W)
                w = (uint16_t)(EPD_W - x);
            if ((uint32_t)y + (uint32_t)h > (uint32_t)EPD_H)
                h = (uint16_t)(EPD_H - y);

            const uint16_t wb = (uint16_t)(w / 8);
            const uint32_t need = (uint32_t)1 + 8 + (uint32_t)wb * (uint32_t)h;
            if (len != need)
                continue;

            // If force_full is requested alongside a rect, we can:
            // - patch prev_frame, then clear+full draw the composed frame.
            if (force_full)
            {
                for (uint16_t yy = 0; yy < h; ++yy)
                {
                    uint8_t *dst = prev_frame + (uint32_t)(y + yy) * BYTES_PER_ROW + (x / 8);
                    const uint8_t *src = rect + (uint32_t)yy * wb;
                    memcpy(dst, src, wb);
                }

                epd.clear_to_white();
                epd.show_full_fullscreen(prev_frame);
            }
            else
            {
                epd.show_partial_window(x, y, w, h, rect, prev_frame);

                // Patch prev_frame to the new state
                for (uint16_t yy = 0; yy < h; ++yy)
                {
                    uint8_t *dst = prev_frame + (uint32_t)(y + yy) * BYTES_PER_ROW + (x / 8);
                    const uint8_t *src = rect + (uint32_t)yy * wb;
                    memcpy(dst, src, wb);
                }
            }
        }

        send_ok();
        gpio_put(LED_PIN, !gpio_get(LED_PIN));
    }
}
