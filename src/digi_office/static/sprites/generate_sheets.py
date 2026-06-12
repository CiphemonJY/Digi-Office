#!/usr/bin/env python3
"""
Generate Digi-Office sprite sheets from digivolution art.

Strategy:
1. Use rembg-cleaned versions where available (transparent bg)
2. For forms where rembg failed (blank output), use original with manual center crop
3. Apply aggressive unsharp mask + contrast boost BEFORE downscaling to preserve edges
4. Scale to 48×48 with LANCZOS

Each sheet: 96×192 (2 cols × 4 rows of 48×48 frames)
States: idle, work, sleep, tool
"""

import os
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

SRC_DIR = os.path.expanduser("~/.config/digi-office/Digivolution photos")
CLEAN_DIR = os.path.expanduser("~/.config/digi-office/LISA_FTM/digi_office/static/sprites")
OUT_DIR = CLEAN_DIR

FORMS = {
    "baby":     ("Baby.PNG",     "baby_clean.png",     False),
    "rookie":   ("Rookie.PNG",   "rookie_clean.png",   False),
    "champion": ("Champion.png", "champion_clean.png", True),   # rembg failed, use original
    "ultimate": ("Ultimate.PNG", "ultimate_clean.png", False),
    "mega":     ("Mega.PNG",     "mega_clean.png",     True),   # rembg failed, use original
}

FRAME_W, FRAME_H = 48, 48
SHEET_W, SHEET_H = 96, 192


def center_crop(img: Image.Image, ratio: float = 0.75) -> Image.Image:
    """Center crop to isolate character from background."""
    w, h = img.size
    new_w = int(w * ratio)
    new_h = int(h * ratio)
    x = (w - new_w) // 2
    y = (h - new_h) // 2
    return img.crop((x, y, x + new_w, y + new_h))


def preprocess_for_sprite(img: Image.Image, use_original: bool) -> Image.Image:
    """Preprocess image before downscaling to preserve edges."""
    if use_original:
        # For originals: center crop to remove background edges
        img = center_crop(img, ratio=0.65)
        # Convert to RGBA
        if img.mode != "RGBA":
            img = img.convert("RGBA")
    else:
        # For cleaned: crop transparent margins
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
    
    # Aggressive edge enhancement before downscale
    # Unsharp mask to emphasize edges
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=3))
    # Boost contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.8)
    # Boost sharpness
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.5)
    
    return img


def fit_to_frame(img: Image.Image, size=(FRAME_W, FRAME_H)) -> Image.Image:
    """Scale to fit frame, preserve aspect ratio, center with transparent padding."""
    w, h = img.size
    target_w, target_h = size
    
    scale = min(target_w / w, target_h / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    
    frame = Image.new("RGBA", size, (0, 0, 0, 0))
    x = (target_w - new_w) // 2
    y = (target_h - new_h) // 2
    frame.paste(resized, (x, y), resized)
    
    return frame


def make_frame(img: Image.Image, size=(FRAME_W, FRAME_H),
               flip=False, brightness=1.0, contrast=1.0,
               sharpness=1.0, extra_sharpen=False) -> Image.Image:
    """Create a single sprite frame."""
    frame = img.copy()
    
    if extra_sharpen:
        frame = frame.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    if sharpness != 1.0:
        frame = ImageEnhance.Sharpness(frame).enhance(sharpness)
    if contrast != 1.0:
        frame = ImageEnhance.Contrast(frame).enhance(contrast)
    if brightness != 1.0:
        frame = ImageEnhance.Brightness(frame).enhance(brightness)
    
    if flip:
        frame = ImageOps.mirror(frame)
    
    return fit_to_frame(frame, size)


def build_sheet(char_img: Image.Image, form_name: str) -> Image.Image:
    """Build a 96×192 sprite sheet."""
    sheet = Image.new("RGBA", (SHEET_W, SHEET_H), (0, 0, 0, 0))
    
    # Row 0: idle
    f00 = make_frame(char_img, brightness=1.0, contrast=1.2, sharpness=2.0, extra_sharpen=True)
    f01 = make_frame(char_img, brightness=1.02, contrast=1.2, sharpness=2.0, extra_sharpen=True)
    sheet.paste(f00, (0, 0))
    sheet.paste(f01, (FRAME_W, 0))
    
    # Row 1: work
    f10 = make_frame(char_img, brightness=1.1, contrast=1.3, sharpness=2.2, extra_sharpen=True)
    f11 = make_frame(char_img, flip=True, brightness=1.1, contrast=1.3, sharpness=2.2, extra_sharpen=True)
    sheet.paste(f10, (0, FRAME_H))
    sheet.paste(f11, (FRAME_W, FRAME_H))
    
    # Row 2: sleep
    f20 = make_frame(char_img, brightness=0.65, contrast=1.0, sharpness=1.8)
    sheet.paste(f20, (0, 2*FRAME_H))
    sheet.paste(f20, (FRAME_W, 2*FRAME_H))
    
    # Row 3: tool
    f30 = make_frame(char_img, brightness=1.1, contrast=1.35, sharpness=2.2, extra_sharpen=True)
    f31 = make_frame(char_img, brightness=1.05, contrast=1.35, sharpness=2.2, extra_sharpen=True)
    sheet.paste(f30, (0, 3*FRAME_H))
    sheet.paste(f31, (FRAME_W, 3*FRAME_H))
    
    return sheet


def process_form(key: str, src_file: str, clean_file: str, use_original: bool) -> bool:
    """Process a single form."""
    if use_original:
        src_path = os.path.join(SRC_DIR, src_file)
    else:
        src_path = os.path.join(CLEAN_DIR, clean_file)
    
    if not os.path.exists(src_path):
        print(f"⚠️  Missing: {src_path}")
        return False
    
    print(f"\n🔄 Processing {key}...")
    if use_original:
        print(f"   Using original (center crop)")
    
    img = Image.open(src_path)
    print(f"   Source: {img.size} mode={img.mode}")
    
    # Preprocess
    char = preprocess_for_sprite(img, use_original)
    print(f"   Preprocessed: {char.size}")
    
    # Build sheet
    sheet = build_sheet(char, key)
    
    out_path = os.path.join(OUT_DIR, f"{key}.png")
    sheet.save(out_path)
    print(f"   ✅ Saved: {out_path}")
    
    img.close()
    return True


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    
    for key, (src_file, clean_file, use_original) in FORMS.items():
        process_form(key, src_file, clean_file, use_original)
    
    # Ciphemon = Rookie
    rookie_path = os.path.join(OUT_DIR, "rookie.png")
    if os.path.exists(rookie_path):
        import shutil
        shutil.copy2(rookie_path, os.path.join(OUT_DIR, "ciphemon.png"))
        print(f"\n✅ ciphemon.png → copied from rookie.png")
    
    print(f"\n📁 All sprites in: {OUT_DIR}")


if __name__ == "__main__":
    main()
