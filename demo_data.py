"""
demo_data.py -- creates a tiny stand-in dataset so you can try the planner without any PSD templates.

It writes (next to this script):
  artworks/                    3 generated gradient "prints" (3:4)
  output/mockups/<print>/verified/<template> - <print>.jpg
                               simple PIL stand-ins for rendered mockups (NOT produced by make_mockups.py)
  pin_metadata.csv             title / text / alt / subject / palette / rooms for each print
  listing_links.csv            placeholder listing URLs

Then try:
  python pin_planner.py plan --from 2026-12-01 --to 2026-12-07 --preview
  python pin_planner.py plan --from 2026-12-01 --to 2026-12-07
"""
import csv
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
PRINTS = {
    "demo_sunset": ((250, 120, 80), (60, 30, 110), "warm orange sunset over a purple sky", "orange, violet", "living room, bedroom"),
    "demo_forest": ((30, 110, 70), (10, 40, 30), "green forest path in soft light", "green, teal", "hallway, reading corner"),
    "demo_ocean": ((70, 150, 220), (10, 30, 90), "blue ocean waves at dusk", "blue, navy", "bathroom, study"),
}
TEMPLATES = {
    "Frame_On_Wall_01": ((236, 230, 220), (330, 120, 670, 640)),
    "Frame_On_Shelf_01": ((222, 226, 232), (120, 160, 430, 640)),
    "Easel_Gallery_01": ((244, 244, 244), (280, 80, 720, 700)),
}


def gradient(c1, c2, size=(900, 1200)):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        row = tuple(round(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = row
    return img


def main():
    (ROOT / "artworks").mkdir(exist_ok=True)
    meta_rows, link_rows = [], []
    for name, (c1, c2, subject, palette, rooms) in PRINTS.items():
        art = gradient(c1, c2)
        art.save(ROOT / "artworks" / (name + ".jpg"), quality=90)
        for tmpl, (bg, box) in TEMPLATES.items():
            room = Image.new("RGB", (1000, 800), bg)
            d = ImageDraw.Draw(room)
            x0, y0, x1, y1 = box
            d.rectangle((x0 - 14, y0 - 14, x1 + 14, y1 + 14), fill=(30, 30, 30))   # frame
            room.paste(art.resize((x1 - x0, y1 - y0)), (x0, y0))
            out = ROOT / "output" / "mockups" / name / "verified" / ("%s - %s.jpg" % (tmpl, name))
            out.parent.mkdir(parents=True, exist_ok=True)
            room.save(out, quality=90)
        meta_rows.append({
            "file": name, "title": "Abstract Gradient Wall Art (%s)" % subject.split(" ")[1],
            "text": "Placeholder description for %s." % subject,
            "alt": "A gradient print showing a %s." % subject, "link": "",
            "subject": subject, "medium": "digital gradient", "palette": palette, "rooms": rooms})
        link_rows.append((name, "https://example.com/listing/" + name))

    cols = ["file", "title", "text", "alt", "link", "subject", "medium", "palette", "rooms"]
    with (ROOT / "pin_metadata.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(meta_rows)
    with (ROOT / "listing_links.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["print", "url"])
        w.writerows(link_rows)
    print("Demo data written: 3 prints x 3 stand-in mockups. Now run:")
    print("  python pin_planner.py plan --from 2026-12-01 --to 2026-12-07 --preview")


if __name__ == "__main__":
    main()
