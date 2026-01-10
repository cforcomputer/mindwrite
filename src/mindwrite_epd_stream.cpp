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

static void blink_status(uint pin, int times, int ms)
{
    for (int i = 0; i < times; i++)
    {
        gpio_put(pin, 1);
        sleep_ms(ms);
        gpio_put(pin, 0);
        sleep_ms(ms);
    }
}

static void make_test_pattern(uint8_t *fb)
{
    memset(fb, 0xFF, FRAME_BYTES); // white

    // chunky checkerboard (very obvious if mapping is correct)
    for (int y = 0; y < EPD_H; ++y)
    {
        for (int x = 0; x < EPD_W; ++x)
        {
            bool black = (((x / 24) + (y / 24)) % 2) == 0;
            if (black)
            {
                int byte_i = y * BYTES_PER_ROW + (x / 8);
                int bit = 7 - (x % 8);
                fb[byte_i] &= ~(1u << bit); // 0 = black
            }
        }
    }
}

// --- CRC32 (bitwise, dependency-free) ---
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
    // Clean 2-byte ACK; no newline.
    putchar_raw('O');
    putchar_raw('K');
    stdio_flush();
}

static inline uint32_t u32le(const uint8_t b[4])
{
    return (uint32_t)b[0] |
           ((uint32_t)b[1] << 8) |
           ((uint32_t)b[2] << 16) |
           ((uint32_t)b[3] << 24);
}

int main()
{
    stdio_init_all();
    sleep_ms(1200); // let USB enumerate

    const uint LED_PIN = 25;
    gpio_init(LED_PIN);
    gpio_set_dir(LED_PIN, GPIO_OUT);
    gpio_put(LED_PIN, 0);

    // Keep prints minimal. Anything you print can appear in the same stream the PC reads.
    printf("mindwrite_epd_stream boot\n");
    stdio_flush();

    // NOTE: match your driver constructor signature.
    // If your header requires an extra bool, keep it. If not, remove it.
    SSD1683_GDEY0579T93 epd(
        spi0,
        PIN_CS, PIN_DC, PIN_RST, PIN_BUSY,
        PIN_SCK, PIN_MOSI,
        true);

    epd.init(SPI_HZ);

    // Boot pattern once (proves display works independent of streaming)
    static uint8_t boot_fb[FRAME_BYTES];
    make_test_pattern(boot_fb);
    epd.show_full_fullscreen(boot_fb);
    blink_status(LED_PIN, 2, 80);

    // Streaming buffer
    static uint8_t frame[FRAME_BYTES];

    // Parser state: sync on "MWF1"
    uint8_t sync[4] = {0, 0, 0, 0};

    while (true)
    {
        // Shift in bytes until we match "MWF1"
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

        // Read length (4), payload (len), crc (4)
        uint8_t len_b[4];
        if (!read_exact(len_b, 4, 2000))
            continue;

        uint32_t len = u32le(len_b);
        if (len != (uint32_t)FRAME_BYTES)
        {
            // wrong length -> drop frame, resync
            continue;
        }

        if (!read_exact(frame, FRAME_BYTES, 5000))
            continue;

        uint8_t crc_b[4];
        if (!read_exact(crc_b, 4, 2000))
            continue;

        uint32_t want_crc = u32le(crc_b);
        uint32_t got_crc = crc32_ieee(frame, FRAME_BYTES);
        if (want_crc != got_crc)
        {
            // bad frame -> ignore, resync
            blink_status(LED_PIN, 2, 40);
            continue;
        }

        // Full refresh (slow). When done, ACK OK so host paces itself.
        epd.show_full_fullscreen(frame);
        send_ok();
        blink_status(LED_PIN, 1, 20);
    }
}
