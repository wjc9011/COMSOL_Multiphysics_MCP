# Kelvin Transform for Open-Boundary EM in COMSOL

A practical guide for COMSOL Multiphysics users who need to solve
unbounded (open-boundary) electromagnetic problems without using
Infinite Element Domain or PML.

Distilled from 5 years (2020-2025) of practical use at the Sugahara
Lab, Kindai University. The associated lab `.mph` models are not
redistributed (large; ~113 GB of solver state across 6 subprojects)
but the procedures + pitfalls below let you reproduce them.

## Why Kelvin Transform

Three common approaches to open-boundary EM in COMSOL:

| Method | Pros | Cons |
|--------|------|------|
| Dirichlet on truncation sphere (large R) | Simple | Big mesh; truncation error scales like 1/R |
| Infinite Element Domain | Built into COMSOL | Stretching can hurt conditioning at high frequency |
| **PML** (Perfectly Matched Layer) | Standard for wave problems | Heavy on conditioning; tuning required |
| **Kelvin transform** (this guide) | Exact open BC, no truncation error | Setup tricky; requires Identity Pair + custom material |

Kelvin shines for:
- **Magnetostatic** problems (Laplace / Poisson)
- **Eddy current** at low-to-mid frequency (quasi-static)
- **Geometry where ground / earth is present** (semi-infinite half-space)

For wave / radiation, PML usually wins. For everything else
quasi-static, Kelvin gives the cleanest answer.

## The math in 4 lines

