#!/usr/bin/env python3
"""Render a video in a setlog format: a format.json from a GitHub repository.

A format is data, never code: format.json names effects from the fixed list in
EFFECTS below with their parameters, and every value is checked and clamped
before it reaches ffmpeg. Files a format uses (a .cube LUT, a PNG to lay over,
a font) must sit inside the same repository; they are copied into a scratch
directory under plain names, so nothing from the repository is ever spliced
into the ffmpeg command as a path.

The output is what the emulator camera shows in setlog's frame: 1710x962
(the 16:9 band setlog keeps of the camera's 1710x1280 frame, see post.sh), 30 fps, no
sound, a few seconds long. setlog keeps a little over 2 s of it.

    render.py check  <format dir>...
    render.py render <format dir> <in video> <out.mp4> [--caption TEXT]
    render.py sheet  <video> <out.png>        four frames side by side
    render.py job    <job dir>                what worker.sh runs
"""
import datetime, json, math, os, re, secrets, shutil, subprocess, sys, tempfile

W, H, FPS = 1710, 962, 30
SPEC = 1
MAX_ASSET = 20 * 1024 * 1024
ASSET_KINDS = {"image": (".png", ".jpg", ".jpeg"), "lut": (".cube",), "font": (".ttf", ".otf", ".ttc")}
BUILTIN_FONTS = {"sans": "Noto Sans CJK JP", "sans-bold": "Noto Sans CJK JP:bold",
                 "serif": "Noto Serif CJK JP", "mono": "monospace"}
COLORS = {"white": "FFFFFF", "black": "000000", "red": "FF3B30", "yellow": "FFD60A",
          "green": "34C759", "blue": "0A84FF", "orange": "FF9F0A", "gray": "8E8E93"}
POSITIONS = ("top-left", "top", "top-right", "left", "center", "right",
             "bottom-left", "bottom", "bottom-right")
WEEKDAYS_JA = "月火水木金土日"


class FormatError(ValueError):
    """Something in format.json that cannot be rendered; the message is for its author."""


# --- reading format.json -------------------------------------------------------

class Params:
    """One effect's parameters, with typed, clamped getters that remember what was read."""

    def __init__(self, where, obj):
        if not isinstance(obj, dict):
            raise FormatError(f"{where}: must be an object")
        self.where, self.obj, self.used = where, obj, {"type"}

    def _get(self, key, default):
        self.used.add(key)
        return self.obj.get(key, default)

    def num(self, key, default, lo, hi):
        v = self._get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise FormatError(f"{self.where}.{key}: must be a number")
        if not lo <= v <= hi:
            raise FormatError(f"{self.where}.{key}: {v} is outside {lo}..{hi}")
        return float(v)

    def choice(self, key, default, options):
        v = self._get(key, default)
        if v not in options:
            raise FormatError(f"{self.where}.{key}: {v!r} is not one of {', '.join(map(str, options))}")
        return v

    def flag(self, key, default):
        v = self._get(key, default)
        if not isinstance(v, bool):
            raise FormatError(f"{self.where}.{key}: must be true or false")
        return v

    def text(self, key, default, limit=200):
        v = self._get(key, default)
        if not isinstance(v, str):
            raise FormatError(f"{self.where}.{key}: must be a string")
        if len(v) > limit:
            raise FormatError(f"{self.where}.{key}: longer than {limit} characters")
        return v

    def color(self, key, default):
        v = self._get(key, default)
        if isinstance(v, str) and v.lower() in COLORS:
            return "0x" + COLORS[v.lower()]
        m = re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", v) if isinstance(v, str) else None
        if not m:
            raise FormatError(f"{self.where}.{key}: {v!r} is not #RRGGBB, #RRGGBBAA or one of {', '.join(COLORS)}")
        alpha = f"@{int(m.group(2), 16) / 255:.3f}" if m.group(2) else ""
        return "0x" + m.group(1).upper() + alpha

    def done(self):
        extra = sorted(set(self.obj) - self.used)
        if extra:
            raise FormatError(f"{self.where}: unknown key(s) {', '.join(extra)}")


