r"""
pin_planner.py -- plans one-pin-a-day style Pinterest schedules and writes a Publer CSV.

Run from the project folder (next to make_mockups.py). Needs only Pillow (already installed).

  python pin_planner.py init            # writes pin_config.json (edit boards/times there) and stubs
  python pin_planner.py links           # makes listing_links.csv: one row per print, paste your shop listing URL next to it
  python pin_planner.py meta            # makes pin_metadata.csv stubs + labelled contact sheets for
                                        #   a chat model to fill in titles / captions / alt text
  python pin_planner.py plan --days 30  # plans the next 30 days, exports the images to upload,
                                        #   writes publer_<date>.csv
  python pin_planner.py plan --from 2026-11-16 --to 2026-11-30   # plan exactly these dates
  python pin_planner.py plan --from 2026-11-16 --to 2026-11-30 --preview   # show the plan only, write nothing
  python pin_planner.py plan --from 2026-11-16 --to 2026-11-30 --streams csv_extra    # only one stream (later: another run with other streams)
  python pin_planner.py status          # shows what is already planned

Streams (edit in pin_config.json):
  mockups  - finished mockups in output\mockups\<print>\verified|check\*.jpg
  folder   - a folder of photos or prints (e.g. artworks, pins_in\photos)
  csv      - rows that already exist in Publer's CSV format (e.g. pins prepared elsewhere); only dates are added

Nothing here changes your mockups or artworks. Image URLs are built from your GitHub repo:
  https://raw.githubusercontent.com/<user>/<repo>/<branch>/<file>
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
Image.MAX_IMAGE_PIXELS = None   # big mockups trigger a harmless warning otherwise

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "pin_config.json"
STATE = ROOT / "planner_state.json"
META = ROOT / "pin_metadata.csv"
SCENES_CSV = ROOT / "pin_scenes.csv"
OUT = ROOT / "pin_out"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
try:   # iPhone photos (.heic): needs  pip install pillow-heif
    from pillow_heif import register_heif_opener
    register_heif_opener()
    IMG_EXT |= {".heic", ".heif"}
except ImportError:
    pass

PUBLER_HEADER = [
    "Date - Intl. format or prompt", "Text", "Link(s) - Separated by comma for FB carousels",
    "Media URL(s) - Separated by comma", "Title - For the video, pin, PDF ..",
    "Label(s) - Separated by comma", "Alt text(s) - Separated by ||", "Comment(s) - Separated by ||",
    "Pin board, FB album, or Google category", "Post subtype - I.e. story, reel, PDF ..",
    "CTA - For Facebook links or Google", "Reminder - For stories, reels, shorts, and TikToks"]
META_COLS = ["file", "title", "text", "alt", "link", "subject", "medium", "palette", "rooms"]
LINKS_CSV = ROOT / "listing_links.csv"
TITLE_MAX, TEXT_MAX, ALT_MAX = 100, 800, 500   # Pinterest limits (Pinterest help: Review Pin specs / alt text)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_CONFIG = {
    "github": {"user": "your-github-user", "repo": "your-image-host-repo", "branch": "main"},
    "image_max_side": 1600,
    "jpeg_quality": 88,
    "upload_batch_size": 90,
    "print_gap_days": 6,
    "template_gap_days": 10,
    "use_check_bucket": True,
    "seed": 7,
    "product_phrase": "printable wall art",
    "default_medium": "mixed media",
    "text_suffix": "",
    "require_links": False,
    "streams": [
        {"name": "art", "type": "folder", "dir": "artworks",
         "board": "Example Board | Original Art", "time": "09:00"},
        {"name": "mockups", "type": "mockups", "dir": "output/mockups",
         "board": "Example Board | Interiors", "time": "12:00"},
        {"name": "photos", "type": "folder", "dir": "pins_in/photos",
         "board": "Example Board | Photography", "time": "15:00"},
        {"name": "csv_extra", "type": "csv", "file": "pins_in/extra_pins.csv",
         "days": ["mon", "wed", "fri"], "time": "20:00", "label": "extra"}],
}

# Scene phrases per mockup template. Add your own templates here, or in pin_scenes.csv
# (columns: template, alt_phrase, title_phrase, room). Templates without an entry just get no scene phrase.
# template file stem -> (phrase used in alt text, short phrase used in the title)
SCENES = {
    "Frame_On_Wall_01": ("a framed print hanging on a light wall", "Framed Print on Wall"),
    "Frame_On_Shelf_01": ("a framed print leaning on a wooden shelf", "Print on a Shelf"),
    "Easel_Gallery_01": ("a print on a wooden easel in a gallery", "Gallery Easel"),
}

# template file stem -> room/setting word used in the SEO description ("" = no room keyword)
SCENE_ROOMS = {
    "Frame_On_Wall_01": "living room",
    "Frame_On_Shelf_01": "reading corner",
    "Easel_Gallery_01": "gallery space",
}


# ------------------------------------------------------------------ helpers
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def get_config():
    if not CONFIG.exists():
        save_json(CONFIG, DEFAULT_CONFIG)
        print("Wrote %s - open it to check boards, times and folders." % CONFIG.name)
    cfg = load_json(CONFIG, DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    have = {x["name"] for x in cfg["streams"]}
    added = [x for x in DEFAULT_CONFIG["streams"] if x["name"] not in have]
    if added:   # streams added in newer versions of this script appear in your config automatically
        cfg["streams"].extend(added)
        save_json(CONFIG, cfg)
        print("Added to %s: %s  (check the board names and times)" % (CONFIG.name, ", ".join(x["name"] for x in added)))
    for x in cfg["streams"]:
        if x.get("type") == "folder" and x.get("dir"):
            (ROOT / x["dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return s[:70] or "img"


def read_meta():
    meta = {}
    if META.exists():
        with META.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("file"):
                    meta[r["file"].strip()] = {c: (r.get(c) or "").strip() for c in META_COLS}
    return meta


def write_meta(meta):
    with META.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_COLS)
        w.writeheader()
        for k in sorted(meta):
            w.writerow({c: (meta[k].get(c) if c != "file" else k) or "" for c in META_COLS})


def read_scenes():
    """template -> (alt_phrase, title_phrase, room)"""
    scenes = {k: (v[0], v[1], SCENE_ROOMS.get(k, "")) for k, v in SCENES.items()}
    if SCENES_CSV.exists():
        with SCENES_CSV.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                t = (r.get("template") or "").strip()
                if t:
                    room = r.get("room")
                    room = SCENE_ROOMS.get(t, "") if room is None else room.strip()
                    scenes[t] = ((r.get("alt_phrase") or "").strip(), (r.get("title_phrase") or "").strip(), room)
    return scenes


def write_scene_file_if_missing():
    if SCENES_CSV.exists():
        return
    with SCENES_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["template", "alt_phrase", "title_phrase", "room"])
        for k in sorted(SCENES):
            w.writerow([k, SCENES[k][0], SCENES[k][1], SCENE_ROOMS.get(k, "")])


def read_links():
    links = {}
    if LINKS_CSV.exists():
        with LINKS_CSV.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r = {(k_ or "").strip().lower(): v_ for k_, v_ in r.items()}
                k = (r.get("print") or r.get("name") or "").strip()
                u = (r.get("url") or r.get("links") or r.get("link") or "").strip()
                if k and u:
                    links[k] = u
    return links


def clip(text, n):
    """Cut to n characters at a word boundary."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" |,;:-")
    return cut


