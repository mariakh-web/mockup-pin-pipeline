# mockup-pin-pipeline

A small, scriptable pipeline for a one-person print shop: it turns a folder of artwork into room mockups, then into a month of scheduled Pinterest pins, with a short human review step in between.

It works with any image you drop into the `artworks/` folder. Where the image came from (painted, photographed, AI-generated) does not matter to the pipeline.

## The problem

Every print needs lifestyle mockups to sell and to promote. Making them by hand means opening a template, replacing a Smart Object, exporting, saving, and repeating that for every print and every scene. Scheduling the pins that use them is a second pile of manual work: picking images, writing titles and alt text, spacing things out, and getting everything into a scheduler.

Done by hand, this work either eats days or does not get done.

## The pipeline

```
artworks/*.jpg ──► probe_psds.py ──► make_mockups.py ──► pin_planner.py ──► CSV for the scheduler
                   (is each PSD      (render every        (titles + alt text    + images for a
                    safe to use?)     print x template)    -> schedule)          public image host)
                                                                ▲
                                                   human-in-the-loop AI step:
                                                   contact sheets -> chat model -> metadata CSV
```

1. **`probe_psds.py`** inspects each PSD template read-only: finds the Smart Object slots, reads the perspective quad, detects duplicate files, and compares psd-tools' render against the preview Photoshop saved inside the file. Each template gets a parity status (`PARITY_OK`, `PARITY_MARGINAL`, `PARITY_FAIL`) and an overall verdict (`CANDIDATE`, `SKIP/RISK`, ...), so unreliable templates are caught before anything is rendered.
2. **`make_mockups.py`** renders every artwork into every usable template. It never modifies a PSD. The artwork is centre-cropped to the slot's shape, perspective-warped onto the slot's real quad, and swapped into the layer stack in memory, so the template's blend modes, masks, shadows and glare layers still apply. Output is a high-quality JPG per print and template, plus a contact sheet per print.
3. **`pin_planner.py meta`** builds labelled contact sheets and prints a ready-to-paste prompt for a chat model, which writes titles, descriptions and alt text into a CSV. A person checks the result. This is the AI-assisted step, and it deliberately keeps a human in the loop.
4. **`pin_planner.py plan`** schedules pins across the dates you choose, applying spacing rules (the same print is not repeated within N days, the same template within M days), exports resized images, and writes a CSV in the scheduler's import format.

### Why the image export exists

The scheduler (Publer) accepts media only as URLs through CSV import, not as file uploads, short of paying for API access. So the planner exports the images and builds `raw.githubusercontent.com` links pointing at a public repo that acts as an image host. That repo is kept separate from this code.

## Result

| | |
|---|---|
| Mockup sets rendered in one run | **61** (2-3 prints per mockup) |
| Wall-clock time | **~40 minutes**, unattended |
| Skipped / errors | **0 / 0** |
| Same work by hand | ~4.5 minutes per mockup in an image editor (timed once, on one template) = **~5 hours** of focused work for 61 |

The manual figure is a lower bound: it was timed on a single template, and sets with several prints take longer. The honest summary is that the pipeline turned a task that would otherwise not have been done into one that runs in the background. New artworks go into the folder and the mockups appear.

## Quick start (no PSD templates needed for the planner)

```bash
pip install -r requirements.txt

# 1. sanity-check the rendering maths (no files needed)
python make_mockups.py --selftest

# 2. try the planner on generated stand-in data
python demo_data.py
python pin_planner.py init
python pin_planner.py plan --from 2026-12-01 --to 2026-12-07 --preview   # shows the plan, writes nothing
python pin_planner.py plan --from 2026-12-01 --to 2026-12-07             # writes the CSV + image folder
```

`demo_data.py` creates simple stand-in mockups with Pillow. They are not produced by `make_mockups.py`, which needs real PSD templates. Warnings such as "same print had to come back sooner than your spacing rules" are expected with only three demo prints. They are the planner telling you the pool is too small. Warnings about the empty `photos` and `csv_extra` streams are also expected: those streams are optional.

## Real use

```bash
python probe_psds.py                     # once per new template batch
python make_mockups.py --dry-run         # shows the plan, crop amounts and skips
python make_mockups.py --workers 2       # render everything (each worker needs several GB RAM)
python make_mockups.py --resume          # continue after an interruption
python pin_planner.py meta               # contact sheets + prompt for the chat model
python pin_planner.py plan --days 30
```

Useful flags on `make_mockups.py`: `--only`, `--art`, `--solo` / `--multi` (single-frame or multi-frame templates), `--set` / `--mix` (which prints go into multi-frame scenes), `--skip-crop` (maximum acceptable crop before a template is skipped, default 25%).

Before the first real run, edit `pin_config.json` (written by `init`): set `github.user` / `github.repo` to your image host and replace the example board names.

## Design decisions worth a look

- **Skip with a reason, never fail silently.** A template with a mesh warp, Smart Filters or a degenerate quad is skipped and the reason goes into the run report. A print that would need more than 25% cropping is skipped for that template.
- **Safe to interrupt.** Each JPG is written to a `.partial` file and renamed, so a crash cannot leave a half-written image. `--resume` skips existing outputs and uses a per-template cache so finished PSDs are not even opened.
- **Fast path.** After the first full render of a template, later prints re-render only the region around the frames and paste it onto the first result.
- **Verified vs. check.** Output from templates that passed the parity test goes to a `verified` folder, everything else to `check`, so review effort goes where it is needed.
- **Planner state.** `planner_state.json` remembers what was used when, so the next batch continues without repeats and the output is deterministic for a given seed.

## Limitations

- Developed and run on Windows with Python 3.10+; paths in the console messages use Windows style.
- Single-machine scripts, not an installable package. No CI. The self-test covers the warp geometry and the in-memory layer swap, not end-to-end rendering of real templates.
- `probe_psds.py` reads a private psd-tools attribute for Smart Object data, so a psd-tools upgrade can break it. Pin the version that works for you.
- The description wording in `compose_mockup_text()` is written for one product type and is meant to be edited. Scene phrases per template live in `SCENES` and `pin_scenes.csv`.
- The PSD templates, artwork and output are not included. Use your own templates and respect their licences.

## What's next

Orchestrating the steps (new file in the folder, then a notification or a scheduled run) with Make and n8n.