def load(fmt_dir, root=None):
    """Read and check <fmt_dir>/format.json. root bounds where its files may come from."""
    fmt_dir = os.path.realpath(fmt_dir)
    root = os.path.realpath(root or fmt_dir)
    path = os.path.join(fmt_dir, "format.json")
    if os.path.getsize(path) > 256 * 1024:
        raise FormatError("format.json is larger than 256 KB")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as e:
        raise FormatError(f"format.json: {e}") from None
    p = Params("format", data)
    fmt = {"id": os.path.basename(fmt_dir), "dir": fmt_dir, "root": root}
    spec = p.num("spec", SPEC, 1, SPEC)
    fmt["name"] = p.text("name", fmt["id"], 40) or fmt["id"]
    fmt["description"] = p.text("description", "", 120)
    fmt["order"] = p.num("order", 100, -1000, 1000)
    fmt["author"] = p.text("author", "", 80)

    c = Params("clip", p._get("clip", {}))
    fmt["clip"] = {"mode": c.choice("mode", "head", ("head", "middle", "tail", "fit")),
                   "start": c.num("start", 0, 0, 3600),
                   "duration": c.num("duration", 3, 0.5, 15),
                   "speed": c.num("speed", 1, 0.25, 8),
                   "loop": c.choice("loop", "none", ("none", "reverse", "boomerang"))}
    c.done()
    f = Params("frame", p._get("frame", {}))
    fmt["frame"] = {"mode": f.choice("mode", "fill", ("fill", "fit")),
                    "turn": f.choice("turn", "none", ("none", "ccw", "cw")),
                    "background": f.color("background", "#0E0E10")}
    f.done()

    effects = p._get("effects", [])
    if not isinstance(effects, list) or len(effects) > 24:
        raise FormatError("effects: must be a list of at most 24 effects")
    fmt["effects"] = []
    for i, e in enumerate(effects):
        where = f"effects[{i}]"
        kind = e.get("type") if isinstance(e, dict) else None
        if kind not in EFFECTS and kind not in FRAME_EFFECTS:
            raise FormatError(f"{where}.type: {kind!r} is not one of {', '.join([*EFFECTS, *FRAME_EFFECTS])}")
        ep = Params(f"{where} ({kind})", e)
        read = EFFECTS[kind][0] if kind in EFFECTS else FRAME_EFFECTS[kind]
        fmt["effects"].append((kind, read(ep, fmt)))
        ep.done()
    p.done()
    fmt["spec"] = int(spec)
    return fmt


def asset(p, key, kind, fmt, required=True, name=None):
    """A file named by the format, resolved inside its repository."""
    name = p.text(key, "", 200) if name is None else name
    if not isinstance(name, str):
        raise FormatError(f"{p.where}.{key}: must be a file name")
    if not name:
        if required:
            raise FormatError(f"{p.where}.{key}: name a file")
        return None
    if name.startswith("/") or "\\" in name:
        raise FormatError(f"{p.where}.{key}: use a path relative to the format's folder")
    path = os.path.realpath(os.path.join(fmt["dir"], name))
    if os.path.commonpath([path, fmt["root"]]) != fmt["root"]:
        raise FormatError(f"{p.where}.{key}: {name} is outside the repository")
    if not os.path.isfile(path):
        raise FormatError(f"{p.where}.{key}: {name} not found")
    if not path.lower().endswith(ASSET_KINDS[kind]):
        raise FormatError(f"{p.where}.{key}: a {kind} must be {' / '.join(ASSET_KINDS[kind])}")
    if os.path.getsize(path) > MAX_ASSET:
        raise FormatError(f"{p.where}.{key}: {name} is larger than 20 MB")
    return path


# --- effects --------------------------------------------------------------------
# Each effect: (read(params, fmt) -> settings, build(settings, ctx) -> filters).
# build returns a list of filters for the main chain, or calls ctx.overlay() for
# effects that bring in another input.

def when(p):
    s = p.num("from", 0, 0, 15)
    e = p.num("until", 0, 0, 15)
    return (s, e) if (s or e) else None


def enable(t):
    if not t:
        return ""
    s, e = t
    return f":enable='between(t,{s:g},{e:g})'" if e else f":enable='gte(t,{s:g})'"


def place(position, margin, w, h):
    """x, y expressions for something w x h (ffmpeg names) on the canvas."""
    col = {"left": 0, "right": 2}.get(position.split("-")[-1], 1)
    row = {"top": 0, "bottom": 2}.get(position.split("-")[0], 1)
    x = [f"{margin:g}", f"({W}-{w})/2", f"{W}-{w}-{margin:g}"][col]
    y = [f"{margin:g}", f"({H}-{h})/2", f"{H}-{h}-{margin:g}"][row]
    return x, y


