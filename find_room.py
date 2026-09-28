#!/usr/bin/env python3
"""find_room.py <send-screen.png> <room name>: print "x y" of that room's checkbox.

setlog's send screen has no text nodes for uiautomator, so the row is found by its
avatar: rooms/<name>.png holds a 128x128 crop of each room's avatar (taken from
a screenshot of the send screen at 1080x2400, keyboard hidden). The avatar is at
x 178..306 and the checkbox at x=106 on the same line; the rows' heights vary.
"""
import os, sys
from PIL import Image, ImageChops, ImageStat

HOME = os.path.dirname(os.path.abspath(__file__))
ROWS = [916, 1116, 1315, 1516, 1716, 1916, 2116]
AVATAR_X, CHECK_X = (178, 306), 106


def diff(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def main():
    shot, name = sys.argv[1], sys.argv[2]
    ref_path = os.path.join(HOME, "rooms", name + ".png")
    if not os.path.exists(ref_path):
        sys.exit(f"no reference avatar for {name!r}: add rooms/{name}.png")
    ref = Image.open(ref_path).convert("RGB")
    im = Image.open(shot).convert("RGB")
    # The rows are not at fixed heights: after the first send a "Select last used"
    # line appears above them and pushes everything down. Scan for the avatar instead,
    # coarsely on a shrunken copy, then exactly around the best hit.
    small_ref = ref.resize((32, 32))
    best = None
    for y in range(660, 2300, 8):
        crop = im.crop((AVATAR_X[0], y - 64, AVATAR_X[1], y + 64)).resize((32, 32))
        d = diff(crop, small_ref)
        if best is None or d < best[0]:
            best = (d, y)
    best = None if best is None else min(
        (diff(im.crop((AVATAR_X[0], y - 64, AVATAR_X[1], y + 64)), ref), y)
        for y in range(best[1] - 10, best[1] + 11))
    d, y = best
    # A real match measures ~0; the keyboard or an empty row scores 15-25 against
    # the near-white avatars, so also insist on beating a flat background.
    crop = im.crop((AVATAR_X[0], y - 64, AVATAR_X[1], y + 64))
    flat = diff(crop, Image.new("RGB", crop.size, (238, 238, 238)))
    if d > 12 or d >= flat:
        sys.exit(f"{name!r} not on this screen (closest row differs by {d:.1f})")
    print(CHECK_X, y)


if __name__ == "__main__":
    main()
