#!/usr/bin/env python3
"""
Digi-Office Procedural Pixel-Art Sprite Generator
=================================================

Takes a CreatureTraits spec and renders a 96×192 PNG sprite sheet
fit for the Digi-Office dashboard (2 cols × 4 rows of 48×48 px).

States (rows):
    row 0: idle     — neutral stance
    row 1: work     — slightly offset / active posture
    row 2: sleep    — dimmed, eyes closed / ZZZ pose
    row 3: tool     — glow aura, focused

Usage:
    python sprite_generator_procedural.py hermes_nous
    python sprite_generator_procedural.py ciphemon
    python sprite_generator_procedural.py --from-traits hermes_nous_traits.json

Outputs:
    hermes_nous.png      → sprite sheet (96×192 PNG)
    hermes_nous.json     → traits + metadata
"""

import random
import math
import json
import hashlib
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, List, Dict, Optional
from PIL import Image, ImageDraw


# ── Config ──────────────────────────────────────────────────────────
FRAME_W, FRAME_H = 48, 48
SHEET_W, SHEET_H = 96, 192  # 2 cols × 4 rows
BACKGROUND = (0, 0, 0, 0)

# Digi-Office palette reference (index → RGBA)
PAL = [
    (0, 0, 0, 0),      # 0: transparent
    (15, 23, 42, 255),   # 1: #0f172a outline
    (191, 219, 254, 255),# 2: #bfdbfe ice blue
    (59, 130, 246, 255), # 3: #3b82f6 blue
    (6, 182, 212, 255),  # 4: #06b6d4 cyan eye
    (255, 255, 255, 255),# 5: #ffffff white
    (244, 114, 182, 255),# 6: #f472b6 pink mark
    (252, 165, 165, 255),# 7: #fca5a5 ear inner
    (168, 85, 247, 255), # 8: #a855f7 nose
    (29, 78, 216, 255),  # 9: #1d4ed8 dark blue
]


def hex_to_rgba(hex_str: str, alpha=255) -> Tuple[int, ...]:
    h = hex_str.lstrip('#')
    rgb = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    return rgb + (alpha,)


# ── Traits ──────────────────────────────────────────────────────────
@dataclass
class CreatureTraits:
    # Body
    body_type: str  # round, boxy, slender, star, blob, triangular, mushroom crystal
    primary_color: Tuple[int, int, int]
    secondary_color: Tuple[int, int, int]
    accent_color: Tuple[int, int, int]
    # Features
    eye_style: str   # round, narrow, dotted, glowing, wide, cute
    mouth_style: str  # smile, fangs, beak, open, neutral
    has_horns: bool
    horn_shape: str   # straight, curved, branched, spiral
    # Accessories
    has_tail: bool
    tail_style: str   # round, spiky, bushy, long, curled, fuzzy, bolt
    has_wings: bool
    wing_type: str    # feather, bat, pixel, glow, butterfly, transparent
    # Extra
    glow_intensity: float
    pixel_factor: float
    has_aura: bool

    @property
    def rarity(self) -> int:
        score = 0
        if self.wing_type == "glow": score += 20
        if self.horn_shape == "spiral": score += 15
        if self.has_aura: score += 10
        if self.glow_intensity > 0.7: score += 10
        if self.eye_style == "glowing": score += 10
        if self.pixel_factor == 1.0: score += 5
        return min(100, score + random.randint(1, 20))

    def to_dict(self) -> Dict:
        return {
            "body_type": self.body_type,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "accent_color": self.accent_color,
            "eye_style": self.eye_style,
            "mouth_style": self.mouth_style,
            "has_horns": self.has_horns,
            "horn_shape": self.horn_shape,
            "has_tail": self.has_tail,
            "tail_style": self.tail_style,
            "has_wings": self.has_wings,
            "wing_type": self.wing_type,
            "glow_intensity": self.glow_intensity,
            "pixel_factor": self.pixel_factor,
            "has_aura": self.has_aura,
            "rarity": self.rarity,
        }


