"""Adapter protocol contract and MPh 1.3.1/1.4 parity tests (S4A).

Every test here is solver-free: no COMSOL, no JVM, no lease, no ``mph`` import.
The fake backend stands in for a real lane so the protocol's rules, error
translation, and rollback semantics are exercised as behaviour.

The parity tests compare two backends that differ **only** in declared lane and
in the two capabilities measured in the lane-reconnaissance notes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp.adapter import (
    ADAPTER_ERROR_CODES,
    DEFAULT_MPH_LANE,
    DNN_FEATURE_METHODS,
    JAVA_WRITE_KINDS,
    OPERATIONS,
    REFERENCE_MPH_LANE,
    SUPPORTED_MPH_LANES,
    AdapterError,
    ComsolAdapter,
    ConvertedValue,
    EvaluationRequest,
    JavaTypedWrite,
    PropertyWritePlan,
    ResolvedNode,
    SessionIdentity,
    SessionRequest,
    available_lanes,
    convert_explicitly,
    describe_protocol,
    installed_mph_version,
    lane_is_supported,
    make_backend,
    matrix_read_row_limit,
    normalize_evaluation_result,
    operation_is_known,
    unwrap_backend_value,
)
from comsol_mcp.adapter.fake_backend import FakeComsolBackend
from comsol_mcp.adapter.mph14_backend import (
    MPH_1_4_LANE,
    REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT,
    REFERENCE_MATRIX_READ_REFUSAL,
    Mph14Backend,
    lane_capabilities,
)
from comsol_mcp.adapter.mph_backend import MphBackendBase, MphReferenceBackend

ROOT = Path(__file__).parents[2]


def _opened(**kwargs) -> FakeComsolBackend:
    """A fake backend with an open session and a loaded model."""
    backend = FakeComsolBackend(**kwargs)
    backend.open_session(SessionRequest(cores=1))
    backend.load_model("fake.mph")
    return backend


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_protocol_declares_every_operation_and_error_code() -> None:
    described = describe_protocol()
    assert described["schema_name"] == "comsol_mcp.adapter.operation"
    assert described["schema_version"] == "1.0.0"
    assert described["reference_lane"] == REFERENCE_MPH_LANE
    assert described["supported_lanes"] == list(SUPPORTED_MPH_LANES)
    # All six plan steps must be representable.
    for step in (
        "session_open",
        "session_close",
        "session_identity",
        "model_identity",
        "node_lookup",
        "convert_value",
        "property_read",
        "property_write",
        "model_load",
        "model_save",
        "model_evaluate",
        "dataset_access",
        "solution_access",
    ):
        assert operation_is_known(step), step
    assert operation_is_known("nonsense") is False
    assert len(OPERATIONS) == len(set(OPERATIONS))


def test_the_declared_range_is_the_two_reviewed_mph_lanes() -> None:
    """The range is the retained 1.3.x line plus the supported 1.4.x line.

    1.4.x is a formally supported lane as of this release, but it is still not
    the default: the reviewed minimum stays the default so a newer line cannot
    become the implicit production path.
    """
    assert REFERENCE_MPH_LANE == "1.3.1"
    assert SUPPORTED_MPH_LANES == ("1.3.1", "1.3.2", "1.4.0")
    assert DEFAULT_MPH_LANE == REFERENCE_MPH_LANE
    assert lane_is_supported("1.3.1") is True
    assert lane_is_supported("1.3.2") is True
    assert lane_is_supported(MPH_1_4_LANE) is True
    assert lane_is_supported("1.2.9") is False
    # Two distinct notions must not be conflated. `available_lanes` names
    # *backend classes*: the reference backend plus the 1.4 backend, because only
    # 1.4 changes method bodies. `lane_is_supported` names the declared
    # *installed-version* range, which additionally admits 1.3.2 because the
    # reference backend keys its capability on the observed installed version.
    assert set(available_lanes()) == {REFERENCE_MPH_LANE, MPH_1_4_LANE}
    assert make_backend(lane=MPH_1_4_LANE).lane == MPH_1_4_LANE
    assert available_lanes().count(REFERENCE_MPH_LANE) == 1
    # The default selection must resolve to the reviewed minimum, not to 1.4.
    assert make_backend().lane == REFERENCE_MPH_LANE
    # A 1.3.2 install is served by the reference backend and reports its own lane.
    assert MphReferenceBackend().observed_lane() == installed_mph_version()


def test_an_unknown_lane_is_refused_with_a_stable_code() -> None:
    with pytest.raises(AdapterError) as excinfo:
        make_backend(lane="9.9.9")
    assert excinfo.value.reason_code == "adapter_unavailable"
    assert excinfo.value.operation == "session_open"


def test_unknown_reason_codes_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="unknown adapter reason code"):
        AdapterError("not_a_real_code", "x")
    assert AdapterError("node_not_found", "x").as_dict()["reason_code"] == "node_not_found"
    assert "adapter_unavailable" in ADAPTER_ERROR_CODES


def test_both_real_backends_satisfy_the_protocol() -> None:
    assert isinstance(MphReferenceBackend(), ComsolAdapter)
    assert MphReferenceBackend().lane == "1.3.1"


def test_importing_the_adapter_does_not_import_mph_or_start_a_jvm() -> None:
    """The seam must never make every consumer a potential solver starter."""
    probe = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "import comsol_mcp.adapter as a;"
        "a.available_lanes();"
        "a.describe_protocol();"
        "print('mph' in sys.modules, 'jpype' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env={"PYTHONPATH": "", "PATH": ""},
    )
    assert completed.stdout.strip() == "False False", completed.stdout


# ---------------------------------------------------------------------------
# Explicit conversion (step 3) — the measured matrix difference lives here
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "form", "expected"),
    [
        (3, "int", 3),
        (2.5, "float", 2.5),
        ("a", "str", "a"),
        (True, "bool", True),
        ([[1, 2], [3, 4]], "int_matrix", [[1, 2], [3, 4]]),
        ([[1.5], [2.5]], "float_matrix", [[1.5], [2.5]]),
        ([["x", "y"]], "str_matrix", [["x", "y"]]),
        ([[True, False]], "bool_matrix", [[True, False]]),
    ],
)
def test_explicit_conversion_accepts_representable_values(value, form, expected) -> None:
    converted = convert_explicitly(value, form=form)
    assert converted.value == expected
    assert converted.form == form
    assert converted.source_type == type(value).__name__


@pytest.mark.parametrize(
    ("value", "form"),
    [
        (True, "int"),  # a bool must not silently become 1
        ("3", "int"),  # no string-to-number coercion
        (None, "float"),
        (float("nan"), "float"),
        (float("inf"), "float"),
        (1, "str"),
        ("x", "bool"),
        ([[1, 2], [3]], "int_matrix"),  # ragged
        ([], "int_matrix"),  # empty
    ],
)
def test_explicit_conversion_refuses_implicit_coercion(value, form) -> None:
    with pytest.raises(AdapterError) as excinfo:
        convert_explicitly(value, form=form)
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_a_jvm_string_proxy_is_unwrapped_for_the_backend() -> None:
    """Licensed defect: COMSOL's `getString` returns a JPype proxy, not a `str`.

    Measured on COMSOL 6.4.0.293: the study step's `plist` read back as
    `jpype._jstring` / `java.lang.String`, so `isinstance(value, str)` was False
    and a strict conversion reported a spurious `property_type_mismatch` (see
    the Java proxy-type probe). Only backend-produced values are
    unwrapped; caller input stays strictly checked.
    """

    class JavaString:
        """Stands in for `jpype._jstring` without importing jpype."""

        __module__ = "jpype._jstring"

        def __init__(self, text: str) -> None:
            self._text = text

        def __str__(self) -> str:
            return self._text

    # Name the class exactly as JPype does so the unwrapper's type check matches.
    JavaString.__name__ = "java.lang.String"
    JavaString.__qualname__ = "java.lang.String"

    proxy = JavaString("")
    assert not isinstance(proxy, str)
    assert unwrap_backend_value(proxy) == ""
    assert isinstance(unwrap_backend_value(proxy), str)
    # The unwrapped value then satisfies a strict conversion.
    assert convert_explicitly(unwrap_backend_value(proxy), form="str").value == ""


def test_a_jvm_numeric_proxy_is_unwrapped() -> None:
    class JavaDouble:
        __module__ = "jpype._jdouble"

        def __init__(self, value: float) -> None:
            self._value = value

        def __float__(self) -> float:
            return self._value

    JavaDouble.__name__ = "java.lang.Double"
    JavaDouble.__qualname__ = "java.lang.Double"

    proxy = JavaDouble(2.5)
    unwrapped = unwrap_backend_value(proxy)
    assert unwrapped == 2.5
    assert convert_explicitly(unwrapped, form="float").value == 2.5


def test_unwrapping_does_not_weaken_caller_input_checks() -> None:
    """A plain string must still be refused for a numeric form."""
    assert unwrap_backend_value("3") == "3"
    with pytest.raises(AdapterError) as excinfo:
        convert_explicitly(unwrap_backend_value("3"), form="int")
    assert excinfo.value.reason_code == "conversion_not_representable"
    # And a Python bool must still not become an int.
    with pytest.raises(AdapterError):
        convert_explicitly(unwrap_backend_value(True), form="int")


def test_an_unrecognized_value_passes_through_unchanged() -> None:
    sentinel = object()
    assert unwrap_backend_value(sentinel) is sentinel
    assert unwrap_backend_value(7) == 7
    assert unwrap_backend_value(None) is None


def test_an_unsupported_form_is_refused() -> None:
    with pytest.raises(AdapterError, match="unsupported conversion form"):
        convert_explicitly(1, form="complex_matrix")


def test_integer_to_float_conversion_requires_exact_float64_representation() -> None:
    with pytest.raises(AdapterError, match="exactly representable"):
        convert_explicitly(2**53 + 1, form="float")


def test_integer_to_float_overflow_is_a_stable_adapter_error() -> None:
    with pytest.raises(AdapterError, match="finite float"):
        convert_explicitly(10**1000, form="float")


def test_real_backend_session_identity_uses_public_client_version() -> None:
    class Client:
        version = "6.4"
        host = "localhost"
        port = 2036
        standalone = False

    backend = MphBackendBase()
    backend._client = Client()
    assert backend.session_identity().comsol_version == "6.4"


@pytest.mark.parametrize(
    ("form", "accessor", "value"),
    [("float", "getDouble", 1.5), ("int", "getInt", 3), ("bool", "getBoolean", True)],
)
def test_real_backend_typed_read_uses_declared_accessor(monkeypatch, form, accessor, value):
    from comsol_mcp.adapter.protocol import NodeRef

    class Java:
        def getString(self, _name):
            raise AssertionError("numeric property must not use getString")

    java = Java()
    calls = []
    setattr(java, accessor, lambda name: calls.append(name) or value)
    backend = MphBackendBase()
    monkeypatch.setattr(backend, "_java_node", lambda _path: java)
    node = NodeRef(tag="comp1", path=("comp1",), node_type=None)
    assert backend.read_property(node, "value", form=form).value == value
    assert calls == ["value"]


@pytest.mark.parametrize(
    "form,accessor,proxy_name,value",
    [
        ("float_matrix", "getDoubleMatrix", "java.lang.Double", 1.5),
        ("int_matrix", "getIntMatrix", "java.lang.Integer", 3),
        ("bool_matrix", "getBooleanMatrix", "java.lang.Boolean", True),
        ("str_matrix", "getStringMatrix", "java.lang.String", "x"),
    ],
)
def test_real_backend_matrix_read_unwraps_cells_without_flattening(
    monkeypatch, form, accessor, proxy_name, value
):
    from comsol_mcp.adapter.protocol import NodeRef

    class Cell:
        def __float__(self):
            return float(value)

        def __int__(self):
            return int(value)

        def __bool__(self):
            return bool(value)

        def __str__(self):
            return str(value)

    Cell.__name__ = proxy_name
    java = type("Java", (), {})()
    setattr(java, accessor, lambda _name: [[Cell(), Cell()], [Cell(), Cell()]])
    backend = MphBackendBase()
    monkeypatch.setattr(backend, "_java_node", lambda _path: java)
    node = NodeRef(tag="comp1", path=("comp1",), node_type=None)
    assert backend.read_property(node, "matrix", form=form).value == [
        [value, value],
        [value, value],
    ]


def test_an_oversized_matrix_is_refused_rather_than_materialized() -> None:
    from comsol_mcp.adapter.conversion import MAX_MATRIX_ELEMENTS

    side = int(MAX_MATRIX_ELEMENTS**0.5) + 2
    with pytest.raises(AdapterError, match="bounded conversion limit"):
        convert_explicitly([[1] * side for _ in range(side)], form="int_matrix")


def test_conversion_records_the_source_type_so_parity_can_be_checked() -> None:
    """Two lanes agreeing on a number but not on a type must be distinguishable."""
    from_int = convert_explicitly(1, form="float")
    from_float = convert_explicitly(1.0, form="float")
    assert from_int.value == from_float.value
    assert from_int.source_type != from_float.source_type


# ---------------------------------------------------------------------------
# Session and model identity (step 1)
# ---------------------------------------------------------------------------


def test_session_must_be_opened_before_use_and_closed_once() -> None:
    backend = FakeComsolBackend()
    with pytest.raises(AdapterError) as excinfo:
        backend.load_model("x.mph")
    assert excinfo.value.reason_code == "session_not_open"
    backend.open_session(SessionRequest(cores=1))
    with pytest.raises(AdapterError) as excinfo:
        backend.open_session(SessionRequest(cores=1))
    assert excinfo.value.reason_code == "session_already_open"
    backend.close_session()
    assert backend.session_identity() is None
    # Closing twice is a no-op, not a failure.
    backend.close_session()


def test_caller_declared_resources_are_passed_through_unchanged() -> None:
    """The backend must not substitute its own cores/version."""
    backend = FakeComsolBackend()
    backend.open_session(SessionRequest(cores=3, version="6.4", host="h", port=2036))
    recorded = dict(backend.calls)["session_open"]
    assert recorded == {"cores": 3, "version": "6.4", "host": "h", "port": 2036}


def test_an_unprovable_model_hash_is_reported_unknown() -> None:
    backend = _opened()
    identity = backend.load_model("x.mph")
    assert identity.content_sha256 is None
    assert identity.file == "x.mph"


# ---------------------------------------------------------------------------
# Node lookup (step 2)
# ---------------------------------------------------------------------------


def test_node_lookup_resolves_and_reports_a_missing_tag() -> None:
    backend = _opened(properties={"comp1": {"L": 0.1}})
    node = backend.find_node(["component", "comp1"])
    assert node.tag == "comp1"
    assert node.path == ("component", "comp1")
    assert list(backend.children(node)) == []
    with pytest.raises(AdapterError) as excinfo:
        backend.find_node(["component", "absent"])
    assert excinfo.value.reason_code == "node_not_found"
    with pytest.raises(AdapterError):
        backend.find_node([])


def test_fake_children_follow_created_nodes_and_removal() -> None:
    backend = _opened()
    study = backend.create_node("study", "std1")
    step = backend.create_node("study_step", "step1", parent_tag="std1", feature_type="Stationary")
    assert list(backend.children(study)) == [step]
    backend.remove_node("study_step", "std1/step1")
    assert list(backend.children(study)) == []


def test_model_identity_calls_methods_rather_than_stringifying_them() -> None:
    """Licensed defect: on MPh 1.3.1 `Model.name`/`Model.file` are METHODS.

    The first implementation used `getattr(model, "name")`, so a receipt recorded
    `<bound method Model.name of Model('s4a_probe')>` instead of the model name.
    The licensed gate caught it. This pins the fix.
    """

    class MethodStyleModel:
        """Mirrors MPh's `def name(self) -> str` / `def file(self) -> Path`."""

        def name(self) -> str:
            return "s4a_probe"

        def file(self) -> str:
            return "E:/licensed/s4a_probe.mph"

    identity = MphBackendBase()._model_identity(MethodStyleModel())  # noqa: SLF001
    assert identity.name == "s4a_probe"
    assert "bound method" not in identity.name
    assert identity.file == "E:/licensed/s4a_probe.mph"
    assert identity.content_sha256 is None


