# COMSOL Electrochemistry Module Guide (COMSOL 6.3, verified)

This guide documents the electrochemistry support in this MCP server. Every
interface type and feature type mentioned here was **verified by creating it in
a real COMSOL 6.3 session** (Battery Design, Electrochemistry, Fuel Cell &
Electrolyzer, Electrodeposition, and Corrosion modules are licensed on this
machine). The authoritative registry lives in
`src/tools/electrochemistry_data.py`; query it at runtime with
`electrochemistry_get_interfaces` and `electrochemistry_get_feature_types`.

## Tools

| Tool | Purpose |
| --- | --- |
| `electrochemistry_get_interfaces` | List the 26 verified interface types (with categories and modules) |
| `electrochemistry_get_feature_types` | List verified feature types of one interface, incl. dimensions and subfeatures |
| `electrochemistry_add_interface` | Add a verified interface to a component/geometry |
| `electrochemistry_list_features` | Inspect an interface's features with their **actual** COMSOL types (`getType()`) |
| `electrochemistry_add_feature` | Create any verified feature (incl. subfeatures via `parent_feature`) |
| `electrochemistry_set_feature_selection` | Assign domains/boundaries to an existing feature |
| `electrochemistry_set_feature_properties` | Set feature properties; rejected names are reported explicitly |
| `electrochemistry_list_feature_properties` | Ask COMSOL for the valid property names of a feature |
| `electrochemistry_list_interface_properties` | Read an interface-level property group (e.g. `BatterySettings`) |
| `electrochemistry_set_interface_properties` | Configure an interface-level property group |

Recommended loop for an unfamiliar feature:
`electrochemistry_get_feature_types` → `electrochemistry_add_feature` →
`electrochemistry_list_feature_properties` → `electrochemistry_set_feature_properties`.

## Interface-level property groups (battery interfaces)

The main settings of the battery interfaces are NOT feature properties - they
live in interface-level property groups, accessible with
`electrochemistry_list_interface_properties` /
`electrochemistry_set_interface_properties`:

- **`BatterySettings`** (all battery interfaces): `Q_cell0` (cell capacity,
  default `1[A*h]`), `SOC_cell0` (initial state of charge), `I_app` (applied
  current; **negative = discharge**, positive = charge), `OperationMode`
  (`Galvanostatic`, `Potentiostatic`, `Power`, charge/discharge cycling,
  ...), `AddFormationLoss` (default on, 5%), `LumpedBatModel`
  (`LumpedCell`/`TwoElectrodes`).
- **`CellEquilibriumPotentialProp`**: `CellEquilibriumPotentialType`
  (`EocvrefAnddEocvdT` = OCV option).

Reference a battery interface by its **tag** (returned by
`electrochemistry_add_interface`); the auto-generated label is localized
(e.g. "集总电池 1" on a Chinese COMSOL installation).

## Verified end-to-end example: 18650 1C constant-current discharge

Task: simulate a 2.3 Ah 18650 cell discharging at 1C for 1 h
(24/24 live checks passed on a real COMSOL 6.3 session):

1. `electrochemistry_add_interface("LumpedBattery")`
2. `electrochemistry_set_interface_properties(tag, "BatterySettings",
   {"Q_cell0": "2.3[A*h]", "SOC_cell0": "1.0", "I_app": "-2.3[A]",
   "AddFormationLoss": "0"})`
3. OCV-SOC table on the Cell Equilibrium Potential feature (`cep1`):
   `electrochemistry_set_feature_properties(tag, "cep1",
   {"Eocv": ["3.00[V]", ..., "4.20[V]"], "SOC_Eocv": ["0", ..., "1"]})`
   (13-point typical LCO curve)
4. `mesh_create`, then `study_create("TimeDependent", step_properties=
   {"tlist": "range(0,60[s],3600[s])"})`, then `study_solve`
5. Output variables (all verified): `lb.SOC` (1 → 0 over exactly 3600 s at 1C),
   `lb.I_cell` (-2.3 A), `lb.Q_cell` (8280 C), `lb.Eocvref_cell` (OCV(SOC),
   4.20 → 3.00 V), `lb.eta_ir` (ohmic overpotential, -10 mV at 1C).
   Terminal voltage = `lb.Eocvref_cell + lb.eta_ir` (4.19 → 2.99 V).

Note: `lb.Vcell` / `lb.Eeq_cell` are **not defined** as global variables in a
plain LumpedCell model - use `lb.Eocvref_cell + lb.eta_ir` for the terminal
voltage.

## Verified interface types

- **battery** (Battery Design Module): `LithiumIonBatteryMPH` (锂离子电池),
  `BatteryBinaryElectrolyte`, `SingleParticleBattery`, `LumpedBattery`,
  `LeadAcidBattery`, `BatteryPack`
- **current_distribution** (Electrochemistry Module): `PrimaryCurrentDistribution`,
  `SecondaryCurrentDistribution`, `TertiaryCurrentDistributionNernstPlanck`,
  `ConcentratedElectrolyteTransport`
- **electroanalysis**: `Electroanalysis`, `TertiaryElectroanalysis`
- **shell_bem_pipe**: `ElectrodeShell`, `CurrentDistributionShell`,
  `CurrentDistributionBEM`, `CurrentDistributionPipe`
- **corrosion** (Corrosion Module): `CathodicProtection`, `CorrosionPrimary`,
  `CorrosionSecondary`, `CorrosionTertiaryNP`
