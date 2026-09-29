#!/usr/bin/env python3
"""The frame-by-frame stage of render.py: effects that need to look at the picture.

    ar.py <spec.json>

render.py writes spec.json (already checked: every value clamped, every file copied
into the scratch dir) and runs this with AR_PYTHON, a Python that has numpy,
opencv-python-headless and onnxruntime (bin/setup-ar.sh). Everything runs on the
CPU: the models are small (a person matte and a depth map, each at a few hundred
pixels wide) so an ordinary laptop keeps up.

Frames stream through one at a time: ffmpeg decodes to raw RGB, each effect changes
the frame in turn, ffmpeg encodes. Nothing holds the whole clip in memory.
"""
import hashlib, json, math, os, subprocess, sys, urllib.request

import cv2
import numpy as np

HOME = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HOME, "cache", "models")
# Pinned downloads (Apache-2.0), checked by sha256 before use.
MODEL_FILES = {
    "matte": ("https://huggingface.co/Xenova/modnet/resolve/fa2fa546052fba4c08921230a26cc69a333fca12/onnx/model.onnx",
              "07c308cf0fc7e6e8b2065a12ed7fc07e1de8febb7dc7839d7b7f15dd66584df9"),
    "depth": ("https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/"
              "4472b7362082ad9968fee890ca0f1e5aca36b93d/onnx/model_quantized.onnx",
              "fcf51f1b230362b28690bb9d1809bf0431f29cad20534e3f589bd7285547f20d"),
}


def model_path(name):
    url, sha = MODEL_FILES[name]
    path = os.path.join(MODELS, f"{name}-{sha[:12]}.onnx")
    if os.path.exists(path):
        return path
    os.makedirs(MODELS, exist_ok=True)
    tmp = path + ".part"
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "setlog-remix"}), timeout=120) as r, \
            open(tmp, "wb") as out:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            out.write(b)
    if h.hexdigest() != sha:
        os.remove(tmp)
        raise RuntimeError(f"model {name}: checksum mismatch")
    os.rename(tmp, path)
    return path


def session(name):
    import onnxruntime as ort
    opt = ort.SessionOptions()
    opt.intra_op_num_threads = min(4, os.cpu_count() or 4)   # what a laptop has to spare
    return ort.InferenceSession(model_path(name), opt, providers=["CPUExecutionProvider"])


def smooth01(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def hsv(h, s, v):
    """Hue 0..1 (arrays welcome) to RGB 0..1."""
    h = np.asarray(h, np.float32) % 1.0
    i = np.floor(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i = i.astype(int) % 6
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], -1).astype(np.float32)


class Spring:
    """A critically damped follower: smooth, a little late, never snapping."""

    def __init__(self, stiffness=40.0):
        self.k, self.x, self.v = stiffness, None, None

    def step(self, target, dt):
        target = np.asarray(target, np.float32)
        if self.x is None:
            self.x, self.v = target.copy(), np.zeros_like(target)
            return self.x
        w = math.sqrt(self.k)
        a = self.k * (target - self.x) - 2 * w * self.v
        self.v = self.v + a * dt
        self.x = self.x + self.v * dt
        return self.x


def pop(t, start=0.0, dur=0.45):
    """0 -> 1 with a soft overshoot, for things that appear."""
    x = (t - start) / dur
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return 1 - math.exp(-6 * x) * math.cos(7 * x)


# --- what the effects share: the matte, the depth, the camera's motion ---------

