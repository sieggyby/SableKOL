# Network graph — Tufte critique + rework plan

**Date:** 2026-05-07
**Subject:** `~/Downloads/solstitch_network_2026_05_07_interactive.html` and the
  generator `scripts/build_network_graph.py`.
**Sources:** Tufte, *VDQI* (1983/2001) Ch. 2-5; *Envisioning Information* (1990)
  Ch. 3, 4, 6. Distillation at
  `~/Desktop/Library Local/Textbooks/Information Design/tufte_principles_for_ator.md`.

---

## What Tufte would say (imagined critique)

Tufte's foundational question: **"compared to what?"** A chart earns its
real estate only by *enforcing a comparison the reader couldn't otherwise
make*. Apply that lens to the current graph:

### 1. The hairball
Force-directed layouts at >1K nodes lose pattern. The reader sees a blob of
dots; the layout tells them about clustering only if you've also computed and
colored modularity. Tufte's pattern of choice for relational data is the
**adjacency matrix** (Bertin) — rows × columns, dot at intersection — which
shows community structure that hairballs hide. *VDQI* p. 121: "Chartjunk can
turn bores into disasters, but it can never rescue a thin data set." A blob
*is* a thin data set even if the underlying graph is rich.

### 2. Lie-factor candidates
* **Size = log10(followers)** is a defensible compression but **isn't labeled
  on the chart**. A reader can't reverse-engineer "this dot represents how
  many followers." Tufte's first lie-factor rule (*VDQI* §2.1): physical size
  must be proportional to the underlying data, *and the encoding must be
  declared* (§1.2 — "label thoroughly, explain on the graphic").
* The legend in the panel says "size = log10(followers)" but nothing else
  spells out the range or the floor/ceiling clipping at 4 and 140. The reader
  cannot calibrate.

### 3. Encoding redundancy
Five visual channels, three data dimensions:
| Channel | Encodes |
|---|---|
| Node size | log10(followers) |
| Background color | in_pool count |
| Border color | role (candidate / cohort / kingmaker) |
| Border width | tier (within candidates) |
| Position | force-directed (no semantic) |
| Pulse animation | in_pool (again — same as background) |

Pulse and color encode the same variable. **Same-variable double encoding
is chartjunk** by *VDQI* §4 ("erase redundant data-ink"). And five channels
for three dimensions is the kind of overload Tufte warns against —
"deception results from the incorrect extrapolation of visual expectations"
(*VDQI* p. 60). Pick two primary channels; reserve a third for emphasis.

### 4. Chartjunk
* **Pulse animation** is the duck: design variation, not data variation. It
  doesn't reveal anything color doesn't. Demote to opt-in.
* **Edges at low opacity** at this density become a uniform grey fog. They
  don't carry inspectable information; they create texture. Per *VDQI* §5,
  drop edges to nearly-invisible at rest and brighten only on hover/selection.
* **Section borders, uppercase eyebrow labels, dark-mode chrome** in the
  control panel — all non-data-ink. *VDQI* §4: "Erase non-data-ink, within
  reason."

### 5. "Compared to what?"
The chart does not state what the reader is comparing. A kingmaker dot says
"@thefabricant: 41/66" only on hover. The data-ink rule wants this on the
graphic itself. Tufte (*VDQI* p. 75) — Connecticut speeding: a comparison
cohort changed the apparent conclusion. Here the comparison is "of 66
surveyed KOLs in the SolStitch-adjacent fashion/culture/nfts space"; that
context belongs in the caption, not the tooltip.

### 6. Density mismatch
Default view is 1,000 nodes. The reader's eye span (Tufte's *EI* p. 76:
"comparisons must be enforced within the scope of the eyespan") cannot
process 1K simultaneously-moving dots. The shrink principle (*VDQI* p. 168)
inverts: shrink the chart, multiply small panels. Better default: 200-400
nodes with **direct labels on every visible dot**, then offer "More" /
"Most" for exploratory scrolling.

### 7. Hierarchical organization absent
*EI* Ch. 3 (Layering and Separation): foreground vs. background hierarchy
should encode importance. Currently every node is at the same visual layer.
Tufte would want:
* Outreach-plan candidates as the primary layer (full saturation, full
  label, full size)
* Surveyed cohort as the secondary layer (medium emphasis)
* Kingmakers as background tissue (pale, only labeled if structurally
  important)

### 8. Aesthetic integrity
"Aesthetic integrity" is *VDQI* shorthand for *the design must not draw
attention away from the data*. Dark theme, neon palette, oscillating dots —
these draw attention to *design choices*, not the data. Tufte's preferred
palette: white background, near-black ink, one accent color reserved for
emphasis.

### 9. The slider doesn't work past halfway (operator-reported bug)
Concrete UX failure. vis.js's `forceAtlas2Based.springLength` only affects
*active* physics iterations. After `stabilization.iterations: 200`, physics
auto-stops. Slider changes the *option*, but no force is applied. Fix: on
slider change, kick physics back on for ~80 iterations.

---

## Rework plan

### Phase A — quick fixes (today)
1. **Fix the distance-slider bug.** On change, call
   `network.setOptions({physics: {enabled: true}})` then
   `network.stabilize(80)` and re-disable. Widen slider range from 20-400 to
   30-1500.
2. **Reduce default density.** "Some" bucket from 1,000 → 400 nodes.
   Re-spread the slider stops: 150 / 250 / 400 / 700 / 1500 / max.
3. **Demote pulsing to opt-in (default off).** It's decorative.
4. **Edges nearly invisible at rest.** Drop opacity from 0.4 → 0.06; brighten
   to 0.5 only for the selected node's neighbors.
5. **Tufte-light panel chrome.** Drop section borders and uppercase
   eyebrow labels; use light-grey separators and a serif label font.
6. **Better initial layout — radial seeding by role.** Candidates outer
   ring, cohort middle ring, kingmakers core. Even pre-stabilization, the
   graph reads.
7. **Add a caption block.** Inside-graphic explanation of what the dots
   are, what size means, what color means, the survey base ("of 66 surveyed
   KOLs"), the date, and the data source.

### Phase B — perceptual cleanup (next)
1. Remove border-width encoding of tier. Tier already shows in tooltip; reduce
   visual variables. Keep only size + color + role-as-border.
2. Direct-label the top 30 by score. Drop labels for everything else
   (current rules over-label).
3. Light-mode toggle (white background, dark ink — Tufte-default).
4. Footer caption with data sources / dates / extraction window / known gaps.

### Phase C — alternative views (Tufte-mode)
1. **Adjacency matrix view.** 66 cohort rows × top-100 kingmakers columns,
   dot at intersection. Reveals structure the force-directed view hides.
   This is the canonical Tufte-approved relational diagram.
2. **Ranked dot-plot small multiples** by sector. 6-9 panels, each panel a
   ranked dot plot of in_pool vs. followers.
3. **Small multiples** by community (after Modularity).

### Phase D — narrative presets
1. Add 3-4 preset filter states with captions:
   * "Top 20 fashion kingmakers" — sector=fashion, top by in_pool
   * "Hidden gems" — followers < 10K, in_pool ≥ 6
   * "Tier-A candidates only" — role=candidate, tier=A
   * "Cross-cohort connectors" — in ≥3 audience extractions
2. Each preset is a one-click button at the top of the panel.

---

## What's executing in this pass

Phase A, all 7 items.
