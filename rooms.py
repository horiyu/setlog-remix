#!/usr/bin/env python3
"""Room avatars for find_room.py.

  rooms.py crop <send-screen.png> <outdir>   cut every room row out of a screenshot of
                                             setlog's send screen (1080x2400, keyboard
                                             hidden) into <outdir>/<n>.png (the avatar
                                             find_room.py matches) and <n>-row.png (the
                                             whole row, to read its name); rows already
                                             in <outdir> are skipped
  rooms.py sheet <outdir>                    <outdir>/sheet.png: every row, numbered
  rooms.py check                             warn about rooms whose avatars look alike
"""
import glob, os, sys
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

HOME = os.path.dirname(os.path.abspath(__file__))
AVATAR_X = (178, 306)          # avatar column; the checkbox is at x=106 on the same line
ROW_X = (24, 1056)


def diff(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean) / 3


def rows(im):
    """(top, bottom) of each room row: grey rounded boxes on the lighter page."""
    g = im.convert("L")
    out, start = [], None
    for y in range(640, 2360):
        inside = g.getpixel((40, y)) < 241
        if inside and start is None:
            start = y
        elif not inside and start is not None:
            if y - start > 100:
                out.append((start, y))
            start = None
    return out


def crop(shot, outdir):
    os.makedirs(outdir, exist_ok=True)
    im = Image.open(shot).convert("RGB")
    have = [Image.open(p) for p in sorted(glob.glob(os.path.join(outdir, "*-row.png")))]
    n = len(have)
    for top, bottom in rows(im):
        cy = (top + bottom) // 2
        # a row cut off by the screen edge is picked up again after scrolling
        if cy - 64 < top - 10 or cy + 64 > bottom + 10:
            continue
        row = im.crop((ROW_X[0], cy - 64, ROW_X[1], cy + 64))
        if any(diff(row, h) < 4 for h in have):
            continue
        n += 1
        row.save(os.path.join(outdir, f"{n}-row.png"))
        im.crop((AVATAR_X[0], cy - 64, AVATAR_X[1], cy + 64)).save(os.path.join(outdir, f"{n}.png"))
        have.append(row)
    print(f"{n} rooms in {outdir}")


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def sheet(outdir):
    items = sorted(glob.glob(os.path.join(outdir, "*-row.png")),
                   key=lambda p: int(os.path.basename(p).split("-")[0]))
    if not items:
        sys.exit(f"no rows in {outdir}")
    w, h = 1032 + 120, 128
    out = Image.new("RGB", (w, h * len(items)), (255, 255, 255))
    d, f = ImageDraw.Draw(out), font(56)
    for i, p in enumerate(items):
        num = os.path.basename(p).split("-")[0]
        d.text((20, i * h + 32), num, font=f, fill=(0, 0, 0))
        out.paste(Image.open(p), (120, i * h))
    path = os.path.join(outdir, "sheet.png")
    out.save(path)
    print(path)


def check():
    refs = {os.path.basename(p)[:-4]: Image.open(p) for p in glob.glob(os.path.join(HOME, "rooms", "*.png"))}
    names, bad = sorted(refs), 0
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if diff(refs[a], refs[b]) < 12:
                print(f"'{a}' and '{b}' have near-identical avatars: posting may pick the wrong one. "
                      "Give one of them its own room picture in setlog.")
                bad += 1
    print(f"{len(names)} rooms, {bad} look-alike pairs")
    return 1 if bad else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "crop" and len(sys.argv) == 4:
        crop(sys.argv[2], sys.argv[3])
    elif cmd == "sheet" and len(sys.argv) == 3:
        sheet(sys.argv[2])
    elif cmd == "check":
        sys.exit(check())
    else:
        sys.exit(__doc__)
