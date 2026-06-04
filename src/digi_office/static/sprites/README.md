# Digi-Office Procedural Sprite Generator

This directory contains a **Python-native procedural pixel-art sprite generator** that creates Digi-Office compatible sprite sheets without requiring AI-generated source images.

## What it does

- Reads a seed string (agent ID) deterministically to generate a unique creature
- Outputs a **96×192 PNG sprite sheet** matching the Digi-Office frame format:
  - **2 columns** × **4 rows** of **48×48 px** frames
  - Row 0 = idle, Row 1 = work, Row 2 = sleep, Row 3 = tool
  - 2 frames per row for subtle animation
- Generates a companion `*_traits.json` file with stats (rarity, colors, accessories)
- No external artwork needed — everything is drawn at the pixel level using `Pillow`

## Dependencies

```bash
pip install Pillow
```

## Quick start

Generate for a single agent:

```bash
cd digi_office
python static/sprites/sprite_generator_procedural.py hermes_nous
```

This creates:
- `static/sprites/hermes_nous.png` — the sprite sheet
- `static/sprites/hermes_nous_traits.json` — creature stats

Generate for a child / agent from a traits JSON you received via Taildrop:

```bash
python static/sprites/sprite_generator_procedural.py \
    --from-traits /tmp/hermes_nous_traits.json \
    hermes_nous
```

Upscale 2× for crisp retro rendering on high-DPI displays:

```bash
python static/sprites/sprite_generator_procedural.py hermes_nous --scale 2
```

## Registering a new sprite in `sprites.json`

Add the output to the `sprites` block:

```json
{
  "sprites": {
    "hermes_nous": {
      "sheet": "sprites/hermes_nous.png",
      "frameWidth": 48,
      "frameHeight": 48,
      "scale": 1,
      "states": {
        "idle":  { "row": 0, "frames": 2, "fps": 3 },
        "work":  { "row": 1, "frames": 2, "fps": 7 },
        "sleep": { "row": 2, "frames": 1, "fps": 1 },
        "tool":  { "row": 3, "frames": 2, "fps": 9 }
      }
    }
  },
  "agentMap": {
    "hermes_nous": "hermes_nous"
  }
}
```

## Creature trait system

Each sprite is defined by:

| Trait | Values |
|---|---|
| **Body** | round, boxy, slender, star, blob, triangular, mushroom, crystal |
| **Eyes** | round, narrow, dotted, glowing, wide, cute |
| **Mouth** | smile, fangs, beak, open, neutral |
| **Horns** | straight, curved, branched, spiral |
| **Tail** | round, spiky, bushy, long, curled, fuzzy, bolt |
| **Wings** | feather, bat, pixel, glow, butterfly, transparent |

The palette is procedural (warm, cool, neon, pastel, cyber, shadow). Rarity is auto-calculated from combos (glow wings + spiral horns = legendary).

## Animation states

| Row | State | Description |
|---|---|---|
| 0 | idle | Subtle bounce (frame 2 offset by 1 px) |
| 1 | work | Horizontal sway / active posture |
| 2 | sleep | Dimmed, eyes closed, optional Z-particles |
| 3 | tool | Glow aura + sparkle particles (if aura trait is present) |

## How it differs from `generate_sheets.py`

| | `generate_sheets.py` | `sprite_generator_procedural.py` |
|---|---|---|
| Input | AI-generated full-body PNGs | None (pure procedural) |
| Style | Photographic / detailed | Pixel-art / retro |
| Dependencies | Pillow + source art | Pillow only |
| Palette | Extracted from photo | Procedural 6-color palette |
| Rarity system | No | Yes (trait scoring) |
| Use case | Digivolution forms from art | Agent avatars from seed |

## Files in this directory

| File | Purpose |
|---|---|
| `sprite_generator_procedural.py` | Main procedural generator |
| `sprites.json` | Dashboard sprite config |
| `generate_sheets.py` | Photo-to-sheet processor (existing) |
| `<name>.png` | Generated sprite sheets |
| `<name>_traits.json` | Creature metadata |

---

*Part of the Digi-Office multi-agent coordination dashboard. See `SPRITES.md` for full sprite sheet format details.*
