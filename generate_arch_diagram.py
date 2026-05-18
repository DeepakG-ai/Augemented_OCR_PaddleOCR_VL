import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

fig, ax = plt.subplots(figsize=(22, 16))
ax.set_xlim(0, 22)
ax.set_ylim(0, 16)
ax.axis("off")
fig.patch.set_facecolor("#0d1117")
ax.set_facecolor("#0d1117")

# ── colour palette ──────────────────────────────────────────────────────────
C = {
    "bg":      "#0d1117",
    "panel":   "#161b22",
    "border":  "#30363d",
    "blue":    "#1f6feb",
    "green":   "#238636",
    "orange":  "#b08800",
    "purple":  "#8957e5",
    "red":     "#da3633",
    "teal":    "#1b7c83",
    "text":    "#e6edf3",
    "subtext": "#8b949e",
    "arrow":   "#58a6ff",
}

def box(ax, x, y, w, h, label, sublabel=None, color=C["blue"], fontsize=9):
    rect = FancyBboxPatch((x, y), w, h,
                          boxstyle="round,pad=0.05",
                          linewidth=1.4,
                          edgecolor=color,
                          facecolor=C["panel"],
                          zorder=3)
    ax.add_patch(rect)
    ty = y + h / 2 + (0.15 if sublabel else 0)
    ax.text(x + w / 2, ty, label,
            ha="center", va="center", color=C["text"],
            fontsize=fontsize, fontweight="bold", zorder=4)
    if sublabel:
        ax.text(x + w / 2, y + h / 2 - 0.22, sublabel,
                ha="center", va="center", color=C["subtext"],
                fontsize=7, zorder=4)

def section_bg(ax, x, y, w, h, label, color):
    rect = FancyBboxPatch((x, y), w, h,
                          boxstyle="round,pad=0.1",
                          linewidth=1.2,
                          edgecolor=color,
                          facecolor=color + "18",
                          zorder=1)
    ax.add_patch(rect)
    ax.text(x + 0.15, y + h - 0.25, label,
            ha="left", va="top", color=color,
            fontsize=8, fontweight="bold", zorder=2)

def arrow(ax, x1, y1, x2, y2, color=C["arrow"]):
    ax.annotate("",
                xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=color,
                                lw=1.5, mutation_scale=14),
                zorder=5)

def label_arrow(ax, x, y, text):
    ax.text(x, y, text, ha="center", va="center",
            color=C["subtext"], fontsize=6.5, zorder=6,
            bbox=dict(facecolor=C["bg"], edgecolor="none", pad=1))

# ── title ────────────────────────────────────────────────────────────────────
ax.text(11, 15.6, "Augmented OCR — System Architecture",
        ha="center", va="center", color=C["text"],
        fontsize=15, fontweight="bold")
ax.text(11, 15.2, "PaddleOCR + Qwen3-VL · Five-Stage Durable Pipeline",
        ha="center", va="center", color=C["subtext"], fontsize=9)

# ════════════════════════════════════════════════════════════════════════════
# ROW 1 — Client + API layer
# ════════════════════════════════════════════════════════════════════════════
section_bg(ax, 0.3, 13.0, 4.5, 1.8, "Client", C["subtext"])
box(ax, 0.6, 13.3, 3.9, 1.2, "Browser (SPA)", "frontend/app.js  ·  5 pages", C["subtext"])

section_bg(ax, 5.3, 13.0, 11.4, 1.8, "API  —  main.py  (FastAPI)", C["blue"])
box(ax, 5.6, 13.3, 2.2, 1.2, "POST /ingest/ui", "vendor detect · enqueue", C["blue"])
box(ax, 8.1, 13.3, 2.5, 1.2, "GET /jobs/{id}/stream", "SSE progress", C["blue"])
box(ax, 10.9, 13.3, 2.2, 1.2, "GET /extractions", "list · count · detail", C["blue"])
box(ax, 13.4, 13.3, 2.9, 1.2, "Review / Corrections API", "spatial memory writes", C["blue"])

section_bg(ax, 17.1, 13.0, 4.5, 1.8, "Auth / Rate Limit", C["orange"])
box(ax, 17.4, 13.3, 3.9, 1.2, "slowapi  ·  30 req/min", "route-order guard", C["orange"])

# ════════════════════════════════════════════════════════════════════════════
# ROW 2 — Pipeline workers
# ════════════════════════════════════════════════════════════════════════════
section_bg(ax, 0.3, 9.8, 21.3, 2.8, "Five-Stage Pipeline  —  worker.py", C["green"])

stages = [
    ("1  normalize", "pypdfium2\ndigital vs scanned", 0.7),
    ("2  ocr", "PaddleOCR\nscanned pages", 4.9),
    ("3  llm", "Qwen3-VL\nfield extraction", 9.1),
    ("4  postprocess", "bbox mapping\nspatial memory", 13.3),
    ("5  outbound", "Excel / CSV\ncontracts.py", 17.5),
]
for label, sub, x in stages:
    box(ax, x, 10.1, 3.8, 2.1, label, sub, C["green"], fontsize=8.5)

