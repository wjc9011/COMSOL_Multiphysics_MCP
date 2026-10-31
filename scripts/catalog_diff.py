#!/usr/bin/env python
"""Diff two static-catalog snapshots (e.g. COMSOL 6.3 vs 6.4).

Usage:
    python scripts/catalog_diff.py <catalog_dir_A> <catalog_dir_B>

Each directory is a src/resources/static_catalog produced by
scripts/generate_static_catalog.py on the respective install.
Reports added/removed/changed physics interfaces, feature-type deltas,
couplings, and layer type lists (geometry/mesh/solver/...).

Typical flow for the 6.4 machine: install this repo there, run
`python scripts/generate_static_catalog.py "D:/Program Files/COMSOL64/..."`,
copy the produced static_catalog folder back, then diff against the
shipped 6.3 snapshot.
"""

import json
import os
import sys


def load(d, name):
    p = os.path.join(d, name + ".json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def main():
    a, b = sys.argv[1], sys.argv[2]
    for d in (a, b):
        if not os.path.exists(os.path.join(d, "physics_interfaces.json")):
            sys.exit(f"no physics_interfaces.json in {d}")

    def index(d):
        idx = {}
        for i in load(d, "physics_interfaces"):
            feats = {f["id"] for f in i["features"]}
            idx[i["id"]] = {"module": i["module"], "license": i.get("license"),
                            "features": feats}
        return idx

    ia, ib = index(a), index(b)
    added = sorted(set(ib) - set(ia))
    removed = sorted(set(ia) - set(ib))
    changed = []
    for k in sorted(set(ia) & set(ib)):
        fa, fb = ia[k]["features"], ib[k]["features"]
        if fa != fb:
            changed.append({"id": k,
                            "features_added": sorted(fb - fa)[:15],
                            "features_removed": sorted(fa - fb)[:15]})
    print(f"interfaces: A={len(ia)} B={len(ib)} | added={len(added)} removed={len(removed)} changed={len(changed)}")
    if added:
        print("\n== ADDED interfaces ==")
        for k in added[:40]:
            print(f"  + {k}  [{ib[k]['module']}] lic={ib[k]['license']}")
    if removed:
        print("\n== REMOVED interfaces ==")
        for k in removed[:40]:
            print(f"  - {k}  [{ia[k]['module']}]")
    if changed:
        print("\n== CHANGED feature sets ==")
        for c in changed[:25]:
            print(f"  ~ {c['id']}: +{c['features_added']} -{c['features_removed']}")

    ca = {c["id"] for c in load(a, "couplings")}
    cb = {c["id"] for c in load(b, "couplings")}
    if ca or cb:
        print(f"\ncouplings: added={sorted(cb - ca)} removed={sorted(ca - cb)}")

    la, lb = load(a, "layer_types"), load(b, "layer_types")
    for layer in sorted(set(la) | set(lb)):
        sa, sb = set(la.get(layer, [])), set(lb.get(layer, []))
        if sa != sb:
            print(f"layer {layer}: +{sorted(sb - sa)[:10]} -{sorted(sa - sb)[:10]}")


if __name__ == "__main__":
    main()
