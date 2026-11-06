r"""
make_mockups.py -- batch mockup generator (reads PSDs, NEVER modifies them).

For every artwork in .\artworks and every usable PSD in .\mockups it:
  * finds the visible Smart Object slot(s) and reads their true placement quad
    (nonAffineTransform = the perspective quad Photoshop stored, not the bounding box),
  * crops the artwork to fill the slot's shape (centre crop, never stretched),
  * warps it onto the quad with a supersampled perspective transform,
  * swaps it into the PSD layer stack IN MEMORY, keeping that layer's blend mode,
    masks, clipping and every light / shadow / glare layer around it,
  * composites with psd-tools and saves a high-quality JPG.

Templates that cannot be handled safely are skipped WITH A REASON (see run_report.txt).

Usage (from the project folder):
    python make_mockups.py --selftest                  # quick check that your install behaves
    python make_mockups.py --dry-run                   # no rendering: shows plan, crops, skips
    python make_mockups.py --only "Living Room"        # one template (substring of file name)
    python make_mockups.py --art "greenpink"           # one artwork (substring)
    python make_mockups.py                             # everything

Needs probe_psds.py in the same folder. Output: .\output\mockups\<artwork>\verified|check\*.jpg
"""
import argparse
import gc
import io
import os
import json
import math
import sys
import time
import traceback
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageCms, ImageDraw, ImageFont
    from psd_tools import PSDImage
    from psd_tools.constants import Resource
except Exception as e:  # pragma: no cover
    print("Missing dependency:", e)
    print('Install with:  python -m pip install "psd-tools[composite]" scipy aggdraw pillow numpy')
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_psds as P  # noqa: E402  (shared PSD-reading helpers)

ROOT = Path(__file__).resolve().parent
ART_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
Image.MAX_IMAGE_PIXELS = None  # large PSD templates (~90 MP) are legitimate
SS = 2  # supersampling factor for the perspective warp


# =========================================================== geometry
def solve_perspective(out_pts, in_pts):
    """Pillow PERSPECTIVE coefficients mapping OUTPUT (x,y) -> INPUT (X,Y)."""
    A, b = [], []
    for (x, y), (X, Y) in zip(out_pts, in_pts):
        A.append([x, y, 1, 0, 0, 0, -x * X, -y * X]); b.append(X)
        A.append([0, 0, 0, x, y, 1, -x * Y, -y * Y]); b.append(Y)
    return tuple(float(v) for v in np.linalg.solve(np.array(A, dtype=np.float64),
                                                    np.array(b, dtype=np.float64)))


def quad_edges(q):
    x1, y1, x2, y2, x3, y3, x4, y4 = q
    top = math.hypot(x2 - x1, y2 - y1)
    bot = math.hypot(x3 - x4, y3 - y4)
    left = math.hypot(x4 - x1, y4 - y1)
    right = math.hypot(x3 - x2, y3 - y2)
    return top, bot, left, right


def polygon_area(q):
    xs, ys = q[0::2], q[1::2]
    return 0.5 * abs(sum(xs[i] * ys[(i + 1) % 4] - xs[(i + 1) % 4] * ys[i] for i in range(4)))


def is_convex(q):
    pts = list(zip(q[0::2], q[1::2]))
    sign = 0
    for i in range(4):
        ax, ay = pts[i]; bx, by = pts[(i + 1) % 4]; cx, cy = pts[(i + 2) % 4]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) < 1e-6:
            return False
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def cover_crop(im, target_aspect):
    """Centre-crop to target_aspect (w/h). Returns (image, fraction_of_area_lost)."""
    w, h = im.size
    a = w / h
    if a > target_aspect:
        nw = max(1, round(h * target_aspect)); x0 = (w - nw) // 2
        return im.crop((x0, 0, x0 + nw, h)), 1 - nw / w
    nh = max(1, round(w / target_aspect)); y0 = (h - nh) // 2
    return im.crop((0, y0, w, y0 + nh)), 1 - nh / h