def traits_from_dict(d: Dict) -> CreatureTraits:
    return CreatureTraits(
        body_type=d["body_type"],
        primary_color=tuple(d["primary_color"]),
        secondary_color=tuple(d["secondary_color"]),
        accent_color=tuple(d["accent_color"]),
        eye_style=d["eye_style"],
        mouth_style=d["mouth_style"],
        has_horns=d.get("has_horns", False),
        horn_shape=d.get("horn_shape", "none"),
        has_tail=d.get("has_tail", False),
        tail_style=d.get("tail_style", "none"),
        has_wings=d.get("has_wings", False),
        wing_type=d.get("wing_type", "none"),
        glow_intensity=d.get("glow_intensity", 0.0),
        pixel_factor=d.get("pixel_factor", 0.0),
        has_aura=d.get("has_aura", False),
    )


def generate_traits(seed: str) -> CreatureTraits:
    """Deterministic procedural traits from a seed string."""
    # Deterministic state
    native_state = random.getstate()
    seed_int = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    random.seed(seed_int)

    palettes = {
        "warm": [(255,107,107),(255,159,67),(255,206,84),(255,99,71)],
        "cool": [(72,126,176),(87,160,211),(67,154,134),(129,152,218)],
        "neon": [(255,0,85),(0,255,255),(191,255,0),(255,0,255)],
        "pastel": [(255,182,193),(176,224,230),(255,218,185),(221,160,221)],
        "cyber": [(0,255,65),(0,149,237),(123,0,255),(0,255,255)],
        "shadow": [(45,45,45),(75,0,130),(25,25,112),(72,61,139)],
    }
    p_name = random.choice(list(palettes.keys()))
    palette = palettes[p_name]
    primary = random.choice(palette)
    secondary = random.choice([c for c in palette if c != primary])
    accent = random.choice([c for c in palette if c not in (primary, secondary)])

    body_types = ["round", "boxy", "slender", "star", "blob", "triangular", "mushroom", "crystal"]
    eye_styles = ["round", "round", "round", "dotted", "glowing", "wide", "cute"]
    mouth_styles = ["smile", "smile", "smile", "open", "neutral", "fangs"]
    horn_shapes = ["straight", "curved", "branched", "spiral", "none"]
    tail_styles = ["round", "spiky", "bushy", "long", "curled", "fuzzy", "bolt"]
    wing_types = ["feather", "bat", "pixel", "glow", "butterfly", "transparent"]

    traits = CreatureTraits(
        body_type=random.choice(body_types),
        primary_color=primary,
        secondary_color=secondary,
        accent_color=accent,
        eye_style=random.choice(eye_styles),
        mouth_style=random.choice(mouth_styles),
        has_horns=random.random() > 0.5,
        horn_shape=random.choice(horn_shapes) if random.random() > 0.3 else "none",
        has_tail=random.random() > 0.4,
        tail_style=random.choice(tail_styles) if random.random() > 0.4 else "none",
        has_wings=random.random() > 0.6,
        wing_type=random.choice(wing_types) if random.random() > 0.6 else "none",
        glow_intensity=random.uniform(0.1, 1.0),
        pixel_factor=random.uniform(0.0, 0.5),
        has_aura=random.random() > 0.7,
    )

    random.setstate(native_state)
    return traits


# ── Pixel Drawing Helpers ──────────────────────────────────────────

def fill_pixels(draw, cx, cy, radius, color):
    """Fill a pixel-art circle (filled)."""
    for y in range(int(cy - radius), int(cy + radius) + 1):
        for x in range(int(cx - radius), int(cx + radius) + 1):
            if (x - cx)**2 + (y - cy)**2 <= radius**2:
                draw.point((int(x), int(y)), fill=color)


