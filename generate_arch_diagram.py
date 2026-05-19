import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, Circle
import matplotlib.patheffects as pe

fig, ax = plt.subplots(figsize=(18, 10))
ax.set_xlim(0, 18)
ax.set_ylim(0, 10)
ax.axis("off")
fig.patch.set_facecolor("white")
ax.set_facecolor("white")

def rbox(ax, x, y, w, h, label, sub=None):
    rect = FancyBboxPatch((x, y), w, h,
                          boxstyle="round,pad=0.15",
                          linewidth=1.8, edgecolor="black", facecolor="white")
    ax.add_patch(rect)
    cy = y + h / 2 + (0.15 if sub else 0)
    ax.text(x + w/2, cy, label, ha="center", va="center",
            fontsize=11, fontfamily="monospace", fontweight="bold")
    if sub:
        ax.text(x + w/2, y + h/2 - 0.25, sub, ha="center", va="center",
                fontsize=8, fontfamily="monospace", color="#555555")

def circ(ax, cx, cy, r, label, sub=None):
    c = Circle((cx, cy), r, linewidth=1.8, edgecolor="black", facecolor="white")
    ax.add_patch(c)
    ty = cy + (0.2 if sub else 0)
    ax.text(cx, ty, label, ha="center", va="center",
            fontsize=11, fontfamily="monospace", fontweight="bold")
    if sub:
        ax.text(cx, cy - 0.3, sub, ha="center", va="center",
                fontsize=8, fontfamily="monospace", color="#555555")

def arr(ax, x1, y1, x2, y2, label=None, label_side="top"):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color="black", lw=1.6,
                                mutation_scale=16))
    if label:
        mx, my = (x1+x2)/2, (y1+y2)/2
        offset = 0.25 if label_side == "top" else -0.25
        ax.text(mx, my + offset, label, ha="center", va="center",
                fontsize=8.5, fontfamily="monospace", color="#333333",
                style="italic")

# ── Title ─────────────────────────────────────────────────────────────────
ax.text(9, 9.5, "Augmented OCR — Processing Pipeline",
        ha="center", va="center", fontsize=14,
        fontfamily="monospace", fontweight="bold")

# ── Stage boxes (horizontal pipeline) ─────────────────────────────────────
#   PDF Upload  →  normalize  →  OCR  →  Qwen3-VL  →  postprocess  →  Export

stages = [
    (0.4,  4.2, 2.2, 1.4, "PDF\nUpload",      None),
    (3.2,  4.2, 2.4, 1.4, "normalize",        "pypdfium2"),
    (6.2,  4.2, 2.4, 1.4, "PaddleOCR",        "scanned pages"),
    (10.8, 4.2, 2.4, 1.4, "postprocess",      "bbox · memory"),
    (13.8, 4.2, 2.4, 1.4, "outbound",         "contracts.py"),
    (16.4, 4.2, 1.2, 1.4, "Excel\nCSV",       None),
]

for x, y, w, h, label, sub in stages:
    rbox(ax, x, y, w, h, label, sub)

# Qwen circle (centre stage)
circ(ax, 9.2, 4.9, 0.95, "Qwen3-VL", "llm-worker")

# ── Horizontal arrows ──────────────────────────────────────────────────────
arr(ax, 2.6,  4.9, 3.2,  4.9, "ingest")
arr(ax, 5.6,  4.9, 6.2,  4.9, "pages")
arr(ax, 8.6,  4.9, 8.25, 4.9, "text\n+ geo")      # OCR → Qwen
arr(ax, 10.15,4.9,10.8,  4.9, "extraction")
arr(ax, 13.2, 4.9,13.8,  4.9, "fields")
arr(ax, 16.2, 4.9,16.4,  4.9)

# ── Supporting services (below) ────────────────────────────────────────────
rbox(ax, 2.0,  1.4, 2.6, 1.1, "PostgreSQL",  "jobs · pages · memory")
rbox(ax, 5.2,  1.4, 2.0, 1.1, "Redis",       "prompt cache")
rbox(ax, 7.8,  1.4, 2.0, 1.1, "MinIO",       "images · exports")
rbox(ax, 10.4, 1.4, 2.8, 1.1, "llama-server","Qwen3-VL GGUF")
rbox(ax, 13.8, 1.4, 2.8, 1.1, "spatial\nmemory","vendor · layout key")

# dotted vertical connectors from pipeline to services
for sx, sy in [(3.3, 4.2), (7.4, 4.2), (8.8, 4.2), (9.2, 4.2-0.95), (12.0, 4.2)]:
    pass  # replaced by simple dashed lines below

def dashed_line(ax, x1, y1, x2, y2):
    ax.plot([x1, x2], [y1, y2], color="#888888", lw=1.2,
            linestyle="dashed", zorder=0)

dashed_line(ax, 3.3,  4.2, 3.3,  2.5)
dashed_line(ax, 3.3,  2.5, 3.3,  2.5)
ax.annotate("", xy=(3.3, 2.5), xytext=(3.3, 4.2),
            arrowprops=dict(arrowstyle="-|>", color="#888888", lw=1.2,
                            linestyle="dashed", mutation_scale=12))

ax.annotate("", xy=(6.2, 2.5), xytext=(6.2, 4.2),
            arrowprops=dict(arrowstyle="-|>", color="#888888", lw=1.2,
                            linestyle="dashed", mutation_scale=12))

ax.annotate("", xy=(8.8, 2.5), xytext=(8.8, 4.2),
            arrowprops=dict(arrowstyle="-|>", color="#888888", lw=1.2,
                            linestyle="dashed", mutation_scale=12))

ax.annotate("", xy=(11.8, 2.5), xytext=(9.2, 3.95),
            arrowprops=dict(arrowstyle="-|>", color="#888888", lw=1.2,
                            linestyle="dashed", mutation_scale=12))

ax.annotate("", xy=(15.2, 2.5), xytext=(15.2, 4.2),
            arrowprops=dict(arrowstyle="-|>", color="#888888", lw=1.2,
                            linestyle="dashed", mutation_scale=12))

# ── SSE feedback arrow (pipeline → browser) ────────────────────────────────
rbox(ax, 0.4, 7.2, 2.2, 1.1, "Browser", "SPA / SSE")
arr(ax, 1.5, 6.2, 1.5, 5.6, "upload")
ax.annotate("", xy=(1.5, 7.2), xytext=(9.2, 5.85),
            arrowprops=dict(arrowstyle="-|>", color="#555555", lw=1.3,
                            connectionstyle="arc3,rad=-0.3",
                            linestyle="dashed", mutation_scale=13))
ax.text(5.5, 7.0, "SSE progress stream", ha="center", va="center",
        fontsize=8, fontfamily="monospace", color="#555555", style="italic")

# ── vendor detection note ──────────────────────────────────────────────────
rbox(ax, 3.2, 7.0, 2.8, 1.1, "vendor_detector", "exact + fuzzy")
arr(ax, 2.6, 7.55, 3.2, 7.55)
ax.text(4.6, 8.3, "rapidfuzz · vendor_aliases DB",
        ha="center", fontsize=7.5, fontfamily="monospace", color="#777777")

plt.tight_layout(pad=0.5)
out = "/home/user/Augemented_OCR_PaddleOCR_VL/docs/architecture.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print(f"Saved: {out}")