# stage arrows
for i in range(len(stages) - 1):
    x1 = stages[i][2] + 3.8
    x2 = stages[i+1][2]
    arrow(ax, x1, 11.15, x2, 11.15)

# ════════════════════════════════════════════════════════════════════════════
# ROW 3 — Core modules
# ════════════════════════════════════════════════════════════════════════════
section_bg(ax, 0.3, 7.1, 21.3, 2.4, "Core Modules", C["purple"])

mods = [
    ("geometry.py", "word bbox\nunification"),
    ("vendor_detector.py", "exact + fuzzy\nrapidfuzz"),
    ("extractor.py", "system prompt\ngold examples"),
    ("spatial_memory.py", "region reuse\nacross docs"),
    ("qwen_bbox_parser.py", "anchor box\nparser"),
    ("object_store.py", "MinIO /\nlocal fallback"),
    ("cache.py", "Redis\nprompt cache"),
]
gx = 0.55
for name, sub in mods:
    box(ax, gx, 7.4, 2.85, 1.8, name, sub, C["purple"], fontsize=7.5)
    gx += 3.05

# ════════════════════════════════════════════════════════════════════════════
# ROW 4 — Infrastructure
# ════════════════════════════════════════════════════════════════════════════
section_bg(ax, 0.3, 4.0, 21.3, 2.8, "Infrastructure", C["teal"])

infra = [
    ("PostgreSQL", "17 tables\nasyncpg · db.py", 0.7),
    ("Redis", "prompt cache\nresult cache", 5.2),
    ("MinIO", "page images\nexport files", 9.7),
    ("LLM Server", "llama.cpp\nQwen3-VL GGUF", 14.2),
    ("Docker Compose", "all services\nport mapping", 18.2),
]
for label, sub, x in infra:
    box(ax, x, 4.3, 3.8, 2.1, label, sub, C["teal"], fontsize=8.5)

# ════════════════════════════════════════════════════════════════════════════
# ROW 5 — DB tables highlight
# ════════════════════════════════════════════════════════════════════════════
section_bg(ax, 0.3, 1.3, 21.3, 2.4, "Key Database Tables  (PostgreSQL)", C["red"])

tables = [
    "vendors", "vendor_aliases", "templates",
    "documents", "extractions", "pages",
    "jobs", "spatial_memory", "gold_examples",
    "field_locations", "review_events",
]
tx = 0.65
for t in tables:
    box(ax, tx, 1.55, 1.75, 1.45, t, None, C["red"], fontsize=7)
    tx += 1.9

# ════════════════════════════════════════════════════════════════════════════
# Vertical arrows: API → Pipeline → Modules → Infra
# ════════════════════════════════════════════════════════════════════════════
# upload flow
arrow(ax, 5.0, 13.3, 3.3, 11.6)   # ingest → normalize
label_arrow(ax, 4.0, 12.55, "enqueue job")

# SSE upward
arrow(ax, 9.35, 13.3, 9.35, 12.2)
arrow(ax, 9.35, 12.2, 9.35, 11.6)

# workers → modules (representative)
arrow(ax, 2.6, 10.1, 2.6, 9.5)
arrow(ax, 6.8, 10.1, 6.8, 9.5)
arrow(ax, 11.0, 10.1, 11.0, 9.5)
arrow(ax, 15.2, 10.1, 15.2, 9.5)

# modules → infra (representative)
arrow(ax, 2.0, 7.4, 2.0, 6.4)
arrow(ax, 5.05, 7.4, 5.05, 6.4)
arrow(ax, 8.1, 7.4, 8.1, 6.4)
arrow(ax, 14.2, 7.4, 15.3, 6.4)

# infra → db tables
arrow(ax, 2.6, 4.3, 2.6, 3.7)

# ── legend ───────────────────────────────────────────────────────────────────
legend_items = [
    (C["blue"],   "API layer"),
    (C["green"],  "Pipeline workers"),
    (C["purple"], "Core modules"),
    (C["teal"],   "Infrastructure"),
    (C["red"],    "DB tables"),
    (C["orange"], "Rate limit / auth"),
]
lx, ly = 0.5, 0.95
for color, label in legend_items:
    p = mpatches.Patch(facecolor=color + "40", edgecolor=color, linewidth=1.2)
    ax.text(lx + 0.3, ly, label, color=C["text"], fontsize=7.5, va="center")
    rect2 = FancyBboxPatch((lx, ly - 0.12), 0.22, 0.24,
                           boxstyle="round,pad=0.02",
                           edgecolor=color, facecolor=color + "40")
    ax.add_patch(rect2)
    lx += 2.0

plt.tight_layout(pad=0)
out = "/home/user/Augemented_OCR_PaddleOCR_VL/docs/architecture.png"
plt.savefig(out, dpi=150, bbox_inches="tight",
            facecolor=fig.get_facecolor())
print(f"Saved: {out}")