def draw_pixel_circle(draw, cx, cy, radius, color):
    """Draw a hollow pixel-art circle."""
    for y in range(int(cy - radius), int(cy + radius) + 1):
        for x in range(int(cx - radius), int(cx + radius) + 1):
            dist = math.sqrt((x - cx)**2 + (y - cy)**2)
            if radius - 1 <= dist <= radius + 0.5:
                draw.point((int(x), int(y)), fill=color)


def draw_pixel_rect(draw, x, y, w, h, color):
    """Draw a filled pixel rectangle."""
    for oy in range(h):
        for ox in range(w):
            draw.point((x + ox, y + oy), fill=color)


def _mirror_x(draw, cx, f):
    # helper for symmetry
    pass


# ── Body, features, accessories ───────────────────────────────────

def draw_body(draw, cx, cy, traits: CreatureTraits, state="idle", offset=(0, 0)):
    """Draw the body shape on a 48×48 frame canvas."""
    ox, oy = offset
    p = traits.primary_color + (255,)
    s = traits.secondary_color + (255,)
    a = traits.accent_color + (255,)

    if traits.body_type == "round":
        fill_pixels(draw, cx+ox, cy+oy+4, 13, p)
    elif traits.body_type == "boxy":
        draw_pixel_rect(draw, int(cx+ox-12), int(cy+oy-7), 24, 22, p)
    elif traits.body_type == "slender":
        draw_pixel_rect(draw, int(cx+ox-7), int(cy+oy-13), 14, 26, p)
    elif traits.body_type == "triangular":
        # top, bottom-left, bottom-right
        pts = [(int(cx+ox), int(cy+oy-12)),
               (int(cx+ox-12), int(cy+oy+12)),
               (int(cx+ox+12), int(cy+oy+12))]
        draw.polygon(pts, fill=p)
    elif traits.body_type == "crystal":
        pts = [(int(cx+ox), int(cy+oy-13)),
               (int(cx+ox+10), int(cy+oy-4)),
               (int(cx+ox+6), int(cy+oy+12)),
               (int(cx+ox-6), int(cy+oy+12)),
               (int(cx+ox-10), int(cy+oy-4))]
        draw.polygon(pts, fill=p)
    else:  # mushroom default
        cap_pts = [(int(cx+ox-13), int(cy+oy)),
                   (int(cx+ox-10), int(cy+oy-10)),
                   (int(cx+ox), int(cy+oy-14)),
                   (int(cx+ox+10), int(cy+oy-10)),
                   (int(cx+ox+13), int(cy+oy))]
        draw.polygon(cap_pts, fill=p)
        draw_pixel_rect(draw, int(cx+ox-5), int(cy+oy), 10, 13, s)

    # outline (darker shade)
    # Keep simple: skip outline for now to save time; can add later


def draw_eyes(draw, cx, cy, traits: CreatureTraits, state="idle", offset=(0,0)):
    ox, oy = offset
    eye_y = cy + oy - 3
    white = (255, 255, 255, 255)
    black = (0, 0, 0, 255)
    yellow = (255, 255, 0, 255)

    if state == "sleep":
        # Zs instead of ZZZ → just closed eyes (short horizontal line)
        for dx in (-6, 6):
            x = int(cx + ox + dx)
            for xx in range(x-3, x+3):
                draw.point((xx, int(eye_y)), fill=black)
        return

    style = traits.eye_style
    if style == "glowing":
        fill_pixels(draw, cx+ox-7, eye_y, 4, yellow)
        fill_pixels(draw, cx+ox+7, eye_y, 4, yellow)
        fill_pixels(draw, cx+ox-7, eye_y, 2, white)
        fill_pixels(draw, cx+ox+7, eye_y, 2, white)
    elif style == "dotted":
        fill_pixels(draw, cx+ox-7, eye_y, 3, black)
        fill_pixels(draw, cx+ox+7, eye_y, 3, black)
    elif style == "wide":
        draw_pixel_rect(draw, int(cx+ox-9), int(eye_y-4), 6, 9, white)
        draw_pixel_rect(draw, int(cx+ox+3), int(eye_y-4), 6, 9, white)
        fill_pixels(draw, cx+ox-6, eye_y, 2, black)
        fill_pixels(draw, cx+ox+6, eye_y, 2, black)
    else:  # round / cute / default
        fill_pixels(draw, cx+ox-7, eye_y, 4, white)
        fill_pixels(draw, cx+ox+7, eye_y, 4, white)
        fill_pixels(draw, cx+ox-7, eye_y, 2, black)
        fill_pixels(draw, cx+ox+7, eye_y, 2, black)