def r_grade(p, _):
    return {k: p.num(k, d, lo, hi) for k, d, lo, hi in
            (("brightness", 0, -1, 1), ("contrast", 1, 0, 3), ("saturation", 1, 0, 3),
             ("gamma", 1, 0.1, 5), ("hue", 0, -180, 180), ("temperature", 0, -1, 1))}


def b_grade(s, _):
    out = [f"eq=brightness={s['brightness']:g}:contrast={s['contrast']:g}:"
           f"saturation={s['saturation']:g}:gamma={s['gamma']:g}"]
    if s["hue"]:
        out.append(f"hue=h={s['hue']:g}")
    if s["temperature"]:              # warm (+) / cool (-): lift red and drop blue, or the reverse
        t = s["temperature"] * 0.25
        out.append(f"colorbalance=rm={t:g}:bm={-t:g}:rh={t / 2:g}:bh={-t / 2:g}")
    return out


def r_sepia(p, _):
    return {"amount": p.num("amount", 1, 0, 1)}


def b_sepia(s, _):
    a = s["amount"]
    m = [[.393, .769, .189], [.349, .686, .168], [.272, .534, .131]]
    rows = [[a * m[i][j] + (1 - a) * (i == j) for j in range(3)] for i in range(3)]
    keys = ("rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb")
    return ["colorchannelmixer=" + ":".join(f"{k}={v:.3f}" for k, v in zip(keys, sum(rows, [])))]


def r_none(p, _):
    return {}


def r_amount(key, default, lo, hi):
    return lambda p, _: {key: p.num(key, default, lo, hi)}


def b_vignette(s, _):
    return [f"vignette=angle={0.2 + 1.2 * s['strength']:.3f}"]


def b_pixelate(s, _):
    n = int(s["size"])
    return [f"scale={max(1, W // n)}:{max(1, H // n)}:flags=area", f"scale={W}:{H}:flags=neighbor"]


def r_scanlines(p, _):
    return {"spacing": p.num("spacing", 4, 2, 40), "opacity": p.num("opacity", 0.3, 0, 1)}


def r_letterbox(p, _):
    return {"ratio": p.num("ratio", 2.39, 1.78, 4), "color": p.color("color", "black")}


def b_letterbox(s, _):
    bar = max(0, round((H - W / s["ratio"]) / 2))
    return [f"drawbox=x=0:y=0:w={W}:h={bar}:color={s['color']}:t=fill",
            f"drawbox=x=0:y={H - bar}:w={W}:h={bar}:color={s['color']}:t=fill"] if bar else []


def r_fade(p, _):
    return {"in": p.num("in", 0, 0, 5), "out": p.num("out", 0, 0, 5), "color": p.choice("color", "black", ("black", "white"))}


def b_fade(s, ctx):
    out = []
    if s["in"]:
        out.append(f"fade=t=in:st=0:d={s['in']:g}:color={s['color']}")
    if s["out"]:
        out.append(f"fade=t=out:st={max(0, ctx.duration - s['out']):g}:d={s['out']:g}:color={s['color']}")
    return out


def r_zoom(p, _):
    return {"from": p.num("from", 1, 1, 3), "to": p.num("to", 1.15, 1, 3)}


def b_zoom(s, ctx):
    n = max(1, round(ctx.duration * FPS) - 1)
    z = f"{s['from']:g}+({s['to'] - s['from']:g})*min(on/{n},1)"
    # zoompan rounds to whole pixels; working at twice the size keeps the push smooth.
    return [f"scale={W * 2}:{H * 2}", f"zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps={FPS}"]


def r_lut(p, fmt):
    return {"file": asset(p, "file", "lut", fmt)}


def b_lut(s, ctx):
    return [f"lut3d=file={ctx.stage(s['file'], 'lut')}"]


def r_text(p, fmt):
    s = {"text": p.text("text", None, 200), "position": p.choice("position", "bottom-right", POSITIONS),
         "margin": p.num("margin", 48, 0, 400), "size": p.num("size", 48, 8, 400),
         "color": p.color("color", "white"), "uppercase": p.flag("uppercase", False),
         "box": p.flag("box", False), "box_color": p.color("box_color", "#00000080"),
         "shadow": p.num("shadow", 0, 0, 20), "shadow_color": p.color("shadow_color", "#000000B0"),
         "border": p.num("border", 0, 0, 20), "border_color": p.color("border_color", "black"),
         "when": when(p)}
    font = p._get("font", "sans")
    s["font"] = font if font in BUILTIN_FONTS else asset(p, "font", "font", fmt)
    return s


