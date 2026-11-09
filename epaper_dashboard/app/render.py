"""View -> HTML -> headless Chromium screenshot -> 4 panel colors -> .epd frame (and 1-bit PNGs for the kitchen)."""
import io
import os
import re
import shutil
import subprocess

import numpy as np
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
W, H = 1360, 480
HALF = W // 2
FRAME_BYTES = (HALF // 4) * H * 2           # 163,200

# Panel color codes: 0 black, 1 white, 2 yellow, 3 red
PAL = np.array([[0, 0, 0], [255, 255, 255], [255, 255, 0], [255, 0, 0]], dtype=np.int32)
SIM = np.array([[38, 36, 34], [232, 228, 216], [226, 184, 24], [178, 40, 38]], dtype=np.uint8)  # ink look

_icons = {}


def icon(name, size, stroke=2.5):
    if name not in _icons:
        with open(os.path.join(STATIC, "icons", name + ".svg")) as f:
            svg = re.sub(r"<!--.*?-->", "", f.read(), flags=re.S).strip()
        _icons[name] = re.sub(r'\s+class="[^"]*"', "", svg, count=1)
    svg = _icons[name]
    svg = svg.replace('width="24"', f'width="{size}"', 1).replace('height="24"', f'height="{size}"', 1)
    svg = svg.replace('stroke-width="2"', f'stroke-width="{stroke}"', 1).replace("<svg", '<svg class="ic"', 1)
    return Markup(svg)


def tri(up):
    pts = "6,0 12,10 0,10" if up else "0,0 12,0 6,10"
    return Markup(f'<svg width="12" height="10" viewBox="0 0 12 10"><polygon points="{pts}" fill="currentColor"/></svg>')


def spark(uid, data, base, w, h, slots=0, sw=2.5):
    """Day chart: black line, yellow fill above the previous close, red below.
    `slots` stretches the x axis to the full session so the line grows through the day."""
    known = [(i, v) for i, v in enumerate(data or []) if v is not None]   # gaps are skipped
    if len(known) < 2:
        return Markup(f'<svg width="{w}" height="{h}"></svg>')
    n = max(slots or 0, len(data))
    vals = [v for _, v in known] + ([base] if base is not None else [])
    lo, hi, pad = min(vals), max(vals), 3
    span = (hi - lo) or 1

    def X(i):
        return pad + i * (w - 2 * pad) / (n - 1)

    def Y(v):
        return pad + (h - 2 * pad) * (1 - (v - lo) / span)

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in known)
    uid = re.sub(r"[^A-Za-z0-9]", "", uid)
    s = f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
    if base is not None:
        by = f"{Y(base):.1f}"
        poly = f"{X(known[0][0]):.1f},{by} {pts} {X(known[-1][0]):.1f},{by}"
        s += (f'<clipPath id="a{uid}"><rect x="0" y="0" width="{w}" height="{by}"/></clipPath>'
              f'<clipPath id="b{uid}"><rect x="0" y="{by}" width="{w}" height="{h}"/></clipPath>'
              f'<polygon points="{poly}" fill="#ff0" clip-path="url(#a{uid})"/>'
              f'<polygon points="{poly}" fill="#f00" clip-path="url(#b{uid})"/>'
              f'<line x1="0" x2="{w}" y1="{by}" y2="{by}" stroke="#000" stroke-width="2" stroke-dasharray="4 4"/>')
    s += f'<polyline points="{pts}" fill="none" stroke="#000" stroke-width="{sw}" stroke-linejoin="round"/></svg>'
    return Markup(s)


_env = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                   autoescape=select_autoescape(["html"]))


def page(vm, clock=None):
    return _env.get_template("dashboard.html").render(
        vm=vm, clock=clock, static="file://" + STATIC, icon=icon, tri=tri, spark=spark)


def chromium():
    for c in (os.environ.get("CHROMIUM"), "chromium", "chromium-browser", "google-chrome"):
        if c and shutil.which(c):
            return shutil.which(c)
    raise RuntimeError("Chromium not found")


def screenshot(html, workdir, size=(W, H), name="dash"):
    w, h = size
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    src, out = os.path.join(workdir, name + ".html"), os.path.join(workdir, name + ".png")
    with open(src, "w") as f:
        f.write(html)
    if os.path.exists(out):
        os.remove(out)
    cmd = [chromium(), "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
           "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={w},{h}",
           # pixel-hinted text, no smoothing (the style picked on the real panel)
           "--font-render-hinting=full", "--disable-lcd-text", "--disable-font-subpixel-positioning",
           "--disable-component-update", "--disable-background-networking", "--disable-extensions",
           "--disable-sync", "--no-first-run", "--no-default-browser-check", "--mute-audio",
           # (no --user-data-dir: headless uses a throwaway profile; a fixed one makes it hang)
           "--virtual-time-budget=3000", f"--screenshot={out}", "file://" + src]
    env = dict(os.environ, FONTCONFIG_FILE=os.path.join(HERE, "fonts.conf"))
    p = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
    if not os.path.exists(out):
        tail = p.stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise RuntimeError("Chromium didn't produce a screenshot: " + " | ".join(tail))
    img = Image.open(out).convert("RGB")
    img.load()
    if img.size != (w, h):
        raise RuntimeError(f"screenshot is {img.size}, expected {w}x{h}")
    return img


def mono_png(img):
    """Black and white only (no dithering), as a 1-bit PNG for ESPHome's online_image."""
    g = np.asarray(img.convert("L"))
    bw = Image.fromarray(np.where(g >= 128, 255, 0).astype(np.uint8)).convert("1", dither=Image.Dither.NONE)
    b = io.BytesIO()
    bw.save(b, "PNG", optimize=True)
    return b.getvalue()


def to_codes(img):
    a = np.asarray(img, dtype=np.int32)
    d = ((a[:, :, None, :] - PAL[None, None, :, :]) ** 2).sum(-1)
    return d.argmin(-1).astype(np.uint8)          # nearest color, no dithering


def pack(codes):
    """Left half (master controller) then right half (slave); 4 pixels per byte, leftmost in the top bits."""
    out = bytearray()
    for half in (codes[:, :HALF], codes[:, HALF:]):
        q = half.reshape(H, HALF // 4, 4)
        out += ((q[:, :, 0] << 6) | (q[:, :, 1] << 4) | (q[:, :, 2] << 2) | q[:, :, 3]).astype(np.uint8).tobytes()
    assert len(out) == FRAME_BYTES
    return bytes(out)


def unpack(frame):
    """.epd bytes back to color codes (for the preview after a restart)."""
    a = np.frombuffer(frame, dtype=np.uint8)
    half = a[: len(a) // 2], a[len(a) // 2:]
    cols = []
    for h in half:
        h = h.reshape(H, HALF // 4)
        cols.append(np.stack([(h >> 6) & 3, (h >> 4) & 3, (h >> 2) & 3, h & 3], axis=-1).reshape(H, HALF))
    return np.concatenate(cols, axis=1).astype(np.uint8)


def previews(codes):
    counts = np.bincount(codes.ravel(), minlength=4).tolist()
    return png(SIM[codes]), png(PAL[codes].astype(np.uint8)), dict(zip(("black", "white", "yellow", "red"), counts))


def png(arr):
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "PNG", optimize=True)
    return b.getvalue()


def render(vm, clock, workdir):
    """clock: {"h": "2", "m": "47"} or None (no clock, e.g. in tomorrow mode)."""
    codes = to_codes(screenshot(page(vm, clock), workdir))
    sim, panel, colors = previews(codes)
    return {"epd": pack(codes), "sim_png": sim, "panel_png": panel, "colors": colors}