def draw_mouth(draw, cx, cy, traits: CreatureTraits, state="idle", offset=(0,0)):
    ox, oy = offset
    my = int(cy + oy + 6)
    black = (0, 0, 0, 255)
    pink = (255, 107, 107, 255)

    if state == "sleep":
        draw_pixel_rect(draw, int(cx+ox-4), my-2, 8, 3, black)
        return

    style = traits.mouth_style
    if style == "smile":
        # curve via small steps
        for i, dx in enumerate(range(-5, 6)):
            yy = my + abs(dx) // 3
            draw.point((int(cx+ox+dx), yy), fill=black)
    elif style == "open":
        fill_pixels(draw, cx+ox, my+1, 4, pink)
    elif style == "fangs":
        draw_pixel_rect(draw, int(cx+ox-5), my-3, 3, 5, black)
        draw_pixel_rect(draw, int(cx+ox+2), my-3, 3, 5, black)
    else:  # neutral / none
        for xx in range(int(cx+ox-4), int(cx+ox+5)):
            draw.point((xx, my), fill=black)


def draw_horns(draw, cx, cy, traits: CreatureTraits, offset=(0,0)):
    if not traits.has_horns or traits.horn_shape == "none":
        return
    ox, oy = offset
    a = traits.accent_color + (255,)
    if traits.horn_shape == "straight":
        draw_pixel_rect(draw, int(cx+ox-9), int(cy+oy-23), 2, 8, a)
        draw_pixel_rect(draw, int(cx+ox+7), int(cy+oy-23), 2, 8, a)
    elif traits.horn_shape == "curved":
        # simple curve via stepping pixels
        for i in range(6):
            draw.point((int(cx+ox-10+i), int(cy+oy-16-i)), fill=a)
            draw.point((int(cx+ox-11+i), int(cy+oy-16-i)), fill=a)
            draw.point((int(cx+ox+6+i), int(cy+oy-16-i)), fill=a)
            draw.point((int(cx+ox+7+i), int(cy+oy-16-i)), fill=a)
    elif traits.horn_shape == "spiral":
        # spiral via small pixel arcs
        pts = [(0,0),(1,-1),(2,-1),(3,-2),(4,-2),(4,-3),(3,-4),(2,-4),(1,-5),(0,-5),(-1,-4)]
        for px, py in pts:
            # mirror
            draw.point((int(cx+ox-8+px), int(cy+oy-16+py)), fill=a)
            draw.point((int(cx+ox-7+px), int(cy+oy-16+py)), fill=a)
            draw.point((int(cx+ox+8-px), int(cy+oy-16+py)), fill=a)
            draw.point((int(cx+ox+9-px), int(cy+oy-16+py)), fill=a)
    else:  # branched
        draw_pixel_rect(draw, int(cx+ox-9), int(cy+oy-20), 2, 6, a)
        draw_pixel_rect(draw, int(cx+ox+7), int(cy+oy-20), 2, 6, a)
        draw_pixel_rect(draw, int(cx+ox-14), int(cy+oy-17), 6, 1, a)
        draw_pixel_rect(draw, int(cx+ox+8), int(cy+oy-17), 6, 1, a)