def test_model_identity_still_accepts_plain_attributes() -> None:
    """A fake or a future lane may expose attributes; both shapes must work."""

    class AttributeStyleModel:
        name = "plain"
        file = "plain.mph"

    identity = MphBackendBase()._model_identity(AttributeStyleModel())  # noqa: SLF001
    assert identity.name == "plain"
    assert identity.file == "plain.mph"


def test_model_identity_reports_unknown_instead_of_stringifying_nothing() -> None:
    class Empty:
        name = None
        file = None

    identity = MphBackendBase()._model_identity(Empty())  # noqa: SLF001
    assert identity.name == ""
    assert identity.file is None
    assert identity.content_sha256 is None


def test_an_unsupported_node_path_shape_is_refused() -> None:
    """The adapter addresses nodes by explicit kind-prefixed Java tags.

    Licensed probes showed MPh's high-level Node view is unusable on this
    localized COMSOL install: `components()` returns `组件 1` rather than `comp1`,
    `Node.exists()` is False even for a created component, and
    `Node.children()` raises `AttributeError: 'NoneType' object has no attribute
    'tags'`. The adapter therefore uses the Java accessor chain the repository's
    runtime jobs already use, and a caller states the node kind explicitly.

    A path shape the adapter does not represent is refused with a stable code
    rather than guessed at. Only the shape check is exercised, so no COMSOL is
    needed.
    """

    class StubBackend(MphBackendBase):
        def _require_model(self, operation: str) -> object:
            return object()

    backend = StubBackend()
    for path in (("nonsense", "a"), ("component",), ("component", "a", "b", "c", "d")):
        with pytest.raises(AdapterError) as excinfo:
            backend.find_node(path)
        assert excinfo.value.reason_code == "node_not_found"


