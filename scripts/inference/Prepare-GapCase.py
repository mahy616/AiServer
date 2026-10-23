"""Render the deterministic fixture; expected answers stay outside the prompt."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
case = ROOT / "eval/cases/gap-threshold-v1"
picture = Image.new("RGB", (768, 256), "white")
draw = ImageDraw.Draw(picture)
font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 21)
small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 17)
draw.text((18, 15), "Fixed synthetic flow / gap-threshold-v1", fill="#334155", font=small)
# Non-numeric execution order ensures IDs are not interpreted as positions.
for x, title, ident in [(20, "GapMeasure", "67"), (280, "GapScript", "2"), (540, "ResultDisplay", "3")]:
    draw.rounded_rectangle((x, 90, x + 205, 190), radius=8, fill="#eff6ff", outline="#1d4ed8", width=3)
    draw.text((x + 14, 106), title, font=font, fill="#0f172a")
    draw.text((x + 14, 144), "ID " + ident, font=font, fill="#0f172a")
for start, end in [(225, 280), (485, 540)]:
    draw.line((start, 140, end - 6, 140), fill="#111827", width=4)
    draw.polygon([(end, 140), (end - 13, 132), (end - 13, 148)], fill="#111827")
picture.save(case / "flow.png")
print(case / "flow.png")
