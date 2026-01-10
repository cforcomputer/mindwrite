#include "ssd1683_gdey0579t93.h"

#include <cstring>

SSD1683_GDEY0579T93::SSD1683_GDEY0579T93(spi_inst_t *spi,
                                         uint pin_cs, uint pin_dc, uint pin_rst, uint pin_busy,
                                         uint pin_sck, uint pin_mosi,
                                         bool busy_active_high)
    : spi_(spi),
      cs_(pin_cs), dc_(pin_dc), rst_(pin_rst), busy_(pin_busy),
      sck_(pin_sck), mosi_(pin_mosi),
      busy_active_high_(busy_active_high) {}

void SSD1683_GDEY0579T93::cs_select_(bool en) { gpio_put(cs_, en ? 0 : 1); }
void SSD1683_GDEY0579T93::dc_cmd_() { gpio_put(dc_, 0); }
void SSD1683_GDEY0579T93::dc_data_() { gpio_put(dc_, 1); }

void SSD1683_GDEY0579T93::write_u8_(uint8_t v) { spi_write_blocking(spi_, &v, 1); }
void SSD1683_GDEY0579T93::write_bytes_(const uint8_t *data, size_t n) { spi_write_blocking(spi_, data, n); }

void SSD1683_GDEY0579T93::cmd_(uint8_t c)
{
    cs_select_(true);
    dc_cmd_();
    write_u8_(c);
    cs_select_(false);
}

void SSD1683_GDEY0579T93::data_(uint8_t d)
{
    cs_select_(true);
    dc_data_();
    write_u8_(d);
    cs_select_(false);
}

void SSD1683_GDEY0579T93::reset_()
{
    gpio_put(rst_, 0);
    sleep_ms(10);
    gpio_put(rst_, 1);
    sleep_ms(10);
}

bool SSD1683_GDEY0579T93::wait_idle(uint32_t timeout_ms)
{
    absolute_time_t start = get_absolute_time();
    while (true)
    {
        int raw = gpio_get(busy_);
        bool busy = busy_active_high_ ? (raw == 1) : (raw == 0);
        if (!busy)
            return true;

        if (absolute_time_diff_us(start, get_absolute_time()) > (int64_t)timeout_ms * 1000)
            return false;

        sleep_ms(5);
    }
}

uint8_t SSD1683_GDEY0579T93::bitrev8_(uint8_t x)
{
    x = (uint8_t)((x >> 4) | (x << 4));
    x = (uint8_t)(((x & 0xCC) >> 2) | ((x & 0x33) << 2));
    x = (uint8_t)(((x & 0xAA) >> 1) | ((x & 0x55) << 1));
    return x;
}

uint8_t SSD1683_GDEY0579T93::xform_(uint8_t b)
{
    if (BIT_REVERSE)
        b = bitrev8_(b);
    if (INVERT_BYTES)
        b = (uint8_t)~b;
    return b;
}

void SSD1683_GDEY0579T93::update_full_()
{
    cmd_(0x22);
    data_(0xF7);
    cmd_(0x20);
    wait_idle(20000);
}

void SSD1683_GDEY0579T93::update_partial_()
{
    cmd_(0x22);
    data_(0xFF); // partial update waveform (matches vendor EPD_Part_Update)
    cmd_(0x20);
    wait_idle(20000);
}

void SSD1683_GDEY0579T93::init(uint32_t spi_hz)
{
    gpio_init(cs_);
    gpio_set_dir(cs_, GPIO_OUT);
    gpio_put(cs_, 1);
    gpio_init(dc_);
    gpio_set_dir(dc_, GPIO_OUT);
    gpio_put(dc_, 0);
    gpio_init(rst_);
    gpio_set_dir(rst_, GPIO_OUT);
    gpio_put(rst_, 1);
    gpio_init(busy_);
    gpio_set_dir(busy_, GPIO_IN);

    spi_init(spi_, spi_hz);
    spi_set_format(spi_, 8, SPI_CPOL_0, SPI_CPHA_0, SPI_MSB_FIRST);
    gpio_set_function(sck_, GPIO_FUNC_SPI);
    gpio_set_function(mosi_, GPIO_FUNC_SPI);

    sleep_ms(20);
    reset_();

    cmd_(0x12); // SWRESET
    wait_idle(5000);

    cmd_(0x3C); // Border waveform
    data_(0x80);

    cmd_(0x18); // Temp sensor
    data_(0x80);

    inited_ = true;
}

// Matches the Arduino demo's MASTER setup (Set_ramMP + Set_ramMA)
void SSD1683_GDEY0579T93::master_addr_setup_()
{
    cmd_(0x11);  // Data entry mode
    data_(0x05); // Y decrement, X increment

    cmd_(0x44); // X window
    data_(0x00);
    data_(0x31); // 0..49 (50 bytes)

    cmd_(0x45);  // Y window
    data_(0x0F); // 0x010F = 271
    data_(0x01);
    data_(0x00);
    data_(0x00);

    cmd_(0x4E); // X cursor
    data_(0x00);

    cmd_(0x4F); // Y cursor
    data_(0x0F);
    data_(0x01);
}