def warp_to_quad(art_rgb, quad, canvas_w, canvas_h):
    """Perspective-warp art_rgb onto quad (TL,TR,BR,BL canvas px).
    Returns (RGBA image, left, top) clipped to the canvas, or None if fully outside."""
    xs, ys = quad[0::2], quad[1::2]
    x0 = max(0, math.floor(min(xs)) - 2); y0 = max(0, math.floor(min(ys)) - 2)
    x1 = min(canvas_w, math.ceil(max(xs)) + 2); y1 = min(canvas_h, math.ceil(max(ys)) + 2)
    if x1 <= x0 or y1 <= y0:
        return None
    w, h = x1 - x0, y1 - y0
    top, bot, left, right = quad_edges(quad)
    sw = max(2, round((top + bot) / 2 * SS))
    sh = max(2, round((left + right) / 2 * SS))
    src = art_rgb.resize((sw, sh), Image.LANCZOS)
    pad = 4  # replicate edges so bicubic never pulls in black at the border
    arr = np.pad(np.asarray(src), ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    src_p = Image.fromarray(arr)
    msk = Image.new("L", (sw + 2 * pad, sh + 2 * pad), 0)
    msk.paste(255, (pad, pad, pad + sw, pad + sh))
    out_pts = [((qx - x0) * SS, (qy - y0) * SS) for qx, qy in zip(xs, ys)]
    in_pts = [(pad, pad), (pad + sw, pad), (pad + sw, pad + sh), (pad, pad + sh)]
    co = solve_perspective(out_pts, in_pts)
    size = (w * SS, h * SS)
    rgb = src_p.transform(size, Image.PERSPECTIVE, co, Image.BICUBIC).reduce(SS)
    alpha = msk.transform(size, Image.PERSPECTIVE, co, Image.BILINEAR).reduce(SS)
    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba, x0, y0


# =========================================================== colour
def _profile(data):
    return ImageCms.ImageCmsProfile(io.BytesIO(data))


def to_doc_space(im, src_icc, doc_icc):
    """Convert artwork pixels into the PSD's colour space (psd-tools converts the
    finished composite to sRGB afterwards, so a round trip leaves colours intact)."""
    if not doc_icc:
        return im
    try:
        src = _profile(src_icc) if src_icc else ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        dst = _profile(doc_icc)
        intent = getattr(getattr(ImageCms, "Intent", ImageCms), "RELATIVE_COLORIMETRIC", 1)
        flags = getattr(getattr(ImageCms, "Flags", ImageCms), "BLACKPOINT_COMPENSATION", 0)
        return ImageCms.profileToProfile(im, src, dst, renderingIntent=intent,
                                         outputMode="RGB", flags=flags)
    except Exception:
        return im


def load_artwork(path):
    im = Image.open(path)
    im.load()
    icc = im.info.get("icc_profile")
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg.convert("RGB")
    elif im.mode == "CMYK" and icc:
        try:
            im = ImageCms.profileToProfile(im, _profile(icc),
                                           ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")),
                                           outputMode="RGB")
            icc = None
        except Exception:
            im = im.convert("RGB")
    else:
        im = im.convert("RGB")
    return im, icc


# =========================================================== in-memory layer swap
class PixelSwap:
    """Temporarily make `layer` render the given RGBA pixels at (left, top).
    The layer keeps its blend mode, opacity, masks, clipping and effects.
    Nothing is written to disk."""

    def __init__(self, layer, rgba, left, top):
        self.layer, self.rgba, self.left, self.top = layer, rgba, left, top

    def __enter__(self):
        L = self.layer
        rec = L._record
        self._saved = (rec.left, rec.top, rec.right, rec.bottom)
        self._had_instance_numpy = "numpy" in L.__dict__
        self._orig_numpy = L.numpy
        arr = np.asarray(self.rgba, dtype=np.float32) / 255.0
        color = np.ascontiguousarray(arr[..., :3])
        shape = np.ascontiguousarray(arr[..., 3:4])
        orig = self._orig_numpy

        def numpy(channel=None, real_mask=True):
            if channel == "color":
                return color
            if channel in ("shape", "alpha"):
                return shape
            if channel is None:
                return np.concatenate([color, shape], axis=2)
            return orig(channel, real_mask=real_mask)

        L.numpy = numpy
        rec.left, rec.top = self.left, self.top
        rec.right, rec.bottom = self.left + self.rgba.width, self.top + self.rgba.height
        L._invalidate_bbox()
        return self

    def __exit__(self, *exc):
        L = self.layer
        rec = L._record
        rec.left, rec.top, rec.right, rec.bottom = self._saved
        if self._had_instance_numpy:
            L.numpy = self._orig_numpy
        else:
            del L.numpy
        L._invalidate_bbox()
        return False


# =========================================================== template analysis
def slot_problem(info, psd):
    """Return a reason string if this slot can't be handled safely, else None."""
    if "smart_object_error" in info:
        return "Smart Object data unreadable (%s)" % info["smart_object_error"]
    g = info.get("geometry", "")
    if g.startswith("WARPED"):
        return "slot uses a mesh warp (%s) - not a plain perspective quad" % g
    if info.get("has_filter_effects"):
        return "slot has Smart Filters that cannot be re-applied"
    q = info.get("nonAffineTransform") or info.get("Trnf")
    if not q or len(q) != 8:
        return "no placement quad found"
    if not is_convex(q) or polygon_area(q) < 100:
        return "placement quad is degenerate"
    cs = info.get("content_size")
    if not cs or not cs[0] or not cs[1]:
        return "no content size found"
    return None


def analyse_template(psd):
    """-> (slots, template_problem). slots = list of dicts with layer + info."""
    if str(psd.color_mode) not in ("ColorMode.RGB", "3", "RGB") and getattr(psd.color_mode, "name", "") != "RGB":
        return [], "colour mode %s is not RGB" % psd.color_mode
    if psd.depth not in (8, 16):
        return [], "bit depth %s unsupported" % psd.depth
    slots = []
    for layer, path, eff, masks in P.walk(psd):
        if layer.kind != "smartobject" or not eff:
            continue
        info = P.smart_object_info(layer, path, eff, masks)
        prob = slot_problem(info, psd)
        if prob:
            return [], "slot '%s': %s" % (info["name"].strip(), prob)
        q = info.get("nonAffineTransform") or info.get("Trnf")
        cw, ch = float(info["content_size"][0]), float(info["content_size"][1])
        slots.append({"layer": layer, "info": info, "quad": q, "aspect": cw / ch,
                      "name": info["path"], "uid": info.get("unique_id")})
    if not slots:
        return [], "no visible Smart Object slot found"
    slots.sort(key=lambda s: (min(s["quad"][0::2]), min(s["quad"][1::2])))
    return slots, None


# =========================================================== rendering
def _to_rgb(img):
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def render_one(psd, slots, art_list, doc_icc, canvas, quality, out_path, args, state):
    """art_list: one (PIL RGB, icc, name) per slot. Returns (reports, image or None).
    `state` is per-template scratch: after the first full render, later prints only
    re-render the area around the frames and are pasted onto that first render."""
    reports, swaps = [], []
    for slot, (art, icc, aname) in zip(slots, art_list):
        crop, loss = cover_crop(art, slot["aspect"])
        top, bot, left, right = quad_edges(slot["quad"])
        eff_w = (top + bot) / 2
        scale = crop.width / eff_w if eff_w else 0
        rep = {"slot": slot["name"], "crop_loss_pct": round(loss * 100, 1),
               "art_px_per_slot_px": round(scale, 2)}
        if loss > args.skip_crop:
            rep["skip"] = "needs %.0f%% crop (shape mismatch, limit %.0f%%)" % (loss * 100, args.skip_crop * 100)
            reports.append(rep)
            return reports, None
        if loss > args.warn_crop:
            rep["warn"] = "crops %.0f%% of the print" % (loss * 100)
        if scale < 1.0:
            rep["warn_res"] = "print is upscaled %.0f%% for this slot (low resolution)" % ((1 / scale - 1) * 100)
        tgt = (max(2, round(eff_w * SS)), max(2, round((left + right) / 2 * SS)))
        work = crop.resize(tgt, Image.LANCZOS) if min(crop.size) > min(tgt) else crop
        work = to_doc_space(work, icc, doc_icc)
        res = warp_to_quad(work, slot["quad"], canvas[0], canvas[1])
        if res is None:
            rep["skip"] = "slot lies outside the canvas"
            reports.append(rep)
            return reports, None
        rgba, x0, y0 = res
        swaps.append(PixelSwap(slot["layer"], rgba, x0, y0))
        reports.append(rep)

    # area that can change when the prints change: the slot footprints plus a margin for
    # shadows/glows attached to the slot layers
    rx0 = min(sw.left for sw in swaps); ry0 = min(sw.top for sw in swaps)
    rx1 = max(sw.left + sw.rgba.width for sw in swaps); ry1 = max(sw.top + sw.rgba.height for sw in swaps)
    margin = max(160, int(0.10 * max(rx1 - rx0, ry1 - ry0)))
    region = (max(0, rx0 - margin), max(0, ry0 - margin), min(canvas[0], rx1 + margin), min(canvas[1], ry1 + margin))
    area_frac = (region[2] - region[0]) * (region[3] - region[1]) / float(canvas[0] * canvas[1])
    use_fast = (not args.no_fast) and state.get("base") is not None and area_frac < 0.6

    for sw in swaps:
        sw.__enter__()
    try:
        if use_fast:
            part = _to_rgb(psd.composite(viewport=region, ignore_preview=True))
        else:
            full = _to_rgb(psd.composite(ignore_preview=True))
    finally:
        for sw in reversed(swaps):
            sw.__exit__(None, None, None)
    if use_fast:
        img = state["base"].copy()
        img.paste(part, (region[0], region[1]))
    else:
        img = full
        if not args.no_fast:
            state["base"] = full
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".partial")
    img.save(tmp_path, "JPEG", quality=quality, subsampling=0, optimize=True, icc_profile=srgb)
    os.replace(tmp_path, out_path)   # a crash can never leave a half-written .jpg
    reports[0]["fast"] = bool(use_fast)
    return reports, img