def test_declared_node_path_shapes_match_the_validator() -> None:
    """The declared shape table and the validator must not drift apart.

    `node_path_shapes()` is what a caller or a receipt reads;
    `_shape_is_representable` is what actually gates resolution. If they disagree,
    a documented shape would be refused or an undocumented one accepted.

    The table's keys are descriptive labels (`("component","geom","feature")`),
    while the validator dispatches on the **first element** plus the arity, so the
    comparison is over `(path[0], arity)` pairs.
    """
    declared = {(kind[0], arity) for kind, arity in MphBackendBase.node_path_shapes().items()}
    assert declared == {
        ("component", 2),
        ("component", 3),
        ("component", 4),
        ("physics", 3),
        ("study", 2),
        ("study", 3),
        ("function", 2),
        ("function", 3),
        ("solution", 2),
        ("dataset", 2),
    }

    kinds = {kind for kind, _ in declared} | {"nonsense"}
    for kind in sorted(kinds):
        for arity in range(1, 6):
            probe = (kind, *(f"t{index}" for index in range(arity - 1)))
            expected = (kind, arity) in declared
            assert MphBackendBase._shape_is_representable(probe) is expected, probe  # noqa: SLF001


def test_every_representable_node_path_shape_reaches_the_java_accessor() -> None:
    """Each supported shape must resolve to the documented accessor chain.

    A recording stub stands in for `model.java`, so the dispatch is exercised
    without COMSOL and each shape's accessor route is pinned.
    """
    calls: list[str] = []

    class Recording:
        def component(self, tag: str) -> "Recording":
            calls.append(f"component({tag})")
            return self

        def geom(self, tag: str) -> "Recording":
            calls.append(f"geom({tag})")
            return self

        def feature(self, tag: str) -> "Recording":
            calls.append(f"feature({tag})")
            return self

        def physics(self, tag: str) -> "Recording":
            calls.append(f"physics({tag})")
            return self

        def study(self, tag: str) -> "Recording":
            calls.append(f"study({tag})")
            return self

        def func(self, tag: str) -> "Recording":
            calls.append(f"func({tag})")
            return self

        def sol(self, tag: str) -> "Recording":
            calls.append(f"sol({tag})")
            return self

        def result(self) -> "Recording":
            calls.append("result()")
            return self

        def dataset(self, tag: str) -> "Recording":
            calls.append(f"dataset({tag})")
            return self

    class StubModel:
        java = Recording()

    class StubBackend(MphBackendBase):
        def _require_model(self, operation: str) -> object:
            return StubModel()

    backend = StubBackend()
    expected = {
        ("component", "comp1"): ["component(comp1)"],
        ("component", "comp1", "geom1"): ["component(comp1)", "geom(geom1)"],
        ("component", "comp1", "geom1", "i1"): [
            "component(comp1)",
            "geom(geom1)",
            "feature(i1)",
        ],
        ("physics", "comp1", "c1"): ["component(comp1)", "physics(c1)"],
        ("study", "std1"): ["study(std1)"],
        ("study", "std1", "step1"): ["study(std1)", "feature(step1)"],
        ("function", "dnn1"): ["func(dnn1)"],
        ("function", "dnn1", "feat1"): ["func(dnn1)", "feature(feat1)"],
        ("solution", "sol1"): ["sol(sol1)"],
        ("dataset", "dset1"): ["result()", "dataset(dset1)"],
    }
    for path, expected_calls in expected.items():
        calls.clear()
        backend._java_node(path)  # noqa: SLF001
        assert calls == expected_calls, path


