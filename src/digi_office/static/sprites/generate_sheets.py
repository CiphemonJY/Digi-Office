#!/usr/bin/env python3
"""
Generate Digi-Office sprite sheets from digivolution art.

Strategy: Crop to face/head (iconic, recognizable at small size),
then resize to 48×48 with sharpening. Much better than full-body
shrunk to a blur.

Each sheet: 96×192 (2 cols × 4 rows of 48×48 frames)
States: idle, work, sleep, tool (2 frames each, except sleep=1)
"""

import os
from PIL import Image, ImageFilter, ImageEnhance, ImageOps

SRC_DIR = os.path.expanduser("~/.openclaw/workspace/Digivolution photos")
OUT_DIR = os.path.expanduser("~/.openclaw/workspace/LISA_FTM/digi_office/static/sprites")

FORMS = {
    "baby":     "Baby.PNG",
    "rookie":   "Rookie.PNG",
    "champion": "Champion.png",
    "ultimate": "Ultimate.PNG",
    "mega":     "Mega.PNG",
}

FRAME_W, FRAME_H = 48, 48
SHEET_W, SHEET_H = 96, 192


def crop_face_region(img: Image.Image) -> Image.Image:
    """Crop to upper-center face region — the most recognizable part."""
    w, h = img.size
    # Face is roughly top 35%, centered horizontally, slightly favoring top
    crop_h = int(h * 0.38)
    crop_w = int(w * 0.55)
    left = (w - crop_w) // 2
    top = int(h * 0.05)  # Slightly below very top to capture crest/head
    return img.crop((left, top, left + crop_w, top + crop_h))


def make_clean_frame(img: Image.Image, size=(FRAME_W, FRAME_H),
                     flip=False, brightness=1.0, contrast=1.1,
                     sharpness=1.5) -> Image.Image:
    """Resize to sprite size with sharpening for crispness."""
    frame = img.copy()

    # Resize with high-quality filter
    frame = frame.resize(size, Image.LANCZOS)

    # Sharpen — critical for making details pop at small size
    frame = frame.filter(ImageFilter.SHARPEN)

    # Enhance contrast and sharpness
    if contrast != 1.0:
        frame = ImageEnhance.Contrast(frame).enhance(contrast)
    if sharpness != 1.0:
        frame = ImageEnhance.Sharpness(frame).enhance(sharpness)

    if brightness != 1.0:
        frame = ImageEnhance.Brightness(frame).enhance(brightness)

    if flip:
        frame = ImageOps.mirror(frame)

    return frame


def build_sheet(face_img: Image.Image, form_name: str) -> Image.Image:
    """Build a 96×192 sprite sheet with 4 animation states."""
    sheet = Image.new("RGBA", (SHEET_W, SHEET_H), (0, 0, 0, 0))

    # Row 0: idle — frames 0-1: slight breathing bounce
    f00 = make_clean_frame(face_img, brightness=1.0, contrast=1.1)
    f01 = make_clean_frame(face_img, brightness=1.03, contrast=1.15, sharpness=1.6)
    sheet.paste(f00, (0 * FRAME_W, 0 * FRAME_H))
    sheet.paste(f01, (1 * FRAME_W, 0 * FRAME_H))

    # Row 1: work — frames 0-1: active/energetic (brighter, sharper)
    f10 = make_clean_frame(face_img, brightness=1.08, contrast=1.2, sharpness=1.7)
    f11 = make_clean_frame(face_img, flip=True, brightness=1.08, contrast=1.2, sharpness=1.7)
    sheet.paste(f10, (0 * FRAME_W, 1 * FRAME_H))
    sheet.paste(f11, (1 * FRAME_W, 1 * FRAME_H))

    # Row 2: sleep — frame 0: dimmed, softened
    f20 = make_clean_frame(face_img, brightness=0.65, contrast=0.9, sharpness=1.0)
    sheet.paste(f20, (0 * FRAME_W, 2 * FRAME_H))
    sheet.paste(f20, (1 * FRAME_W, 2 * FRAME_H))  # Same for both cols

    # Row 3: tool — frames 0-1: focused, high contrast
    f30 = make_clean_frame(face_img, brightness=1.05, contrast=1.25, sharpness=1.8)
    f31 = make_clean_frame(face_img, brightness=1.0,  contrast=1.3,  sharpness=1.8)
    sheet.paste(f30, (0 * FRAME_W, 3 * FRAME_H))
    sheet.paste(f31, (1 * FRAME_W, 3 * FRAME_H))

    return sheet


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # First, backup existing pixel art sprites
    backup_dir = os.path.join(OUT_DIR, "pixel_art_backup")
    os.makedirs(backup_dir, exist_ok=True)
    for f in ["ciphemon.png", "hermes.png", "worker.png"]:
        src = os.path.join(OUT_DIR, f)
        if os.path.exists(src):
            import shutil
            shutil.copy2(src, os.path.join(backup_dir, f))
            print(f"💾 Backed up {f} → pixel_art_backup/")

    for key, filename in FORMS.items():
        src_path = os.path.join(SRC_DIR, filename)
        if not os.path.exists(src_path):
            print(f"⚠️  Missing: {src_path}")
            continue

        img = Image.open(src_path)
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # Crop to face/head region
        face = crop_face_region(img)

        # Build sprite sheet
        sheet = build_sheet(face, key)

        out_path = os.path.join(OUT_DIR, f"{key}.png")
        sheet.save(out_path)
        print(f"✅ {key:10s} → {out_path}  (face crop: {face.size} → sheet: {SHEET_W}×{SHEET_H})")

        img.close()

    # Update ciphemon.png to use Rookie form (the iconic look)
    rookie_path = os.path.join(SRC_DIR, FORMS["rookie"])
    if os.path.exists(rookie_path):
        img = Image.open(rookie_path).convert("RGBA")
        face = crop_face_region(img)
        sheet = build_sheet(face, "ciphemon")
        out_path = os.path.join(OUT_DIR, "ciphemon.png")
        sheet.save(out_path)
        print(f"✅ {'ciphemon':10s} → {out_path}  (Rookie face, primary identity)")
        img.close()

    # Restore pixel art backups for hermes and worker
    for f in ["hermes.png", "worker.png"]:
        backup = os.path.join(backup_dir, f)
        if os.path.exists(backup):
            import shutil
            shutil.copy2(backup, os.path.join(OUT_DIR, f))
            print(f"🔄 Restored pixel art {f}")

    print(f"\n📁 All sprites in: {OUT_DIR}")
    print("🎨 Pixel art backups in: pixel_art_backup/")


if __name__ == "__main__":
    main()