def finish_text(text, cfg):
    text = (text or "").strip()
    suffix = (cfg.get("text_suffix") or "").strip()
    if suffix:
        text = text + " " + suffix
    if len(text) > TEXT_MAX:
        cut = text[:TEXT_MAX]
        k = cut.rfind(". ")
        text = cut[:k + 1] if k > 200 else clip(text, TEXT_MAX)
    return text


def article(w):
    w = w.strip()
    return ("an " if w[:1].lower() in "aeiou" else "a ") + w


def cap1(s_):
    return s_[:1].upper() + s_[1:] if s_ else s_


def join_or(items):
    items = [i.strip() for i in items if i.strip()]
    if not items:
        return "any room"
    arts = [article(i) for i in items]
    return arts[0] if len(arts) == 1 else ", ".join(arts[:-1]) + " or " + arts[-1]


def compose_mockup_text(pm, room, cfg, seed):
    """Keyword-led description for a mockup pin. Falls back to the print's own text when the
    print has no 'subject' filled in. Alt text is written separately and stays literal."""
    subj = (pm.get("subject") or "").strip()
    if not subj:
        return pm["text"]
    med = (pm.get("medium") or "").strip() or cfg.get("default_medium", "mixed media")
    pal = (pm.get("palette") or "").strip()
    rooms = join_or((pm.get("rooms") or "").split(","))
    prod = cfg.get("product_phrase", "art print")
    r_for = article(room) if room else "your home"
    shown = "%s mockup" % article(room) if room else "a room mockup"
    pal_adds = ("%s add" % cap1(pal)) if pal else "Bold colour adds"
    pal_wo = pal if pal else "bold colour"
    pal_with = (" with %s tones" % pal) if pal else ""
    pats = [
        "%s, drawn in %s: %s styled for %s. %s a joyful, hand-drawn touch, and it suits %s. "
        "Instant digital download, ready to print and frame; it mixes well with other prints on a gallery wall." % (cap1(subj), med, prod, r_for, pal_adds, rooms),
        "Looking for %s wall art for %s? This %s %s adds %s without feeling heavy. "
        "A cheerful digital download for %s, small spaces and gifts for art lovers." % (med, r_for, subj, prod, pal_wo, rooms),
        "%s drawing of %s, shown in %s. Colourful, modern %s%s for %s. "
        "Digital download: save this idea for your home decor, gallery wall or housewarming gift list." % (cap1(med), article(subj), shown, prod, pal_with, rooms),
        "Bring colour to %s: %s, drawn in %s%s. Cheerful, contemporary %s that suits %s "
        "and looks good in a simple frame. Digital download." % (r_for, subj, med, pal_with, prod, rooms),
    ]
    i = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(pats)
    return pats[i]