The Kelvin inversion `r → R²/r` maps the **exterior** of a sphere
of radius R to its **interior** (and vice-versa). For Laplace's
equation `∇²V = 0`:

    V(r) is harmonic in exterior ⇔ Ṽ(r') = (R/r') · V(R²/r') is harmonic in interior

Equivalently, for the magnetic potential A (linear media), the
transformed equation on the interior of the **Kelvin sphere** is:

    ∇·(ν · ∇A) = 0     with effective material  ν_kelvin = ν_0 · (r'/R)²

i.e. you solve the same PDE on a finite domain, with a
position-dependent permeability/reluctivity, glued to the physical
domain along the sphere boundary.

## COMSOL recipe (step-by-step)

The lab's working pattern, refined from
`2020_06_12_Kelvin変換の練習`:

### 1. Geometry: two spheres

```
Physical sphere:   center (0, 0, 0),     radius R (e.g. 30 mm)
Kelvin sphere:     center (X_offset, 0, 0),  radius R (same)
```

The two spheres must have the **same radius**; the offset can be
arbitrary (typical: `X_offset = 2.1 R` so the spheres do not
overlap visually).

For 2D axisymmetric, use two **circles** in the rz-plane.

### 2. Coordinate System: define the inversion

In COMSOL's **Component → Definitions → Coordinate Systems → Mapping**:

- For 3D: define `(x_phys, y_phys, z_phys) = R² / r'² · (x', y', z')`
  where `r'² = x'² + y'² + z'²`, primed coordinates are local to the
  Kelvin sphere (subtract its center first).
- For 2D axisym: `(r_phys, z_phys) = R² / (r'² + z'²) · (r', z')`

This mapping makes the Kelvin sphere "look like" the exterior of
the physical sphere in transformed coordinates.

### 3. Identity Pair (THE critical step)

In **Definitions → Pairs → Identity Pair**:

- Source = surface of physical sphere
- Destination = surface of Kelvin sphere
- This pairs corresponding points (after Kelvin inversion) so COMSOL
  enforces continuity of A across the boundary.

Lab's exported MATLAB shows the canonical incantation:

```matlab
model.component('comp1').cpl.create('linext1', 'LinearExtrusion');
% Then for each face / vertex pair:
model.component('comp1').cpl('linext1').selection.set([face_phys face_kelvin]);
model.component('comp1').cpl('linext1').selection('srcvertex1').set([vid_phys]);
model.component('comp1').cpl('linext1').selection('dstvertex1').set([vid_kelvin]);
% ... repeat for all corner vertices of the matched faces
```

For 3 vertices per face (triangulated sphere octant), three
`srcvertex1/2/3` + `dstvertex1/2/3` mappings are needed per face
pair. Lab files `Kelvin_3D.mph` and `Kelvin_3D_4Q.mph` show full
setup; the `.m` MATLAB export of `Kelvin_3D` is the easiest way to
inspect the wiring.

### 4. Material in the Kelvin sphere

Inside the Kelvin sphere, define material properties as:

    mu_r_kelvin = 1               (vacuum permeability)
    nu_kelvin = nu_0 * (r_kelvin / R)²

where `r_kelvin = sqrt((x-X_off)² + y² + z²)` is the distance from
the Kelvin sphere center.

Implemented in COMSOL as a Variable:

```
nu_factor = ((x - X_offset)^2 + y^2 + z^2) / R^2
```

then assign reluctivity = `nu0 * nu_factor` in the Kelvin domain
material.

### 5. Mesh: matched on the pair faces

The Identity Pair requires the **two paired surfaces to have the
same mesh topology** (one-to-one node correspondence). In COMSOL:

- Mesh the physical sphere surface first
- Use **Copy Face** (or "Identity Pair" auto-meshing) to copy the
  mesh onto the Kelvin sphere surface
- This usually works for moderate density (~10⁴ surface elements)

Pitfall (from lab note): for a flat-bottom geometry with size > 110
units, COMSOL's mesh copy fails — see "Pitfalls" below.

### 6. Physics: standard mf / mef / acdc

Apply the usual physics interface (e.g. `mf.Ampere's Law and Current
Conservation`) to both spheres. The Identity Pair takes care of
continuity. Far-field is implicitly enforced because the Kelvin
sphere interior maps to infinity.

## Lab subprojects (S:\COMSOL\88_ケルビン変換)

The 5-year progression, each subproject building on the last:

| Year | Subproject | What it adds | Files |
|------|-----------|--------------|-------|
| 2020-06 | `Kelvin変換の練習` | 2D + 3D + 3D-quarter basic patterns | `Kelvin_2D.mph`, `Kelvin_3D.mph`, `Kelvin_3D_4Q.mph`, `Kelvin_3D.m` (MATLAB export) |
| 2021-06 | `大地を模擬したケルビン変換@ECT` | Ground-simulated Kelvin (half-space) for **Eddy Current Testing**; coil + ground + Kelvin | `case2_30.mph`, `Case3_ゲージ固定無_無限遠.mph`, ECT_01 subfolder |
| 2021-09 | `各面に対して強制的に周期境界条件を課すスクリプト` | MATLAB script to force-apply Periodic BC on every face (alternative to Identity Pair on hex meshes) | `Set_periodic_COMSOL.m` |
| 2021-10 | `大地を模擬したケルビン変換@WPT` | Same as 2021-06 but for **Wireless Power Transfer** geometry (large coil + ground) | 89 GB; Coreform Cubit `.py` scripts for pre-mesh |
| 2023-03 | `高周波のケルビン変換_ダイポール球` | **High-frequency** Kelvin for radiating dipole — radiation problem (Kelvin + reduced-potential) | 7.6 GB |

The COMSOL `.mph` files are not redistributed in this fork — too
large + lab-internal. Setup wiring is reproducible from the
recipe above + the MATLAB export `Kelvin_3D.m`.

## Pitfalls (the hard-won kind)

### 1. "大地 Brickの xy方向に 110 が最大" (size limit on ground Brick)

Lab note from `※問題ありで休止中.txt` (2021-06 ECT):

> 大地 Brick の xy 方向に 110 が最大で、それ以上が Kelvin 変換
> 出来なかったので、途中で止まっている。

Translation: When the ground Brick has xy-dimension > 110 units,
the Kelvin transform setup failed (Identity Pair couldn't match).
Workaround: use a **smaller physical domain** + larger ratio of
Kelvin sphere offset. Or split the Brick into multiple sections.

### 2. Mesh non-conformity on the Identity Pair

If the source and destination surfaces of the Identity Pair don't
have one-to-one node correspondence, COMSOL silently produces a
WRONG solution (no error). Always verify:

- After meshing, both surfaces have the SAME number of triangles
- COMSOL's "Mesh Statistics" → check element counts per surface
- Run a sanity-check Poisson problem with known analytical answer
  (point source at origin → 1/r decay)

### 3. Periodic BC vs Identity Pair

For **hex meshes**, Identity Pair often fails because hex faces
won't match identically after Kelvin inversion. The lab's
`2021_09_01_Set_periodic_COMSOL.m` script provides a workaround:
force-apply Periodic BC on every face explicitly. Use this for
hex meshes; Identity Pair for tet meshes.

### 4. The Kelvin sphere does NOT touch the physical sphere

A common beginner mistake: putting the Kelvin sphere TANGENT to the
physical sphere (offset = 2R exactly). This causes overlapping
surfaces. Always use `offset = 2.1 R` or similar to leave a small
gap; the Identity Pair maps surfaces, not bulk space.

### 5. Reduced potential vs full potential

For high-frequency / radiating problems
(`2023_03_18_高周波のケルビン変換_ダイポール球`), use the
**reduced magnetic vector potential** (A_red) formulation:

    A = A_source + A_red

where A_source is the analytical Biot-Savart field from the source
current, and A_red is what FEM solves. This avoids amplifying source
oscillations into the Kelvin sphere where the (r/R)² scaling makes
high-frequency oscillations diverge near r → 0.

### 6. ECT-specific: ground material at the Kelvin sphere boundary

For Eddy Current Testing (`2021_06_18` subproject):
- Ground (sigma > 0) extends to the Kelvin sphere surface
- The Kelvin sphere INTERIOR must use **vacuum** (sigma = 0) material
  (it maps to infinity, where no current flows)
- Easy mistake: forgetting to override conductivity in the Kelvin
  domain → wrong eddy-current field

## Cross-references

### Inside this fork

After enabling RAG with the lab COMSOL textbooks (see
`MULTILINGUAL_AND_EXTRA_PDFS.md`):

```
search("Kelvin transform infinite domain")     # English query
search("ケルビン変換 大地", language_filter="ja")  # Japanese query
```

The 5 lab textbooks cover Kelvin transform from theoretical and
COMSOL-implementation angles.

### Outside this fork

If you also use NGSolve / Radia, the Sugahara lab maintains the
parallel implementation:

- `radia_mcp.fem(topic="kelvin_transform")` — NGSolve recipe
  (uses Periodic BC instead of COMSOL Identity Pair)
- `radia_mcp.matrix_solvers(topic="shifted_preconditioner")` —
  AMS preconditioner with Kelvin reluctivity scaling
- `radia_mcp.differential_forms` — the math behind why r → R²/r
  preserves the de Rham complex

### Public references

- D. Henrotte, K. Hameyer, "An algorithm to exploit the field
  periodicity in 3-D finite element analysis", IEEE T-MAG 38(2):
  1389-1392, 2002 (Identity Pair for periodic BC; same machinery)
- Lord Kelvin's original 1845 inversion paper (Camb. Dublin Math. J.)
- Sugahara, Igarashi *et al.* — IEEE T-MAG papers using the
  lab's Kelvin transform code (~2018-2024, see lab publication
  list)

## What to do next

If you are **starting** with COMSOL Kelvin transform:

1. Open `2020_06_12_Kelvin変換の練習/Kelvin_3D.mph` (if available
   on your machine) and trace through every step
2. If not available, manually build the geometry per "COMSOL recipe"
   above on a sphere with R = 30 mm, X_offset = 70 mm
3. Apply a Dirichlet `V = 1` on the physical sphere surface; solve
   Laplace → far-field potential should decay as 1/r at large r
4. Compare against the analytical solution `V(r) = R/r`

If you are **debugging** an existing Kelvin setup:

1. First check the Identity Pair mesh conformity (Pitfall 2)
2. Then check material assignment in the Kelvin domain (Pitfall 6)
3. Then check the inversion sign / formula (Pitfall 4)

If you are **scaling up** to a large geometry:

1. Use a smaller physical domain, larger Kelvin offset (Pitfall 1)
2. Or switch to NGSolve (which uses Periodic BC + no Identity Pair
   manual wiring) — see the `radia_mcp.fem` link above

## Contributing back

If you use this guide and find a new pitfall or improvement, please
open an issue or PR on the upstream
[wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP)
or this fork
[ksugahar/COMSOL_Multiphysics_MCP](https://github.com/ksugahar/COMSOL_Multiphysics_MCP).

Kelvin transform is undocumented territory in the official COMSOL
manuals — every practical writeup helps the next user.