def rgba(c):
    """0xRRGGBB[@a] (what Params.color returns) as an RGBA tuple for Pillow."""
    hexpart, _, alpha = c[2:].partition("@")
    return tuple(int(hexpart[i:i + 2], 16) for i in (0, 2, 4)) + (round(float(alpha or 1) * 255),)


def b_text(s, ctx):
    # Drawn with Pillow into a PNG and laid over, not with ffmpeg's drawtext: not every
    # ffmpeg build has it, and this way no text from a format ever reaches the filter graph.
    from PIL import Image, ImageDraw, ImageFont
    text = fill_in(s["text"], ctx.vars)
    if s["uppercase"]:
        text = text.upper()
    if not text.strip():
        return []
    font = ImageFont.truetype(ctx.font(s["font"]), round(s["size"]))
    border, shadow = round(s["border"]), round(s["shadow"])
    pad = max(4, round(s["size"] / 4)) if s["box"] else 0
    probe_ = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = (round(v) for v in probe_.multiline_textbbox((0, 0), text, font=font, stroke_width=border, align="center"))
    img = Image.new("RGBA", (r - l + 2 * pad + shadow, b - t + 2 * pad + shadow), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if s["box"]:
        d.rectangle((0, 0, r - l + 2 * pad - 1, b - t + 2 * pad - 1), fill=rgba(s["box_color"]))
    at = (pad - l, pad - t)
    kw = dict(font=font, stroke_width=border, stroke_fill=rgba(s["border_color"]), align="center")
    if shadow:
        d.multiline_text((at[0] + shadow, at[1] + shadow), text, fill=rgba(s["shadow_color"]), **kw)
    d.multiline_text(at, text, fill=rgba(s["color"]), **kw)
    name = f"text{ctx.n}.png"
    ctx.n += 1
    img.save(os.path.join(ctx.work, name))
    x, y = place(s["position"], s["margin"], "overlay_w", "overlay_h")
    ctx.overlay(name, "format=rgba", f"overlay=x={x}:y={y}:eof_action=repeat{enable(s['when'])}")
    return []


def r_image(p, fmt):
    return {"file": asset(p, "file", "image", fmt), "position": p.choice("position", "center", POSITIONS),
            "margin": p.num("margin", 0, 0, 400), "width": p.num("width", W, 8, W * 2),
            "opacity": p.num("opacity", 1, 0, 1), "when": when(p)}


def b_image(s, ctx):
    x, y = place(s["position"], s["margin"], "overlay_w", "overlay_h")
    ctx.overlay(ctx.stage(s["file"], "img"),
                f"scale={round(s['width'])}:-2,format=rgba,colorchannelmixer=aa={s['opacity']:g}",
                f"overlay=x={x}:y={y}:eof_action=repeat{enable(s['when'])}")
    return []


def r_thermal(p, _):
    return {"palette": p.choice("palette", "inferno", ("inferno", "magma", "plasma", "turbo", "heat", "fiery", "cool")),
            "contrast": p.num("contrast", 1.3, 0.5, 3), "blur": p.num("blur", 2, 0, 10)}


def b_thermal(s, _):
    out = ["hue=s=0"]
    if s["blur"]:
        out.append(f"gblur=sigma={s['blur']:g}")
    return out + [f"eq=contrast={s['contrast']:g}", "format=yuv444p", f"pseudocolor=p={s['palette']}"]


EFFECTS = {
    "grade": (r_grade, b_grade),                                          # colour: brightness, contrast, ...
    "mono": (r_none, lambda s, _: ["hue=s=0"]),
    "sepia": (r_sepia, b_sepia),
    "invert": (r_none, lambda s, _: ["negate"]),
    "mirror": (r_none, lambda s, _: ["hflip"]),
    "grain": (r_amount("amount", 20, 0, 100), lambda s, _: [f"noise=alls={round(s['amount'])}:allf=t"]),
    "vignette": (r_amount("strength", 0.5, 0, 1), b_vignette),
    "blur": (r_amount("radius", 4, 0, 60), lambda s, _: [f"gblur=sigma={s['radius']:g}"] if s["radius"] else []),
    "sharpen": (r_amount("amount", 1, 0, 3), lambda s, _: [f"unsharp=5:5:{s['amount']:g}"]),
    "pixelate": (r_amount("size", 16, 2, 200), b_pixelate),
    "rgbshift": (r_amount("amount", 6, 0, 60), lambda s, _: [f"rgbashift=rh={-round(s['amount'])}:bh={round(s['amount'])}"]),
    "scanlines": (r_scanlines, lambda s, _: [f"drawgrid=w={W * 2}:h={round(s['spacing'])}:t=1:c=black@{s['opacity']:g}"]),
    "letterbox": (r_letterbox, b_letterbox),
    "fade": (r_fade, b_fade),
    "zoom": (r_zoom, b_zoom),
    "lut": (r_lut, b_lut),
    "text": (r_text, b_text),
    "image": (r_image, b_image),
    "thermal": (r_thermal, b_thermal),
}


# --- frame effects (ar.py): read and checked here, drawn there ------------------------

def rgb01(c):
    return [v / 255 for v in rgba(c)[:3]]


def r_particles(p, _):
    kind = p.choice("kind", "snow", ("snow", "petals", "bubbles", "confetti"))
    count, size, speed = {"snow": (160, 0.012, 0.35), "petals": (70, 0.018, 0.3),
                          "bubbles": (36, 0.04, 0.25), "confetti": (140, 0.011, 0.6)}[kind]
    return {"kind": kind, "count": p.num("count", count, 1, 400), "size": p.num("size", size, 0.002, 0.1),
            "speed": p.num("speed", speed, 0, 3), "wind": p.num("wind", 0.03, -1, 1),
            "opacity": p.num("opacity", 0.9, 0, 1), "depth": p.flag("depth", True), "seed": p.num("seed", 1, 0, 1e6)}


def r_card(p, fmt, size):
    s = {"text": p.text("text", "{caption}", 200), "size": p.num("size", size, 12, 200),
         "appear": p.num("appear", 0.15, 0, 5), "uppercase": p.flag("uppercase", False)}
    font = p._get("font", "sans-bold")
    s["font"] = font if font in BUILTIN_FONTS else asset(p, "font", "font", fmt)
    return s


def r_pin(p, fmt):
    s = r_card(p, fmt, 60)
    s.update({"style": p.choice("style", "card", ("card", "neon")), "auto": p.flag("auto", True),
              "x": p.num("x", 0.5, 0, 1),
              "y": p.num("y", 0.62, 0, 1), "tilt": p.num("tilt", -4, -30, 30),
              "behind_people": p.flag("behind_people", True)})
    return s


def r_speech(p, fmt):
    return r_card(p, fmt, 52)


def r_aura(p, _):
    return {"width": p.num("width", 0.04, 0.005, 0.2), "strength": p.num("strength", 0.8, 0, 2),
            "cycle": p.flag("cycle", True), "hue": p.num("hue", 0.55, 0, 1), "color": rgb01(p.color("color", "#7FD4FF"))}


def r_background(p, fmt):
    files = p._get("files", None)
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise FormatError(f"{p.where}.files: list 1 to 8 images")
    return {"files": [asset(p, "files", "image", fmt, name=f) for f in files],
            "choose": p.choice("choose", "random", ("random", "first")), "seed": 0,
            "subject": p.choice("subject", "auto", ("auto", "person", "near")), "drift": p.num("drift", 0.06, 0, 0.3)}


def r_glitch(p, _):
    return {"amount": p.num("amount", 0.5, 0, 1), "rate": p.num("rate", 2, 0.2, 8), "seed": p.num("seed", 3, 0, 1e6)}


def r_neon(p, _):
    return {"thickness": p.num("thickness", 2, 1, 6), "glow": p.num("glow", 0.8, 0, 2), "dim": p.num("dim", 0.12, 0, 1),
            "cycle": p.num("cycle", 0.35, 0, 3), "color": rgb01(p.color("color", "#FF4FD8"))}


def r_trail(p, _):
    return {"decay": p.num("decay", 0.9, 0.5, 0.99), "hue_shift": p.num("hue_shift", 0.6, 0, 3),
            "strength": p.num("strength", 0.9, 0, 1)}


FRAME_EFFECTS = {"particles": r_particles, "pin": r_pin, "speech": r_speech, "aura": r_aura,
                 "background": r_background, "glitch": r_glitch, "neon": r_neon, "trail": r_trail}


# --- text variables --------------------------------------------------------------

def variables(caption, when_, fmt, speed):
    x = f"{speed:.1f}".rstrip("0").rstrip(".")
    return {"caption": caption, "format": fmt["name"], "speed": x, "_when": when_}


def fill_in(template, v):
    """{caption}, {format}, {speed}, {date}, {time}, {weekday_ja}, ... and {date:%b %d %Y} (strftime)."""
    t = v["_when"]
    named = {"date": "%Y.%m.%d", "time": "%H:%M", "datetime": "%Y.%m.%d %H:%M", "year": "%Y",
             "month": "%m", "day": "%d", "hour": "%H", "minute": "%M", "second": "%S", "weekday": "%a"}

    def one(m):
        key, spec = m.group(1), m.group(2)
        if key in named:
            return t.strftime(spec if spec is not None else named[key])
        if key == "weekday_ja":
            return WEEKDAYS_JA[t.weekday()]
        if key in ("caption", "format", "speed"):
            return v[key]
        return m.group(0)
    return re.sub(r"\{(\w+)(?::([^{}]{1,40}))?\}", one, template)


# --- building the ffmpeg command -------------------------------------------------

def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:stream_side_data=rotation:format=duration:format_tags=creation_time",
                        "-of", "json", path], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise FormatError(f"cannot read the video: {r.stderr.strip()[:200]}")
    info = json.loads(r.stdout)
    st = (info.get("streams") or [{}])[0]
    w, h = st.get("width"), st.get("height")
    if not w or not h:
        raise FormatError("the file has no video")
    rot = next((abs(int(d.get("rotation", 0))) for d in st.get("side_data_list", []) if "rotation" in d), 0)
    if rot in (90, 270):
        w, h = h, w
    fmtinfo = info.get("format", {})
    made = None
    ct = (fmtinfo.get("tags") or {}).get("creation_time")
    if ct:
        try:
            made = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone()
        except ValueError:
            pass
    return {"w": w, "h": h, "duration": float(fmtinfo.get("duration") or 0), "made": made}


