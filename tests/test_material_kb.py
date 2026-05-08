"""Tests for the KB-side material lookup tools (no live COMSOL needed).

We point MATERIALS_CATALOG / MATERIALS_JSON_DIR at a temporary fixture
that emulates the layout produced by extract_basic_material_lib.py.

Spec: plans/mcp_material_tools_spec.md §4, §8.1.
"""

import csv
import json
from pathlib import Path

import pytest

from src.knowledge import material_kb_tools as kb


@pytest.fixture
def fake_kb(tmp_path, monkeypatch):
    """Build a tiny KB layout under tmp_path and patch the tool module."""
    catalog_dir = tmp_path / "catalogs"
    json_dir = tmp_path / "materials" / "basic"
    catalog_dir.mkdir(parents=True)
    json_dir.mkdir(parents=True)

    catalog_path = catalog_dir / "materials_catalog.csv"
    fields = [
        "name", "source_lib", "source_path", "extracted_at",
        "thermalconductivity", "density", "heatcapacity",
        "youngsmodulus", "poissonsratio", "electricconductivity",
        "relpermittivity", "relpermeability", "refractiveindex",
        "ratioofspecificheat", "dynamicviscosity",
        "thermalexpansioncoefficient",
        "property_count_total",
    ]
    rows = [
        {
            "name": "Cast iron",
            "source_lib": "basic",
            "source_path": r"C:\fake\comsol_basic_material_lib.mph",
            "extracted_at": "2026-04-27T12:00:00",
            "thermalconductivity": "52[W/(m*K)]",
            "density": "7150[kg/m^3]",
            "heatcapacity": "490[J/(kg*K)]",
            "youngsmodulus": "1.0e11",
            "poissonsratio": "0.27",
            "electricconductivity": "",
            "relpermittivity": "",
            "relpermeability": "",
            "refractiveindex": "",
            "ratioofspecificheat": "",
            "dynamicviscosity": "",
            "thermalexpansioncoefficient": "",
            "property_count_total": "5",
        },
        {
            "name": "Iron",
            "source_lib": "basic",
            "source_path": r"C:\fake\comsol_basic_material_lib.mph",
            "extracted_at": "2026-04-27T12:00:00",
            "thermalconductivity": "76.2[W/(m*K)]",
            "density": "7870[kg/m^3]",
            "heatcapacity": "440[J/(kg*K)]",
            "youngsmodulus": "200e9",
            "poissonsratio": "0.29",
            "electricconductivity": "",
            "relpermittivity": "",
            "relpermeability": "",
            "refractiveindex": "",
            "ratioofspecificheat": "",
            "dynamicviscosity": "",
            "thermalexpansioncoefficient": "",
            "property_count_total": "5",
        },
        {
            "name": "Copper",
            "source_lib": "basic",
            "source_path": r"C:\fake\comsol_basic_material_lib.mph",
            "extracted_at": "2026-04-27T12:00:00",
            "thermalconductivity": "400[W/(m*K)]",
            "density": "8960[kg/m^3]",
            "heatcapacity": "385[J/(kg*K)]",
            "youngsmodulus": "110e9",
            "poissonsratio": "0.35",
            "electricconductivity": "5.998e7[S/m]",
            "relpermittivity": "1",
            "relpermeability": "1",
            "refractiveindex": "",
            "ratioofspecificheat": "",
            "dynamicviscosity": "",
            "thermalexpansioncoefficient": "",
            "property_count_total": "8",
        },
    ]
    with catalog_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    # Per-material JSON
    iron_record = {
        "name": "Iron",
        "source_lib": "basic",
        "source_path": rows[1]["source_path"],
        "comsol_version": "6.1",
        "extracted_at": rows[1]["extracted_at"],
        "propertyGroups": {
            "def": {
                "thermalconductivity": "76.2[W/(m*K)]",
                "density": "7870[kg/m^3]",
                "heatcapacity": "440[J/(kg*K)]",
            },
            "Enu": {
                "youngsmodulus": "200e9",
                "poissonsratio": "0.29",
            },
        },
    }
    cast_iron_record = {
        "name": "Cast iron",
        "source_lib": "basic",
        "source_path": rows[0]["source_path"],
        "comsol_version": "6.1",
        "extracted_at": rows[0]["extracted_at"],
        "propertyGroups": {
            "def": {
                "thermalconductivity": "52[W/(m*K)]",
                "density": "7150[kg/m^3]",
                "heatcapacity": "490[J/(kg*K)]",
            },
            "Enu": {
                "youngsmodulus": "1.0e11",
                "poissonsratio": "0.27",
            },
        },
    }
    copper_record = {
        "name": "Copper",
        "source_lib": "basic",
        "source_path": rows[2]["source_path"],
        "comsol_version": "6.1",
        "extracted_at": rows[2]["extracted_at"],
        "propertyGroups": {
            "def": {
                "thermalconductivity": "400[W/(m*K)]",
                "density": "8960[kg/m^3]",
                "heatcapacity": "385[J/(kg*K)]",
                "electricconductivity": "5.998e7[S/m]",
                "relpermittivity": "1",
                "relpermeability": "1",
            },
            "Enu": {
                "youngsmodulus": "110e9",
                "poissonsratio": "0.35",
            },
        },
    }
    (json_dir / "Iron.json").write_text(
        json.dumps(iron_record, indent=2), encoding="utf-8"
    )
    (json_dir / "Cast_iron.json").write_text(
        json.dumps(cast_iron_record, indent=2), encoding="utf-8"
    )
    (json_dir / "Copper.json").write_text(
        json.dumps(copper_record, indent=2), encoding="utf-8"
    )

    monkeypatch.setattr(kb, "MATERIALS_CATALOG", catalog_path)
    monkeypatch.setattr(kb, "MATERIALS_JSON_DIR", tmp_path / "materials")
    return tmp_path