def draw_tail(draw, cx, cy, traits: CreatureTraits, offset=(0,0)):
    if not traits.has_tail or traits.tail_style == "none":
        return
    ox, oy = offset
    p = traits.primary_color + (255,)
    ty = int(cy + oy + 14)
    if traits.tail_style in ("round", "bushy"):
        fill_pixels(draw, cx+ox, ty+8, 5, p)
    elif traits.tail_style == "long":
        draw_pixel_rect(draw, int(cx+ox-1), ty, 2, 14, p)
    elif traits.tail_style == "curled":
        # upside-down U
        for i, dx in enumerate(range(-6, 7)):
            yy = ty + 6 - int(math.sqrt(max(0, 36 - dx*dx)))
            draw.point((int(cx+ox+dx), yy), fill=p)
    elif traits.tail_style == "spiky":
        for i in range(5):
            draw.point((int(cx+ox-3+i*2), ty+i*3), fill=p)
    elif traits.tail_style == "fuzzy":
        for i in range(4):
            fill_pixels(draw, cx+ox, ty+5+i*4, 4, p)
    else:
        draw_pixel_rect(draw, int(cx+ox-2), ty, 4, 10, p)


def draw_wings(draw, cx, cy, traits: CreatureTraits, offset=(0,0)):
    if not traits.has_wings or traits.wing_type == "none":
        return
    ox, oy = offset
    s = traits.secondary_color + (255,)
    if traits.wing_type == "glow":
        for dx, dy in [(-16, -3), (16, -3)]:
            fill_pixels(draw, cx+ox+dx, cy+oy+dy, 7, s)
    elif traits.wing_type == "feather":
        for side, sign in ((-1,), (1,)):
            sx = sign[0]
            # simple wing shape
            top = (int(cx+ox+sx*10), int(cy+oy-5))
            mid = (int(cx+ox+sx*20), int(cy+oy+5))
            bot = (int(cx+ox+sx*10), int(cy+oy+8))
            pts = [top, mid, bot]
            # approximate with small circle
            fill_pixels(draw, cx+ox+sx*14, cy+oy+2, 6, s)
    elif traits.wing_type == "bat":
        for sign in (-1, 1):
            pts = [(int(cx+ox+sign*10), int(cy+oy+2)),
                   (int(cx+ox+sign*20), int(cy+oy-5)),
                   (int(cx+ox+sign*22), int(cy+oy+8)),
                   (int(cx+ox+sign*10), int(cy+oy+10))]
            draw.polygon(pts, fill=s)
    elif traits.wing_type == "pixel":
        for sign in (-1, 1):
            draw_pixel_rect(draw, int(cx+ox+sign*10), int(cy+oy-5), 6, 4, s)
            draw_pixel_rect(draw, int(cx+ox+sign*10-2), int(cy+oy-8), 4, 3, s)
    else:  # butterfly / default
        for sign in (-1, 1):
            draw_pixel_rect(draw, int(cx+ox+sign*8), int(cy+oy+1), 8, 10, s)


def draw_aura(draw, cx, cy, offset=(0,0)):
    ox, oy = offset
    a = (255, 255, 255, 40)
    fill_pixels(draw, cx+ox, cy+oy, 19, a)
    fill_pixels(draw, cx+ox, cy+oy, 14, (255,255,255,30))


def draw_particles(draw, cx, cy, offset=(0,0)):
    """Tiny sparkle pixels for tool state."""
    ox, oy = offset
    c = (255, 255, 150, 200)
    for dx, dy in [(-6, -14), (8, -12), (0, -18), (5, -5), (-8, -7)]:
        draw.point((int(cx+ox+dx), int(cy+oy+dy)), fill=c)


# ── Frame composition ──────────────────────────────────────────────

