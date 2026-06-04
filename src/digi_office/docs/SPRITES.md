# Digi-Office — Sprite Creation Guide

Every agent in the dashboard is represented by an animated pixel-art sprite inside a mini office scene. This guide explains how to create, customise, and add new sprites using any pixel-art tool.

---

## How the sprite system works

```
static/sprites/
  sprites.json        ← config: which image file each agent uses
  ciphemon.png        ← sprite sheet for agent "ciphemon"
  hermes.png          ← sprite sheet for agent "hermes"
  worker.png          ← default sheet for all other agents
  generate_sheets.py  ← script that generates the default PNGs
```

At dashboard startup the browser fetches `sprites/sprites.json`, loads each PNG, and uses it for that agent's animation.
If a PNG is missing or fails to load, the dashboard falls back to built-in pixel art automatically — so you can replace sheets one at a time without breaking anything.

---

## Sprite sheet format

Each PNG is a **sprite sheet**: a grid where rows are animation states and columns are frames.

```
         frame 0      frame 1
        ┌──────────┬──────────┐
  idle  │          │          │  row 0
        ├──────────┼──────────┤
  work  │          │          │  row 1
        ├──────────┼──────────┤
  sleep │          │          │  row 2  (1 frame is fine)
        ├──────────┼──────────┤
  tool  │          │          │  row 3  (tool-call aura state)
        └──────────┴──────────┘
```

| Property | Default | Notes |
|---|---|---|
| Frame size | 48 × 48 px | Set `frameWidth`/`frameHeight` in `sprites.json` |
| Frames per row | up to 2 | More frames = smoother animation |
| Rows | 4 | idle · work · sleep · tool |
| Scale | 1× | Set `scale: 2` to double the render size |
| Format | PNG with alpha | Transparency supported |

You can use any frame size — 16×16, 32×32, 48×48, or larger. Just update `sprites.json` to match.

---

## Animation states

| Row | State | When shown |
|---|---|---|
| 0 | **idle** | Agent online, no task claimed |
| 1 | **work** | Agent has a task (`current_task_id` set) |
| 2 | **sleep** | Agent offline / heartbeat timeout |
| 3 | **tool** | Agent is mid tool-call (purple aura overlay added by engine) |

For **sleep**, one frame is enough — the engine draws floating ZZZ letters on top.
For **tool**, the engine also draws an orbiting-particle aura on top of your sprite, so a simple 1–2 frame loop works well.

---

## Quick start with Piskel (free, browser-based)

[Piskel](https://www.piskelapp.com) runs entirely in the browser — no install needed.

1. **Open Piskel** → click **Create Sprite**
2. Set canvas size: **12 × 12** (for the built-in style) or **48 × 48** for more detail
3. Draw frame 0 of your idle animation
4. Click **+ Add frame** in the Frames panel to add frame 1
5. Repeat: add 2 work frames, 1 sleep frame, 2 tool frames (8 frames total)
6. Export → **PNG** → choose **Spritesheet** layout:
   - Columns = **number of frames per row** (2 recommended)
   - Rows = **4** (one per state)
   - Arrange your frames so idle is on top, then work, sleep, tool
7. Save the PNG into `digi_office/static/sprites/yourname.png`
8. Update `sprites.json` (see below)

**Tip:** Piskel's onion-skin mode lets you see the previous frame while drawing, which makes smooth looping animations much easier.

---

## Quick start with Aseprite

Aseprite is the industry-standard pixel-art tool (~$20, often on Steam sale).

1. **File → New**: width = `frameWidth × numFrames`, height = `frameHeight × 4`
2. Draw each state in its own horizontal strip at the correct Y offset:
   - Y=0: idle strip
   - Y=frameHeight: work strip
   - Y=frameHeight×2: sleep strip
   - Y=frameHeight×3: tool strip
3. Use **layers** for each strip to keep them separate while drawing
4. **File → Export As** → PNG → export the **flattened** image
5. Drop it in `static/sprites/` and update `sprites.json`

**Aseprite tip:** Use **Edit → Grid → Set Grid** to show the frame boundaries while drawing.

---

## Using existing sprite sheets

Many free pixel-art sprite sheets are available online (itch.io, OpenGameArt, fan-made Digimon sheets).  If the frames aren't arranged in the idle/work/sleep/tool order, you have two options:

**Option A** — Rearrange in an image editor (Photoshop, GIMP, etc.)  
Cut each animation strip and paste it into the correct row.

**Option B** — Use a different row order  
Edit `sprites.json` to map states to different rows:

```json
"states": {
  "idle":  { "row": 2, "frames": 4, "fps": 4 },
  "work":  { "row": 0, "frames": 6, "fps": 8 },
  "sleep": { "row": 3, "frames": 2, "fps": 2 },
  "tool":  { "row": 1, "frames": 4, "fps": 8 }
}
```

---

## Updating sprites.json

```json
{
  "sprites": {
    "agumon": {
      "sheet": "sprites/agumon.png",
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
    "jetson":  "agumon",
    "default": "worker"
  }
}
```

`agentMap` maps **agent IDs** (what the coordinator knows) to **sprite keys** (entries in `sprites`).  
`"default"` is the fallback for any agent not listed.

---

## Generating the default sprites from pixel art

If you want to start from the built-in pixel art and modify it:

```bash
cd digi_office/static/sprites
pip install Pillow
python generate_sheets.py
```

This creates `ciphemon.png`, `hermes.png`, `worker.png` as 48×48-per-frame sheets at 4× scale.  Open them in any pixel editor and paint over them.

---

## Adding a sprite for a new agent

1. Create your PNG sprite sheet
2. Save it as `digi_office/static/sprites/newagent.png`
3. Add it to `sprites.json`:

```json
"sprites": {
  "newagent": { "sheet": "sprites/newagent.png", "frameWidth": 48, ... }
},
"agentMap": {
  "new_machine_hostname": "newagent"
}
```

4. Reload the dashboard — no server restart needed

---

## Palette reference (built-in pixel art)

Each sprite type has its own 10-colour palette (indices 0–9). Index 0 is always transparent.

| Index | Ciphemon | Hermes | Worker |
|---|---|---|---|
| 1 | `#0f172a` outline | `#0f172a` outline | `#0f172a` outline |
| 2 | `#bfdbfe` ice blue | `#fef3c7` cream | `#bbf7d0` mint |
| 3 | `#3b82f6` blue | `#fbbf24` amber | `#16a34a` green |
| 4 | `#06b6d4` cyan eye | `#111827` dark eye | `#ef4444` LED red |
| 5 | `#ffffff` white | `#ef4444` beak | `#ffffff` highlight |
| 6 | `#f472b6` pink mark | `#f59e0b` wing | `#94a3b8` panel |
| 7 | `#fca5a5` ear inner | `#ffffff` foot | `#fcd34d` gold |
| 8 | `#a855f7` nose | `#d97706` dark | `#065f46` dark |
| 9 | `#1d4ed8` dark blue | `#fde68a` light | `#6ee7b7` glow |

To change a colour, edit the `PAL` object in `dashboard.html` for the pixel-art fallback, or simply paint over the PNG for the loaded sprite.