- **electrodeposition** (Electrodeposition Module): `ElectrodepositionPrimary`,
  `ElectrodepositionSecondary`, `ElectrodepositionTertiaryNP`
- **fuel_cell_electrolyzer** (Fuel Cell & Electrolyzer Module): `HydrogenFuelCell`,
  `WaterElectrolyzer`
- **transport**: `ElectrophoreticTransport`

Note the correct spelling `LithiumIonBatteryMPH` — `LithiumIonBattery` is *not*
a valid COMSOL API type name.

## Feature dimension rules

`electrochemistry_add_feature` resolves the feature dimension automatically:

- The **interface's selection dimension** is the "domain" level. Plain
  interfaces select whole domains (3D → domain=3, boundary=2); **shell**
  interfaces (`ElectrodeShell`, `CurrentDistributionShell`) select boundaries,
  so their feature dimensions are one lower (3D → domain feature=2, boundary
  feature=1).
- Registry entries carry `dim_3d` (verified on a 3D geometry) and `levels`
  (domain/boundary/edge/point). A few types only exist in specific geometries
  (e.g. axisymmetric current sources: verified on 2D axisym, dim 1); these have
  an explicit `dims` map in the registry.
- Pass `dimension` explicitly to override.

## Singleton features

`"singleton": true` in the registry (and in `electrochemistry_get_feature_types`)
means the interface already creates one instance by default and COMSOL refuses a
second (`只允许此特征的一个实例` / "only one instance of this feature allowed").
`electrochemistry_add_feature` detects this and **configures the existing
instance instead**. Examples: `SOCAndInitialChargeDistribution`,
`CellEquilibriumPotential`, all `SingleParticleBattery` and `BatteryPack`
features.

Also note: in the `LithiumIonBatteryMPH` interface the default features'
selections are **managed automatically** by the SOC feature
(`SOCAndInitialChargeDistribution`, auto domain assignment). Trying to change
them fails with `不可编辑选择` ("selection not editable") — this is COMSOL
behavior, not a tool bug. Add additional features (e.g. a second `Separator` or
`PorousElectrode`) with explicit selections instead.

## Default subfeatures

New instances of the key domain/boundary features come **pre-populated** with
subfeatures (registry `default_children`):

- A new `PorousElectrode` ships with `ParticleIntercalation` (pin) and
  `PorousElectrodeReaction` (per) — configure those instead of adding new ones.
- A new `ElectrodeSurface` ships with `ElectrodeReaction` (er).
- Extra subfeatures can be added with `parent_feature`, e.g.
  `DoubleLayerCapacitance`, `NonFaradaicReactions`,
  `PorousMatrixDoubleLayerCapacitance`.

## Property names (verified examples)

Property names are COMSOL-internal and often differ from GUI labels; always
check with `electrochemistry_list_feature_properties`:

- `ElectroSurface` (ElectrodeSurface): ~100 properties; the boundary-condition
  selector is `BoundaryCondition` with verified values `ElectricPotential`
  (default), `TotalCurrent`, `AverageCurrentDensity`, `ExternalShort`,
  `ElectrodePotential`, `ChargeDischargeCycling`, `CircuitTerminal`,
  `CyclicVoltammetry`. Total current is `Itl` (with `TotalCurrentType`).
- `Electrolyte` (SecondaryCurrentDistribution): conductivity is `sigmal`
  (electrolyte) with material toggle `sigmal_mat` = `from_mat` (default) |
  `linearizedRes` | `userdef`. Without a material, set
  `{"sigmal_mat": "userdef", "sigmal": "1[S/m]"}` or solvers fail with
  `未定义...材料属性"sigmal"`.
- `ExternalShort`: resistance property is `R` (not `Rshort`).
- `CircuitTerminal`: current is `ICircuit`; name via `TerminalName`.
- `ChargeDischargeCycling`: `StartWith`, `ChargingCurrentType`, `Ich`,
  `Crate_ch`, `Vmax`, `Vmin`, `trech`, `tredch`.

## Mesh and solve requirements

- Solving via the API does **not** auto-mesh: run `mesh_create` before
  `study_solve`, otherwise the solver produces an empty solution.
- `study_create` step types: `Stationary`, `TimeDependent` (→ Transient),
  `Eigenfrequency`, `Frequency`, `Perturbation`.
- Primary/secondary current-distribution models need a conductivity (see
  `sigmal` above); battery-domain models need proper materials for all
  electrochemical properties.

## Verified end-to-end examples

1. **Secondary current distribution (2D, stationary)** — square 0.1 m,
   `ElectrodeSurface` left with `{"BoundaryCondition": "TotalCurrent",
   "Itl": "1[A]"}`, `ElectrodeSurface` right grounded, `sigmal` user-defined,
   Stationary study → `phil` spans 0.25 V (real solution).
2. **Lumped battery (3D, time-dependent)** — see the 18650 1C discharge task
   above; solves with `lb.SOC`, `lb.Eocvref_cell`, `lb.eta_ir` outputs.
   The variable scope of the lumped battery interface is `lb.`.

## Known non-registrable types

`ReferenceElectrode`, `ElectricPotentialReference`, `InitialCellChargeDistribution`,
`EquilibriumReaction` (Electroanalysis) appear in the COMSOL GUI but are
compound application-builder items; they do not exist as raw physics feature
types and are excluded from the registry (`design_only_types`).
