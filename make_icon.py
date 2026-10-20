"""Draws the program icon (kompas-mcp.ico): a blue tile with a white isometric cube."""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))


def draw(size: int) -> Image.Image:
    s = size / 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=48 * s, fill=(31, 111, 235))
    top = [(128, 52), (200, 92), (128, 132), (56, 92)]
    left = [(56, 92), (128, 132), (128, 212), (56, 172)]
    right = [(128, 132), (200, 92), (200, 172), (128, 212)]
    for poly, col in ((top, (255, 255, 255)), (left, (205, 222, 250)), (right, (160, 192, 245))):
        d.polygon([(x * s, y * s) for x, y in poly], fill=col)
    return im


if __name__ == "__main__":
    big = draw(256)
    big.save(os.path.join(HERE, "kompas-mcp.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("kompas-mcp.ico written")
