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
        {
            return false;
        }
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
    // Vendor partial update control (matches the pattern in the Arduino demo)
    cmd_(0x22);
    data_(0xFF);
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
    for (int col = 0; col < MASTER_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y);
            uint8_t b = frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    cmd_(0x26); // old buffer (clear)
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

    cmd_(0xA6); // old buffer (clear)
    for (int i = 0; i < SLAVE_COLS * HEIGHT; ++i)
        data_(0x00);

    update_full_();
}

void SSD1683_GDEY0579T93::show_partial_fullscreen(const uint8_t *new_frame, const uint8_t *old_frame)
{
    if (!inited_ || !new_frame || !old_frame)
        return;

    // Optional vendor "partial pre-step" (seen in Arduino EPD_Dis_Part_*):
    cmd_(0x22);
    data_(0xC0);
    cmd_(0x20);
    wait_idle(5000);

    // -------- MASTER --------
    master_addr_setup_();
    wait_idle(5000);

    // OLD -> 0x26
    cmd_(0x26);
    for (int col = 0; col < MASTER_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y);
            uint8_t b = old_frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    // NEW -> 0x24
    cmd_(0x24);
    for (int col = 0; col < MASTER_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y);
            uint8_t b = new_frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    // -------- SLAVE --------
    slave_addr_setup_();
    wait_idle(5000);

    // OLD -> 0xA6
    cmd_(0xA6);
    for (int col = SLAVE_START; col < SLAVE_START + SLAVE_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y);
            uint8_t b = old_frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    // NEW -> 0xA4
    cmd_(0xA4);
    for (int col = SLAVE_START; col < SLAVE_START + SLAVE_COLS; ++col)
    {
        for (int y = 0; y < HEIGHT; ++y)
        {
            int src_row = (HEIGHT - 1 - y);
            uint8_t b = new_frame[src_row * BYTES_PER_ROW + col];
            data_(xform_(b));
        }
    }

    update_partial_();
}
