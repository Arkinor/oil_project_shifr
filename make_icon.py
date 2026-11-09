# make_icon.py — запустить один раз
from PIL import Image, ImageDraw, ImageFont

sizes = [16, 24, 32, 48, 64, 128, 256]
images = []
for size in sizes:
    img = Image.new("RGBA", (size, size), (67, 97, 238, 255))
    draw = ImageDraw.Draw(img)
    m = size // 8
    draw.rectangle([m, m*2, size-m, size-m*2], fill=(255,255,255,255))
    draw.rectangle([m*2, m*3, size-m*2, m*4], fill=(67,97,238,255))
    draw.rectangle([m*2, m*5, size-m*2, m*6], fill=(67,97,238,255))
    images.append(img)
images[0].save("icon.ico", sizes=[(s, s) for s in sizes], append_images=images[1:])
print("icon.ico создан")