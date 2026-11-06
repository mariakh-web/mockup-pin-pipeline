r"""
probe_psds.py  --  READ-ONLY diagnostic for the mockup pipeline.

What it does (it never writes to, renames, or modifies any PSD):
  1. Fingerprints PSDs so byte-identical duplicates ("- Copy") are detected.
  2. For every Smart Object layer, records the placement quad (Trnf),
     the perspective quad (nonAffineTransform), warp style, content canvas size,
     unique_id, blend mode, opacity, masks, and whether its ancestors are visible.
  3. Classifies each visible slot's geometry: axis-aligned / affine / perspective / warped.
  4. PARITY TEST: renders the untouched PSD with psd-tools and compares it to the
     preview image Photoshop saved inside the file. This tells us, per template,
     whether psd-tools can reproduce Photoshop's compositing BEFORE any artwork
     is inserted. Side-by-side images are saved for you to look at.

Usage (from the project folder, with the venv active):
    python probe_psds.py                  # all unique PSDs in .\mockups
    python probe_psds.py --no-parity      # structure only (fast)
    python probe_psds.py --only "Poster_on_Stool"   # name filter (substring)
    python probe_psds.py --include-duplicates
Outputs go to .\output\probe\  (probe_report.txt, probe_results.json, parity\*.jpg)
"""
import argparse
import hashlib
import json
import math
import re
import sys
import time
import traceback
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    from psd_tools import PSDImage
    from psd_tools.constants import Resource
except Exception as e:  # pragma: no cover
    print("Missing dependency:", e)
    print('Install with:  pip install "psd-tools[composite]" pillow numpy')
    sys.exit(1)

try:
    import psutil  # optional, only for memory numbers
except Exception:
    psutil = None

ROOT = Path(__file__).resolve().parent