class Ctx:
    def __init__(self, work, duration, vars_):
        self.work, self.duration, self.vars = work, duration, vars_
        self.inputs, self.graph, self.pending = [], [], []
        self.label, self.n = "0:v", 0

    def stage(self, path, stem):
        """Copy a file from the repository into the scratch dir under a plain name."""
        ext = os.path.splitext(path)[1].lower()
        name = f"{stem}{self.n}{ext}"
        self.n += 1
        shutil.copyfile(path, os.path.join(self.work, name))
        return name

    def font(self, font):
        if font in BUILTIN_FONTS:
            r = subprocess.run(["fc-match", "-f", "%{file}", BUILTIN_FONTS[font]], capture_output=True, text=True)
            path = r.stdout.strip()
            if not path or not os.path.isfile(path):
                raise FormatError(f"no system font for {font!r} (install fonts-noto-cjk)")
            return path
        return font

    def add(self, filters):
        self.pending.extend(filters)

    def flush(self):
        if self.pending or self.label == "0:v":
            out = f"v{len(self.graph)}"
            self.graph.append(f"[{self.label}]{','.join(self.pending) or 'null'}[{out}]")
            self.label, self.pending = out, []

    def overlay(self, filename, prep, over):
        self.flush()
        idx = len(self.inputs) + 1
        self.inputs.append(filename)
        out = f"v{len(self.graph)}"
        self.graph.append(f"[{idx}:v]{prep}[i{idx}];[{self.label}][i{idx}]{over}[{out}]")
        self.label = out

    def raw(self, text):
        """A graph piece with several pads; {in} and {out} are filled in."""
        self.flush()
        out = f"v{len(self.graph)}"
        self.graph.append(text.format(i=self.label, o=out))
        self.label = out


