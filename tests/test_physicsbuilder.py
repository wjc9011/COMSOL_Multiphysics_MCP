"""Offline tests for physicsbuilder tools (no COMSOL session needed)."""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.tools.physicsbuilder as pb


def test_generate_substitutions():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "TestPhys.mphphb")
        r = pb.generate_mphphb(out, "TestPhys", "Test Physics", "c",
                               "D*test(c)-f*test(c)")
        assert os.path.exists(out)
        root = ET.parse(out).getroot()
        iface = next(e for e in root.iter("DesignEntity")
                     if e.get("op") == "PhysicsInterface")
        pv = {p.get("name"): p.get("value") for p in iface.findall("propertyValue")}
        assert pv["p:type"] == "TestPhys"
        assert pv["p:description"] == "Test Physics"
        weak = next(e for e in root.iter("DesignEntity") if e.get("op") == "WeakEquation")
        wpv = {p.get("name"): p.get("value") for p in weak.findall("propertyValue")}
        assert wpv["p:expression"] == "D*test(c)-f*test(c)"
        # no template field-variable leftovers, no eigenvalue restriction
        blob = open(out, encoding="utf-8").read()
        assert "psi" not in blob
        assert "name=\"p:studyTypeSelection\"" not in blob
        # param renames landed
        names = [p.get("value") for p in root.iter("propertyValue")
                 if p.get("name") == "p:name"]
        for expected in ("D", "f", "u0", "c"):
            assert expected in names


def test_register_dedup_and_unescape(tmp_patch=None):
    with tempfile.TemporaryDirectory() as home:
        prefs = os.path.join(home, "comsol.prefs")
        # simulate a COMSOL-written escaped + duplicated state
        with open(prefs, "w", encoding="utf-8") as f:
            f.write("physicsbuilder.settings.developmentfiles="
                    "C\\:/x/first.mphphb,C\\:/x/first.mphphb\n")
        orig = pb._PREFS_CANDIDATES
        pb._PREFS_CANDIDATES = [prefs,
                                os.path.join(home, "comsolserver.prefs")]
        try:
            r = pb.register_mphphb(r"C:\x\second.mphphb")
            lines = open(prefs, encoding="utf-8").read().splitlines()
            dev = next(l for l in lines if l.startswith(
                "physicsbuilder.settings.developmentfiles=")).split("=", 1)[1]
            entries = dev.split(",")
            assert entries == ["C:/x/first.mphphb", "C:/x/second.mphphb"], entries
            assert any(l == "physicsbuilder.settings.usebuilder=on" for l in lines)
            assert not os.path.exists(pb._PREFS_CANDIDATES[1])  # missing file skipped
        finally:
            pb._PREFS_CANDIDATES = orig


def test_status():
    with tempfile.TemporaryDirectory() as home:
        prefs = os.path.join(home, "comsol.prefs")
        with open(prefs, "w", encoding="utf-8") as f:
            f.write("physicsbuilder.settings.usebuilder=on\n"
                    "physicsbuilder.settings.developmentfiles=C\\:/a.mphphb\n")
        orig = pb._PREFS_CANDIDATES
        pb._PREFS_CANDIDATES = [prefs]
        try:
            st = pb.physicsbuilder_status()
            assert st[prefs]["usebuilder"] == "on"
            assert st[prefs]["developmentfiles"] == ["C:/a.mphphb"]
        finally:
            pb._PREFS_CANDIDATES = orig


if __name__ == "__main__":
    for name, fn in [(k, v) for k, v in sorted(globals().items())
                     if k.startswith("test_") and callable(v)]:
        fn()
        print("PASS", name)
    print("all physicsbuilder tests passed")