def make_contact_sheet(paths, out_file, thumb_w=420):
    thumbs = []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((thumb_w, thumb_w))
            thumbs.append((p.stem[:46], im))
        except Exception:
            pass
    if not thumbs:
        return
    cols = 4
    cell_h = max(t.height for _, t in thumbs) + 22
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * (thumb_w + 8) + 8, rows * (cell_h + 8) + 8), (245, 245, 245))
    d = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, (name, t) in enumerate(thumbs):
        cx = 8 + (i % cols) * (thumb_w + 8); cy = 8 + (i // cols) * (cell_h + 8)
        sheet.paste(t, (cx, cy))
        d.text((cx, cy + t.height + 4), name, fill=(40, 40, 40), font=font)
    sheet.save(out_file, quality=88)


# =========================================================== discovery
def discover_templates(mock_dir, only):
    files = sorted(list(mock_dir.glob("*.psd")) + list(mock_dir.glob("*.psb")))
    by_size = {}
    for f in files:
        by_size.setdefault(f.stat().st_size, []).append(f)
    dup_of = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        seen = {}
        for f in sorted(group, key=lambda p: (" - Copy" in p.name, len(p.name), p.name)):
            h = P.file_hash(f)
            if h in seen:
                dup_of[f.name] = seen[h]
            else:
                seen[h] = f.name
    keep = [f for f in files if f.name not in dup_of]
    if only:
        keep = [f for f in keep if only.lower() in f.name.lower()]
    return keep, dup_of


def load_probe_status(out_root):
    status = {}
    pj = out_root / "probe" / "probe_results.json"
    if pj.exists():
        try:
            for r in json.loads(pj.read_text(encoding="utf-8")):
                if "parity" in r:
                    status[r["file"]] = r["parity"].get("status")
        except Exception:
            pass
    return status


# =========================================================== self test
def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
        ok = ok and cond

    print("SELF-TEST 1: perspective warp lands on the right corners")
    art = Image.new("RGB", (400, 600), (255, 255, 255))
    d = ImageDraw.Draw(art)
    d.rectangle((0, 0, 120, 120), fill=(255, 0, 0))        # TL red
    d.rectangle((280, 0, 400, 120), fill=(0, 255, 0))      # TR green
    d.rectangle((280, 480, 400, 600), fill=(0, 0, 255))    # BR blue
    d.rectangle((0, 480, 120, 600), fill=(255, 255, 0))    # BL yellow
    quad = [100.0, 50.0, 300.0, 80.0, 280.0, 360.0, 90.0, 330.0]
    res = warp_to_quad(art, quad, 400, 400)
    check("warp returns pixels", res is not None)
    rgba, x0, y0 = res
    cx = sum(quad[0::2]) / 4; cy = sum(quad[1::2]) / 4

    def sample(px, py):
        return rgba.getpixel((int(px - x0), int(py - y0)))

    def toward(qx, qy, f=0.07):
        return qx + (cx - qx) * f * 4, qy + (cy - qy) * f * 4

    for nm, (qx, qy), want in (("TL", quad[0:2], (255, 0, 0)), ("TR", quad[2:4], (0, 255, 0)),
                                ("BR", quad[4:6], (0, 0, 255)), ("BL", quad[6:8], (255, 255, 0))):
        px, py = toward(qx, qy)
        got = sample(px, py)
        check("corner %s colour" % nm, all(abs(a - b) < 40 for a, b in zip(got[:3], want)) and got[3] > 250,
              "got %s" % (got,))
    outside = sample(quad[0] - 1, quad[1] - 1) if quad[0] - 1 >= x0 and quad[1] - 1 >= y0 else (0, 0, 0, 0)
    check("outside the quad is transparent", outside[3] < 40, "alpha=%s" % outside[3])
    cov = sum(1 for a in rgba.getchannel("A").tobytes() if a > 127)
    check("covered area matches quad area", abs(cov - polygon_area(quad)) / polygon_area(quad) < 0.02,
          "%d vs %.0f px" % (cov, polygon_area(quad)))

    print("SELF-TEST 2: in-memory swap keeps blend mode and leaves the rest untouched")
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import BlendMode
    psd = PSDImage.new("RGB", (400, 400), color=(200, 200, 200))
    ph = Image.new("RGBA", (300, 300), (90, 90, 90, 255))
    bgl = PixelLayer.frompil(Image.new("RGBA", (400, 400), (200, 200, 200, 255)), psd, "bg", top=0, left=0)
    psd.append(bgl)
    layer = PixelLayer.frompil(ph, psd, "slot", top=50, left=50)
    layer.blend_mode = BlendMode.MULTIPLY
    psd.append(layer)
    layer.visible = False
    base = np.asarray(psd.composite(ignore_preview=True).convert("RGB"), dtype=np.int16)
    layer.visible = True
    res = warp_to_quad(art, quad, 400, 400)
    with PixelSwap(layer, res[0], res[1], res[2]):
        out = np.asarray(psd.composite(ignore_preview=True).convert("RGB"), dtype=np.int16)
    plain1 = np.asarray(psd.composite(ignore_preview=True).convert("RGB"), dtype=np.int16)
    check("layer restored after swap (placeholder renders again)",
          int(abs(plain1[200, 200] - 200 * (90 / 255)).max()) < 3 and int(abs(plain1[10, 10] - 200).max()) == 0,
          "centre=%s" % (plain1[200, 200],))
    px, py = toward(*quad[0:2])
    got = out[int(py), int(px)]
    # multiply of red (255,0,0) over grey 200 -> (200,0,0)
    check("MULTIPLY blend preserved inside quad", abs(int(got[0]) - 200) < 6 and got[1] < 6 and got[2] < 6,
          "pixel=%s expected ~(200,0,0)" % (tuple(int(v) for v in got),))
    mask = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(mask).polygon(list(zip(quad[0::2], quad[1::2])), fill=255)
    from PIL import ImageFilter
    m = np.asarray(mask.filter(ImageFilter.MaxFilter(9))) > 0
    outside_diff = np.abs(out - base).max(axis=2)[~m].max()
    check("pixels outside the slot are untouched", int(outside_diff) == 0, "max diff %d" % outside_diff)
    print("SELF-TEST RESULT:", "ALL PASS" if ok else "FAILURES - see the checks above")
    return 0 if ok else 1


# =========================================================== template cache (lets --resume skip opening PSDs)
def tkey(tpath):
    st = tpath.stat()
    return "%d|%d" % (st.st_size, int(st.st_mtime))


_SIZE_CACHE = {}


def art_aspect(path):
    k = str(path)
    if k not in _SIZE_CACHE:
        with Image.open(path) as im:
            _SIZE_CACHE[k] = im.size
    w, h = _SIZE_CACHE[k]
    return w / h


def crop_loss_for(aspect_art, aspect_slot):
    if aspect_art > aspect_slot:
        return 1 - aspect_slot / aspect_art
    return 1 - aspect_art / aspect_slot


def load_slot_cache(out_dir):
    f = out_dir / "template_cache.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_slot_cache(out_dir, cache):
    try:
        (out_dir / "template_cache.json").write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except Exception:
        pass


# =========================================================== per-template job
_ART_CACHE = {}


def _get_art(path):
    key = str(path)
    if key not in _ART_CACHE:
        _ART_CACHE[key] = load_artwork(path)
    return _ART_CACHE[key]


def process_template(job):
    """Runs in this process or in a worker. Returns (log_lines, results, outputs)
    outputs = list of (art_key_string, output_path_string)."""
    args, tpath, art_paths, status, out_dir, live_log, slotcache = job
    lines, results, outputs = [], [], []

    def log(msg):
        lines.append(msg)
        if live_log:
            print(msg, flush=True)

    t0 = time.time()
    log("-" * 70)
    log("%s" % tpath.name)
    psd = None
    cache_entry = None
    try:
        # ---- shortcut: decide from the cache whether this template needs opening at all
        ce = slotcache.get(tpath.name)
        if ce and ce.get("key") == tkey(tpath):
            if ce.get("problem"):
                log("  SKIPPED (known): %s" % ce["problem"])
                results.append({"template": tpath.name, "skipped": ce["problem"]})
                return lines, results, outputs, None
            n = len(ce["aspects"])
            if args.solo and n != 1:
                log("  not a single-frame template (%d frames), left out by --solo" % n)
                return lines, results, outputs, None
            if args.multi and n < 2:
                log("  single-frame template, left out by --multi")
                return lines, results, outputs, None
            if args.resume and not (args.set or args.mix or args.dry_run):
                st0 = status.get(tpath.name)
                b0 = "verified" if (st0 == "PARITY_OK" or (args.trust_nopreview and st0 == "NO_PREVIEW")) else "check"
                todo = 0
                for p in art_paths:
                    op = out_dir / p.stem / b0 / ("%s - %s.jpg" % (tpath.stem, p.stem))
                    if op.exists():
                        outputs.append((str(p), str(op)))
                        continue
                    try:
                        ar = art_aspect(p)
                    except Exception:
                        todo += 1
                        continue
                    worst = max(crop_loss_for(ar, a) for a in ce["aspects"])
                    if worst > args.skip_crop:
                        results.append({"template": tpath.name, "artwork": p.name,
                                        "skipped": "needs %.0f%% crop (shape mismatch, limit %.0f%%)"
                                        % (worst * 100, args.skip_crop * 100)})
                        log("  %s: SKIPPED - needs %.0f%% crop" % (p.name, worst * 100))
                    else:
                        todo += 1
                if todo == 0:
                    log("  nothing new to do (all outputs exist), PSD not opened")
                    return lines, results, outputs, None
        try:
            psd = PSDImage.open(tpath)
        except Exception as e:
            log("  SKIPPED: cannot open (%s)" % e)
            results.append({"template": tpath.name, "skipped": "cannot open: %s" % e})
            return lines, results, outputs, cache_entry
        for layer, _p, eff, _m in P.walk(psd):
            if P.DELETE_RE.search(layer.name or "") and layer.visible:
                layer.visible = False  # promo / "delete this layer" cards, hidden in memory only
        slots, problem = analyse_template(psd)
        cache_entry = {"name": tpath.name, "key": tkey(tpath), "aspects": [sl["aspect"] for sl in slots], "problem": problem}
        if problem:
            log("  SKIPPED: %s" % problem)
            results.append({"template": tpath.name, "skipped": problem})
            return lines, results, outputs, cache_entry
        if args.solo and len(slots) != 1:
            log("  not a single-frame template (%d frames), left out by --solo" % len(slots))
            return lines, results, outputs, cache_entry
        if args.multi and len(slots) < 2:
            log("  single-frame template, left out by --multi")
            return lines, results, outputs, cache_entry
        st = status.get(tpath.name)
        bucket = "verified" if (st == "PARITY_OK" or (args.trust_nopreview and st == "NO_PREVIEW")) else "check"
        log("  %d slot(s), preview check: %s -> '%s' folder" % (len(slots), st or "unknown", bucket))
        try:
            doc_icc = psd.image_resources.get_data(Resource.ICC_PROFILE)
        except Exception:
            doc_icc = None
        canvas = (psd.width, psd.height)

        jobs = []   # (key Path, [(img, icc, stem)...])
        loaded = {}
        for p in art_paths:
            try:
                loaded[p] = _get_art(p)
            except Exception as e:
                log("  artwork %s: cannot open (%s)" % (p.name, e))
        if args.set:
            if len(slots) < 2:
                log("  single-frame template, not used in --set mode")
                return lines, results, outputs, cache_entry
            picked = []
            for tok in args.set:
                hit = [p for p in loaded if tok.lower() in p.name.lower()]
                if not hit:
                    log("  --set: no artwork matches '%s'" % tok)
                    return lines, results, outputs, cache_entry
                picked.append(hit[0])
            chosen = [(loaded[picked[k % len(picked)]][0], loaded[picked[k % len(picked)]][1],
                       picked[k % len(picked)].stem) for k in range(len(slots))]
            jobs.append((Path("set - " + " + ".join(p.stem for p in picked)), chosen))
        else:
            plist = list(loaded)
            for ai, apath in enumerate(plist):
                if args.mix:
                    chosen = [(loaded[plist[(ai + k) % len(plist)]][0], loaded[plist[(ai + k) % len(plist)]][1],
                               plist[(ai + k) % len(plist)].stem) for k in range(len(slots))]
                else:
                    chosen = [(loaded[apath][0], loaded[apath][1], apath.stem)] * len(slots)
                jobs.append((apath, chosen))

        state = {}
        for apath, chosen in jobs:
            out_path = out_dir / apath.stem / bucket / ("%s - %s.jpg" % (tpath.stem, apath.stem))
            if args.dry_run:
                reps = []
                for slot, (a, _i, _n) in zip(slots, chosen):
                    _c, loss = cover_crop(a, slot["aspect"])
                    reps.append("%s: crop %.1f%%" % (slot["info"]["name"].strip(), loss * 100))
                flag = " <-- would be SKIPPED (shape mismatch)" if any(
                    cover_crop(a, s["aspect"])[1] > args.skip_crop for s, (a, _i, _n) in zip(slots, chosen)) else ""
                log("  %-28s %s%s" % (apath.name[:28], "; ".join(reps), flag))
                continue
            if args.resume and out_path.exists():
                log("  %s: already done, skipped (--resume)" % apath.name)
                outputs.append((str(apath), str(out_path)))
                continue
            t1 = time.time()
            try:
                reps, img = render_one(psd, slots, chosen, doc_icc, canvas, args.quality, out_path, args, state)
            except Exception as e:
                log("  %s: FAILED (%s)" % (apath.name, e))
                log(traceback.format_exc())
                results.append({"template": tpath.name, "artwork": apath.name, "error": str(e)})
                continue
            dt = time.time() - t1
            if img is None:
                why = next((r.get("skip") for r in reps if r.get("skip")), "unknown")
                log("  %s: SKIPPED - %s" % (apath.name, why))
                results.append({"template": tpath.name, "artwork": apath.name, "skipped": why})
                continue
            notes = [r.get("warn") or r.get("warn_res") for r in reps if r.get("warn") or r.get("warn_res")]
            log("  %s: OK %.0fs%s -> %s%s" % (apath.name, dt, " (fast)" if reps[0].get("fast") else "",
                                            out_path.relative_to(out_dir),
                                            ("  [" + "; ".join(notes) + "]") if notes else ""))
            results.append({"template": tpath.name, "artwork": apath.name, "bucket": bucket,
                            "output": str(out_path), "seconds": round(dt, 1), "slots": reps})
            outputs.append((str(apath), str(out_path)))
        state.clear()
    except Exception as e:
        log("  FAILED template: %s" % e)
        log(traceback.format_exc())
        results.append({"template": tpath.name, "error": str(e)})
    finally:
        psd = None
        gc.collect()
    log("  template time %.0fs" % (time.time() - t0))
    return lines, results, outputs, cache_entry


# =========================================================== main
def main():
    ap = argparse.ArgumentParser(description="Batch mockup generator")
    ap.add_argument("--mockups", default=str(ROOT / "mockups"))
    ap.add_argument("--artworks", default=str(ROOT / "artworks"))
    ap.add_argument("--out", default=str(ROOT / "output"))
    ap.add_argument("--only", default=None, help="template file-name substring")
    ap.add_argument("--art", default=None, help="artwork file-name substring")
    ap.add_argument("--solo", action="store_true", help="only single-frame templates")
    ap.add_argument("--multi", action="store_true", help="only templates with 2+ frames")
    ap.add_argument("--set", nargs="+", default=None, metavar="ART",
                    help="multi-frame scenes only: artworks (name substrings) for the frames, left to right")
    ap.add_argument("--mix", action="store_true", help="multi-frame scenes use different artworks per frame")
    ap.add_argument("--workers", type=int, default=1,
                    help="templates rendered in parallel (each needs several GB RAM; try 2)")
    ap.add_argument("--no-fast", action="store_true", help="always render the full canvas (slower)")
    ap.add_argument("--resume", action="store_true", help="skip outputs that already exist")
    ap.add_argument("--trust-nopreview", action="store_true",
                    help="file templates without an embedded preview under 'verified' (you checked them by eye)")
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--warn-crop", type=float, default=0.10, help="flag crops above this fraction")
    ap.add_argument("--skip-crop", type=float, default=0.25, help="skip a template if the crop would exceed this")
    ap.add_argument("--dry-run", action="store_true", help="analyse only, render nothing")
    ap.add_argument("--no-contact-sheet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    P.check_dependencies()
    if args.selftest:
        sys.exit(selftest())

    out_root = Path(args.out)
    out_dir = out_root / "mockups"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "run_log.txt"

    def log(msg):
        print(msg, flush=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    art_dir = Path(args.artworks)
    art_paths = sorted(p for p in art_dir.iterdir() if p.suffix.lower() in ART_EXT)
    if args.art and not args.set:
        art_paths = [p for p in art_paths if args.art.lower() in p.name.lower()]
    if not art_paths:
        log("No artworks found in %s" % art_dir)
        sys.exit(1)
    templates, dup_of = discover_templates(Path(args.mockups), args.only)
    status = load_probe_status(out_root)
    log("=" * 70)
    log("RUN %s | %d artwork(s) x %d template(s) | %d duplicate PSD(s) ignored | workers=%d"
        % (time.strftime("%Y-%m-%d %H:%M"), len(art_paths), len(templates), len(dup_of), args.workers))
    if not status:
        log("NOTE: no probe_results.json found - every result goes to 'check' (unverified).")

    all_results, outputs = [], {}
    slotcache = load_slot_cache(out_dir)
    jobs = [(args, t, art_paths, status, out_dir, args.workers <= 1, slotcache) for t in templates]

    def absorb(i, res):
        lines, results, outs, centry = res
        if centry:
            slotcache[centry["name"]] = centry
            save_slot_cache(out_dir, slotcache)
        if args.workers > 1:
            log("[%d/%d]" % (i, len(jobs)))
            for ln in lines:
                log(ln)
        else:
            with log_file.open("a", encoding="utf-8") as f:
                for ln in lines:
                    f.write(ln + "\n")
        all_results.extend(results)
        for k, o in outs:
            outputs.setdefault(k, []).append(Path(o))

    if args.workers > 1:
        import concurrent.futures as cf
        import multiprocessing as mp
        with cf.ProcessPoolExecutor(max_workers=args.workers, mp_context=mp.get_context("spawn")) as ex:
            futs = {ex.submit(process_template, j): j[1].name for j in jobs}
            for i, f in enumerate(cf.as_completed(futs), 1):
                try:
                    absorb(i, f.result())
                except Exception as e:
                    log("[%d/%d] %s: worker crashed (%s)" % (i, len(jobs), futs[f], e))
                    all_results.append({"template": futs[f], "error": "worker crashed: %s" % e})
    else:
        for i, j in enumerate(jobs, 1):
            print("[%d/%d]" % (i, len(jobs)), end=" ", flush=True)
            absorb(i, process_template(j))

    if not args.dry_run:
        (out_dir / "run_report.json").write_text(json.dumps(all_results, indent=2, default=str), encoding="utf-8")
        if not args.no_contact_sheet:
            for key, paths in outputs.items():
                if paths:
                    cs = out_dir / Path(key).stem / "contact_sheet.jpg"
                    make_contact_sheet(sorted(paths), cs)
                    log("Contact sheet: %s" % cs)
    skipped = [r for r in all_results if r.get("skipped")]
    log("=" * 70)
    log("DONE. rendered=%d skipped=%d errors=%d" % (
        sum(1 for r in all_results if r.get("output")), len(skipped), sum(1 for r in all_results if r.get("error"))))
    for r in skipped:
        log("  skipped: %s%s -> %s" % (r["template"], (" / " + r["artwork"]) if r.get("artwork") else "", r["skipped"]))


if __name__ == "__main__":
    main()