def render_frame(draw, traits: CreatureTraits, state="idle", frame_i=0):
    """Render one 48×48 frame based on state and animation frame index."""
    # Offsets per state for subtle movement
    base_x, base_y = 24, 24
    offsets = {
        "idle": [(0, 0), (0, -1)],
        "work": [(1, 0), (-1, 0)],
        "sleep": [(0, 0), (0, 0)],
        "tool": [(0, 0), (0, 0)],
    }
    # For sleep row only 1 real frame; tool can have aura variations
    frame_offset = offsets[state][frame_i % 2]

    # Background aura (if applicable)
    if traits.has_aura and state in ("idle", "tool"):
        draw_aura(draw, base_x, base_y, offset=frame_offset)

    # Body
    draw_body(draw, base_x, base_y, traits, state=state, offset=frame_offset)

    # Accessories
    draw_tail(draw, base_x, base_y, traits, offset=frame_offset)
    draw_wings(draw, base_x, base_y, traits, offset=frame_offset)
    draw_horns(draw, base_x, base_y, traits, offset=frame_offset)

    # Face
    draw_eyes(draw, base_x, base_y, traits, state=state, offset=frame_offset)
    draw_mouth(draw, base_x, base_y, traits, state=state, offset=frame_offset)

    # Tool-state extras
    if state == "tool":
        draw_particles(draw, base_x, base_y, offset=frame_offset)

    # Pixel-factor post-processing (snap nearby pixels to grid for retro feel)
    # Skip — done by drawing on int grid already


# ── Sheet assembly ────────────────────────────────────────────────

def build_sheet(traits: CreatureTraits) -> Image.Image:
    """Build 96×192 sprite sheet with 4 rows × 2 cols of 48×48 frames."""
    sheet = Image.new("RGBA", (SHEET_W, SHEET_H), BACKGROUND)

    # State rows, 2 frames per state
    states_frames = [
        ("idle", 0),  ("idle", 1),
        ("work", 0),  ("work", 1),
        ("sleep", 0), ("sleep", 1),
        ("tool", 0),  ("tool", 1),
    ]

    for idx, (state, frame_i) in enumerate(states_frames):
        row = idx // 2
        col = idx % 2
        frame = Image.new("RGBA", (FRAME_W, FRAME_H), BACKGROUND)
        draw = ImageDraw.Draw(frame)
        render_frame(draw, traits, state=state, frame_i=frame_i)
        sheet.paste(frame, (col * FRAME_W, row * FRAME_H), frame)

    return sheet


# ── CLI ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Digi-Office Procedural Sprite Generator")
    parser.add_argument("id", nargs="?", default="agent_01", help="Sprite/agent ID (seed)")
    parser.add_argument("--from-traits", help="Load traits from JSON file")
    parser.add_argument("--out-dir", default=".", help="Output directory")
    parser.add_argument("--scale", type=int, default=1, help="Final scale factor (1=48px, 2=96px per frame)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_traits:
        with open(args.from_traits) as f:
            traits = traits_from_dict(json.load(f))
    else:
        traits = generate_traits(args.id)

    print(f"🎨 Generating sprite sheet for '{args.id}'...")
    print(f"   Body: {traits.body_type}, Eyes: {traits.eye_style}, Mouth: {traits.mouth_style}")
    print(f"   Wings: {traits.wing_type if traits.has_wings else 'none'}, Tail: {traits.tail_style if traits.has_tail else 'none'}")
    print(f"   Rarity: {traits.rarity}/100")

    sheet = build_sheet(traits)

    # Optional upscale
    if args.scale > 1:
        w, h = sheet.size
        sheet = sheet.resize((w * args.scale, h * args.scale), Image.NEAREST)
        print(f"   Upscaled {args.scale}x (nearest-neighbor)")

    png_path = out_dir / f"{args.id}.png"
    json_path = out_dir / f"{args.id}_traits.json"

    sheet.save(png_path)
    with open(json_path, "w") as f:
        json.dump(traits.to_dict(), f, indent=2)

    print(f"   ✅ {png_path}")
    print(f"   ✅ {json_path}")


if __name__ == "__main__":
    main()
