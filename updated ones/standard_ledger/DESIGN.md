# Design System Strategy: High-Density Financial Precision

## 1. Overview & Creative North Star
**Creative North Star: The Sovereign Ledger**
This design system moves beyond "SaaS-standard" by embracing the brutalist precision of a high-end financial terminal (Bloomberg) and the meticulous craftsmanship of modern engineering tools (Linear). We are not building a generic dashboard; we are building a high-performance instrument for financial professionals.

The "Sovereign Ledger" aesthetic is defined by **Mathematical Rigor**. Every pixel is accounted for, corners are unyielding (0px radius), and the UI breathes through high-contrast typography and subtle grid-based alignment rather than soft shadows or rounded containers. It is a visual language of absolute certainty.

---

## 2. Colors & Surface Architecture
The palette is a monochromatic foundation punctuated by a singular, high-energy "Augmented" blue.

### Surface Hierarchy & Nesting
We do not use elevation in the traditional Material sense. Depth is achieved through **Tonal Carving**. By nesting darker surfaces within lighter ones (or vice-versa), we create "wells" of information.
- **Base Layer:** `surface` (#131313) – The infinite void.
- **Structural Sections:** `surface_container_low` (#1c1b1b) – Used for primary navigation or sidebar zones.
- **Active Workspaces:** `surface_container` (#201f1f) – The primary canvas for data tables.
- **Elevated Insights:** `surface_container_high` (#2a2a2a) – For flyouts or focused modal content.

### The "No-Line" Rule & The Ghost Fallback
*   **Prohibition:** Avoid 1px solid borders for general sectioning. Instead, define boundaries via the shift from `surface` to `surface_container_low`.
*   **The Exception:** In high-density OCR environments, where data must be surgically separated, use a **"Ghost Border"**. This is a 1px hairline using `outline_variant` at 15-20% opacity. It should feel like a suggestion of a line, not a physical barrier.

### Signature Accents
- **Primary Blue:** `primary_fixed` (#0053db) is reserved exclusively for "Augmented" state headers and the most critical call-to-action.
- **Data Purest:** All OCR-extracted data should utilize `primary` (#ffffff) to ensure maximum legibility against the charcoal depths.

---

## 3. Typography: Editorial Authority
The type system creates a friction-less transition between UI navigation and heavy data consumption.

- **UI Navigation (Inter):** Used for the "scaffolding." It is neutral, legible, and stays out of the way. 
    - *Usage:* `label-md` for navigation, `title-sm` for section headers.
- **Data Values (Berkeley Mono):** This is the soul of the system. Every OCR-extracted figure, currency, or confidence score must use Berkeley Mono. 
    - *Rationale:* Monospaced fonts imply precision and auditability. It aligns digits vertically in tables, allowing the eye to scan for anomalies instantly.
- **The Hierarchy:** We use dramatic scale shifts. A `display-lg` headline in Inter creates an editorial, high-end feel, while tiny `label-sm` Berkeley Mono tags provide technical density.

---

## 4. Elevation & Depth
In this design system, shadows are almost entirely replaced by **Atmospheric Layering**.

- **The Layering Principle:** To lift a card, do not add a shadow. Instead, change the background token from `surface_container` to `surface_container_highest`. 
- **Glassmorphism:** For floating command palettes (CMD+K), use `surface_container_high` with a 24px backdrop-blur and 60% opacity. This creates a "frosted obsidian" look that maintains the dark-mode aesthetic while allowing the data grid below to peek through.
- **Ambient Glow:** For the 'AUGMENTED OCR' header, apply a subtle outer glow using the `primary_fixed` color (#0053db) at 5% opacity, giving the brand accent a "lit-from-within" professional energy.

---

## 5. Components

### Buttons & Inputs
- **Primary Action:** 0px radius. Background: `primary_fixed`. Text: `on_primary_fixed` (White). No gradients, no shadows—just a solid block of authoritative blue.
- **Secondary Action:** Ghost style. `outline` hairline border (20% opacity). Hover state: background shifts to `surface_bright`.
- **Input Fields:** Use `surface_container_lowest` for the field background. The focus state is a 1px `primary_fixed` bottom-border only.

### Data Tables (The Core)
- **Dividers:** Forbid horizontal lines. Use `0.4rem` (Spacing-2) vertical padding and subtle background alternating (`surface` to `surface_container_low`) to separate rows.
- **OCR Confidence Chips:** Sharp-edged boxes. Confidence >90% uses `secondary_container`. Confidence <50% uses `error_container`.

### Information Density
- **The Spacing Rule:** Use `0.2rem` (Spacing-1) and `0.4rem` (Spacing-2) aggressively. The UI should feel "tight." Information density is a feature, not a bug, for finance users.

---

## 6. Do’s and Don'ts

### Do
*   **Do** use Berkeley Mono for all numbers, dates, and ID strings.
*   **Do** embrace the 0px corner radius across every single element (buttons, cards, modals).
*   **Do** use `surface_container_highest` for hover states on list items.
*   **Do** use `outline_variant` at low opacity to create "Subtle Grids" in the background of empty work areas to reinforce the precision vibe.

### Don’t
*   **Don’t** use standard "Grey" (#808080). Use the specific `on_surface_variant` (#c6c6c6) for muted text to maintain the charcoal warmth.
*   **Don’t** use rounded icons. Select sharp, linear icon sets (1.5pt stroke weight).
*   **Don’t** add drop shadows to cards. If it needs to pop, use a high-contrast background shift or a "Ghost Border."
*   **Don’t** waste space. If a layout feels "airy," increase the information density by reducing padding to the next step down in the Spacing Scale.