def plan(fmt, src, caption="", when_=None):
    """The clip to read from the source: (seek, span, speed, output seconds)."""
    c = fmt["clip"]
    d, speed = c["duration"], c["speed"]
    total = src["duration"] or d * speed
    if c["mode"] == "fit":
        speed = max(0.25, min(64.0, total / d))
        return 0.0, total, speed, d
    span = d * speed
    start = {"head": c["start"],
             "middle": max(0.0, (total - span) / 2),
             "tail": max(0.0, total - span - c["start"])}[c["mode"]]
    start = min(start, max(0.0, total - 0.1))
    return start, span, speed, d


def prepare(fmt, src_path, caption="", when_=None):
    """What every pass needs to know: the source, the clip plan, the text variables."""
    src = probe(src_path)
    seek, span, speed, dur = plan(fmt, src)
    loop = fmt["clip"]["loop"]
    part = dur / 2 if loop == "boomerang" else dur        # the forward half of a boomerang
    if loop == "boomerang":
        span = span / 2 if fmt["clip"]["mode"] != "fit" else span
        speed = speed if fmt["clip"]["mode"] != "fit" else speed * 2
    when_ = when_ or src["made"] or datetime.datetime.now().astimezone()
    return {"src": src, "seek": seek, "span": span, "speed": speed, "dur": dur, "part": part, "loop": loop,
            "vars": variables(caption, when_, fmt, speed)}