// Matches the Arduino demo's SLAVE setup (Set_ramSP + Set_ramSA)
void SSD1683_GDEY0579T93::slave_addr_setup_()
{
    cmd_(0x91);
    data_(0x04);

    cmd_(0xC4); // X window (reverse)
    data_(0x31);
    data_(0x00);

    cmd_(0xC5); // Y window
    data_(0x0F);
    data_(0x01);
    data_(0x00);
    data_(0x00);

    cmd_(0xCE); // X cursor
    data_(0x31);

    cmd_(0xCF); // Y cursor
    data_(0x0F);
    data_(0x01);
}

// Window setup for MASTER (global x bytes)
void SSD1683_GDEY0579T93::master_window_(uint8_t x_start, uint8_t x_end, uint16_t y_top, uint16_t y_bottom)
{
    // y_top <= y_bottom, in pixel rows [0..271]
    // Controller wants Y start high (bottom) and decrements down to end (top).
    cmd_(0x11);
    data_(0x05); // Y dec, X inc

    cmd_(0x44);
    data_(x_start);
    data_(x_end);

    cmd_(0x45);
    data_((uint8_t)(y_bottom & 0xFF));
    data_((uint8_t)(y_bottom >> 8));
    data_((uint8_t)(y_top & 0xFF));
    data_((uint8_t)(y_top >> 8));

    cmd_(0x4E);
    data_(x_start);

    cmd_(0x4F);
    data_((uint8_t)(y_bottom & 0xFF));
    data_((uint8_t)(y_bottom >> 8));
}

// Window setup for SLAVE (slave internal x bytes)
void SSD1683_GDEY0579T93::slave_window_(uint8_t slave_x_start, uint8_t slave_x_end, uint16_t y_top, uint16_t y_bottom)
{
    cmd_(0x91);
    data_(0x04);

    cmd_(0xC4);
    data_(slave_x_start);
    data_(slave_x_end);

    cmd_(0xC5);
    data_((uint8_t)(y_bottom & 0xFF));
    data_((uint8_t)(y_bottom >> 8));
    data_((uint8_t)(y_top & 0xFF));
    data_((uint8_t)(y_top >> 8));

    cmd_(0xCE);
    data_(slave_x_start);

    cmd_(0xCF);
    data_((uint8_t)(y_bottom & 0xFF));
    data_((uint8_t)(y_bottom >> 8));
}

void SSD1683_GDEY0579T93::write_master_window_new_old_(uint8_t x_start, uint8_t x_end,
                                                       uint16_t y_top, uint16_t y_bottom,
                                                       uint16_t rect_xb, uint16_t rect_y,
                                                       uint16_t rect_wb,
                                                       const uint8_t *rect_new,
                                                       const uint8_t *old_full)
{
    // NEW -> 0x24
    cmd_(0x24);
    for (uint16_t gcol = x_start; gcol <= x_end; ++gcol)
    {
        for (int yy = (int)y_bottom; yy >= (int)y_top; --yy)
        {
            const uint16_t local_y = (uint16_t)(yy - rect_y);
            const uint16_t local_xb = (uint16_t)(gcol - rect_xb);
            uint8_t bnew = rect_new[local_y * rect_wb + local_xb];
            data_(xform_(bnew));
        }
    }

    // OLD -> 0x26
    cmd_(0x26);
    for (uint16_t gcol = x_start; gcol <= x_end; ++gcol)
    {
        for (int yy = (int)y_bottom; yy >= (int)y_top; --yy)
        {
            uint8_t bold = old_full[(uint16_t)yy * BYTES_PER_ROW + (uint16_t)gcol];
            data_(xform_(bold));
        }
    }
}

void SSD1683_GDEY0579T93::write_slave_window_new_old_(uint8_t gcol_start, uint8_t gcol_end,
                                                      uint16_t y_top, uint16_t y_bottom,
                                                      uint16_t rect_xb, uint16_t rect_y,
                                                      uint16_t rect_wb,
                                                      const uint8_t *rect_new,
                                                      const uint8_t *old_full)
{
    // NEW -> 0xA4
    cmd_(0xA4);
    for (uint16_t gcol = gcol_start; gcol <= gcol_end; ++gcol)
    {
        for (int yy = (int)y_bottom; yy >= (int)y_top; --yy)
        {
            const uint16_t local_y = (uint16_t)(yy - rect_y);
            const uint16_t local_xb = (uint16_t)(gcol - rect_xb);
            uint8_t bnew = rect_new[local_y * rect_wb + local_xb];
            data_(xform_(bnew));
        }
    }

    // OLD -> 0xA6
    cmd_(0xA6);
    for (uint16_t gcol = gcol_start; gcol <= gcol_end; ++gcol)
    {
        for (int yy = (int)y_bottom; yy >= (int)y_top; --yy)
        {
            uint8_t bold = old_full[(uint16_t)yy * BYTES_PER_ROW + (uint16_t)gcol];
            data_(xform_(bold));
        }
    }
}