def test_child_nodes_extend_the_path_without_losing_the_parent() -> None:
    parent = _opened(properties={"c": {"a": 1}}).find_node(["c"])
    child = parent.child("a")
    assert child.path == ("c", "a")
    assert parent.path == ("c",)


# ---------------------------------------------------------------------------
# Typed property access and rollback (step 4)
# ---------------------------------------------------------------------------


def test_a_property_write_reports_the_applied_value() -> None:
    backend = _opened(properties={"comp1": {"L": 0.1}})
    node = backend.find_node(["comp1"])
    plan = backend.plan_write(node, "L", ConvertedValue(0.2, "float", "float"))
    assert plan.rollback_required is True
    assert plan.previous.value == 0.1
    receipt = backend.apply_write(plan)
    assert receipt.applied is True
    assert receipt.rolled_back is False
    assert backend.property_value("comp1", "L") == 0.2
    assert backend.applied_writes == [("comp1", "L", 0.2)]


def test_a_failed_write_is_rolled_back_and_never_claims_success() -> None:
    backend = _opened(
        properties={"comp1": {"L": 0.1}}, failures={"property_write": "backend_internal_error"}
    )
    node = backend.find_node(["comp1"])
    plan = backend.plan_write(node, "L", ConvertedValue(0.9, "float", "float"))
    receipt = backend.apply_write(plan)
    assert receipt.applied is False
    assert receipt.rolled_back is True
    assert receipt.reason_code == "backend_internal_error"
    # The original value survived: this is the failure-atomic guarantee.
    assert backend.property_value("comp1", "L") == 0.1
    assert backend.applied_writes == []


def test_a_write_to_a_missing_property_is_not_representable() -> None:
    """No previous value means no rollback, so the write must not be planned."""
    backend = _opened(properties={"comp1": {"L": 0.1}})
    node = backend.find_node(["comp1"])
    with pytest.raises(AdapterError) as excinfo:
        backend.plan_write(node, "absent", ConvertedValue(1.0, "float", "float"))
    assert excinfo.value.reason_code == "property_not_found"


def test_a_type_mismatch_is_reported_not_coerced() -> None:
    backend = _opened(properties={"comp1": {"tag": "a-string"}})
    node = backend.find_node(["comp1"])
    with pytest.raises(AdapterError) as excinfo:
        backend.read_property(node, "tag", form="float")
    assert excinfo.value.reason_code == "property_type_mismatch"


def test_a_write_plan_carries_the_previous_value_for_rollback() -> None:
    node = _opened(properties={"c": {"a": 1}}).find_node(["c"])
    plan = PropertyWritePlan(
        node=node,
        name="a",
        value=ConvertedValue(2, "int", "int"),
        previous=ConvertedValue(1, "int", "int"),
    )
    assert plan.rollback_required is True
    assert plan.previous.value == 1


# ---------------------------------------------------------------------------
# Evaluate and dataset/solution access (step 5)
# ---------------------------------------------------------------------------


def test_evaluation_and_dataset_access_return_declared_shapes() -> None:
    backend = _opened()
    result = backend.evaluate(EvaluationRequest(expression="1+1"))
    assert result.form == "float"
    assert backend.dataset_names() == ["study1//solution1"]
    assert backend.solution_names() == ["sol1"]


