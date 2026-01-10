#pragma once

#include <cstddef>
#include <cstdint>

#include "pico/stdlib.h"
#include "hardware/spi.h"

class SSD1683_GDEY0579T93
{
public:
    static constexpr int WIDTH = 792;
    static constexpr int HEIGHT = 272;

    static constexpr int BYTES_PER_ROW = (WIDTH + 7) / 8;      // 99
    static constexpr int FRAME_BYTES = BYTES_PER_ROW * HEIGHT; // 26928

    // Master is 400px (50 bytes), Slave is 400px (50 bytes) with a 1-byte overlap
    // so 50 + 50 - 1 = 99 bytes total (792px).
    static constexpr int MASTER_COLS = 50; // bytes (global cols 0..49)
    static constexpr int SLAVE_COLS = 50;  // bytes (slave internal 0x31..0x00)
    static constexpr int SLAVE_START = 49; // overlap byte index (global col 49)

    SSD1683_GDEY0579T93(spi_inst_t *spi,
                        uint pin_cs, uint pin_dc, uint pin_rst, uint pin_busy,
                        uint pin_sck, uint pin_mosi,
                        bool busy_active_high = true);

    void init(uint32_t spi_hz);

    // Full-screen write in the vendor "column-major" order, but from a row-major buffer.
    // frame format: row-major, top row first, MSB = left pixel in each byte.
    void show_full_fullscreen(const uint8_t *frame);

    // Fullscreen partial (still writes full framebuffer but uses partial update waveform)
    // new_frame/old_frame are full 792x272 row-major 1bpp.
    void show_partial_fullscreen(const uint8_t *new_frame, const uint8_t *old_frame);

    // Smart partial window:
    // - (x,y,w,h) in pixels, MUST be byte-aligned on x and w (multiples of 8).
    // - rect_new is row-major for just the rectangle: width_bytes = w/8, height = h.
    // - old_full is the previous full framebuffer (792x272 row-major) used to fill OLD buffer.
    void show_partial_window(uint16_t x, uint16_t y, uint16_t w, uint16_t h,
                             const uint8_t *rect_new,
                             const uint8_t *old_full);

    void clear_to_white();

    // Busy wait (true = success)
    bool wait_idle(uint32_t timeout_ms);

private:
    spi_inst_t *spi_;
    uint cs_, dc_, rst_, busy_, sck_, mosi_;
    bool busy_active_high_;
    bool inited_ = false;

    // Tune these if black/white is flipped on your glass
    static constexpr bool INVERT_BYTES = false; // set true if white/black are swapped
    static constexpr bool BIT_REVERSE = false;  // set true if each byte looks bit-mirrored

    void cs_select_(bool en);
    void dc_cmd_();
    void dc_data_();

    void write_u8_(uint8_t v);
    void write_bytes_(const uint8_t *data, size_t n);

    void cmd_(uint8_t c);
    void data_(uint8_t d);
    void reset_();

    static uint8_t bitrev8_(uint8_t x);
    static uint8_t xform_(uint8_t b);

    // Vendor-style address setup (matches demo code)
    void master_addr_setup_();
    void slave_addr_setup_();

    // Window setup helpers
    void master_window_(uint8_t x_start, uint8_t x_end, uint16_t y_top, uint16_t y_bottom);
    void slave_window_(uint8_t slave_x_start, uint8_t slave_x_end, uint16_t y_top, uint16_t y_bottom);

    // Update triggers
    void update_full_();
    void update_partial_();

    // Write a master/slave window from rect_new (NEW) and old_full (OLD)
    void write_master_window_new_old_(uint8_t x_start, uint8_t x_end,
                                      uint16_t y_top, uint16_t y_bottom,
                                      uint16_t rect_xb, uint16_t rect_y,
                                      uint16_t rect_wb,
                                      const uint8_t *rect_new,
                                      const uint8_t *old_full);

    void write_slave_window_new_old_(uint8_t gcol_start, uint8_t gcol_end,
                                     uint16_t y_top, uint16_t y_bottom,
                                     uint16_t rect_xb, uint16_t rect_y,
                                     uint16_t rect_wb,
                                     const uint8_t *rect_new,
                                     const uint8_t *old_full);
};
