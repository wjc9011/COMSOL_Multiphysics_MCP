# COMSOL MCP Server for COMSOL 6.4

Version 0.8.0 separates research policy from native execution. Training and surrogate/ONNX remain available. It retains [experimental solver-free Ubuntu support](docs/experimental_linux/README.md) and [explicit strict wavelength validation](docs/strict_wavelength/README.md). Windows remains the native solver platform.

English | [中文](README_CN.md)

[![CI](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
![Candidate: 0.8.1](https://img.shields.io/badge/candidate-0.8.1-blue)
![Status: alpha](https://img.shields.io/badge/status-alpha-red)
[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> A maintained fork of [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP), accepted for the **`COMSOL 6.4.0.*` release line** and **MPh 1.3.1 standalone/clientapi**. Licensed reference evidence uses COMSOL **6.4.0.293**. A third numeric component change is a separate release family and requires new acceptance.

This server gives AI agents a safer, smaller interface for COMSOL inspection, controlled one-point validation, durable staged sweeps, and offline manual lookup. It is designed for the `model.java` clientapi object returned by `mph.Client()`, whose API differs materially from the direct `com.comsol.model.Model` API targeted by the upstream project.

## Featured capabilities

- **Evidence integrity and anti-hallucination verification (default-on).** Formal
  claims can be checked against exact outcome contracts, raw artifact chains,
  summary citations, and resume producer/driver identity. Users may explicitly
  opt out per check for exploration, but affected results carry an unverified
  warning. Read the independent [evidence-integrity guide](docs/evidence_integrity/README.md).
- **Interactive COMSOL Desktop/Server collaboration (default-off).** A user and
  agent can take explicit turns with one user-owned local Server, one connected
  Desktop, and one exact server-held model. It requires explicit independent
  feature enablement and per-session confirmation. Read the [interactive guide](docs/interactive_shared_session/README.md).
- **Five simulation execution modes.** `interactive`, `inline`, `launcher`,
  `standalone`, and `mphonly` distinguish short feedback, direct scripts,
  durable local campaigns, Python-free Windows handoff, and portable COMSOL
  cluster/cloud delivery. Agents default to the first three and must collect
  the target environment before preparing either cross-device mode. Read the
  independent [execution-modes guide](docs/simulation_execution_modes/README.md).

## Recommended companion skill

For Claude Code, Codex CLI, opencode, Hermes Agent, and other skill-aware agents, use the
[COMSOL 6.4+ metasurface agent skill](https://github.com/garbage-enzyme/COMSOL_6_4_agentskill_for_metasurfaces)
alongside this server. Its short `SKILL.md` entry routes agents to focused
reference modules for clientapi, periodic Wave Optics, materials and boundaries,
durable jobs, physical evidence, resource safety, and troubleshooting without
forcing the full guide into context on every turn. Repository development and
release procedures remain in this repository's development kit.

## Client compatibility and deployment

Earlier installed-server tests covered Codex CLI and opencode. A Windows 11 test with Claude Code 2.1.220 covered stdio initialization and the tested `wave_optics` tool surface. It called `capabilities`, `comsol_status`, and `solver_status`. The server exited without creating a solver lease or COMSOL process.

Those results cover transport, schemas, discovery, and status calls only. They do not prove `comsol_start`, licensed start/solve/cleanup behavior, or the complete error surface.

For 0.7.6, the external-client review remains incomplete. The Claude Python SDK smoke is not evidence of native Claude Tasks support. A later durable sweep completed, but its solved wavelength did not match the requested wavelengths. That run does not validate the requested spectrum. OpenCode testing stopped after a connection failure. Neither client has complete native interoperability acceptance for this release.

Hermes Agent remains configuration-level guidance without an end-to-end client
test. Use the complete deployment guide for fresh installation, exact
configuration paths, profile selection, restart behavior, and solver-free
verification:

- [Deployment guide](DEPLOYMENT.md)

The essential rules are: perform a non-editable install, configure the absolute
installed `comsol-mcp` executable, use the Settings GUI for ordinary user
configuration, restart the client after changing startup settings or the
package, and keep COMSOL tool calls serialized. Direct JSON editing is the
advanced path for developers, deployment automation, and agents acting on an
explicit user request. Call `capabilities` to verify the deployed profile
without starting COMSOL.

The checked-in client examples follow the official
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp), Hermes
[MCP documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md),
and Hermes [client source](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py).
An example is configuration guidance until its exact client path is exercised.
A real client acceptance report should include `initialize`, live `list_tools`,
and `capabilities` readback without starting COMSOL, then separately label any
licensed start/solve/cleanup coverage. Treat live discovery, not a count copied
from documentation, as the authority for the installed tool surface.

Release `0.7.6` moves the runtime base to the newest stable MCP Python SDK,
`2.2.x`. Its tools, profiles, schemas, and stdio configuration remain on the
same application contract, and MCP 1.x clients stay wire-compatible through the
ordinary-tool path. The SDK version, the `2026-07-28` protocol revision, and any
extension wire generation are three separate identities: an SDK upgrade is not
an extension implementation.

This release also implements the stable `2026-07-28` MCP Tasks extension
(`io.modelcontextprotocol/tasks`) as a bounded adapter over the existing durable
job engine, so a client that opts in can start a long simulation and poll it
through `tasks/get` instead of blocking. The adapter adds no second scheduler:
`job_submit` is the only task-capable tool, a task handle is emitted only when
*that* request declares the extension, and an identical retry returns the
existing durable job rather than launching another one.
Task TTL permits later expiry. The current implementation retains mappings after TTL, including completed tasks.
It never abandons a running solve or erases scientific evidence.
Cancellation acknowledgement remains separate from verified cleanup. Clients that do not opt in keep using the ordinary
`job_submit`/`job_status`/`job_tail`/`job_cancel`/`job_resume` tools unchanged,
and the experimental `2025-11-25` Tasks dialect is refused rather than
reinterpreted.

The 2026-07-28 core revision uses per-request metadata instead of an initialize handshake.
The client must declare the protocol revision and client capabilities on each request.
Older revisions retain initialize negotiation and ordinary tool results.
The Tasks path uses the same registered submission guards as ordinary `job_submit`.

The legacy initialize response also carries a short safety instruction through the
legacy MCP initialize schema: discover capabilities and preflight first, require
an explicit user request before start/solve/mutation, keep source models
read-only, and separate execution success from evidence integrity and scientific
validation. Clients may use or ignore this hint. The server's enforced ownership
and validation checks remain the security boundary.

### Optional DeepSeek Harness bridge

This repository also contains [`dsh_bridge/`](dsh_bridge/), an optional
repo-only Node.js plugin for DeepSeek Harness. It connects DSH to the standard
installed `comsol-mcp` executable over MCP stdio, registers discovered tools
with clean names, and mirrors durable `job_submit` work into `ctx.jobs` for
completion notices, progress output, cancellation, and rehydration. It does
not alter the Python package, public schemas, solver ownership, or production
settings.

The bridge is excluded from the Python wheel and sdist. Node.js 20+ is required
and there are no npm dependencies. Validate it with
`npm test --prefix dsh_bridge` and `npm run smoke --prefix dsh_bridge`, then
install it only when DSH is the intended client:

```powershell
.\dsh_bridge\install.ps1
```

Add the `comsol-bridge` entry shown in [`dsh_bridge/README.md`](dsh_bridge/README.md)
to the DSH web profile, using the absolute installed `comsol-mcp.exe` and the
runtime settings path. Do not configure a second `dsh-mcp-client` COMSOL entry
in the same DSH session: the bridge owns the single serialized MCP connection.
See the versioned
[`DeepSeek compatibility contract`](dsh_bridge/DEEPSEEK_COMPATIBILITY.md) for
installation, settings-change, verification, and recovery boundaries.
Fake-server tests are solver-free. Real DSH acceptance and licensed solves must
be reported separately.
Restart recovery routes offline completions to the original session. Notification
persistence follows native DSH semantics and does not guarantee delivery across
a crash before the session is saved.

## Highlights

- **ClientAPI compatibility.** Licensed tests cover geometry, physics, materials, meshes, studies, results, model cloning, and Unicode-safe `.mph` saves on COMSOL 6.4.0.293. Final build changes within 6.4.0.* inherit that release-line conclusion. Other release families remain unknown.
- **Typed Acoustics and PDE construction.** The `basic_fem` profile includes
  rollback-safe named Box/side selections, Pressure Acoustics, Coefficient,
  General, and Weak Form PDE interfaces, and atomic boundary batches with exact
  type and property allowlists.
- **Safe solver ownership.** An ASCII-path lease, process identity checks, external-client detection, status, and preflight checks prevent accidental competing COMSOL clients.
- **Durable background work.** Detached workers execute caller-declared staged sweeps and validation matrices. They preserve immutable specifications, atomic state, durable evidence rows, checkpoints, validated resume, and verified cancellation. The [local launcher](launcher/README.md) supports project drivers.
- **Bounded native adjoint optimization (experimental).** An `adjoint_optimization`
  durable job supports one allowlisted fixed-topology periodic-MIM adapter with
  validated COMSOL-native gradients and GCMMA. Every request must declare its
  cores, solve/iteration/wall/commit/disk/review budgets. Accepted results bind
  move limits, explicit solution/dataset identities, remeshed fresh-forward
  evidence, and immutable source bytes. It is not generic CAD, multi-state
  robust optimization, topology optimization, or a global-optimality claim.
- **Python-free standalone execution.** A reviewed native Windows x64 launcher
  keeps the same three layers: an installed and licensed COMSOL 6.4, a
  COMSOL-compiled Java point driver, and the launcher EXE. The target does not
  need Python, Conda, MPh, JPype, or an external Java installation. COMSOL
  itself is still required and is never bundled or replaced.
- **Shared Desktop collaboration (default-off).** The independent `shared_server.enabled` feature works with every tool profile. It attaches to a manually started local COMSOL Server and adopts exactly one server-held model. It enforces non-owning leases and revision locks, and runs durable attached jobs. Detach preserves the user's Server, Desktop, listener, and model. At most two independent solver owners may run in parallel, each with one core. The server refuses multi-core solver use, a third owner, and cross-owner model access.
- **COMSOL Model Manager (default-off).** `model_manager.enabled` adds bounded Model Manager tools to any profile. The separate `model_manager.upload_enabled` setting controls saving to Model Manager. Opening, running, and downloading remain available with uploads disabled. Session sharing enables neither switch. Uploads require an explicit derived source identity. The server refuses an existing destination, revision mismatch, or overwrite. A durable journal records every attempt, including refusals.
- **Wave Optics validation.** A focused profile provides read-only model preflight and a one-wavelength evidence audit for periodic metasurfaces.
- **Progressive discovery.** `capabilities` returns compact registry identity and counts instead of all 141 artifact-schema entries. The read-only `catalog` tool returns domain/tool summaries, one domain with full tool input schemas, one tool, or one named schema. The server holds the authoritative registry. Its fingerprint binds the compact summary and on-demand results. The separate `discovery.pagination_enabled` switch enables experimental `tools/list` cursor pages. It defaults to off, so first-page-only clients receive the complete ordinary list. When enabled, each page states the total. Clients must use that metadata and fetch subsequent pages to discover all tools.
- **Bounded offline manuals (default-off).** `manuals.root` identifies the original COMSOL PDF tree. After local index generation, `lexical_docs.enabled` adds SQLite FTS5/BM25 search and page retrieval. An isolated process returns bounded source/page citations.
- **Optional semantic retrieval.** The isolated `semantic_docs.enabled` feature works with every profile. Its baseline model failed the quality and memory promotion gates. Lexical manual search remains the recommended default.

## Shared project settings

The **Settings GUI is the preferred configuration path for users**. It edits one
shared `settings.json` used by Codex, opencode, Claude Code, and Hermes so that
agents do not silently receive different profiles, paths, Java runtimes, or
evidence rules. The checked-in [`settings.json`](settings.json) is the canonical
template and an advanced interface for developers, deployment automation, and
agents. The complete field meanings, defaults, and accepted values are in the
[settings guide](docs/setting_guide/README.md).
Missing entries use their documented safe defaults. An invalid entry keeps only
that entry at its default and is reported by `capabilities` and
`evidence_integrity_status`. Malformed JSON falls back to the complete safe default
document and reports the error. Do not create a second agent-owned settings file.

In a source checkout, the project-root file is writable. In an installed wheel,
the bundled file is a read-only template and persistent settings live at
`%LOCALAPPDATA%/comsol_mcp/settings.json` unless the one absolute locator below
selects another file:

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

The old individual `COMSOL_MCP_*`, `COMSOL_SEMANTIC_*`, and Java variables remain
one-release compatibility overrides, but they are intentionally absent from the
checked-in client examples. Change `settings.json`, restart the MCP host for
profile/shared-server/Java changes, then call `capabilities` and inspect the
`project_settings` status.

First-run defaults keep the settings file and Unicode-capable model-read root
under `%LOCALAPPDATA%/comsol_mcp`, while ASCII-only runtime and owned artifacts
live under `%PROGRAMDATA%/comsol_mcp`. Optional semantic assets remain unset.

On first installed use, `capabilities.project_settings` reports
`setup_required: true`, `configuration_source: bundled_template`, setup methods
`settings.start` and `agent_edit`, and that a restart is required. For an
ordinary user, call profile-independent `settings.start` once, tell the user
that startup-setting changes apply after restarting Codex or the owning MCP
client, stop further task output, and wait for the user's next message. Do not
edit JSON while the GUI is open. Use `agent_edit` only when the user explicitly
requests agent-managed or automated configuration. Edit only the resolved
writable file, validate it, and request the same restart.
The installed `comsol-mcp-settings` command opens the GUI directly. Repository
and source-distribution users can also run `./Open_Settings_GUI.ps1`. The script
discovers a supported standard-GIL Python 3.14/3.15 interpreter (preferring
3.14 when using the Python launcher), or accepts explicit
`-PythonPath` and `-SettingsPath` values. `-ValidateOnly` checks the manual
launcher without opening the GUI or starting COMSOL. The server can report
`agent_action_required: pause_for_user`. It cannot technically force arbitrary
third-party agents to comply.
The installed entry also accepts `--settings-path` and `--validate-only` and
works with MCP stdio stopped. Its About page can explicitly create or remove the
per-user `COMSOL MCP Settings.lnk`. Installation and ordinary GUI actions never
create that shortcut automatically. The shortcut targets the installed
`comsol-mcp-settings-gui` Windows GUI-subsystem entry, so it does not open a
terminal window. The console `comsol-mcp-settings` entry remains available for
validation and scripted actions.

## Profiles

Users select a profile in the Settings GUI before starting the server.
Developers and agents may set the equivalent `profile.name` field in
`settings.json`. A profile is fixed for the lifetime of that server process.
Restart after changing it.

| Profile | Intended use |
| --- | --- |
| `core` (default) | Compact, mature control plane: status, ownership, session/model inspection, and one-point solve/evaluation. |
| `basic_fem` | `core` plus typed conventional FEM construction, named selections, Pressure Acoustics and mathematical PDE interfaces, derived-geometry edits, bounded exports, and the Python-free standalone launcher tools. |
| `wave_optics` | Recommended for metasurfaces: `core` plus derived-geometry edits, material preview, locale-safe field discovery and bounded NPZ/manifest extraction, periodic-mesh audit/smoke, visual-review contracts, Wave Optics preflight, and point/reference audits. Durable staged jobs remain under `job_submit`. |
| `electro_chemistry` | Isolated Electrochemistry Module profile: `core` plus electrochemistry discovery, interface creation, electrode reaction, and electrolyte tools. Not merged into `full`. |
| `experimental` | Explicit opt-in generic creation, async, property escape hatches, and project helpers. |
| `full` | Broad compatibility/discovery surface containing every non-feature-gated tool except the isolated `electro_chemistry` additions. |
| `comsolless_read_only` | Computers without a COMSOL installation: offline model/evidence inspection tools only; nothing can start COMSOL or Java. |

Version 0.8 removes the research campaign, optimizer, robustness, spectral,
convergence, thermal-analysis, and robust-shape tools from discovery. There are
no compatibility wrappers. Use the accepted 0.7.8 package when a workflow still
requires those tools. Callers own analysis, candidate selection, aggregate
objectives, and scientific acceptance. Training and surrogate/ONNX tools remain
in MCP.

| Removed responsibility | Removed tools |
| --- | --- |
| Thermal analysis | `thermal_kirchhoff_assess`, `thermal_material_evaluate`, `thermal_material_validate`, `thermal_radiation_evaluate` |
| Spectral analysis | `spectral_characterize`, `spectral_model_compare` |
| Convergence and continuation | `convergence_evaluate`, `branch_continuation_plan` |
| Research strategy | `research_campaign_compile`, `research_robustness_plan`, `research_optimizer_advance` |
| Robust campaigns | `robust_shape_plan_preview`, `robust_shape_job_submit`, `robust_shape_evidence_inspect`, `robust_shape_evidence_verify` |

The retained `job_spec_preview` and `job_submit` tools accept a
`validation_matrix` point with the `native_condition_forward` or
`native_condition_gradient` collector. Their `inputs` use
`comsol_mcp.native_condition_forward_request` or
`comsol_mcp.native_condition_gradient_request`, respectively, at version `1.0.0`:
`support`, `shape_policy`, `adapter_configuration`, `condition_table`, and
`initial_values` bind one explicit condition and candidate. The point wavelength,
incidence, source hash, observable, and mesh bound must match that request.
The current native adapter is `lin2025_pedot_cylinder_v1`.
Forward collection retains condition-controls versions `1.0.0` through `1.5.0`.
Gradient collection requires the explicit sensitivity controls in `1.4.0` or `1.5.0`.
Forward collection does not run a gradient solve.

Native execution requires an explicit COMSOL version, startup RAM/disk policy,
and resource policy. Out-of-core execution also requires the caller's existing
absolute ASCII `comsol_temporary_directory`; the worker verifies Java's
`COMSOL_TMPDIR` readback. MCP retains source snapshots, sole solver ownership,
wall watchdogs, cancellation, recovery checks, and derived-model cleanup.
Both collectors persist forward observations, control readbacks, and cleanup
evidence. The gradient collector also persists raw complex gradients.
Their manifests report `native_unchecked` and
`UNVALIDATED`. Callers must perform independent gradient and scientific
validation before accepting those results.

Robust condition-controls schema `1.4.0` retains readable `1.3.0` mesh-reference
controls and additionally binds the native Sensitivity study features, regenerated
solver topology, direct-solver out-of-core policy, derivative solution/dataset,
adjoint method, and stationary nonlinearity. The licensed runtime replaces the
incompatible pre-existing solver sequence, verifies the generated identities,
and persists both raw complex `fsens` values and the accepted real components.
The same contract explicitly merges the generated Wave Optics and material-
coordinate segregated steps when their constraints cross at oblique incidence,
then reads back the single merged step and its caller-selected direct solver.
The caller-owned mesh reference remains validation-fixture evidence, never an
automatic coarsening rule or a substitute for independent finer-mesh finalist
convergence. Accepted gradients still require independent finite-difference and
directional reconciliation.

Robust condition-controls schema `1.5.0` adds the mandatory
`forward_shape_application` block so every licensed candidate is evaluated on a
mesh actually deformed by the candidate shape. A dedicated linear `Stationary`
deformation stage solving only the derived shape physics is placed before the
Wave Optics step, the regenerated sequence is scoped per step, and the solved
shape is read back per variable: the declared displacement component is
evaluated over the mesh vertices, the deformed radius is measured from the
maximum-component vertex, and it is compared with the requested radius
(relative tolerance declared by the caller) before any condition solve is
trusted. The gradient path removes the staged step and regenerates the coupled
sequence, so native adjoint receipts are unchanged. The `1.4.0` contract
remains readable.

Profiles only control the visibility of COMSOL automation/simulation tools and
future autonomous-exploration tools. Orthogonal functionality uses independent,
default-off Boolean feature gates that compose with every profile and with each
other:

| Feature (advanced JSON setting) | Added surface |
| --- | --- |
| `lexical_docs.enabled=true` | Two isolated SQLite FTS5/BM25 manual-search and exact-page tools. |
| `shared_server.enabled=true` | Protected shared Desktop/attached-Server workflow with explicit confirmation and exact process/listener/model identity. |
| `model_manager.enabled=true` | Five bounded COMSOL Model Manager tools (status, open, run, download, upload). Default off, combinable with any profile, and independent of `shared_server.enabled`; the separate `model_manager.upload_enabled` setting additionally gates uploading caller-designated derived copies, while open, run, and download never depend on it and overwrite or revision conflicts are still refused. |
| `semantic_docs.enabled=true` | Three isolated experimental vector-assisted manual-retrieval tools. |

Call `capabilities` to discover the active profile, exact registered tools, target versions, disabled groups, and restart requirements without starting COMSOL. The bounded `deployment_identity` reports the loading source and frozen profile/schema and catalog hashes. After restart, compare these values to detect same-version stale installs or source shadowing. The response does not expose local paths.

Enable shared Desktop/attached-Server work in the Settings GUI Profile tab. It
is a default-off feature independent of the selected profile. The advanced JSON
equivalent is `shared_server.enabled=true`.
The user must start COMSOL Server manually, connect Desktop to it, confirm
the endpoint, and explicitly confirm each attach. The legacy `comsol_connect`
tool remains an experimental compatibility surface and is not a substitute for
the protected shared-session lifecycle.

Control-plane responses from capabilities, solver ownership, durable jobs, and lexical manuals include a compact rolling `control_plane` block. It retains at most 256 samples per operation and reports success/busy/timeout/error counts plus p50/p95/max latency. Full logs and unbounded telemetry are never returned inline.

### Standalone Windows launcher

The standalone route targets Windows 10/11 x64 and the COMSOL 6.4 release
line. Licensed acceptance is currently exact for COMSOL 6.4.0.293. It does not
support Windows Server, pre-6.4 COMSOL, Linux, or macOS. The generated EXE takes
one runtime value: `--comsol-path <COMSOL-root>`. It uses that installation's
`comsolcompile.exe`, `comsolbatch.exe`, bundled Java, solver, and license.

Building uses the 64-bit C# compiler from the `.NET Framework 4.x` component
supplied and serviced with supported Windows 10/11 workstations:
`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`. It does not require a
separate modern `.NET Runtime`, `.NET Desktop Runtime`, `.NET SDK`, Visual
Studio, package download, or network access. If that inbox compiler is absent,
`standalone_build` fails without installing or downloading a replacement.
Microsoft's [.NET Framework platform table](https://learn.microsoft.com/en-us/dotnet/framework/install/guide-for-developers)
documents the inbox 4.x versions supplied with Windows 10 and Windows 11.

Select the `basic_fem` profile in the normal Python MCP host to expose
`standalone_build`, `standalone_start`, `standalone_status`,
`standalone_pause`, `standalone_resume`, `standalone_tail`, and
`standalone_results`. The build and campaign directories must remain under the
configured owned ASCII artifact root. The MCP host verifies the packaged source
identity, build manifest, and EXE hash before execution. Once generated, the
EXE can also be copied and operated directly without that Python MCP host.
`standalone_start` and `standalone_resume` use an explicit `comsol_root` when
provided. Otherwise they use `comsol.installation_root` from shared settings.

## Recommended workflows

### General solver work

1. Call `solver_status`.
2. Call `solver_preflight` before connecting, starting COMSOL, or submitting substantial work.
3. Use the session/model tools or submit a durable staged sweep.

The server fails closed when an external MPh/COMSOL owner or a valid lease is present. `solver_recover_stale_lease` only removes a lease that process identity evidence proves stale. It never terminates an unowned process.

### Parallel-plate capacitor end-to-end example

The source checkout includes a three-dimensional dielectric capacitor recipe.
It identifies the two electrode faces from their measured centers and normals,
applies Ground and Electric Potential conditions, and uses a stationary
Electrostatics study. Build-only is the default. Add `--solve` to compare the
energy-derived capacitance with `epsilon_0 * epsilon_r * area / separation`.

```powershell
python -m recipes.parallel_plate_capacitor `
  --output-model D:\comsol_outputs\parallel_plate_capacitor.mph `
  --solve
```

The recipe shares its model and validation functions with the licensed e2e
probe. It performs solver-ownership preflight and publishes the final model only
after the COMSOL client and lease are released. Existing output is preserved
unless `--overwrite-output` is explicit.

### Minimal Pressure Acoustics example

The source checkout includes a physical two-dimensional air-duct recipe. It
uses rigid sidewalls, a one-pascal inlet, and a zero-pressure outlet at a
frequency below the first transverse cutoff. Build-only is the default. Add
`--solve` to compare the numerical center pressure with the analytical
one-dimensional Helmholtz solution before the receipt is marked verified.

```powershell
python -m recipes.acoustic_duct_2d `
  --output-model D:\comsol_outputs\minimal_acoustic_duct.mph `
  --solve
```

The recipe performs a fresh solver-ownership preflight, requires four distinct
named side selections, saves to a unique staging model, clears the COMSOL client
and lease, and only then publishes the final `.mph` file. It never overwrites an
existing output unless `--overwrite-output` is explicit.

Durable sweep controls are `job_submit`, `job_status`, `job_tail`, `job_cancel`, and `job_resume`. Each job has its own ASCII-only runtime directory containing its immutable specification, state, CSV journal, checkpoint, and log. Resume accepts only matching, finite, successful rows. Cancellation reaches a terminal cancelled state only after worker/process cleanup and lease release are verified. This coordination is for a shared runtime directory on one host. It is not distributed execution.

An owned staged sweep reports `completed` only after its client and lease cleanup succeeds.
A cleanup failure preserves result rows and reports `failed`, `cleanup_pending`, and `owned_cleanup` details.

The interactive `mesh_convergence_study` helper also writes a manifest beside
its CSV. Any resume or append to an existing journal requires
`source_model_path`. The source hash, study, expressions, parameter setting,
mesh identity, and each level's exact property map must match before a completed
level can be skipped. CSV and checkpoint failures are terminal persistence
outcomes for that level and never trigger another solve.

Three solver-free preview tools make configuration review explicit before any
COMSOL admission or mutation. `simulation_configuration_validate` accepts a
closed typed contract for source/producer identity, geometry semantics, ordered
layers, material state and loss sign, incidence and polarization, mesh keys,
model-tree identities, solver termination, units, and artifact chains. It
normalizes supported units and returns a content-bound fingerprint.
`simulation_configuration_diff` classifies leaves as exact, within tolerance,
semantic, label-only, or unavailable. Labels never become physical identity.
`job_spec_preview` reuses the exact discriminated input validator used by
`job_submit` and reports its bounded point/stage inventory, path/resource
checks, requirements, and declared submit-time side effects without admission,
ownership acquisition, filesystem writes, process creation, or solver startup.

Version 0.8.0 removes the standalone thermal-radiation and thermal-material analysis tools.
Use version 0.7.8 in a separate environment if these tools are required.
COMSOL material operations and native thermal-to-optical execution remain available.

A licensed durable thermal-to-optical replay uses
`job_type: "thermo_optomechanical_replay"` plus an ASCII JSON
`specification_path` and exact `specification_sha256`. The bounded manifest is
validated against the complete closed contract before submission. This keeps
core MCP discovery bounded without accepting free-form configuration. It binds an
immutable source, one validated thermal material state, exact Heat Transfer,
Solid Mechanics, Moving Mesh, Wave Optics, study, selection, parameter, mesh,
and expression tags, and a caller-supplied resource and acceptance policy. Five
hash-chained stages persist preflight, thermal/structural solve, state evidence,
spatial-frame deformation transfer, and exact optical replay. Resume verifies
orphaned evidence before appending its missing row and never repeats a completed
stage. Acceptance keeps raw R/T/A, temperature, stress, displacement, energy,
mesh, wavelength, zero-CTE, zero-temperature-rise, rollback, source-integrity,
and cleanup evidence separate from execution completion. The minimal supported
route forbids topology change and requires a licensed COMSOL 6.4 synthetic
fixture gate before use with research models.

Set `model_contract.expressions.transfer_displacement_max` to measure transferred mesh displacement in the transfer study dataset.
If omitted, the transfer stage uses the declared `displacement_max` expression in that same dataset.
An unavailable expression fails the stage. The executor does not substitute a prior structural result.

### Wave Optics metasurfaces

Use the `wave_optics` profile and follow this bounded sequence:

```text
solver_status -> wave_optics_preflight -> wave_optics_reference_audit (optional) -> wave_optics_point_audit
```

`wave_optics_preflight` is read-only and solver-free. It reports source provenance, topology, periodic/Floquet selections, ports, wavelength linkage, mesh/study metadata, and explicit unknowns.

`wave_optics_point_audit` solves exactly one declared wavelength after ownership and source-hash checks. It writes a running manifest, one fsync'd CSV row, and a final manifest. Raw evidence can include requested/evaluated wavelength, frequency linkage, caller-provenanced R/T/A and flux direction, closure, loss expressions, bounded top-air field statistics, mesh state, and source/config/policy hashes.

`wave_optics_reference_audit` is an experimental reference-power tool. It creates a fresh provenance-tracked clone and requires exact caller material/domain declarations. It replaces clone component materials with lossless air and samples a bounded homogeneous region. Method evidence cannot pass until the tool removes the clone. It never mutates the source model. Licensed acceptance is version/model-specific.

Without a caller-supplied versioned validation policy, an audit is evidence-only: it does not declare a model pass/fail or recommend a long sweep. S/P labels and structure total fields stay explicitly qualified until an incident-reference artifact supports a stronger claim.

## Manual retrieval

`manual_search` and `manual_read_pages` are the production documentation path. They use an offline SQLite FTS5/BM25 index, bounded worker processes, and compact source/page citations. The MCP control process does not import Torch or SentenceTransformer for this path. They are independently default-off because a COMSOL installation may omit local manuals.

The Settings GUI Docs tab separates manual keyword search from semantic search. Choose a raw PDF root and an ASCII-only SQLite destination, then use **Generate Index**. The background dialog reports stage, current PDF, page counts, and percentage. It builds and validates a sibling temporary database, atomically publishes only a valid result, and preserves the previous index on cancellation or failure. Enable manual search only after a valid index exists.

`semantic_docs.enabled` is opt-in and isolated from COMSOL control. The vector retrieval worker is an English diagnostic baseline, not a multilingual or production-quality result. The frozen benchmark improved exact-match recall but reduced paraphrase and multi-concept recall. It returned no direct-Chinese matches and failed negative-query abstention. Memory use grew substantially during soak tests. The baseline model and index assets were removed. A replacement requires a complete benchmark gate before deployment. Use lexical manual search for normal work.

## ClientAPI compatibility notes

The fork repairs the clientapi paths exercised by its test and real-COMSOL checks, including:

- `tags()` iteration and `feature().size()` instead of direct-model indexing and `len()`.
- `physics().create(tag, type, sdim_string)` for interfaces, while child features take an integer entity dimension.
- ClientAPI mesh inspection through `getNumElem()` and `getNumVertex()`, and explicit mesh-sequence creation.
- Full study-type names and `model.java.study('std1').run()`.
- JSON-safe normalization of Java strings, localized labels, real/NumPy/complex values, and model metadata.
- `model.java.save(full_path)` for Unicode-safe `.mph` saves and clientapi-safe clone cleanup.
- Correct component-owned material and multiphysics construction.

The electrostatics helper can create `ChargeConservation` and a material node. In COMSOL 6.3+/6.4, the default `fsp1` FreeSpace domain feature otherwise uses vacuum permittivity instead of material relative permittivity.

For a wavelength sweep, `record_wavelength_controls` records the requested value, evaluated `wl`, and `c_const/ewfd.freq`. It does not compare them. Configure the study step explicitly when the study does not follow the model parameter. Check all three values with a declared tolerance before accepting a spectrum. A job marked `completed` or a row marked `ok` does not establish wavelength agreement.

## Verification

The source dependency/process-only gate, optimized-Python production guard,
compileall, hash-locked isolated non-editable wheel/install gate, licensed
attached sweep/cancellation/recovery, PID-reuse rejection, and detach-preservation
receipt are maintained as release checks. Unit tests are side-effect-free:
collection does not start COMSOL, and integration probes run only when explicitly
requested in fresh, sequential subprocesses with exact process-tree cleanup.
Repository-only tests, release fixtures, gates, and provenance are documented in
`development_kit/README.md`. Ordinary wheel/sdist artifacts exclude that directory.

```bash
python -m pytest -q
python -m pytest -q -m integration development_kit/tests/integration
```

Real COMSOL checks cover localized JSON transport, circle/union geometry, DXF import, parametric sweep properties, multiphysics coupling, clone cleanup, and Unicode-path saving. They also cover solver ownership, durable interruption/restart/resume/cancellation, profile discovery, Wave Optics preflight, one-point audit, and bounded manual retrieval.

The Python 3.14 licensed parallel-plate regression returns **1.8593794419540677 pF**, versus the theoretical **1.8593794406880002 pF**, on COMSOL **6.4.0.293**.

The following spectral, convergence, and continuation receipts describe historical 0.7.8 capabilities.
They do not establish 0.8.0 acceptance or restore removed tools.

Licensed adaptive spectral acceptance on the same COMSOL build used a neutral
air-dielectric-air periodic-port slab with 4,798 elements and 1,039 vertices.
The accepted 10-row spectrum found its interpolated own peak at
**5.200823291715346 um**, with **T = 0.9999455828498357**,
**FWHM = 0.4807802607560452 um**, and **Q = 10.817464268472365**.
Its raw rows span **R = 0.000428181826928114 to 0.506857218704363**,
**T = 0.493142781295616 to 0.999571818173077**, and
**max |A| = 2.985136902408465e-17**. Maximum power-closure error is
**2.103241887902518e-14** and maximum wavelength-sync error is zero. A separate
9-row boundary control expanded its declared window and completed normally as
`unresolved_at_declared_cap`. Its raw ranges are
**R = 0.113752050554409 to 0.697262752330585**,
**T = 0.302737247669409 to 0.886247949445593**, and
**max |A| = 1.695203805977834e-17**, with maximum closure error
**2.903982508976606e-14** and zero wavelength-sync error. Both runs preserved
the source SHA-256 and released the solver lease and client.

Licensed convergence acceptance used three neutral periodic-port slab meshes:
**2,386/560**, **4,798/1,039**, and **13,904/2,752** elements/vertices. Their
own peaks were **5.200438265718366**, **5.200823291715278**, and
**5.200959692754783 um**. Fitted peak T values were
**0.9999455861474655**, **0.9999455828498416**, and
**0.9999455989864663**. The governing medium-to-fine peak shift was
**0.1364010395043668 nm**. Across 30 raw rows,
**R = 0.000426677111557779 to 0.506857218704365**,
**T = 0.493142781295614 to 0.999573322888467**, and
**max |A| = 4.526776969362989e-17**. Maximum closure error was
**2.48772546066357e-14** and wavelength-sync error was zero. A separate campaign
with a declared **0.001 nm** peak-shift tolerance completed all three levels as
`residual` while its amplitude check passed. Both campaigns preserved all source
hashes and left no client, process, or lease residue.

Licensed branch-continuation acceptance used two immutable 4,798-element,
1,039-vertex neutral slab models with exact periodic-parent and two-port
incidence readback at **0 and 10 degrees**. Their own peaks were
**5.200823291715293** and **5.195931563688228 um**, a measured shift of
**4.891728027065 nm** within the caller's guard window. Peak T values were
**0.9999455828498354** and **0.9999448178081717**. FWHM values were
**0.4807802607560502** and **0.4836621446746728 um**. Q values were
**10.817464268472143** and **10.742894851080779**. Across 20 raw rows,
**R = 0.000422180032088570 to 0.512242443246136**,
**T = 0.487757556753878 to 0.999577819967919**, and
**max |A| = 3.015050383793419e-17**. Maximum closure error was
**1.33935578502046e-14** and wavelength-sync error was zero. A separate 18-row
boundary control consumed its one declared expansion and completed as
`unresolved_at_declared_cap` with no additional request and no branch-
disappearance claim. Both campaigns preserved the source hashes and left no
client, process, or lease residue.

## Requirements and installation

- COMSOL Multiphysics 6.4. Licensed acceptance is pinned to build 6.4.0.293
- Python 3.14 (standard GIL-enabled build, not the Windows Store build)
- MPh `>=1.3.1,<1.5`, MCP SDK `>=2.2.0,<2.3`, `pydantic`, and `psutil>=5.9.0`
- COMSOL's Java 21 runtime in the verified configuration

Python 3.14 remains the primary build, release and production lane. Standard
Windows x64 Python 3.15 is a formal supported lane (caller decision,
2026-10-26/30): its CI lane runs the full solver-free suite, package/install
and stdio gates, currently pinned to 3.15.2 (final). It builds
JPype 1.7.1, PyYAML 6.0.3 and the exact Pydantic 2.13.5/core 2.46.5 pair
from source where cp315 wheels are unavailable. MSVC/Windows SDK, a JDK with
Ant, and Rust are required for those builds. This lane excludes the
experimental `semantic-docs` extra and does not establish licensed COMSOL
compatibility or free-threaded Python support.

This project is under active development, so dependency ranges and locked
versions may change without a long deprecation window. Before updating an
existing deployment, validate Python, COMSOL/MPh/JPype, and any optional extras
in an isolated environment, then replace the active installation and restart
the MCP host only after those checks pass.

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_4_Calibrated
python -m pip install .

# Preferred user configuration after installation:
comsol-mcp-settings

# Recommended offline manual index; use an ASCII-only output path.
python -m pip install ".[manuals]"
python -m comsol_mcp.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
```

For optional isolated semantic retrieval (sentence-transformers, not ChromaDB):

```powershell
python -m pip install ".[semantic-docs]"
# Prefer the Settings GUI Docs tab. For developer/agent JSON automation:
#   manuals.root = "D:/COMSOL64/Multiphysics/doc"
#   lexical_docs.enabled = true
#   lexical_docs.index_path = "D:/comsol_docs_fts/manuals.sqlite3"
#   semantic_docs.enabled = true
#   semantic_docs.root = "D:/comsol_semantic"
#   semantic_docs.model_path = "D:/comsol_semantic/models/<model>/<revision>"
```

On Windows accounts whose user path contains non-ASCII characters, avoid editable installs. Run `python -m pip install . --no-deps` after source changes, then restart the MCP host. The server does not hot-reload `comsol_mcp/tools/`.

Configure an MCP client, for example:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["D:\\path\\to\\python-env\\Scripts\\comsol-mcp.exe"],
      "environment": {
        "COMSOL_MCP_SETTINGS_PATH": "D:\\path\\to\\COMSOL_Multiphysics_MCP\\settings.json"
      }
    }
  }
}
```

Select `core` in the Settings GUI for the compact default. Developers and
agents can set the equivalent `profile.name` value in JSON. Client examples are available at
`config/claude-code-mcp.example.json`, `config/codex-mcp.example.toml`,
`config/hermes-mcp.example.yaml`, and `config/opencode-mcp.example.json`.

## How this fork differs from upstream

This is a COMSOL 6.4.0.293 standalone/clientapi compatibility and reliability fork, not a general replacement for upstream. Other COMSOL builds remain unknown until independently accepted. The fork keeps the upstream project's foundation while making a deliberately narrower, safer execution surface for agent-driven COMSOL work.

| Area | Upstream orientation | This fork |
| --- | --- | --- |
| COMSOL API target | Direct `com.comsol.model.Model` API assumptions. | MPh 1.3.1 standalone `model.java` clientapi wrappers, including their different overloads, tags, lists, and Java-string transport. |
| Tool surface | Broad feature discovery by default. | Compact `core` default. Larger construction and compatibility surfaces require an explicit profile. |
| Solver concurrency | No same-host ownership protocol. | Process-aware lease, external-client detection, status, preflight, and stale-lease recovery that never kills an unowned process. |
| Long runs | Interactive/current-process workflows. | Detached sweeps and validation matrices with immutable specifications, durable evidence rows, validated resume, and verified cancellation cleanup. |
| Wave Optics | General tools only. | A dedicated preflight plus one-point evidence audit for periodic metasurfaces, with raw evidence separated from caller policy. |
| Manual search | No bounded manual retrieval. | Bounded isolated lexical manual retrieval is the production default. Experimental semantic retrieval is isolated and explicitly not promoted. The legacy in-process ChromaDB path has been removed. |
| Windows paths | No special guarantee for Unicode save paths. | Clientapi Java save path for Unicode `.mph` saves. ASCII-only runtime/index roots for native and durable artifacts. |

Use this fork when the upstream server fails under MPh standalone with errors such as `No matching overloads`, `Operation_cannot_be_created_in_this_context`, or client-list indexing errors. Use `full` only when compatibility with a broad legacy surface is genuinely required.

## License

This repository is distributed under the [MIT License](LICENSE). COMSOL,
licensed manuals, third-party models, papers, and datasets are not relicensed by
this repository. Version 0.8.0 removes the external `mmapy` execution path.
The historical optional wheel uses GPL-3.0-or-later and is not distributed with this package.
We acknowledge Krister Svanberg for MMA/GCMMA and Arjen Deetman for the Python implementation.

## Publication and citation

The paper below describes the upstream COMSOL-MCP project, not this maintained
fork. It is listed here for attribution and source acknowledgement:

> Naiyin Zhang and Junchao Wang, “COMSOL-MCP: An open-source model context
> protocol interface for AI-assisted multiphysics simulation,” *Neurocomputing*,
> vol. 703, article 134481, 2026.

Publisher record: [ScienceDirect](https://linkinghub.elsevier.com/retrieve/pii/S0925231226018795)

```bibtex
@article{Zhang2026COMSOLMCP,
  title   = {COMSOL-MCP: An Open-Source Model Context Protocol Interface for AI-Assisted Multiphysics Simulation},
  author  = {Zhang, Naiyin and Wang, Junchao},
  journal = {Neurocomputing},
  volume  = {703},
  pages   = {134481},
  year    = {2026},
  doi     = {10.1016/j.neucom.2026.134481},
  url     = {https://doi.org/10.1016/j.neucom.2026.134481}
}
```

The paper's method, architecture, capabilities, and evaluation refer to the
upstream project. For this fork, cite the repository through GitHub's **Cite
this repository** metadata in [`CITATION.cff`](CITATION.cff), and identify the
exact fork release or commit, repository URL, and access date for reproducibility.

We thank Naiyin Zhang and Junchao Wang for originating COMSOL-MCP, publishing
the associated paper, and contributing the upstream citation guidance added in
[commit `99172f8`](https://github.com/wjc9011/COMSOL_Multiphysics_MCP/commit/99172f8f43c6753c2442c406cd5c6055ea8c5bef), which this maintained fork has
adapted to its own repository and release-evidence boundary.
