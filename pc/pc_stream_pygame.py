import argparse
import binascii
import struct
import time
import serial
import pygame

W, H = 792, 272
BYTES_PER_ROW = (W + 7) // 8
FRAME_BYTES = BYTES_PER_ROW * H


def pack_1bpp(surface: pygame.Surface, invert=False) -> bytes:
    rgb = pygame.image.tostring(surface, "RGB")
    fb = bytearray([0xFF]) * FRAME_BYTES  # white

    for y in range(H):
        row = y * W * 3
        out_row = y * BYTES_PER_ROW
        for x in range(W):
            r = rgb[row + 3 * x + 0]
            g = rgb[row + 3 * x + 1]
            b = rgb[row + 3 * x + 2]
            lum = (30 * r + 59 * g + 11 * b) // 100
            black = lum < 128
            if invert:
                black = not black
            if black:
                i = out_row + (x // 8)
                bit = 7 - (x % 8)
                fb[i] &= ~(1 << bit)
    return bytes(fb)


def build_packet(payload: bytes) -> bytes:
    magic = b"MWF1"
    ln = struct.pack("<I", len(payload))
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    return magic + ln + payload + struct.pack("<I", crc)


def wait_for_ok(ser: serial.Serial, timeout_s: float) -> bool:
    """
    Read bytes until we see b'OK' (in-stream), while not blocking pygame.
    """
    deadline = time.monotonic() + timeout_s
    last = bytearray()

    while time.monotonic() < deadline:
        # Keep pygame responsive even while waiting on serial
        pygame.event.pump()

        chunk = ser.read(64)  # non-blocking-ish due to ser.timeout
        if chunk:
            last += chunk
            if b"OK" in last:
                return True
            # keep buffer bounded
            if len(last) > 256:
                last = last[-256:]

        # tiny sleep prevents 100% CPU spin
        time.sleep(0.001)

    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--fps",
        type=float,
        default=0.2,
        help="Target send rate. Full refresh is slow; start low.",
    )
    ap.add_argument("--invert", action="store_true")
    ap.add_argument(
        "--ack-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for OK after a frame",
    )
    args = ap.parse_args()

    pygame.init()
    screen = pygame.display.set_mode((W, H))
    clock = pygame.time.Clock()

    # IMPORTANT: small timeout so reads don't freeze pygame
    with serial.Serial(args.port, args.baud, timeout=0.05, write_timeout=5) as ser:
        time.sleep(0.5)

        # Clear any boot text so we don't accidentally match old data
        ser.reset_input_buffer()

        x = 0
        vx = 12

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return

            screen.fill((255, 255, 255))
            pygame.draw.rect(screen, (0, 0, 0), pygame.Rect(x, 40, 120, 80), 0)
            pygame.display.flip()

            payload = pack_1bpp(screen, invert=args.invert)
            pkt = build_packet(payload)

            # Drain any stray text before sending (helps if anything prints)
            waiting = ser.in_waiting
            if waiting:
                ser.read(waiting)

            ser.write(pkt)
            ser.flush()

            ok = wait_for_ok(ser, args.ack_timeout)
            if not ok:
                print("ACK timeout (no OK).")
                # Resync: flush input so next frame starts clean
                ser.reset_input_buffer()

            x += vx
            if x < 0 or x + 120 > W:
                vx = -vx

            clock.tick(args.fps)


if __name__ == "__main__":
    main()