def _stub_mcp(register_fn, store):
    """Capture every @mcp.tool() registration into `store`."""
    class StubMCP:
        def tool(self):
            def deco(fn):
                store[fn.__name__] = fn
                return fn
            return deco
    register_fn(StubMCP())


def test_kb_material_list_basic_and_query_filter(fake_kb):
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)

    r_all = tools["kb_material_list"]()
    assert r_all["success"] is True
    assert r_all["total_in_catalog"] == 3
    names = [m["name"] for m in r_all["materials"]]
    assert {"Cast iron", "Iron", "Copper"} <= set(names)

    r_iron = tools["kb_material_list"](query="iron")
    assert r_iron["success"] is True
    iron_names = [m["name"] for m in r_iron["materials"]]
    assert "Cast iron" in iron_names
    assert "Iron" in iron_names
    assert "Copper" not in iron_names


def test_kb_material_list_summary_includes_thermal_conductivity(fake_kb):
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)
    r = tools["kb_material_list"](query="cast iron")
    assert r["success"] is True
    assert len(r["materials"]) == 1
    assert r["materials"][0]["summary"]["thermalconductivity"] == "52[W/(m*K)]"


def test_kb_material_list_filters_by_has_property(fake_kb):
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)
    r = tools["kb_material_list"](has_property="electricconductivity")
    assert r["success"] is True
    names = [m["name"] for m in r["materials"]]
    assert names == ["Copper"]


def test_kb_material_get_exact_match(fake_kb):
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)
    r = tools["kb_material_get"]("Cast iron")
    assert r["success"] is True
    mat = r["material"]
    assert mat["name"] == "Cast iron"
    assert mat["propertyGroups"]["def"]["thermalconductivity"] == "52[W/(m*K)]"
    assert "Enu" in mat["propertyGroups"]


def test_kb_material_get_case_insensitive(fake_kb):
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)
    r = tools["kb_material_get"]("cast iron")
    assert r["success"] is True
    assert r["material"]["name"] == "Cast iron"


def test_kb_material_get_missing(fake_kb):
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)
    r = tools["kb_material_get"]("Nonexistent_Material")
    assert r["success"] is False
    assert "not found" in r["error"].lower()


def test_kb_material_list_missing_catalog(tmp_path, monkeypatch):
    """When no catalog exists yet, return a structured error."""
    monkeypatch.setattr(kb, "MATERIALS_CATALOG", tmp_path / "missing.csv")
    monkeypatch.setattr(kb, "MATERIALS_JSON_DIR", tmp_path / "no_materials")
    tools = {}
    _stub_mcp(kb.register_material_kb_tools, tools)
    r = tools["kb_material_list"]()
    assert r["success"] is False
    assert "catalog not found" in r["error"].lower()