class Scene:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self._matte = self._depth = self._prev_matte = None
        self._cache = {}
        self.prev_gray = None
        self.motion = np.eye(2, 3, dtype=np.float32)   # previous frame -> this frame
        self.frame = None
        self.index = -1

    def next(self, frame, i):
        self.frame, self.index = frame, i
        self._cache = {}
        small = cv2.resize(frame, (self.w // 3, self.h // 3), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor((small * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        self.motion = np.eye(2, 3, dtype=np.float32)
        if self.prev_gray is not None:
            pts = cv2.goodFeaturesToTrack(self.prev_gray, 300, 0.01, 8)
            if pts is not None and len(pts) >= 12:
                nxt, st, _ = cv2.calcOpticalFlowPyrLK(self.prev_gray, gray, pts, None)
                ok = st.ravel() == 1
                if ok.sum() >= 10:
                    m, _ = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC, ransacReprojThreshold=2.0)
                    if m is not None:
                        m = m.astype(np.float32)
                        m[:, 2] *= 3                     # back to full-size pixels
                        self.motion = m
        self.prev_gray = gray

    def matte(self):
        """0..1, 1 = person, full size. Computed once per frame, only if asked for."""
        if "matte" not in self._cache:
            if self._matte is None:
                self._matte = session("matte")
            m = cv2.resize(self._run_matte(self.frame, 512, 288), (self.w, self.h), interpolation=cv2.INTER_LINEAR)
            # a small, distant person loses detail at that size: look again, closer, around them
            ys, xs = np.nonzero(m > 0.5)
            if len(xs) > 200:
                x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
                bw, bh = x1 - x0, y1 - y0
                if bw * bh < 0.35 * self.w * self.h:
                    pad_x, pad_y = int(bw * 0.3) + 16, int(bh * 0.15) + 16
                    x0, x1 = max(0, x0 - pad_x), min(self.w, x1 + pad_x)
                    y0, y1 = max(0, y0 - pad_y), min(self.h, y1 + pad_y)
                    cw, ch = x1 - x0, y1 - y0
                    k = 512 / max(cw, ch)
                    iw, ih = max(32, int(cw * k) // 32 * 32), max(32, int(ch * k) // 32 * 32)
                    crop = self._run_matte(self.frame[y0:y1, x0:x1], iw, ih)
                    m[:] = 0
                    m[y0:y1, x0:x1] = cv2.resize(crop, (cw, ch), interpolation=cv2.INTER_LINEAR)
            m = np.clip(m, 0, 1).astype(np.float32)
            if self._prev_matte is not None:          # steadier from frame to frame
                m = 0.7 * m + 0.3 * self._prev_matte
            self._prev_matte = m
            self._cache["matte"] = m
        return self._cache["matte"]

    def _run_matte(self, img, w, h):
        x = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        x = ((x - 0.5) / 0.5).transpose(2, 0, 1)[None].astype(np.float32)
        return self._matte.run(None, {self._matte.get_inputs()[0].name: x})[0][0, 0]

    def depth(self):
        """0..1, 1 = nearest, full size. Estimated every other frame, reused between."""
        if "depth" not in self._cache:
            if self._depth is None:
                self._depth = session("depth")
                self._last_depth = None
            if self._last_depth is None or self.index % 2 == 0:
                x = cv2.resize(self.frame, (364, 210), interpolation=cv2.INTER_AREA)
                x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
                x = x.transpose(2, 0, 1)[None].astype(np.float32)
                d = self._depth.run(None, {self._depth.get_inputs()[0].name: x})[0][0]
                lo, hi = np.percentile(d, 2), np.percentile(d, 98)
                d = np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1)
                d = cv2.GaussianBlur(d.astype(np.float32), (0, 0), 1.2)
                new = cv2.resize(d, (self.w, self.h), interpolation=cv2.INTER_LINEAR)
                self._last_depth = new if self._last_depth is None else 0.35 * self._last_depth + 0.65 * new
            self._cache["depth"] = self._last_depth
        return self._cache["depth"]

    def subject(self, mode="auto"):
        """What counts as 'in front': the person, else the nearest third of the scene."""
        key = "subject-" + mode
        if key not in self._cache:
            m = None
            if mode in ("person", "auto"):
                m = self.matte()
                if mode == "auto" and (m > 0.5).mean() < 0.02:
                    m = None
            if m is None:
                d = self.depth()
                m = smooth01((d - 0.62) / 0.12)
            self._cache[key] = m
        return self._cache[key]


def paste(frame, sprite, alpha, x, y, mode="over"):
    """Blend sprite (h,w,3) with alpha (h,w) at top-left x,y, clipped to the frame."""
    h, w = alpha.shape
    H, W = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x0 >= x1 or y0 >= y1:
        return None
    sa = alpha[y0 - y:y1 - y, x0 - x:x1 - x, None]
    sc = sprite[y0 - y:y1 - y, x0 - x:x1 - x] if sprite.ndim == 3 else sprite
    region = frame[y0:y1, x0:x1]
    if mode == "screen":
        region[:] = 1 - (1 - region) * (1 - sc * sa)
    else:
        region[:] = region * (1 - sa) + sc * sa
    return (x0, y0, x1, y1)


# --- effects ------------------------------------------------------------------------

class Particles:
    """Snow, petals, bubbles or confetti in the space, behind whatever is nearer."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        rng = np.random.default_rng(int(s["seed"]))
        self.rng = rng
        n = int(s["count"])
        W, H = ctx.w, ctx.h
        self.z = rng.uniform(0.08, 1.0, n).astype(np.float32)            # 1 = nearest the lens
        self.x = rng.uniform(-0.1, 1.1, n).astype(np.float32) * W
        self.y = rng.uniform(-0.1, 1.1, n).astype(np.float32) * H
        self.rot = rng.uniform(0, 2 * math.pi, n).astype(np.float32)
        self.spin = rng.uniform(-3, 3, n).astype(np.float32)
        self.phase = rng.uniform(0, 2 * math.pi, n).astype(np.float32)
        self.hue = rng.uniform(0, 1, n).astype(np.float32)
        kind = s["kind"]
        sp = s["speed"] * H
        self.born = np.zeros(n, np.float32)
        self.glitter = np.zeros(n, bool)
        if kind == "confetti":
            # cannons low in the frame fire at their times; the pieces flutter down after
            # (time, x, y, aim in degrees, spread in degrees)
            layouts = {1: [(0.0, 0.5, 1.02, -90, 55)],
                       2: [(0.0, 0.03, 1.02, -62, 22), (0.0, 0.97, 1.02, -118, 22)],
                       3: [(0.0, 0.03, 1.02, -62, 22), (0.0, 0.97, 1.02, -118, 22), (0.55, 0.5, 1.02, -90, 40)],
                       4: [(0.0, 0.03, 1.02, -62, 22), (0.0, 0.97, 1.02, -118, 22), (0.5, 0.5, 1.02, -90, 40),
                           (1.0, 0.2, 1.02, -75, 25), (1.0, 0.8, 1.02, -105, 25)]}
            cannons = layouts[int(s["bursts"])]
            which = rng.integers(0, len(cannons), n)
            self.z[:] = rng.uniform(0.5, 1.0, n)                    # in front of the scene
            v = rng.uniform(1.1, 2.6, n) * sp * 2.2
            self.vx, self.vy = np.zeros(n, np.float32), np.zeros(n, np.float32)
            for k, (bt, bx, by, aim, spread) in enumerate(cannons):
                sel = which == k
                m = int(sel.sum())
                ang = np.radians(aim + rng.normal(0, spread / 2, m))
                self.born[sel] = bt + rng.uniform(0, 0.08, m)
                self.x[sel] = W * bx + rng.normal(0, W * 0.015, m)
                self.y[sel] = H * by + rng.normal(0, H * 0.015, m)
                self.vx[sel] = np.cos(ang) * v[sel]
                self.vy[sel] = np.sin(ang) * v[sel]
            palette = [(1, .22, .35), (1, .78, .1), (.15, .7, 1), (.3, .9, .4), (1, 1, 1), (.85, .35, 1), (1, .5, .1)]
            self.color = np.array([palette[i] for i in rng.integers(0, len(palette), n)], np.float32)
            # a share of the pieces are glitter: small, gold or silver, twinkling
            self.glitter = rng.random(n) < s["glitter"]
            metal = np.array([(1, .85, .4), (.9, .92, 1)], np.float32)
            self.color[self.glitter] = metal[rng.integers(0, 2, int(self.glitter.sum()))]
        else:
            self.vx = np.zeros(n, np.float32)
            self.vy = np.zeros(n, np.float32)
            if kind == "petals":
                self.color = hsv(rng.uniform(0.93, 1.0, n), rng.uniform(0.15, 0.35, n), 1.0)
            else:
                self.color = np.ones((n, 3), np.float32)

    def sprite(self, i, r, t):
        kind, rot = self.s["kind"], self.rot[i]
        size = int(r * 2 + 4) | 1
        c = size // 2
        if kind == "snow":
            yy, xx = np.mgrid[:size, :size].astype(np.float32) - c
            d2 = (xx * xx + yy * yy) / max(c * c, 1)                # 1 at the patch edge
            a = np.clip((np.exp(-d2 * 3.2) - np.exp(-3.2)) / (1 - np.exp(-3.2)), 0, 1)
            return None, a
        if kind == "bubbles":
            yy, xx = np.mgrid[:size, :size].astype(np.float32) - c
            d = np.sqrt(xx * xx + yy * yy) / max(r, 1)
            rim = np.exp(-((d - 0.92) / 0.09) ** 2)
            glint = np.exp(-(((xx + r * 0.38) ** 2 + (yy + r * 0.38) ** 2) / (2 * (r * 0.16) ** 2)))
            a = np.clip(rim * 0.85 + (d < 1) * 0.06 + glint * 0.9, 0, 1)
            col = hsv(np.arctan2(yy, xx) / (2 * math.pi) + t * 0.3 + self.hue[i], 0.55, 1.0)
            return col, a
        # petals and confetti: a shape tumbling in 3D (its width breathes with the flip)
        a = np.zeros((size, size), np.float32)
        flip = abs(math.cos(self.phase[i] + t * (4 + self.spin[i])))
        deg = math.degrees(rot)
        if kind == "petals":
            cv2.ellipse(a, (c, c), (max(1, int(r)), max(1, int(r * 0.55 * (0.25 + 0.75 * flip)))), deg, 0, 360, 1.0, -1,
                        cv2.LINE_AA)
            a = cv2.GaussianBlur(a, (3, 3), 0)
        else:
            box = cv2.boxPoints(((c, c), (max(1.5, r * 1.6 * (0.15 + 0.85 * flip)), r * 0.9), deg))
            cv2.fillConvexPoly(a, box.astype(np.int32), 1.0, cv2.LINE_AA)
        return None, a

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        W, H = ctx.w, ctx.h
        kind = s["kind"]
        near = ctx.scene.depth() if s["depth"] else None
        # the camera moved: the space (and what is in it) moves with the picture, more for near things
        m = ctx.scene.motion
        z = self.z
        px = m[0, 0] * self.x + m[0, 1] * self.y + m[0, 2]
        py = m[1, 0] * self.x + m[1, 1] * self.y + m[1, 2]
        self.x += (px - self.x) * (0.3 + 0.7 * z)
        self.y += (py - self.y) * (0.3 + 0.7 * z)
        sp = s["speed"] * H
        wind = s["wind"] * H
        if kind == "confetti":
            live = (t >= self.born).astype(np.float32)
            d = dt * live
            self.vy += 1.1 * sp * d
            self.vx *= (1 - 1.5 * d)
            self.vy *= (1 - 1.5 * d)
            self.vy = np.where(live > 0, np.minimum(self.vy, sp * 0.35 * (0.4 + z)), self.vy)
            self.x += (self.vx + np.sin(self.phase + t * 5) * sp * 0.15 + wind) * d
            self.y += self.vy * d
        else:
            fall = {"snow": 0.35, "petals": 0.28, "bubbles": -0.22}[kind]
            sway = {"snow": 0.06, "petals": 0.16, "bubbles": 0.08}[kind]
            self.x += (np.sin(self.phase + t * 1.7) * sway * sp + wind * (0.4 + z)) * dt
            self.y += fall * sp * (0.35 + 0.9 * z) * dt
            # things that leave come back on the other side
            self.x = np.where(self.x < -0.12 * W, self.x + 1.24 * W, np.where(self.x > 1.12 * W, self.x - 1.24 * W, self.x))
            self.y = np.where(self.y < -0.12 * H, self.y + 1.24 * H, np.where(self.y > 1.12 * H, self.y - 1.24 * H, self.y))
        self.rot += self.spin * dt
        base = s["size"] * H
        order = np.argsort(z)                                   # far first
        for i in order:
            if t < self.born[i]:
                continue
            r = base * (0.25 + 1.35 * z[i] ** 1.5) * (0.45 if self.glitter[i] else 1.0)
            col, a = self.sprite(i, r, t)
            if kind in ("snow", "petals") and z[i] > 0.82:      # too close to focus on
                k = int(r * (z[i] - 0.82) * 1.2) * 2 + 1
                if k >= 3:
                    a = cv2.GaussianBlur(a, (k, k), 0)
            a = a * (s["opacity"] * (0.45 + 0.55 * z[i]))
            h = a.shape[0]
            x0, y0 = int(self.x[i]) - h // 2, int(self.y[i]) - h // 2
            if near is not None:
                ys, xs = slice(max(0, y0), min(H, y0 + h)), slice(max(0, x0), min(W, x0 + h))
                if ys.start >= ys.stop or xs.start >= xs.stop:
                    continue
                occ = smooth01((z[i] - near[ys, xs] + 0.06) / 0.12)
                full = np.zeros_like(a)
                full[ys.start - y0:ys.stop - y0, xs.start - x0:xs.stop - x0] = occ
                a = a * full
            if col is None and kind == "confetti":
                # the piece catches the light as it turns: brighter face-on, a white glint at the peak
                flip = abs(math.cos(self.phase[i] + t * (4 + self.spin[i])))
                shine = 0.55 + 0.7 * flip + (0.9 if self.glitter[i] and flip > 0.8 else 0.0)
                col = np.broadcast_to(np.minimum(1, self.color[i] * shine + (flip > 0.94) * 0.35), a.shape + (3,))
            color = col if col is not None else np.broadcast_to(self.color[i], a.shape + (3,))
            paste(frame, color, a, x0, y0, "screen" if kind == "bubbles" else "over")
        return frame


def draw_card(text, font, size, style):
    """The pinned sign / speech bubble, drawn once with Pillow: RGBA float array."""
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype(font, size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = probe.multiline_textbbox((0, 0), text, font=f, align="center", spacing=size // 5)
    tw, th = r - l, b - t
    pad = int(size * 0.55)
    tail = int(size * 0.7) if style == "speech" else 0
    w, h = tw + 2 * pad, th + 2 * pad
    shadow = int(size * 0.18)
    img = Image.new("RGBA", (w + shadow * 2, h + tail + shadow * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ox, oy = shadow, shadow
    if style == "neon":
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).multiline_text((ox + pad - l, oy + pad - t), text, font=f, fill=(255, 80, 200, 255),
                                            align="center", spacing=size // 5, stroke_width=max(2, size // 10),
                                            stroke_fill=(255, 80, 200, 255))
        from PIL import ImageFilter
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(size / 5)))
        d = ImageDraw.Draw(img)
        d.multiline_text((ox + pad - l, oy + pad - t), text, font=f, fill=(255, 235, 250, 255), align="center",
                         spacing=size // 5)
    else:
        rad = int(size * 0.5) if style == "speech" else int(size * 0.2)
        d.rounded_rectangle((ox + 3, oy + 5, ox + w + 3, oy + h + 5), rad, fill=(0, 0, 0, 70))
        d.rounded_rectangle((ox, oy, ox + w, oy + h), rad, fill=(255, 255, 255, 250))
        if tail:
            cx = ox + w // 2
            d.polygon([(cx - tail * 0.6, oy + h - 2), (cx + tail * 0.6, oy + h - 2), (cx - tail * 0.1, oy + h + tail)],
                      fill=(255, 255, 255, 250))
        if style == "card":
            d.rounded_rectangle((ox, oy, ox + w, oy + h), rad, outline=(0, 0, 0, 40), width=2)
        d.multiline_text((ox + pad - l, oy + pad - t), text, font=f, fill=(25, 25, 30, 255), align="center",
                         spacing=size // 5)
    a = np.asarray(img, np.float32) / 255
    return a, (ox + w // 2, oy + h + tail)          # the point that sits on the anchor


class Pin:
    """A sign that stays where it was put, as the camera moves (and goes behind people)."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        self.card, self.foot = draw_card(s["text"], s["font"], int(s["size"]), s["style"])
        self.at = None                                                      # where the foot is, frame 0
        self.world = np.eye(3, dtype=np.float32)                            # frame 0 -> now

    def spot(self):
        """The anchor: as given, or (auto) the candidate the person covers least."""
        s, ctx = self.s, self.ctx
        if not s["auto"]:
            return np.array([s["x"] * ctx.w, s["y"] * ctx.h], np.float32)
        m = ctx.scene.matte()
        if (m > 0.5).mean() < 0.02:                          # nobody in the way: put it in plain sight
            return np.array([0.5 * ctx.w, 0.62 * ctx.h], np.float32)
        ch, cw = self.card.shape[:2]
        best = None
        for fx, fy in ((0.24, 0.62), (0.76, 0.62), (0.26, 0.42), (0.74, 0.42), (0.5, 0.72), (0.5, 0.35)):
            x, y = fx * ctx.w, fy * ctx.h
            x0, x1 = int(max(0, x - cw / 2 - 40)), int(min(ctx.w, x + cw / 2 + 40))
            y0, y1 = int(max(0, y - ch)), int(min(ctx.h, y + 20))
            cover = float(m[y0:y1, x0:x1].mean()) if x1 > x0 and y1 > y0 else 1.0
            if best is None or cover < best[0] - 0.02:
                best = (cover, x, y)
        return np.array(best[1:], np.float32)

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        if self.at is None:
            self.at = self.spot()
        m = np.vstack([ctx.scene.motion, [0, 0, 1]]).astype(np.float32)
        self.world = m @ self.world
        k = pop(t, s["appear"])
        if k <= 0.001 or not s["text"].strip():
            return frame
        ch, cw = self.card.shape[:2]
        # card -> frame: scale about the foot (the pop), tilt, put the foot on the anchor, follow the world
        a = math.radians(s["tilt"])
        ca, sa = math.cos(a) * k, math.sin(a) * k
        fx, fy = self.foot
        place = np.array([[ca, -sa, self.at[0] - (ca * fx - sa * fy)],
                          [sa, ca, self.at[1] - (sa * fx + ca * fy)],
                          [0, 0, 1]], np.float32)
        full = (self.world @ place)[:2]
        layer = cv2.warpAffine(self.card, full, (ctx.w, ctx.h), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))
        alpha = layer[..., 3]
        if s["behind_people"]:
            alpha = alpha * (1 - ctx.scene.matte())
        frame[:] = frame * (1 - alpha[..., None]) + layer[..., :3] * alpha[..., None]
        return frame