def build(fmt, info, in_path, out_path, work, effects, first=True, last=True):
    """One ffmpeg pass. The first cuts the clip and fits the frame; the last pads and
    trims to length and encodes for keeps; passes in between keep quality for the next."""
    ctx = Ctx(work, info["dur"], info["vars"])
    if first:
        ctx.add([f"setpts=(PTS-STARTPTS)/{info['speed']:g}", f"fps={FPS}",
                 f"trim=duration={info['part']:g}", "setpts=PTS-STARTPTS"])
        if info["loop"] == "reverse":
            ctx.add(["reverse"])
        elif info["loop"] == "boomerang":
            ctx.raw("[{i}]split[bf][bb];[bb]reverse[br];[bf][br]concat=n=2:v=1:a=0[{o}]")
        fr, src = fmt["frame"], info["src"]
        if src["h"] > src["w"] and fr["turn"] != "none":
            ctx.add(["transpose=2" if fr["turn"] == "ccw" else "transpose=1"])
        if fr["mode"] == "fill":
            ctx.add([f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos", f"crop={W}:{H}", "setsar=1"])
        else:
            ctx.raw(f"[{{i}}]split[fa][fb];[fa]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                    f"gblur=sigma=40,eq=brightness=-0.25:saturation=0.8[fbg];"
                    f"[fb]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[ffg];"
                    f"[fbg][ffg]overlay=(W-w)/2:(H-h)/2,setsar=1[{{o}}]")
    for kind, settings in effects:
        ctx.add(EFFECTS[kind][1](settings, ctx))
    if last:   # hold the last frame if the source ran short, then cut to length
        ctx.add([f"tpad=stop_mode=clone:stop_duration={info['dur']:g}", f"trim=duration={info['dur']:g}"])
    ctx.add(["format=yuv420p"])
    ctx.flush()

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin"]
    if first:
        cmd += ["-ss", f"{info['seek']:.3f}", "-t", f"{info['span'] + 0.5:.3f}"]
    cmd += ["-i", os.path.abspath(in_path)]
    for f in ctx.inputs:
        cmd += ["-i", f]
    cmd += ["-filter_complex", ";".join(ctx.graph), "-map", f"[{ctx.label}]", "-an", "-c:v", "libx264"]
    cmd += (["-preset", "medium", "-crf", "16", "-g", "15", "-r", str(FPS), "-movflags", "+faststart"] if last
            else ["-preset", "veryfast", "-crf", "12", "-r", str(FPS)])
    return cmd + [os.path.abspath(out_path)]


def ar_python():
    """The Python for ar.py: AR_PYTHON (environment or settings.conf), else .venv, else this one."""
    p = os.environ.get("AR_PYTHON")
    if not p:
        try:
            import repo
            p = repo.conf().get("AR_PYTHON", "")
        except Exception:                       # render.py used alone, without settings
            p = ""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    return os.path.expanduser(p) if p else (here if os.path.exists(here) else sys.executable)


def frame_pass(info, in_path, out_path, work, effects, n):
    """Effects that look at the picture (ar.py), in AR_PYTHON."""
    ctx = Ctx(work, info["dur"], info["vars"])
    ctx.n = 1000 * (n + 1)                      # staged names never clash with an ffmpeg pass
    out = []
    for kind, s in effects:
        s = dict(s)
        if kind in ("pin", "speech"):
            text = fill_in(s["text"], info["vars"])
            s["text"] = text.upper() if s.pop("uppercase") else text
            s["font"] = ctx.font(s["font"])
        if kind == "background":
            s["files"] = [ctx.stage(f, "bg") for f in s["files"]]
            if s.pop("choose") == "random":
                s["seed"] = secrets.randbelow(1 << 30)
        out.append({"kind": kind, "settings": s})
    spec = {"input": os.path.abspath(in_path), "output": os.path.abspath(out_path), "work": work,
            "width": W, "height": H, "fps": FPS, "duration": info["dur"], "effects": out}
    path = os.path.join(work, f"ar{n}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False)
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([ar_python(), os.path.join(here, "ar.py"), path], cwd=work, capture_output=True, text=True,
                       timeout=900)
    if r.returncode:
        tail = r.stderr.strip()[-600:]
        if "No module named" in tail:
            tail += "\n(the AR effects need numpy, opencv and onnxruntime: bin/setup-ar.sh)"
        raise RuntimeError(f"ar.py failed: {tail}")


def render(fmt, src_path, out_path, caption="", when_=None):
    """ffmpeg passes and frame passes in the order the effects come, the first and last
    always ffmpeg (they cut, fit, pad and encode)."""
    runs = []
    for kind, settings in fmt["effects"]:
        is_frame = kind in FRAME_EFFECTS
        if runs and runs[-1][0] == is_frame:
            runs[-1][1].append((kind, settings))
        else:
            runs.append((is_frame, [(kind, settings)]))
    if not runs or runs[0][0]:
        runs.insert(0, (False, []))
    if runs[-1][0]:
        runs.append((False, []))
    with tempfile.TemporaryDirectory(prefix="setlog-remix-") as work:
        info = prepare(fmt, src_path, caption, when_)
        cur = src_path
        for i, (is_frame, effects) in enumerate(runs):
            last = i == len(runs) - 1
            nxt = out_path if last else os.path.join(work, f"pass{i}.mp4")
            if is_frame:
                frame_pass(info, cur, nxt, work, effects, i)
            else:
                cmd = build(fmt, info, cur, nxt, work, effects, first=i == 0, last=last)
                r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=600)
                if r.returncode:
                    raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-600:]}\n{' '.join(cmd)}")
            cur = nxt