def test_a_real_numpy_style_scalar_array_is_normalized() -> None:
    """MPh returns a numpy array for a scalar expression, not a Python float.

    This is measured licensed behaviour, not an assumption: `model.evaluate("a1")`
    returned `array(1.5)` and `model.evaluate("1+1")` returned `array(2.)` on
    COMSOL 6.4.0.293 (see the measured eval-shape receipt). The
    normalizer must therefore duck-type a numpy-like object through `tolist`.
    """

    class NumpyLike:
        """Stands in for a 0-d numpy array without importing numpy."""

        def __init__(self, value: object) -> None:
            self._value = value

        def tolist(self) -> object:
            return self._value

    for raw, expected in ((1.5, 1.5), (2.0, 2.0), (3, 3)):
        converted = normalize_evaluation_result(NumpyLike(raw))
        assert converted.value == expected
        assert converted.form == ("int" if isinstance(raw, int) else "float")

    backend = _opened()
    backend.set_evaluation("a1", NumpyLike(1.5))
    assert backend.evaluate(EvaluationRequest(expression="a1")).value == 1.5


def test_an_empty_evaluation_result_is_refused_not_zeroed() -> None:
    """An undefined operator yields an empty array; substituting 0 would fabricate."""
    with pytest.raises(AdapterError, match="produced no value"):
        normalize_evaluation_result([])

    backend = _opened()
    backend.set_evaluation("undefined_op(1)", [])
    with pytest.raises(AdapterError) as excinfo:
        backend.evaluate(EvaluationRequest(expression="undefined_op(1)"))
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_a_one_element_and_multi_element_result_are_distinguished() -> None:
    """One element is a scalar; several are one matrix row."""
    assert normalize_evaluation_result([7.0]).value == 7.0
    multi = normalize_evaluation_result([1.0, 2.0, 3.0])
    assert multi.form == "float_matrix"
    assert multi.value == [[1.0, 2.0, 3.0]]
    nested = normalize_evaluation_result([[1.0], [2.0]])
    assert nested.value == [[1.0], [2.0]]


def test_an_unrepresentable_evaluation_shape_is_refused() -> None:
    with pytest.raises(AdapterError, match="does not represent"):
        normalize_evaluation_result(object())


def test_evaluation_requires_a_loaded_model() -> None:
    backend = FakeComsolBackend()
    backend.open_session(SessionRequest())
    with pytest.raises(AdapterError) as excinfo:
        backend.evaluate(EvaluationRequest(expression="1+1"))
    assert excinfo.value.reason_code == "model_load_failed"


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_backend_exceptions_are_translated_to_one_stable_code() -> None:
    translator = MphBackendBase()
    cases = [
        (FileNotFoundError("gone"), "model_load_failed"),
        (KeyError("tag"), "node_not_found"),
        (AttributeError("attr"), "node_not_found"),
        (ValueError("bad"), "property_type_mismatch"),
        (TypeError("bad"), "property_type_mismatch"),
        (RuntimeError("boom"), "backend_internal_error"),
        (NotImplementedError("one client"), "session_already_open"),
    ]
    for exc, expected in cases:
        translated = translator._translate(exc, "op")  # noqa: SLF001
        assert translated.reason_code == expected, exc
        assert translated.operation == "op"
        assert expected in ADAPTER_ERROR_CODES


def test_translation_preserves_an_already_translated_error() -> None:
    original = AdapterError("node_not_found", "x", operation="op")
    assert MphBackendBase()._translate(original, "other") is original  # noqa: SLF001


# ---------------------------------------------------------------------------
# 1.3.1 / 1.4.0 behaviour parity (step 5's gate)
# ---------------------------------------------------------------------------

_PARITY_PROPERTIES = {"comp1": {"L": 0.1, "rows": 2}}
_PARITY_EQUATION = "1+1"


def _pairs() -> tuple[FakeComsolBackend, FakeComsolBackend]:
    reference = _opened(lane="1.3.1", properties=_PARITY_PROPERTIES)
    candidate = _opened(lane="1.4.0", properties=_PARITY_PROPERTIES)
    return reference, candidate


def test_lanes_agree_on_the_operations_that_must_not_drift() -> None:
    """Behavioural parity for everything except the two measured differences."""
    reference, candidate = _pairs()

    for backend in (reference, candidate):
        node = backend.find_node(["comp1"])
        assert backend.read_property(node, "L", form="float").value == 0.1
        assert backend.evaluate(EvaluationRequest(expression=_PARITY_EQUATION)).value == 0.0
        assert backend.dataset_names() == ["study1//solution1"]
        assert backend.solution_names() == ["sol1"]
        assert backend.convert(3, form="int", target="t").value == 3

    assert reference.operations_called() == candidate.operations_called()


def test_lanes_agree_on_the_same_refusal_codes() -> None:
    failures = {"property_read": "property_not_found"}
    reference = _opened(lane="1.3.1", properties=_PARITY_PROPERTIES, failures=failures)
    candidate = _opened(lane="1.4.0", properties=_PARITY_PROPERTIES, failures=failures)
    codes = []
    for backend in (reference, candidate):
        node = backend.find_node(["comp1"])
        with pytest.raises(AdapterError) as excinfo:
            backend.read_property(node, "L", form="float")
        codes.append(excinfo.value.reason_code)
    assert codes[0] == codes[1] == "property_not_found"


def test_the_quoted_matrix_refusal_matches_the_installed_reference_lane() -> None:
    """The quoted refusal is version-specific and must not be assumed present.

    MPh removed the two-row limit on this read path in **1.3.2**, so the refusal
    text exists in 1.3.1 only. `pyproject.toml` allows `mph>=1.3.1,<1.4` and 1.3.2
    is published, so CI's eager-upgrade lane legitimately installs 1.3.2 where the
    text is absent. An earlier version of this test hard-coded the 1.3.1 string and
    failed on that lane.

    The test asserts the relationship instead of a fixed string: a lane either
    limits the read or generalizes it, and the adapter's declared capability must
    agree with whichever is installed.
    """
    import mph

    node_source = (Path(mph.__file__).parent / "node.py").read_text(encoding="utf-8-sig")
    lane_still_refuses = REFERENCE_MATRIX_READ_REFUSAL in node_source
    generalized = "rows = [array(row) for row in value]" in node_source

    assert lane_still_refuses or generalized, (
        "neither the limited nor the generalized DoubleRowMatrix read was found in "
        f"the installed MPh {mph.__version__}; the adapter's assumption is stale"
    )
    # The declared capability must match what the installed lane actually does.
    declared = matrix_read_row_limit(mph.__version__)
    if lane_still_refuses:
        assert declared == REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT
    else:
        assert declared is None
    # The write path refuses above two rows on every lane examined (1.3.1, 1.3.2,
    # 1.4.0), so its message is deliberately NOT translated per-lane.
    assert "Will not cast object arrays with more than two rows." in node_source


