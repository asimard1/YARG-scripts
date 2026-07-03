from pathlib import Path

from PIL import Image
import texture2ddecoder


# ==========================================================
# Configuration
# ==========================================================

INPUT_DIR = Path(".")

WIDTH = 256
HEIGHT = 256

HEADER_SIZE = 32
BC1_BLOCK_SIZE = 8


# ==========================================================
# Helpers
# ==========================================================

def fix_png_xbox_bc1_layout(data: bytes) -> bytes:
    """
    Convert Xbox 360 BC1 blocks into standard BC1 blocks by:
      - swapping the RGB565 endpoints
      - swapping the two lookup bytes
    """
    out = bytearray(len(data))

    for i in range(0, len(data), BC1_BLOCK_SIZE):

        block = bytearray(data[i:i + BC1_BLOCK_SIZE])

        if len(block) < BC1_BLOCK_SIZE:
            break

        # RGB565 endpoints
        block[0], block[1] = block[1], block[0]
        block[2], block[3] = block[3], block[2]

        # Lookup table
        block[4], block[5] = block[5], block[4]
        block[6], block[7] = block[7], block[6]

        out[i:i + BC1_BLOCK_SIZE] = block

    return bytes(out)


# ==========================================================
# Decoder
# ==========================================================

def decode_png_xbox(raw: bytes) -> Image.Image:

    blocks_x = WIDTH // 4
    blocks_y = HEIGHT // 4

    image_size = blocks_x * blocks_y * BC1_BLOCK_SIZE

    pixel_data = raw[HEADER_SIZE:HEADER_SIZE + image_size]

    if len(pixel_data) != image_size:
        raise ValueError("Unexpected end of file.")

    pixel_data = fix_png_xbox_bc1_layout(pixel_data)

    rgba = texture2ddecoder.decode_bc1(
        pixel_data,
        WIDTH,
        HEIGHT,
    )

    return Image.frombytes(
        "RGBA",
        (WIDTH, HEIGHT),
        rgba,
        "raw",
        "BGRA",
    )


# ==========================================================
# Main
# ==========================================================

files = sorted(INPUT_DIR.rglob("*.png_xbox"))

if not files:
    print("No .png_xbox files found.")
    raise SystemExit

print(f"Found {len(files)} .png_xbox files.\n")

success = 0

for file in files:

    try:
        img = decode_png_xbox(file.read_bytes())

        output_path = file.with_name("album.png")
        img.save(output_path)

        print(f"OK   {file} -> {output_path.name}")
        success += 1

    except Exception as e:
        print(f"FAIL {file}: {e}")

print(f"\nDone. Converted {success}/{len(files)} images.")