void SSD1683_GDEY0579T93::show_partial_window(uint16_t x, uint16_t y, uint16_t w, uint16_t h,
                                              const uint8_t *rect_new,
                                              const uint8_t *old_full)
{
    if (!inited_ || !rect_new || !old_full)
        return;

    // Require byte-aligned x and w
    if ((x & 7u) != 0 || (w & 7u) != 0)
        return;

    if (w == 0 || h == 0)
        return;

    if (x >= WIDTH || y >= HEIGHT)
        return;

    // Clamp to screen
    if (x + w > WIDTH)
        w = (uint16_t)(WIDTH - x);
    if (y + h > HEIGHT)
        h = (uint16_t)(HEIGHT - y);

    const uint16_t rect_xb = (uint16_t)(x / 8);
    const uint16_t rect_wb = (uint16_t)(w / 8);

    const uint16_t y_top = y;
    const uint16_t y_bottom = (uint16_t)(y + h - 1);

    const uint16_t x_endb = (uint16_t)(rect_xb + rect_wb - 1);

    // MASTER range: global cols 0..49
    uint16_t m_start = rect_xb;
    uint16_t m_end = x_endb;
    bool do_master = true;
    if (m_start > 49)
        do_master = false;
    if (m_end > 49)
        m_end = 49;

    // SLAVE range: global cols 49..98
    uint16_t s_start = rect_xb;
    uint16_t s_end = x_endb;
    bool do_slave = true;
    if (s_end < SLAVE_START)
        do_slave = false;
    if (s_start < SLAVE_START)
        s_start = SLAVE_START;
    if (s_end > 98)
        s_end = 98;

    // MASTER write
    if (do_master)
    {
        master_window_((uint8_t)m_start, (uint8_t)m_end, y_top, y_bottom);
        wait_idle(5000);
        write_master_window_new_old_((uint8_t)m_start, (uint8_t)m_end,
                                     y_top, y_bottom,
                                     rect_xb, y, rect_wb,
                                     rect_new, old_full);
    }

    // SLAVE write (global->slave x mapping: slave_x = 0x31 - (gcol - 49))
    if (do_slave)
    {
        const uint8_t slave_x_start = (uint8_t)(0x31 - (uint8_t)(s_start - SLAVE_START));
        const uint8_t slave_x_end = (uint8_t)(0x31 - (uint8_t)(s_end - SLAVE_START));

        slave_window_(slave_x_start, slave_x_end, y_top, y_bottom);
        wait_idle(5000);
        write_slave_window_new_old_((uint8_t)s_start, (uint8_t)s_end,
                                    y_top, y_bottom,
                                    rect_xb, y, rect_wb,
                                    rect_new, old_full);
    }

    update_partial_();
}

void SSD1683_GDEY0579T93::show_partial_fullscreen(const uint8_t *new_frame, const uint8_t *old_frame)
{
    // Use windowed partial with full region (x=0, w=WIDTH) and rect buffer == full frame
    if (!new_frame || !old_frame)
        return;
    show_partial_window(0, 0, WIDTH, HEIGHT, new_frame, old_frame);
}

void SSD1683_GDEY0579T93::clear_to_white()
{
    static uint8_t white[FRAME_BYTES];
    memset(white, 0xFF, sizeof(white));
    show_full_fullscreen(white);
}

void SSD1683_GDEY0579T93::show_full_fullscreen(const uint8_t *frame)
{
    if (!inited_ || !frame)
        return;

    // -------- MASTER --------
    master_addr_setup_();
    wait_idle(5000);

    cmd_(0x24);

    // Column-major (X outer), Y decrements from 271 to 0.
    for (int col = 0; col < MASTER_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y); // maps controller y order (271..0)
            uint8_t b = frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    // OLD buffer for full refresh isn't critical; clear to 0
    cmd_(0x26);
    for (int i = 0; i < MASTER_COLS * HEIGHT; ++i)
        data_(0x00);

    // -------- SLAVE --------
    slave_addr_setup_();
    wait_idle(5000);

    cmd_(0xA4);

    for (int col = SLAVE_START; col < SLAVE_START + SLAVE_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y);
            uint8_t b = frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    cmd_(0xA6);
    for (int i = 0; i < SLAVE_COLS * HEIGHT; ++i)
        data_(0x00);

    update_full_();
}