def list_images(folder):
    d = ROOT / folder
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMG_EXT)


def collect_items(cfg, meta, scenes, links, warn):
    """-> {stream_name: [item,...]} ; item keys: id, stream, path|row, print, template, title, text, alt, link, needs_link"""
    pools = {}
    for s in cfg["streams"]:
        items = []
        if s["type"] == "folder":
            for p in list_images(s["dir"]):
                m = meta.get(p.stem)
                if not m or not m["title"] or not m["alt"]:
                    warn.append("%s: %s has no title/alt in pin_metadata.csv - left out" % (s["name"], p.name))
                    continue
                is_art = s["name"] == "art"
                link = (links.get(p.stem) if is_art else "") or m["link"]
                items.append({"id": "%s|%s" % (s["name"], p.stem), "stream": s["name"], "path": p,
                              "print": p.stem if is_art else None, "template": None,
                              "title": clip(m["title"], TITLE_MAX), "text": finish_text(m["text"], cfg),
                              "alt": clip(m["alt"], ALT_MAX), "link": link, "needs_link": is_art})
        elif s["type"] == "mockups":
            base = ROOT / s["dir"]
            buckets = ["verified"] + (["check"] if cfg.get("use_check_bucket", True) else [])
            if base.exists():
                for pdir in sorted(d for d in base.iterdir() if d.is_dir()):
                    pm = meta.get(pdir.name)
                    if not pm or not pm["title"] or not pm["alt"]:
                        warn.append("mockups: print '%s' has no title/alt in pin_metadata.csv - its mockups are left out" % pdir.name)
                        continue
                    link = links.get(pdir.name) or pm["link"]
                    for b_ in buckets:
                        for p in sorted((pdir / b_).glob("*.jpg")) if (pdir / b_).exists() else []:
                            tmpl = p.stem[: -len(" - " + pdir.name)] if p.stem.endswith(" - " + pdir.name) else p.stem
                            alt_ph, title_ph, room = scenes.get(tmpl, ("", "", ""))
                            alt = pm["alt"].rstrip(".") + "." + (" Shown as %s." % alt_ph if alt_ph else "")
                            title = pm["title"] + (" | %s" % title_ph if title_ph else "")
                            if len(title) > TITLE_MAX:
                                title = pm["title"]
                            text = compose_mockup_text(pm, room, cfg, pdir.name + "|" + tmpl)
                            items.append({"id": "%s|%s|%s" % (s["name"], pdir.name, tmpl), "stream": s["name"], "path": p,
                                          "print": pdir.name, "template": tmpl, "title": clip(title, TITLE_MAX),
                                          "text": finish_text(text, cfg), "alt": clip(alt, ALT_MAX),
                                          "link": link, "needs_link": True})
        elif s["type"] == "csv":
            f = ROOT / s["file"]
            if f.exists():
                with f.open(encoding="utf-8-sig", newline="") as fh:
                    rd = csv.reader(fh)
                    next(rd, None)
                    for row in rd:
                        if not any(row):
                            continue
                        row = (row + [""] * 12)[:12]
                        h = hashlib.sha1((row[3] + row[4] + row[1]).encode("utf-8")).hexdigest()[:12]
                        items.append({"id": "%s|%s" % (s["name"], h), "stream": s["name"], "row": row,
                                      "print": None, "template": None})
            else:
                warn.append("%s: file %s not found" % (s["name"], s["file"]))
        pools[s["name"]] = items
    return pools