def test_the_declared_matrix_limit_matches_the_installed_lane() -> None:
    """A capability claim must describe the lane that is actually installed."""
    import mph

    assert installed_mph_version() == mph.__version__
    assert matrix_read_row_limit(mph.__version__) == matrix_read_row_limit(installed_mph_version())
    # 1.3.2 generalizes the read, so claiming a limit there would be wrong.
    assert matrix_read_row_limit("1.3.1") == REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT
    assert matrix_read_row_limit("1.3.2") is None
    assert matrix_read_row_limit("1.4.0") is None
    with pytest.raises(AdapterError):
        matrix_read_row_limit("1.9.9")


def test_the_whole_declared_mph_range_is_a_supported_reference_lane() -> None:
    """`pyproject.toml` allows `mph>=1.3.1,<1.5`, so 1.3.2 and 1.4.0 must be supported."""
    assert lane_is_supported("1.3.1") is True
    assert lane_is_supported("1.3.2") is True
    assert lane_is_supported("1.4.0") is True
    assert lane_is_supported("1.2.9") is False
    assert lane_is_supported("1.5.0") is False


def test_a_backend_reports_the_installed_lane_not_its_class_lane() -> None:
    """The declared class lane must not override the observed installed version."""
    backend = MphReferenceBackend()
    assert backend.observed_lane() == installed_mph_version()
    # The capability follows the installed lane, whatever the class attribute says.
    assert backend.matrix_row_capacity() == matrix_read_row_limit(installed_mph_version())


def test_lane_capabilities_use_the_version_table_for_every_supported_lane() -> None:
    """1.3.2 must be reported as generalized rather than described as 1.3.1 was."""
    legacy = lane_capabilities("1.3.1")
    modern = lane_capabilities("1.3.2")
    candidate = lane_capabilities(MPH_1_4_LANE)
    assert legacy["double_row_matrix_row_limit"] == REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT
    assert modern["double_row_matrix_row_limit"] is None
    assert candidate["double_row_matrix_row_limit"] is None
    # Only 1.4.0 is the dbmodel:// lane; 1.3.2 is not, despite the matrix parity.
    assert legacy["supports_dbmodel_uri"] is False
    assert modern["supports_dbmodel_uri"] is False
    assert candidate["supports_dbmodel_uri"] is True
    assert legacy.keys() == modern.keys() == candidate.keys()


def test_lanes_differ_only_on_the_two_documented_capabilities() -> None:
    """The documented differences are the *only* ones the protocol models."""
    reference_caps = lane_capabilities("1.3.1")
    candidate_caps = lane_capabilities(MPH_1_4_LANE)
    assert reference_caps["supports_dbmodel_uri"] is False
    assert candidate_caps["supports_dbmodel_uri"] is True
    assert reference_caps["double_row_matrix_row_limit"] == REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT
    assert candidate_caps["double_row_matrix_row_limit"] is None
    assert reference_caps.keys() == candidate_caps.keys()
    differing = {key for key in reference_caps if reference_caps[key] != candidate_caps[key]}
    assert differing == {"lane", "supports_dbmodel_uri", "double_row_matrix_row_limit"}


def test_the_14_lane_refuses_live_dbmodel_loading_with_a_stable_code() -> None:
    """1.4 can load dbmodel:// URIs; this release still performs no live operation."""
    from comsol_mcp.adapter.mph14_backend import Mph14Backend

    backend = Mph14Backend()
    with pytest.raises(AdapterError) as excinfo:
        backend.load_model("dbmodel://library/models/cell")
    assert excinfo.value.reason_code == "model_load_failed"
    assert "not enabled in this release" in str(excinfo.value)
    assert backend.supports_dbmodel_uri() is True


def test_the_14_lane_reports_no_matrix_row_limit() -> None:
    """1.4 generalizes DoubleRowMatrix conversion; the lanes are not equivalent."""
    from comsol_mcp.adapter.mph14_backend import Mph14Backend

    assert Mph14Backend().matrix_row_capacity() is None


def test_an_unknown_lane_has_no_capability_record() -> None:
    with pytest.raises(AdapterError) as excinfo:
        lane_capabilities("9.9.9")
    assert excinfo.value.reason_code == "adapter_unavailable"


def test_the_matrix_difference_maps_to_a_stable_code() -> None:
    """The reference lane's live matrix read refusal becomes one stable code.

    The real path has two steps: MPh raises a bare ``TypeError`` while reading a
    ``DoubleRowMatrix`` wider than two rows, and ``MphBackendBase._translate``
    maps it to ``property_type_mismatch`` carrying MPh's exact message. The 1.4
    lane then collapses that message to ``conversion_not_representable``.

    Both steps run for real here: the first through the shared translator, the
    second through the lane's own collapse method.
    """
    from comsol_mcp.adapter.mph_backend import MphBackendBase

    # Step 1: the raw MPh exception is translated rather than passed through.
    translated = MphBackendBase()._translate(  # noqa: SLF001
        TypeError(REFERENCE_MATRIX_READ_REFUSAL), "property_read"
    )
    assert translated.reason_code == "property_type_mismatch"
    assert REFERENCE_MATRIX_READ_REFUSAL in str(translated)

    # Step 2: the 1.4 lane collapses that message to a stable code.
    lane = Mph14Backend()
    collapsed = lane._collapse_matrix_refusal(translated)  # noqa: SLF001
    assert collapsed.reason_code == "conversion_not_representable"
    assert f"{REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT} rows" in str(collapsed)


def test_an_unrelated_failure_is_not_reported_as_a_matrix_difference() -> None:
    """Only the quoted refusal is collapsed; anything else passes through."""
    lane = Mph14Backend()
    original = AdapterError("node_not_found", "no node", operation="node_lookup")
    assert lane._collapse_matrix_refusal(original) is original  # noqa: SLF001


# ---------------------------------------------------------------------------
# Typed Java access and feature lifecycle (step 6)
# ---------------------------------------------------------------------------

#: The stub below stands in for one resolved ClientAPI feature. It records the
#: exact call the adapter made, so "the adapter owns Java typing" is proven by
#: observable calls rather than by the absence of a string in the source.
_FEATURE_SCALAR_NAMES = ("layertype",)