class Speech:
    """A speech bubble above the person's head, following them a little late."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        self.card, self.foot = draw_card(s["text"], s["font"], int(s["size"]), "speech")
        self.follow = Spring(35.0)

    def head(self):
        m = self.ctx.scene.matte()
        small = cv2.resize(m, (m.shape[1] // 4, m.shape[0] // 4), interpolation=cv2.INTER_AREA) > 0.5
        if small.mean() < 0.01:
            return None
        n, lab, stats, _ = cv2.connectedComponentsWithStats(small.astype(np.uint8))
        if n <= 1:
            return None
        big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        top, height = stats[big, cv2.CC_STAT_TOP], stats[big, cv2.CC_STAT_HEIGHT]
        band = lab[top:top + max(2, int(height * 0.12))] == big
        ys, xs = np.nonzero(band)
        return np.array([xs.mean() * 4, top * 4], np.float32)

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        if not s["text"].strip():
            return frame
        target = self.head()
        if target is None:
            target = np.array([ctx.w * 0.5, ctx.h * 0.45], np.float32) if self.follow.x is None else self.follow.x
        else:
            target = target - np.array([0, ctx.h * 0.03], np.float32)
        x, y = self.follow.step(target, dt)
        k = pop(t, s["appear"])
        if k <= 0.001:
            return frame
        card = self.card
        if abs(k - 1) > 1e-3:
            card = cv2.resize(card, None, fx=k, fy=k, interpolation=cv2.INTER_LINEAR)
        ch, cw = card.shape[:2]
        fx, fy = self.foot[0] * k, self.foot[1] * k
        bob = math.sin(t * 3.2) * ctx.h * 0.004
        x0 = int(np.clip(x - fx, 8, ctx.w - cw - 8))
        y0 = int(np.clip(y - fy + bob, 8, ctx.h - ch - 8))
        paste(frame, card[..., :3], card[..., 3], x0, y0)
        return frame


class Aura:
    """A soft, shifting glow around the person's outline."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        rng = np.random.default_rng(7)
        n = rng.random((ctx.h // 8 + 64, ctx.w // 8 + 64)).astype(np.float32)
        self.noise = cv2.GaussianBlur(n, (0, 0), 6)
        self.noise = (self.noise - self.noise.min()) / (np.ptp(self.noise) + 1e-6)

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        m = ctx.scene.matte()
        width = s["width"] * ctx.h
        spread = cv2.GaussianBlur(m, (0, 0), max(1.0, width / 2.5))
        outside = (1 - m) ** 2
        glow = np.clip(spread - m, 0, 1) * 2.4 * outside
        rim = np.clip(cv2.dilate(m, np.ones((5, 5), np.uint8)) - m, 0, 1) * outside
        # a flame-like shimmer: noise drifting upward
        off = int(t * 40) % 64
        nz = cv2.resize(self.noise[off:off + ctx.h // 8, 0:ctx.w // 8], (ctx.w, ctx.h), interpolation=cv2.INTER_LINEAR)
        light = (glow * (0.55 + 0.9 * nz) + rim * 0.6) * s["strength"]
        yy = np.linspace(0, 1, ctx.h, dtype=np.float32)[:, None]
        if s["cycle"]:
            col = hsv(s["hue"] + yy * 0.25 + t * 0.35, 0.75, 1.0)
        else:
            col = np.broadcast_to(np.array(s["color"], np.float32), (ctx.h, 1, 3))
        frame[:] = 1 - (1 - frame) * (1 - np.clip(light[..., None] * col, 0, 1))
        return frame


class Background:
    """Keep the person (or whatever is nearest) and put them somewhere else."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        files = s["files"]
        pick = files[int(s["seed"]) % len(files)]
        img = cv2.cvtColor(cv2.imread(os.path.join(ctx.work, pick), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        # cover the frame with room to drift
        big = 1.12
        W, H = int(ctx.w * big), int(ctx.h * big)
        sc = max(W / img.shape[1], H / img.shape[0])
        img = cv2.resize(img, (int(img.shape[1] * sc) + 1, int(img.shape[0] * sc) + 1), interpolation=cv2.INTER_AREA)
        y0, x0 = (img.shape[0] - H) // 2, (img.shape[1] - W) // 2
        self.bg = img[y0:y0 + H, x0:x0 + W].astype(np.float32) / 255
        self.world = np.eye(3, dtype=np.float32)

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        m = ctx.scene.subject(s["subject"])
        m = cv2.GaussianBlur(m, (0, 0), 1.2)
        # the backdrop drifts slowly, and a little with the camera, so it does not look pasted on
        mo = np.vstack([ctx.scene.motion, [0, 0, 1]]).astype(np.float32)
        self.world = mo @ self.world
        k = 1.0 + s["drift"] * t / max(ctx.duration, 0.1)
        H, W = self.bg.shape[:2]
        room_x, room_y = (W - ctx.w) / 2 * 0.9, (H - ctx.h) / 2 * 0.9     # stay inside the backdrop
        dx = float(np.clip(self.world[0, 2] * 0.35, -room_x, room_x))
        dy = float(np.clip(self.world[1, 2] * 0.35, -room_y, room_y))
        cx, cy = W / 2 - dx, H / 2 - dy
        M = np.array([[k, 0, ctx.w / 2 - k * cx], [0, k, ctx.h / 2 - k * cy]], np.float32)
        bg = cv2.warpAffine(self.bg, M, (ctx.w, ctx.h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        # light wrap: a little of the new world bleeds onto the edges of the person
        edge = (m * (1 - m) * 4)[..., None]
        blur_bg = cv2.GaussianBlur(bg, (0, 0), 8)
        fg = frame * (1 - edge * 0.35) + blur_bg * edge * 0.35
        frame[:] = fg * m[..., None] + bg * (1 - m[..., None])
        return frame


class Glitch:
    """Rows slip, colours split, blocks tear: in short bursts, the first one early."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        rng = np.random.default_rng(int(s["seed"]))
        self.rng = rng
        n = max(1, int(s["rate"] * ctx.duration))
        starts = sorted(rng.uniform(0.15, max(0.2, ctx.duration - 0.15), n).tolist())
        starts[0] = rng.uniform(0.1, 0.45)                       # one inside what setlog keeps
        self.bursts = [(a, a + rng.uniform(0.12, 0.3)) for a in starts]

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        amt = s["amount"]
        W, H = ctx.w, ctx.h
        on = any(a <= t < b for a, b in self.bursts)
        base = int(round(3 + 7 * amt))                           # a faint split all the time
        out = frame.copy()
        out[..., 0] = np.roll(frame[..., 0], base, axis=1)
        out[..., 2] = np.roll(frame[..., 2], -base, axis=1)
        if on:
            rng = self.rng
            for _ in range(int(4 + 10 * amt)):                   # rows slipping sideways
                y = int(rng.integers(0, H - 8))
                h = int(rng.integers(4, max(6, int(H * 0.08 * amt) + 6)))
                dx = int(rng.normal(0, W * 0.06 * amt))
                out[y:y + h] = np.roll(out[y:y + h], dx, axis=1)
            sh = int(W * 0.015 * amt) + 3
            out[..., 0] = np.roll(out[..., 0], sh, axis=1)
            out[..., 1] = np.roll(out[..., 1], -sh // 2, axis=0)
            for _ in range(int(1 + 3 * amt)):                    # a torn block
                bw, bh = int(rng.integers(W // 20, W // 5)), int(rng.integers(H // 30, H // 8))
                x, y = int(rng.integers(0, W - bw)), int(rng.integers(0, H - bh))
                sx, sy = int(np.clip(x + rng.integers(-W // 8, W // 8), 0, W - bw)), y
                out[y:y + bh, x:x + bw] = out[sy:sy + bh, sx:sx + bw][..., ::-1] if rng.random() < 0.4 \
                    else np.round(out[sy:sy + bh, sx:sx + bw] * 4) / 4
            if rng.random() < 0.5:
                out[::2] *= 0.82                                  # interlace flicker
        frame[:] = out
        return frame


class Neon:
    """The scene goes dark and only its outlines glow, colours travelling along them."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        yy, xx = np.mgrid[:ctx.h, :ctx.w].astype(np.float32)
        self.pos = (xx / ctx.w * 0.6 + yy / ctx.h * 0.4)

    def __call__(self, frame, t, dt):
        s, ctx = self.s, self.ctx
        half = cv2.resize(frame, (ctx.w // 2, ctx.h // 2), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor((half * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        g = cv2.GaussianBlur(cv2.bilateralFilter(g, 9, 60, 9), (0, 0), 1.6)
        e = cv2.Canny(g, 60, 150)
        if (e > 0).mean() < 0.015:                           # a plain subject: take fainter outlines too
            e = cv2.Canny(g, 15, 45)
        if s["thickness"] > 1:
            e = cv2.dilate(e, np.ones((int(s["thickness"]), int(s["thickness"])), np.uint8))
        e = cv2.resize(e.astype(np.float32) / 255, (ctx.w, ctx.h), interpolation=cv2.INTER_LINEAR)
        wave = 0.5 + 0.5 * np.sin(2 * math.pi * (self.pos * 0.8 + t * s["cycle"]))
        col = hsv(0.5 + 0.42 * wave, 0.8, 1.0) if s["cycle"] else np.broadcast_to(
            np.array(s["color"], np.float32), (ctx.h, ctx.w, 3))
        line = e[..., None] * col
        glow = cv2.GaussianBlur(line, (0, 0), 5) * s["glow"] * 1.3
        frame[:] = np.clip(frame * s["dim"] + line + glow, 0, 1)
        return frame


class Trail:
    """What moves leaves a trail that fades and changes colour as it ages."""

    def __init__(self, s, ctx):
        self.s, self.ctx = s, ctx
        self.buf = self.prev = None

    def __call__(self, frame, t, dt):
        s = self.s
        small = cv2.resize(frame, (self.ctx.w // 4, self.ctx.h // 4), interpolation=cv2.INTER_AREA)
        if self.prev is None:
            self.prev, self.buf = small, np.zeros_like(frame)
            return frame
        # where the picture changed, the camera's own movement included: a swung phone
        # leaves light trails like a long exposure
        diff = np.abs(small - self.prev).max(-1)
        moving = smooth01((cv2.GaussianBlur(diff, (0, 0), 1.5) - 0.025) / 0.08)
        moving = cv2.resize(moving, (self.ctx.w, self.ctx.h), interpolation=cv2.INTER_LINEAR)
        self.prev = small
        buf = self.buf * s["decay"]
        if s["hue_shift"]:
            hs = cv2.cvtColor(np.clip(buf, 0, 1), cv2.COLOR_RGB2HSV)
            hs[..., 0] = (hs[..., 0] + s["hue_shift"] * 360 * dt) % 360
            hs[..., 1] = np.minimum(1, hs[..., 1] * 1.08 + 0.04)
            buf = cv2.cvtColor(hs, cv2.COLOR_HSV2RGB)
        ghost = frame * (0.35 + 0.65 * moving[..., None]) * moving[..., None]
        self.buf = np.maximum(buf, ghost)
        frame[:] = 1 - (1 - frame) * (1 - self.buf * s["strength"] * (1 - moving[..., None] * 0.7))
        return frame


KINDS = {"particles": Particles, "pin": Pin, "speech": Speech, "aura": Aura, "background": Background,
         "glitch": Glitch, "neon": Neon, "trail": Trail}


class Ctx:
    def __init__(self, spec):
        self.w, self.h, self.fps = spec["width"], spec["height"], spec["fps"]
        self.duration, self.work = spec["duration"], spec["work"]
        self.scene = Scene(self.w, self.h)


def main():
    if sys.argv[1:] == ["--fetch-models"]:           # bin/setup-ar.sh: download them now, not on the first post
        for name in MODEL_FILES:
            print(name, model_path(name))
        return
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    ctx = Ctx(spec)
    effects = [KINDS[e["kind"]](e["settings"], ctx) for e in spec["effects"]]
    W, H, fps = ctx.w, ctx.h, ctx.fps
    dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", spec["input"], "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "12", "-pix_fmt", "yuv420p", spec["output"]], stdin=subprocess.PIPE)
    n, size, dt = 0, W * H * 3, 1.0 / fps
    try:
        while True:
            raw = dec.stdout.read(size)
            if len(raw) < size:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3).astype(np.float32) / 255
            ctx.scene.next(frame, n)
            t = n * dt
            for fx in effects:
                frame = fx(frame, t, dt)
            enc.stdin.write((np.clip(frame, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes())
            n += 1
    finally:
        enc.stdin.close()
        enc.wait()
        dec.wait()
    if enc.returncode or n == 0:
        raise SystemExit(f"ar.py: encoding failed after {n} frames")


if __name__ == "__main__":
    main()