# ------------------------------------------------------------------ commands
def cmd_init(cfg):
    write_scene_file_if_missing()
    for s in cfg["streams"]:
        if s["type"] == "folder":
            (ROOT / s["dir"]).mkdir(parents=True, exist_ok=True)
        if s["type"] == "csv":
            (ROOT / s["file"]).parent.mkdir(parents=True, exist_ok=True)
    print("Config:   %s" % CONFIG.name)
    print("Scenes:   %s (alt-text phrases per mockup template; edit if you like)" % SCENES_CSV.name)
    print("Photo folders created under pins_in\\ . Put extra photos in pins_in\\photos.")
    print("Listing links: run  python pin_planner.py links  to create %s, then paste each listing URL into it." % LINKS_CSV.name)
    print("Optional: put pre-written pins at %s (Publer CSV format, Date column empty)." % [s for s in cfg["streams"] if s["type"] == "csv"][0]["file"])


def label_sheets(files, prefix):
    font = ImageFont.load_default()
    per = 12
    made = []
    for n in range(0, len(files), per):
        chunk = files[n:n + per]
        tw, th = 360, 360
        cols = 4
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 34) + 10), (245, 245, 245))
        d = ImageDraw.Draw(sheet)
        for i, p in enumerate(chunk):
            try:
                im = Image.open(p).convert("RGB")
            except Exception:
                continue
            im.thumbnail((tw, th))
            x = 10 + (i % cols) * (tw + 10)
            y = 10 + (i // cols) * (th + 34)
            sheet.paste(im, (x, y))
            d.text((x, y + im.height + 4), p.stem[:60], fill=(0, 0, 0), font=font)
        out = ROOT / ("%s_%02d.jpg" % (prefix, n // per + 1))
        sheet.save(out, quality=88)
        made.append(out.name)
    return made


def print_names(cfg):
    names = set()
    for s in cfg["streams"]:
        if s["type"] == "folder" and s["name"] == "art":
            names.update(p.stem for p in list_images(s["dir"]))
        if s["type"] == "mockups":
            base = ROOT / s["dir"]
            if base.exists():
                names.update(d.name for d in base.iterdir() if d.is_dir())
    return sorted(names)


def cmd_links(cfg):
    names = print_names(cfg)
    have = {}
    if LINKS_CSV.exists():
        with LINKS_CSV.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r = {(k_ or "").strip().lower(): v_ for k_, v_ in r.items()}
                k = (r.get("print") or r.get("name") or "").strip()
                if k:
                    have[k] = (r.get("url") or r.get("links") or r.get("link") or "").strip()
    for n in names:
        have.setdefault(n, "")
    with LINKS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["print", "url"])
        for n in sorted(have):
            w.writerow([n, have[n]])
    empty = [n for n in sorted(have) if not have[n]]
    print("%s: %d print(s), %d still without a link." % (LINKS_CSV.name, len(have), len(empty)))
    print("Open it in Excel or Notepad and paste each listing's link next to its print name.")
    if empty:
        print("Missing: " + ", ".join(empty[:30]) + (" ..." if len(empty) > 30 else ""))


def cmd_meta(cfg):
    meta = read_meta()
    scenes = read_scenes()
    new_files = []
    for s in cfg["streams"]:
        if s["type"] == "folder":
            for p in list_images(s["dir"]):
                if p.stem not in meta or not meta[p.stem]["title"] or not meta[p.stem]["alt"]:
                    meta.setdefault(p.stem, {c: "" for c in META_COLS})
                    new_files.append(p)
    write_meta(meta)
    if not new_files:
        print("Every image already has a title and alt text. Nothing to label.")
        return
    sheets = label_sheets(new_files, "pin_label_sheet")
    print("%d image(s) need a title / caption / alt text." % len(new_files))
    print("Contact sheets: %s" % ", ".join(sheets))
    print("\nUpload each sheet to your chat model with this message:\n")
    print("  Each image has its file name printed under it. For EVERY image write one CSV row with columns:")
    print("  file,title,text,alt,subject,palette,rooms   (file = the exact name under the image, without .jpg)")
    print("  title: lead with the main search words, max 70 characters, no site name.")
    print("  text: 2-3 natural sentences, 250-450 characters, with words people search for: what it is, the medium,")
    print("        colours, rooms or uses (e.g. light living room, bathroom, gift). It must NOT repeat the alt text.")
    print("  alt: ONE plain sentence saying only what is visible, no keywords.")
    print("  subject/palette/rooms: for artworks only (e.g. blue moka pot still life / turquoise, lilac / kitchen, dining room);")
    print("        leave empty for photos. Do not name places or artists unless certain. Output only the CSV, with a header row.")
    print("\nThen paste the rows into %s (replace the empty rows for those files)." % META.name)


def sort_key_date(item_state):
    return item_state.get("last", "0000-00-00")


def choose(pool, day, st, cfg, day_prints, rng, warn_msgs, stream_name):
    pg = cfg["print_gap_days"]
    tg = cfg["template_gap_days"]
    prints = st["prints"]
    tmpls = st["templates"]
    used = st["items"]

    def ok(it, relax):
        pr, tp = it.get("print"), it.get("template")
        if pr and pr in day_prints:
            return False
        if relax >= 2:
            return True
        if pr and pr in prints and (day - dt.date.fromisoformat(prints[pr])).days < (pg if relax == 0 else max(1, pg // 2)):
            return False
        if tp and tp in tmpls and (day - dt.date.fromisoformat(tmpls[tp])).days < (tg if relax == 0 else max(1, tg // 2)):
            return False
        return True

    for relax in (0, 1, 2):
        cands = [it for it in pool if ok(it, relax)]
        if cands:
            if relax:
                warn_msgs.append("%s on %s: spacing rules had to be relaxed (too few images in this stream)" % (stream_name, day))
            cands.sort(key=lambda it: (used.get(it["id"], {}).get("uses", 0),
                                       prints.get(it.get("print"), "0000-00-00") if it.get("print") else "0000-00-00",
                                       tmpls.get(it.get("template"), "0000-00-00") if it.get("template") else "0000-00-00",
                                       sort_key_date(used.get(it["id"], {})), rng.random()))
            return cands[0]
    return None


def cmd_plan(cfg, args):
    meta = read_meta()
    scenes = read_scenes()
    links = read_links()
    warn = []
    pools = collect_items(cfg, meta, scenes, links, warn)
    if args.require_links or cfg.get("require_links"):
        skipped = set()
        for name, pool in pools.items():
            keep = []
            for it in pool:
                if it.get("needs_link") and not it.get("link"):
                    skipped.add(it["print"])
                else:
                    keep.append(it)
            pools[name] = keep
        if skipped:
            warn.append("left out (no listing link in %s): %s" % (LINKS_CSV.name, ", ".join(sorted(skipped))))
    missing_links = set()
    picks = {}
    st = load_json(STATE, {"items": {}, "prints": {}, "templates": {}, "last_date": None, "exported": {}, "csv_used": []})
    for k in ("items", "prints", "templates", "exported"):
        st.setdefault(k, {})
    st.setdefault("csv_used", [])

    ndays = args.days
    if args.date_from:
        start = dt.date.fromisoformat(args.date_from)
        if args.date_to:
            end = dt.date.fromisoformat(args.date_to)
            if end < start:
                sys.exit("--to (%s) is before --from (%s)" % (end, start))
            ndays = (end - start).days + 1
    elif args.date_to:
        sys.exit("--to needs --from as well")
    elif args.start:
        start = dt.date.fromisoformat(args.start)
    elif st.get("last_date"):
        start = dt.date.fromisoformat(st["last_date"]) + dt.timedelta(days=1)
    else:
        start = dt.date.today() + dt.timedelta(days=1)
    rng = random.Random("%s-%s" % (cfg["seed"], start.isoformat()))
    rows, report, to_export, warn_msgs = [], [], {}, []
    gh = cfg["github"]

    for i in range(ndays):
        day = dt.date.fromordinal(start.toordinal() + i)
        wd = DAYS[day.weekday()]
        day_prints = set()
        for s in cfg["streams"]:
            if s.get("days") and wd not in s["days"]:
                continue
            times = s.get("times") or [s.get("time", "12:00")]
            for tm in times:
                pool = pools.get(s["name"], [])
                if s["type"] == "csv":
                    pool = [it for it in pool if it["id"] not in st["csv_used"]]
                    if not pool:
                        warn_msgs.append("%s: out of rows on %s - add more rows to %s" % (s["name"], day, s["file"]))
                        continue
                    it = pool[0]
                    st["csv_used"].append(it["id"])
                    row = list(it["row"])
                    row[0] = "%s %s" % (day.strftime("%Y/%m/%d"), tm)
                    if s.get("board") and not row[8]:
                        row[8] = s["board"]
                    if s.get("label") and not row[5]:
                        row[5] = s["label"]
                    rows.append((day, tm, row))
                    report.append((day, tm, s["name"], row[4][:50]))
                    continue
                if not pool:
                    warn_msgs.append("%s: no usable images (missing metadata or empty folder)" % s["name"])
                    continue
                it = choose(pool, day, st, cfg, day_prints, rng, warn_msgs, s["name"])
                if it is None:
                    warn_msgs.append("%s on %s: nothing eligible" % (s["name"], day))
                    continue
                if it.get("needs_link") and not it.get("link"):
                    missing_links.add(it["print"])
                picks.setdefault(s["name"], {}).setdefault(it["id"], 0)
                picks[s["name"]][it["id"]] += 1
                rec = st["items"].setdefault(it["id"], {"uses": 0})
                rec["uses"] += 1
                rec["last"] = day.isoformat()
                if it.get("print"):
                    st["prints"][it["print"]] = day.isoformat()
                    day_prints.add(it["print"])
                if it.get("template"):
                    st["templates"][it["template"]] = day.isoformat()
                fname = st["exported"].get(it["id"])
                if not fname:
                    base = slug(it["stream"] + "__" + (it["print"] + "__" + it["template"] if it.get("template") else Path(it["path"]).stem))
                    fname = base + ".jpg"
                    n = 2
                    taken = set(st["exported"].values())
                    while fname in taken:
                        fname = "%s_%d.jpg" % (base, n)
                        n += 1
                    st["exported"][it["id"]] = fname
                    to_export[fname] = it["path"]
                url = "https://raw.githubusercontent.com/%s/%s/%s/%s" % (gh["user"], gh["repo"], gh["branch"], fname)
                row = ["%s %s" % (day.strftime("%Y/%m/%d"), tm), it["text"], it.get("link", ""), url, it["title"],
                       s.get("label", ""), it["alt"], "", s["board"], "", "", ""]
                rows.append((day, tm, row))
                report.append((day, tm, s["name"], it["title"][:50]))
    last_day = dt.date.fromordinal(start.toordinal() + ndays - 1)
    st["last_date"] = last_day.isoformat()

    # export images for upload
    exported_batches = []
    if to_export and not args.preview:
        OUT.mkdir(exist_ok=True)
        names = sorted(to_export)
        size = cfg["upload_batch_size"]
        existing = len([d for d in OUT.iterdir() if d.is_dir()]) if OUT.exists() else 0
        for b in range(0, len(names), size):
            folder = OUT / ("upload_%03d" % (existing + b // size + 1))
            folder.mkdir(parents=True, exist_ok=True)
            for nm in names[b:b + size]:
                im = Image.open(to_export[nm]).convert("RGB")
                im.thumbnail((cfg["image_max_side"], cfg["image_max_side"]), Image.LANCZOS)
                im.save(folder / nm, "JPEG", quality=cfg["jpeg_quality"], optimize=True)
            exported_batches.append((folder, len(names[b:b + size])))

    rows.sort(key=lambda r: (r[0], r[1]))
    stamp = "%s_to_%s" % (start.strftime("%Y%m%d"), last_day.strftime("%Y%m%d"))
    if args.streams:
        stamp += "_" + args.streams.replace(",", "-").replace(" ", "")
    files = []
    for n in range(0, len(rows), 500) if not args.preview else []:   # Publer takes up to 500 posts per file
        f = ROOT / ("publer_%s%s.csv" % (stamp, "" if len(rows) <= 500 else "_part%d" % (n // 500 + 1)))
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(PUBLER_HEADER)
            for _d, _t, row in rows[n:n + 500]:
                w.writerow(row)
        files.append(f.name)
    if not args.preview:
        save_json(STATE, st)

    print("\nPLAN %s -> %s  (%d pins)%s" % (start, last_day, len(rows), "   [PREVIEW - nothing was written or remembered]" if args.preview else ""))
    cur = None
    for d, tm, sname, title in sorted(report):
        if d != cur:
            print("  %s %s" % (d, DAYS[d.weekday()]))
            cur = d
        print("      %s  %-8s %s" % (tm, sname, title))
    for sname_, cnt in picks.items():
        n_pins, n_imgs = sum(cnt.values()), len(cnt)
        pool_n = len(pools.get(sname_, []))
        if n_pins > pool_n > 0 and sname_ != "mockups":
            warn_msgs.append("%s: %d pins but only %d different images, so the same image is posted up to %d times in this range"
                             " (about every %d days). Add images, or plan this stream for fewer days with --streams."
                             % (sname_, n_pins, pool_n, max(cnt.values()), pool_n))
    relaxed = {}
    other = []
    for w in warn + warn_msgs:
        m = re.match(r"(\w+) on (\S+): spacing rules had to be relaxed", w)
        if m:
            relaxed.setdefault(m.group(1), []).append(m.group(2))
        else:
            other.append(w)
    for name, days_ in relaxed.items():
        print("  ! %s: on %d day(s) (first: %s) the same print/template had to come back sooner than your spacing rules"
              " - add more images to this stream to avoid that" % (name, len(days_), days_[0]))
    for w in sorted(set(other)):
        print("  ! " + w)
    if missing_links:
        print("  ! no listing link yet for %d print(s): %s" % (len(missing_links), ", ".join(sorted(missing_links))))
        print("    Those pins have an empty Link column. Run  python pin_planner.py links , paste the listing links,")
        print("    then plan again (use --preview first if you do not want to use up the dates).")
    if args.preview:
        print("\nPreview only. Run the same command without --preview to write the CSV and the image folder.")
        return
    print("\nCSV for Publer: %s" % ", ".join(files))
    if exported_batches:
        print("\nImages to upload to https://github.com/%s/%s (Add file > Upload files, then Commit):" % (gh["user"], gh["repo"]))
        for folder, n in exported_batches:
            print("   %s  (%d files: select ALL files inside the folder and drag them in)" % (folder.relative_to(ROOT), n))
        first = sorted(to_export)[0]
        print("After committing, open this in your browser - you should see the picture:")
        print("   https://raw.githubusercontent.com/%s/%s/%s/%s" % (gh["user"], gh["repo"], gh["branch"], first))
        print("Do that BEFORE importing the CSV into Publer. If it shows an error, check that the image repo is public and the branch name matches pin_config.json.")
    else:
        print("No new images to upload.")


def cmd_status(cfg):
    st = load_json(STATE, None)
    if not st:
        print("Nothing planned yet.")
        return
    print("Planned up to %s. %d images used, %d files exported." % (st.get("last_date"), len(st["items"]), len(st["exported"])))


def main():
    ap = argparse.ArgumentParser(description="Pinterest/Publer planner")
    ap.add_argument("command", choices=["init", "meta", "links", "plan", "status"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--from", dest="date_from", default=None, help="first day to plan, YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", default=None, help="last day to plan (inclusive), YYYY-MM-DD")
    ap.add_argument("--preview", action="store_true", help="show the plan only; write and remember nothing")
    ap.add_argument("--require-links", action="store_true", help="leave out prints that have no listing link yet")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: day after the last planned day)")
    ap.add_argument("--streams", default=None, help="only plan these streams, comma separated, e.g. csv_extra  or  art,mockups")
    args = ap.parse_args()
    cfg = get_config()
    if args.streams:
        want = [x.strip() for x in args.streams.split(",") if x.strip()]
        known = [x["name"] for x in cfg["streams"]]
        bad = [x for x in want if x not in known]
        if bad:
            sys.exit("Unknown stream(s): %s. Available: %s" % (", ".join(bad), ", ".join(known)))
        cfg["streams"] = [x for x in cfg["streams"] if x["name"] in want]
    {"init": lambda: cmd_init(cfg), "meta": lambda: cmd_meta(cfg), "links": lambda: cmd_links(cfg), "plan": lambda: cmd_plan(cfg, args),
     "status": lambda: cmd_status(cfg)}[args.command]()


if __name__ == "__main__":
    main()
