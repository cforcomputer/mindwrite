import argparse
import struct
import time
import serial

W, H = 792, 272
BYTES_PER_ROW = (W + 7) // 8  # 99
FRAME_BYTES = BYTES_PER_ROW * H


def set_pixel(buf, x, y, black):
    if x < 0 or x >= W or y < 0 or y >= H:
        return
    i = y * BYTES_PER_ROW + (x // 8)
    bit = 7 - (x % 8)
    if black:
        buf[i] &= ~(1 << bit)
    else:
        buf[i] |= 1 << bit


def make_pattern():
    fb = bytearray([0xFF]) * FRAME_BYTES

    # border
    for x in range(W):
        set_pixel(fb, x, 0, True)
        set_pixel(fb, x, H - 1, True)
    for y in range(H):
        set_pixel(fb, 0, y, True)
        set_pixel(fb, W - 1, y, True)

    # big checker squares
    for y in range(H):
        for x in range(W):
            black = ((x // 24) + (y // 24)) % 2 == 0
            if black and 40 < x < W - 40 and 20 < y < H - 20:
                set_pixel(fb, x, y, True)

    return bytes(fb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    frame = make_pattern()
    pkt = b"MWFR" + struct.pack("<I", len(frame)) + frame

    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        time.sleep(1.0)
        ser.write(pkt)
        ser.flush()
        print("sent", len(pkt), "bytes payload", len(frame))


if __name__ == "__main__":
    main()