def check_dependencies():
    """psd-tools needs scipy for curves/levels/hue-sat/gradients when compositing."""
    missing = []
    for mod in ("scipy", "aggdraw"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if "scipy" in missing:
        print("\nERROR: scipy is not installed in this Python environment.")
        print("Templates with Curves / Levels / Hue-Saturation layers WILL FAIL without it.")
        print("Fix:   pip install scipy aggdraw")
        print("(then run this script again)\n")
        sys.exit(2)
    if missing:
        print("NOTE: optional package(s) missing: %s  (pip install %s)" % (", ".join(missing), " ".join(missing)))
DELETE_RE = re.compile(r"delete\s*this\s*layer", re.I)


# ----------------------------------------------------------------- helpers
def rss_mb():
    if psutil is None:
        return None
    return round(psutil.Process().memory_info().rss / 1024 / 1024)


def file_hash(path, chunk=8 * 1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def enum_name(val):
    if val is None:
        return None
    e = getattr(val, "enum", val)
    if isinstance(e, bytes):
        return e.decode("latin-1")
    return str(getattr(e, "name", e))


def mesh_info(warp):
    """For warpCustom: how far the mesh deviates from a plain bilinear quad (px)."""
    try:
        cew = warp.get(b"customEnvelopeWarp")
        if cew is None:
            return None
        mp = cew.get(b"meshPoints")
        xs = [float(v) for v in mp[b"Hrzn"].values]
        ys = [float(v) for v in mp[b"Vrtc"].values]
        n = int(round(len(xs) ** 0.5))
        if n * n != len(xs) or n < 2:
            return {"points": len(xs), "max_dev_px": None}
        def P(i, j):
            return xs[i * n + j], ys[i * n + j]
        tl, tr, bl, br = P(0, 0), P(0, n - 1), P(n - 1, 0), P(n - 1, n - 1)
        dev = 0.0
        for i in range(n):
            for j in range(n):
                u, v = j / (n - 1.0), i / (n - 1.0)
                bx = (1 - u) * (1 - v) * tl[0] + u * (1 - v) * tr[0] + (1 - u) * v * bl[0] + u * v * br[0]
                by = (1 - u) * (1 - v) * tl[1] + u * (1 - v) * tr[1] + (1 - u) * v * bl[1] + u * v * br[1]
                dev = max(dev, math.hypot(xs[i * n + j] - bx, ys[i * n + j] - by))
        return {"points": len(xs), "max_dev_px": round(dev, 2),
                "corners_tl_tr_bl_br": [tl, tr, bl, br]}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def classify_quad(q, tol=1.0):
    """q = 8 floats (x1,y1,x2,y2,x3,y3,x4,y4): TL, TR, BR, BL."""
    if not q or len(q) != 8:
        return "unknown"
    x1, y1, x2, y2, x3, y3, x4, y4 = q
    axis = (abs(y1 - y2) <= tol and abs(y4 - y3) <= tol
            and abs(x1 - x4) <= tol and abs(x2 - x3) <= tol)
    if axis:
        return "axis-aligned"
    # parallelogram test: TL + BR == TR + BL
    para = (abs((x1 + x3) - (x2 + x4)) <= 2 * tol and abs((y1 + y3) - (y2 + y4)) <= 2 * tol)
    return "affine (rotated/skewed)" if para else "PERSPECTIVE"


def quad_list(v):
    try:
        return [round(float(x), 2) for x in v]
    except Exception:
        return None


def walk(layers, path=(), vis_chain=True, mask_chain=()):
    """Yield (layer, path, effective_visible, ancestor_masks)."""
    for layer in layers:
        p = path + (layer.name,)
        eff = vis_chain and bool(layer.visible)
        yield layer, p, eff, mask_chain
        if layer.is_group():
            m = mask_chain
            if layer.has_mask() or layer.has_vector_mask():
                m = mask_chain + ("%s[%s%s]" % (
                    layer.name,
                    "mask" if layer.has_mask() else "",
                    "+vector" if layer.has_vector_mask() else ""),)
            for item in walk(layer, p, eff, m):
                yield item


def smart_object_info(layer, path, eff, ancestor_masks):
    info = {
        "path": " / ".join(path),
        "name": layer.name,
        "own_visible": bool(layer.visible),
        "effective_visible": bool(eff),
        "bbox": [int(layer.left), int(layer.top), int(layer.right), int(layer.bottom)],
        "blend_mode": str(layer.blend_mode),
        "opacity": int(layer.opacity),
        "own_mask": bool(layer.has_mask()),
        "own_vector_mask": bool(layer.has_vector_mask()),
        "clipping": bool(getattr(layer, "clipping", False)),
        "ancestor_masks": list(ancestor_masks),
    }
    try:
        so = layer.smart_object
        cfg = so._config.data  # private attribute: pin psd-tools version
        trnf = quad_list(cfg.get(b"Trnf"))
        nonaff = quad_list(cfg.get(b"nonAffineTransform"))
        sz = cfg.get(b"Sz  ")
        warp = cfg.get(b"warp")
        warp_style = enum_name(warp.get(b"warpStyle")) if warp is not None else None
        info.update({
            "kind": so.kind,
            "filetype": so.filetype,
            "filename": so.filename,
            "unique_id": so.unique_id,
            "content_size": [sz.get(b"Wdth"), sz.get(b"Hght")] if sz is not None else None,
            "Trnf": trnf,
            "nonAffineTransform": nonaff,
            "quads_differ": (trnf is not None and nonaff is not None and trnf != nonaff),
            "warp_style": warp_style,
        })
        geom_quad = nonaff or trnf
        geom = classify_quad(geom_quad)
        mesh = mesh_info(warp) if warp is not None else None
        info["warp_mesh"] = mesh
        if warp_style and "none" not in warp_style.lower():
            dev = (mesh or {}).get("max_dev_px")
            if dev is not None and dev < 1.5:
                geom += " [warpCustom, mesh ~ plain quad]"
            else:
                geom = "WARPED (%s, mesh dev=%s px)" % (warp_style, dev)
        info["geometry"] = geom
        # Smart Filters / filter effects attached to this layer?
        tag_names = [str(k) for k in layer.tagged_blocks.keys()]
        info["has_filter_effects"] = any("FILTER_EFFECTS" in t for t in tag_names)
    except Exception as e:
        info["smart_object_error"] = "%s: %s" % (type(e).__name__, e)
        info["geometry"] = "unreadable"
    return info


# ----------------------------------------------------------------- parity
def parity_test(psd, out_img_path, max_w=1600):
    res = {}
    if not psd.has_preview():
        res["status"] = "NO_PREVIEW"
        res["note"] = "PSD has no embedded preview (Maximize Compatibility was off); cannot gate by parity."
        return res
    t0 = time.time()
    ref = psd.topil()  # what Photoshop saved
    t1 = time.time()
    ours = psd.composite(ignore_preview=True)  # what psd-tools computes from layers
    t2 = time.time()
    res["render_seconds"] = round(t2 - t1, 1)
    res["rss_mb_after_render"] = rss_mb()
    if ref is None or ours is None:
        res["status"] = "RENDER_FAILED"
        return res
    ref = ref.convert("RGB")
    ours = ours.convert("RGB")
    if ref.size != ours.size:
        res["status"] = "SIZE_MISMATCH"
        res["note"] = "preview %s vs render %s" % (ref.size, ours.size)
        return res
    scale = min(1.0, max_w / ref.size[0])
    if scale < 1.0:
        sz = (int(ref.size[0] * scale), int(ref.size[1] * scale))
        ref_s, ours_s = ref.resize(sz, Image.BOX), ours.resize(sz, Image.BOX)
    else:
        ref_s, ours_s = ref, ours
    a = np.asarray(ref_s, dtype=np.int16)
    b = np.asarray(ours_s, dtype=np.int16)
    d = np.abs(a - b).max(axis=2)  # worst channel per pixel
    res["mean_abs_diff"] = round(float(d.mean()), 3)
    res["p99_diff"] = int(np.percentile(d, 99))
    res["pct_pixels_diff_gt12"] = round(float((d > 12).mean() * 100), 3)
    # Provisional thresholds -- adjust them for your own templates.
    if res["mean_abs_diff"] < 2.0 and res["pct_pixels_diff_gt12"] < 0.5:
        res["status"] = "PARITY_OK"
    elif res["mean_abs_diff"] < 6.0:
        res["status"] = "PARITY_MARGINAL"
    else:
        res["status"] = "PARITY_FAIL"
    # side by side: [photoshop preview | psd-tools render | amplified diff]
    heat = np.clip(d * 6, 0, 255).astype(np.uint8)
    heat_img = Image.fromarray(heat, "L").convert("RGB")
    w, h = ref_s.size
    sheet = Image.new("RGB", (w * 3, h))
    sheet.paste(ref_s, (0, 0))
    sheet.paste(ours_s, (w, 0))
    sheet.paste(heat_img, (2 * w, 0))
    sheet.save(out_img_path, quality=85)
    res["compare_image"] = str(out_img_path)
    return res


# ----------------------------------------------------------------- per file
def probe_file(path, do_parity, parity_dir):
    r = {"file": path.name, "bytes": path.stat().st_size}
    t0 = time.time()
    psd = PSDImage.open(path)
    r["open_seconds"] = round(time.time() - t0, 1)
    r["canvas"] = [psd.width, psd.height]
    r["bit_depth"] = psd.depth
    r["color_mode"] = str(psd.color_mode)
    r["has_preview"] = bool(psd.has_preview())
    try:
        r["has_icc_profile"] = psd.image_resources.get_data(Resource.ICC_PROFILE) is not None
    except Exception:
        r["has_icc_profile"] = None

    slots, hidden, suspicious = [], [], []
    for layer, p, eff, masks in walk(psd):
        if DELETE_RE.search(layer.name or ""):
            suspicious.append({"path": " / ".join(p), "visible": bool(eff),
                               "bbox": [int(layer.left), int(layer.top), int(layer.right), int(layer.bottom)],
                               "note": "name says delete this layer"})
        if layer.kind == "smartobject":
            info = smart_object_info(layer, p, eff, masks)
            (slots if eff else hidden).append(info)
    r["visible_smart_objects"] = slots
    r["hidden_smart_objects"] = hidden
    r["delete_me_layers"] = suspicious

    # linked instances: same unique_id among visible AND hidden
    by_uid = {}
    for s in slots + hidden:
        by_uid.setdefault(s.get("unique_id"), []).append(s["path"])
    r["shared_unique_ids"] = {k: v for k, v in by_uid.items() if k and len(v) > 1}

    if do_parity:
        try:
            parity_dir.mkdir(parents=True, exist_ok=True)
            out = parity_dir / (path.stem + "__parity.jpg")
            r["parity"] = parity_test(psd, out)
        except Exception as e:
            r["parity"] = {"status": "ERROR", "note": "%s: %s" % (type(e).__name__, e),
                           "trace": traceback.format_exc()}
    if do_parity and r.get("parity", {}).get("status") != "PARITY_OK":
        try:
            r["layer_dump"] = layer_dump(psd)
        except Exception as e:
            r["layer_dump"] = "failed: %s" % e
    r["total_seconds"] = round(time.time() - t0, 1)
    return r


def layer_dump(psd):
    """Blend settings of every layer, so we can see what psd-tools might mis-render."""
    out = []
    for layer, p, eff, masks in walk(psd):
        rec = getattr(layer, "_record", None)
        br = getattr(rec, "blending_ranges", None)
        # "Blend If" sliders are stored here; non-default ranges are rarely handled by renderers
        try:
            comp = list(getattr(br, "composite_ranges", []) or [])
            blend_if = any(tuple(x) != (0, 255, 0, 255) for x in comp) if comp else False
        except Exception:
            blend_if = None
        out.append({
            "path": " / ".join(p), "kind": layer.kind, "visible": bool(layer.visible),
            "blend_mode": str(layer.blend_mode), "opacity": int(layer.opacity),
            "fill_opacity": getattr(layer, "fill_opacity", None),
            "clipping": bool(getattr(layer, "clipping", False)),
            "mask": bool(layer.has_mask()), "vector_mask": bool(layer.has_vector_mask()),
            "blend_if_nondefault": blend_if,
        })
    return out


def verdict(r):
    reasons = []
    slots = r["visible_smart_objects"]
    if not slots:
        return "SKIP", ["no visible Smart Object found"]
    for s in slots:
        g = s.get("geometry", "")
        if g.startswith("WARPED"):
            reasons.append("slot '%s' is warped" % s["name"])
        if g == "unreadable":
            reasons.append("slot '%s' transform unreadable" % s["name"])
        if s.get("has_filter_effects"):
            reasons.append("slot '%s' has Smart Filters" % s["name"])
    par = r.get("parity", {}).get("status")
    if par in ("PARITY_FAIL", "ERROR", "SIZE_MISMATCH", "RENDER_FAILED"):
        reasons.append("parity: %s" % par)
    if reasons:
        return "SKIP/RISK", reasons
    notes = []
    if par == "NO_PREVIEW":
        notes.append("no embedded preview, parity unknown")
    if par == "PARITY_MARGINAL":
        notes.append("parity marginal - look at the compare image")
    if any(s.get("geometry") == "PERSPECTIVE" for s in slots):
        notes.append("perspective slot(s) - needs real-render test")
    return ("CANDIDATE" if not notes else "CANDIDATE?"), notes


# ----------------------------------------------------------------- main
def main():
    check_dependencies()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mockups", default=str(ROOT / "mockups"))
    ap.add_argument("--out", default=str(ROOT / "output" / "probe"))
    ap.add_argument("--no-parity", action="store_true")
    ap.add_argument("--only", default=None, help="substring filter on file name")
    ap.add_argument("--include-duplicates", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="reuse the previous probe_results.json; only re-probe files whose parity step ERRORed")
    args = ap.parse_args()

    mock = Path(args.mockups)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(mock.glob("*.psd")) + sorted(mock.glob("*.psb"))
    if args.only:
        files = [f for f in files if args.only.lower() in f.name.lower()]
    print("Found %d PSD/PSB files in %s" % (len(files), mock))

    # duplicate detection: hash only files that share a byte size
    by_size = {}
    for f in files:
        by_size.setdefault(f.stat().st_size, []).append(f)
    dup_of = {}
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        seen = {}
        for f in group:
            h = file_hash(f)
            if h in seen:
                dup_of[f.name] = seen[h]
            else:
                seen[h] = f.name
    print("Byte-identical duplicates: %d" % len(dup_of))

    prev = {}
    pj = out / "probe_results.json"
    if args.resume and pj.exists():
        for r in json.loads(pj.read_text(encoding="utf-8")):
            prev[r.get("file")] = r
        print("Resuming: loaded %d earlier results" % len(prev))

    results = []
    for i, f in enumerate(files, 1):
        old = prev.get(f.name)
        if old is not None and "duplicate_of" not in old and "fatal_error" not in old \
                and old.get("parity", {}).get("status") != "ERROR":
            results.append(old)
            print("[%d/%d] %s  -> kept from earlier run" % (i, len(files), f.name))
            continue
        if f.name in dup_of and not args.include_duplicates:
            results.append({"file": f.name, "duplicate_of": dup_of[f.name]})
            print("[%d/%d] %s  -> duplicate of %s (skipped)" % (i, len(files), f.name, dup_of[f.name]))
            continue
        print("[%d/%d] probing %s ..." % (i, len(files), f.name), flush=True)
        try:
            r = probe_file(f, not args.no_parity, out / "parity")
        except Exception as e:
            r = {"file": f.name, "fatal_error": "%s: %s" % (type(e).__name__, e),
                 "trace": traceback.format_exc()}
        results.append(r)
        (out / "probe_results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    # ---------------- human-readable report
    lines = ["MOCKUP PROBE REPORT", "psd-tools only; read-only. Thresholds are provisional.", ""]
    lines.append("%-48s %-11s %-6s %-5s %s" % ("file", "verdict", "slots", "depth", "details"))
    lines.append("-" * 130)
    for r in results:
        if "duplicate_of" in r:
            lines.append("%-48s %-11s %s" % (r["file"][:48], "DUPLICATE", "same bytes as " + r["duplicate_of"]))
            continue
        if "fatal_error" in r:
            lines.append("%-48s %-11s %s" % (r["file"][:48], "ERROR", r["fatal_error"]))
            continue
        v, why = verdict(r)
        geoms = sorted({s.get("geometry", "?") for s in r["visible_smart_objects"]})
        par = r.get("parity", {})
        pdesc = ""
        if par:
            pdesc = "parity=%s mean=%s gt12=%s%% %ss" % (
                par.get("status"), par.get("mean_abs_diff"), par.get("pct_pixels_diff_gt12"),
                par.get("render_seconds"))
        if par.get("status") == "ERROR":
            lines.append("    parity error: " + str(par.get("note", "")).splitlines()[0][:160])
        lines.append("%-48s %-11s %-6d %-5s %s | %s | %s" % (
            r["file"][:48], v, len(r["visible_smart_objects"]), r["bit_depth"],
            ",".join(geoms), pdesc, "; ".join(why)))
        if r["delete_me_layers"]:
            lines.append("    ! 'delete this layer' layers: " +
                         ", ".join("%s (visible=%s)" % (d["path"], d["visible"]) for d in r["delete_me_layers"]))
        if r["shared_unique_ids"]:
            lines.append("    linked Smart Object instances: %d group(s) share a unique_id" % len(r["shared_unique_ids"]))
    cnt = {}
    for r in results:
        if "duplicate_of" in r or "fatal_error" in r:
            continue
        cnt[verdict(r)[0]] = cnt.get(verdict(r)[0], 0) + 1
    lines += ["", "Summary: " + ", ".join("%s=%d" % kv for kv in sorted(cnt.items()))]
    (out / "probe_report.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print("\nWrote: %s" % (out / "probe_report.txt"))
    print("Wrote: %s" % (out / "probe_results.json"))
    print("Side-by-side parity images: %s" % (out / "parity"))


if __name__ == "__main__":
    main()
