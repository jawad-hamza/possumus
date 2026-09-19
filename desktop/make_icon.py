"""Draws the PLACEHOLDER application icon, desktop/zehnms.ico.

ZehnMS has no logo yet (see PRODUCT.md, Brand Commitments). Until it does, this gives the exe,
the window, the taskbar button and the shortcuts something better than the generic Python icon:
a rounded square with "ZM". Replace zehnms.ico with the real artwork when it exists; nothing
else has to change (keep the 16 to 256 px sizes inside the .ico).

    .venv\\Scripts\\python.exe desktop\\make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
BACKGROUND = (31, 42, 55, 255)
FOREGROUND = (255, 255, 255, 255)
ACCENT = (56, 189, 148, 255)
FONT_CANDIDATES = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")


def load_font(size):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_icon(size):
    # Draw large and scale down: small sizes come out smoother than drawing them directly.
    scale = 4 if size < 128 else 2
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, canvas - 1, canvas - 1), radius=int(canvas * 0.22), fill=BACKGROUND)
    font = load_font(int(canvas * 0.5))
    left, top, right, bottom = draw.textbbox((0, 0), "ZM", font=font)
    draw.text(((canvas - (right - left)) / 2 - left, (canvas - (bottom - top)) / 2 - top - canvas * 0.04),
              "ZM", font=font, fill=FOREGROUND)
    bar_top = int(canvas * 0.78)
    draw.rounded_rectangle((int(canvas * 0.24), bar_top, int(canvas * 0.76), bar_top + max(2, int(canvas * 0.06))),
                           radius=int(canvas * 0.03), fill=ACCENT)
    return image.resize((size, size), Image.LANCZOS)


def main():
    target = Path(__file__).resolve().parent / "zehnms.ico"
    images = [draw_icon(size) for size in SIZES]
    images[-1].save(target, format="ICO", sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    print(f"Wrote {target} ({target.stat().st_size:,} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