class _StubFeature:
    """A recording stand-in for a resolved ClientAPI feature object."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.entries: list[tuple[str, str, str]] = []
        self.calls: list[str] = []
        self.strings: dict[str, str] = {}
        self.string_arrays: dict[str, list[str]] = {}
        self.declared_types: dict[str, str] = {}
        self.allowed: dict[str, list[str]] = {}
        self.value_type_fails = False

    # writes
    def set(self, name: str, value: object) -> None:
        self.writes.append((name, type(value).__name__))

    def setEntry(self, name: str, key: str, value: str) -> None:
        self.entries.append((name, key, value))

    # reads
    def getValueType(self, name: str) -> str:
        if self.value_type_fails:
            raise RuntimeError("no such property")
        return self.declared_types.get(name, "double")

    def getString(self, name: str) -> str:
        if name not in self.strings:
            raise RuntimeError("not a string property")
        return self.strings[name]

    def getStringArray(self, name: str) -> list[str]:
        if name not in self.string_arrays:
            raise RuntimeError("not a string-array property")
        return self.string_arrays[name]

    def getAllowedPropertyValues(self, name: str) -> list[str]:
        if name not in self.allowed:
            raise RuntimeError("not an enumerated property")
        return self.allowed[name]

    # lifecycle
    def run(self) -> None:
        self.calls.append("run")

    def continueRun(self) -> None:
        self.calls.append("continueRun")

    def runTest(self) -> None:
        self.calls.append("runTest")

    def discardData(self) -> None:
        self.calls.append("discardData")

    def importData(self) -> None:
        self.calls.append("importData")

    def export(self, path: str) -> None:
        self.calls.append(f"export({path})")


class _StubContainer:
    """A recording stand-in for the ``study()``/``func()`` container accessor."""

    def __init__(self, tags: list[str]) -> None:
        self._tags = tags
        self.tags_reads = 0
        #: The step list a study exposes through ``feature()``, built on demand so
        #: constructing a container cannot recurse into constructing another.
        self._steps: _StubContainer | None = None

    def tags(self) -> list[str]:
        self.tags_reads += 1
        return list(self._tags)

    def create(self, tag: str, *extra: str) -> None:
        del extra
        self._tags.append(tag)

    def remove(self, tag: str) -> None:
        if tag in self._tags:
            self._tags.remove(tag)

    def feature(self, tag: str | None = None) -> "_StubContainer":
        del tag
        if self._steps is None:
            self._steps = _StubContainer([])
        return self._steps


class _StubJava:
    """A recording stand-in for ``model.java`` covering the step-6 containers."""

    def __init__(self) -> None:
        self.study_container = _StubContainer(["std1"])
        self.func_container = _StubContainer([])

    def study(self, tag: str | None = None) -> _StubContainer:
        del tag
        return self.study_container

    def func(self, tag: str | None = None) -> _StubContainer:
        del tag
        return self.func_container


class _Step6Backend(MphBackendBase):
    """A backend whose model is a recording stub, so no COMSOL is needed."""

    def __init__(self) -> None:
        super().__init__()
        self.java = _StubJava()

    def _require_model(self, operation: str) -> object:
        del operation
        return type("Model", (), {"java": self.java})()


def _handle(feature: _StubFeature) -> ResolvedNode:
    return ResolvedNode(handle=feature, label="dnn1")


def test_an_unnamed_java_write_kind_is_refused() -> None:
    """A write must name its Java type, and the vocabulary is closed."""
    assert "string_matrix" in JAVA_WRITE_KINDS
    assert "string_entry" in JAVA_WRITE_KINDS
    assert len(JAVA_WRITE_KINDS) == len(set(JAVA_WRITE_KINDS))
    # There is deliberately no generic "set this object" kind.
    for generic in ("object", "any", "auto", "python"):
        assert generic not in JAVA_WRITE_KINDS
    with pytest.raises(AdapterError) as excinfo:
        JavaTypedWrite(node=_handle(_StubFeature()), name="L", value=1, kind="nonsense")
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_a_string_entry_write_uses_set_entry_and_requires_a_mapping() -> None:
    """Alternating key/value properties go through ``setEntry``, one pair at a time."""
    backend = _Step6Backend()
    feature = _StubFeature()
    backend.java_typed_write(
        JavaTypedWrite(
            node=_handle(feature),
            name="globaldnnfunction",
            value={"1": "dnn1", "2": "dnn2"},
            kind="string_entry",
        )
    )
    assert feature.entries == [
        ("globaldnnfunction", "1", "dnn1"),
        ("globaldnnfunction", "2", "dnn2"),
    ]
    assert feature.writes == []
    with pytest.raises(AdapterError) as excinfo:
        backend.java_typed_write(
            JavaTypedWrite(
                node=_handle(feature), name="args", value=["1", "a"], kind="string_entry"
            )
        )
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_a_typed_read_reports_the_accessor_and_the_rejections() -> None:
    """The winning accessor is recorded, and an unreadable property says why."""
    backend = _Step6Backend()
    feature = _StubFeature()
    feature.strings["trainingloss"] = "0.025"
    feature.string_arrays["activation"] = ["tanh", "relu"]

    scalar = backend.java_typed_read(_handle(feature), "trainingloss")
    assert scalar.accessor == "getString"
    assert scalar.value == "0.025"
    assert scalar.rejected == ("getString",) or scalar.rejected == ()
    array = backend.java_typed_read(_handle(feature), "activation")
    assert array.accessor == "getStringArray"
    assert array.value == ["tanh", "relu"]

    with pytest.raises(AdapterError) as excinfo:
        backend.java_typed_read(_handle(feature), "absent")
    assert excinfo.value.reason_code == "property_not_found"
    # The rejection list is structured data, so a caller can report it without
    # parsing the message.
    assert any(item.startswith("getString:") for item in excinfo.value.rejected)


def test_a_feature_method_outside_the_vocabulary_is_refused() -> None:
    """There is no generic "call this method" form on the adapter."""
    assert DNN_FEATURE_METHODS == ("run", "continueRun", "runTest", "discardData", "importData")
    backend = _Step6Backend()
    feature = _StubFeature()
    for method in DNN_FEATURE_METHODS:
        backend.run_feature(_handle(feature), method=method)
    assert feature.calls == list(DNN_FEATURE_METHODS)
    with pytest.raises(AdapterError) as excinfo:
        backend.run_feature(_handle(feature), method="delete")
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_an_unresolvable_handle_is_refused_rather_than_treated_as_java() -> None:
    """An arbitrary object must not become an untyped Java escape hatch."""
    backend = _Step6Backend()
    with pytest.raises(AdapterError) as excinfo:
        backend.export_feature(_StubFeature(), "model.onnx")  # type: ignore[arg-type]
    assert excinfo.value.reason_code == "node_not_found"
    assert "ResolvedNode" in str(excinfo.value)


def test_an_exported_feature_records_its_path() -> None:
    backend = _Step6Backend()
    feature = _StubFeature()
    backend.export_feature(_handle(feature), "model.onnx")
    assert feature.calls == ["export(model.onnx)"]


def test_a_declared_value_type_is_measured_and_unknown_is_reported() -> None:
    """Property form is measured, and an unreadable type stays unknown."""
    backend = _Step6Backend()
    feature = _StubFeature()
    feature.declared_types["globaldnnfunction"] = "[[Ljava.lang.String;"
    assert backend.java_value_type(_handle(feature), "globaldnnfunction") == "[[Ljava.lang.String;"
    feature.value_type_fails = True
    assert backend.java_value_type(_handle(feature), "globaldnnfunction") is None
    assert backend.read_allowed_values(_handle(feature), "loss") is None
    feature.value_type_fails = False
    feature.allowed["loss"] = ["mse", "mae"]
    assert backend.read_allowed_values(_handle(feature), "loss") == ["mse", "mae"]


def test_container_tags_and_node_lifecycle_use_declared_accessors() -> None:
    """Only the declared containers are enumerable, creatable, or removable."""
    backend = _Step6Backend()
    assert backend.container_tags("study") == ["std1"]
    assert backend.container_tags("function") == []

    created = backend.create_node("study", "std2")
    assert created.path == ("study", "std2")
    assert backend.container_tags("study") == ["std1", "std2"]
    step = backend.create_node("study_step", "step1", parent_tag="std1", feature_type="StudyStep")
    assert step.path == ("study", "std1", "step1")
    function = backend.create_node("function", "dnn1", feature_type="DnnFunction")
    assert function.path == ("function", "dnn1")
    assert backend.container_tags("function") == ["dnn1"]

    backend.remove_node("study", "std2")
    backend.remove_node("function", "dnn1")
    assert backend.container_tags("study") == ["std1"]
    assert backend.container_tags("function") == []

    for call in (
        lambda: backend.container_tags("dataset"),
        lambda: backend.create_node("dataset", "d1"),
        lambda: backend.remove_node("dataset", "d1"),
    ):
        with pytest.raises(AdapterError) as excinfo:
            call()
        assert excinfo.value.reason_code == "node_not_found"
    # A function or step without a declared type is refused, not guessed.
    for call in (
        lambda: backend.create_node("function", "dnn1"),
        lambda: backend.create_node("study_step", "step1"),
    ):
        with pytest.raises(AdapterError) as excinfo:
            call()
        assert excinfo.value.reason_code == "conversion_not_representable"


def test_every_registered_backend_and_the_fake_satisfy_the_extended_protocol() -> None:
    """The Protocol grew in step 6; every implementation must have kept up."""
    members = (
        "open_session",
        "close_session",
        "session_identity",
        "load_model",
        "save_model",
        "find_node",
        "children",
        "convert",
        "read_property",
        "plan_write",
        "apply_write",
        "evaluate",
        "dataset_names",
        "solution_names",
        "java_typed_write",
        "java_typed_read",
        "run_feature",
        "export_feature",
        "attach_client",
        "container_tags",
        "create_node",
        "remove_node",
        "read_allowed_values",
        "java_value_type",
    )
    backends = [FakeComsolBackend(), *(make_backend(lane) for lane in available_lanes())]
    for backend in backends:
        missing = [name for name in members if not callable(getattr(backend, name, None))]
        assert missing == [], f"{type(backend).__name__} is missing {missing}"
        assert isinstance(backend, ComsolAdapter)


def test_an_adopted_client_is_released_rather_than_cleared() -> None:
    """Ownership does not transfer, so closing must not clear someone else's client."""
    clients: list[object] = []

    class Client:
        def __init__(self, models: list[object]) -> None:
            self._models = models
            self.cleared = False

        def models(self) -> list[object]:
            return list(self._models)

        def clear(self) -> None:
            self.cleared = True

    class Model:
        """Mirrors MPh 1.3.1: no back-reference to the owning client."""

    backend = FakeComsolBackend()
    model = Model()
    adopted = Client([model])
    clients.append(adopted)
    # The client is required: a bare model cannot be used to discover it.
    with pytest.raises(AdapterError) as excinfo:
        backend.attach_client(model)
    assert excinfo.value.reason_code == "adapter_unavailable"
    backend.attach_client(model, client=adopted)
    assert backend.attached is True
    assert backend.adopted_client() is adopted
    backend.close_session()
    assert adopted.cleared is False

    # A session the backend opened itself is still cleaned up by the backend.
    owned_client = Client([])
    clients.append(owned_client)

    class OpenedBackend(FakeComsolBackend):
        def open_session(self, request: SessionRequest) -> SessionIdentity:
            self._client = owned_client
            self._session = SessionIdentity(
                mph_version=self._mph_version,
                comsol_version=self._comsol_version,
                host=None,
                port=None,
                standalone=True,
            )
            return self._session

    opened = OpenedBackend()
    opened.open_session(SessionRequest(cores=1))
    opened.close_session()
    assert owned_client.cleared is True
    assert opened.cleared_clients == [owned_client]


def test_a_failed_int_write_rolls_back_through_the_int_route() -> None:
    backend = _opened(
        properties={"comp1": {"count": 7}}, failures={"property_write": "backend_internal_error"}
    )
    node = backend.find_node(["comp1"])
    plan = backend.plan_write(node, "count", ConvertedValue(11, "int", "int"))
    receipt = backend.apply_write(plan)
    assert receipt.applied is False
    assert receipt.rolled_back is True
    assert backend.property_value("comp1", "count") == 7


def test_attach_client_refuses_while_an_owned_session_is_open() -> None:
    from comsol_mcp.adapter.protocol import AdapterError

    backend = _opened(properties={"comp1": {}})
    client = type("Client", (), {"models": staticmethod(lambda: [])})()
    model = object()
    with pytest.raises(AdapterError, match="close the owned session first"):
        backend.attach_client(model, client=client)
    backend.close_session()
    backend.attach_client(model, client=client)