def sheet(video, out_png, n=4):
    d = probe(video)["duration"] or 3
    step = d / n
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vf",
                    f"fps={1 / step:g},scale=570:-2,tile={n}x1:padding=6:color=0x0e0e10", "-frames:v", "1", out_png],
                   check=True)


# --- jobs (worker.sh) -------------------------------------------------------------

def run_job(job_dir):
    """Render <job>/input.* by job.json into <job>/out.mp4."""
    import repo
    with open(os.path.join(job_dir, "job.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    root = repo.fetch(job["repo"], job.get("sha"))
    fmt = load(os.path.join(root, "formats", job["format"]), root)
    src = next(os.path.join(job_dir, f) for f in sorted(os.listdir(job_dir)) if f.startswith("input."))
    when_ = datetime.datetime.fromisoformat(job["received"]) if job.get("received") else None
    render(fmt, src, os.path.join(job_dir, "out.mp4"), job.get("caption", ""), when_)


def main(argv):
    if len(argv) >= 2 and argv[0] == "check":
        bad = 0
        for d in argv[1:]:
            try:
                f = load(d, os.path.dirname(os.path.dirname(os.path.realpath(d))))
                print(f"ok    {f['id']}: {f['name']} ({len(f['effects'])} effects)")
            except (FormatError, OSError) as e:
                bad += 1
                print(f"ERROR {os.path.basename(d.rstrip('/'))}: {e}")
        return 1 if bad else 0
    if len(argv) >= 4 and argv[0] == "render":
        caption = argv[argv.index("--caption") + 1] if "--caption" in argv else ""
        d = argv[1]
        render(load(d, os.path.dirname(os.path.dirname(os.path.realpath(d)))), argv[2], argv[3], caption)
        return 0
    if len(argv) == 3 and argv[0] == "sheet":
        sheet(argv[1], argv[2])
        return 0
    if len(argv) == 2 and argv[0] == "job":
        run_job(argv[1])
        return 0
    print(__doc__.split("\n\n")[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1:]))
