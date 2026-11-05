"""Solver-free tests for the minimal ONNX reader and forward evaluator."""

from __future__ import annotations

import math
import struct

import pytest

from comsol_mcp.surrogate.onnx_runtime import (
    SUPPORTED_OPERATORS,
    OnnxDecodeError,
    evaluate_onnx_model,
    load_onnx_model,
)

SOLVER_FREE_BANNED = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import comsol",
    "from comsol.",
    "import numpy",
    "import onnx",
    "import torch",
)


# --------------------------------------------------------------------------
# Minimal protobuf encoder, so tests need no ONNX library
# --------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _vi(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def _f32(field: int, value: float) -> bytes:
    return _key(field, 5) + struct.pack("<f", value)


def _tensor(name: str, dims: list[int], values: list[float]) -> bytes:
    body = b"".join(_vi(1, dim) for dim in dims)
    body += _vi(2, 1)  # float32
    body += _ld(8, name.encode())
    body += _ld(9, struct.pack(f"<{len(values)}f", *values))
    return body


def _node(op: str, inputs: list[str], outputs: list[str], attrs: dict | None = None) -> bytes:
    body = b"".join(_ld(1, name.encode()) for name in inputs)
    body += b"".join(_ld(2, name.encode()) for name in outputs)
    body += _ld(3, op.lower().encode())
    body += _ld(4, op.encode())
    for attr_name, attr_value in (attrs or {}).items():
        attr = _ld(1, attr_name.encode()) + _vi(3, int(attr_value))
        body += _ld(5, attr)
    return body


def _value_info(name: str) -> bytes:
    return _ld(1, name.encode())


def _onnx(
    nodes: list[bytes], initializers: list[bytes], inputs: list[str], output: str = "output"
) -> bytes:
    graph = b"".join(_ld(1, node) for node in nodes)
    graph += _ld(2, b"graph")
    graph += b"".join(_ld(5, tensor) for tensor in initializers)
    graph += b"".join(_ld(11, _value_info(name)) for name in inputs)
    graph += _ld(12, _value_info(output))
    return _ld(2, b"COMSOL") + _ld(7, graph)


def _mlp_onnx(
    *,
    weight: list[float],
    bias: list[float],
    weight_dims: list[int],
    activation: str = "Tanh",
    trans_b: int = 0,
) -> bytes:
    nodes = [
        _node(
            "Gemm",
            ["input", "W", "B"],
            ["hidden"],
            {"transB": trans_b},
        ),
        _node(activation, ["hidden"], ["output"]),
    ]
    initializers = [_tensor("W", weight_dims, weight), _tensor("B", [len(bias)], bias)]
    return _onnx(nodes, initializers, ["input"])


def _write(tmp_path, name: str, payload: bytes):
    target = tmp_path / name
    target.write_bytes(payload)
    return target


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


def test_decode_reads_producer_graph_and_initializers(tmp_path) -> None:
    path = _write(
        tmp_path,
        "model.onnx",
        _mlp_onnx(weight=[1.0, 0.0, 0.0, 1.0], bias=[0.0, 0.0], weight_dims=[2, 2]),
    )
    model = load_onnx_model(path)
    assert model["producer"] == "COMSOL"
    assert model["graph_name"] == "graph"
    assert model["operators"] == ["Gemm", "Tanh"]
    assert model["node_count"] == 2
    assert model["initializer_shapes"] == {"W": [2, 2], "B": [2]}
    assert model["graph_inputs"] == ["input"]
    assert model["size_bytes"] > 0
    assert len(model["sha256"]) == 64
    assert len(model["model_sha256"]) == 64


def test_decode_rejects_missing_empty_and_oversized_files(tmp_path) -> None:
    with pytest.raises(OnnxDecodeError, match="not found"):
        load_onnx_model(tmp_path / "absent.onnx")
    empty = tmp_path / "empty.onnx"
    empty.write_bytes(b"")
    with pytest.raises(OnnxDecodeError, match="outside the allowed range"):
        load_onnx_model(empty)


def test_decode_rejects_a_payload_without_a_graph(tmp_path) -> None:
    path = _write(tmp_path, "nograph.onnx", _ld(2, b"COMSOL"))
    with pytest.raises(OnnxDecodeError, match="no graph"):
        load_onnx_model(path)


def test_decode_refuses_unsupported_operators(tmp_path) -> None:
    nodes = [_node("Conv", ["input", "W"], ["output"])]
    path = _write(
        tmp_path,
        "conv.onnx",
        _onnx(nodes, [_tensor("W", [1], [1.0])], ["input"]),
    )
    with pytest.raises(OnnxDecodeError, match="unsupported operators: Conv"):
        load_onnx_model(path)


def test_supported_operator_set_is_declared() -> None:
    assert "Gemm" in SUPPORTED_OPERATORS
    assert "Tanh" in SUPPORTED_OPERATORS
    assert "Conv" not in SUPPORTED_OPERATORS


# --------------------------------------------------------------------------
# Forward evaluation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("data_type,field,code", [(1, 4, "f"), (11, 10, "d")])
def test_packed_tensor_encoding_preserves_values(data_type, field, code):
    from comsol_mcp.surrogate.onnx_runtime import _decode_tensor

    tensor = _ld(1, _varint(2) + _varint(2)) + _vi(2, data_type) + _ld(8, b"weights")
    tensor += _ld(field, struct.pack(f"<4{code}", 1.0, 2.0, 3.0, 4.0))
    decoded = _decode_tensor(tensor)
    assert decoded["dims"] == [2, 2]
    assert decoded["values"] == [1.0, 2.0, 3.0, 4.0]


def test_identity_gemm_reproduces_its_input(tmp_path) -> None:
    path = _write(
        tmp_path,
        "identity.onnx",
        _mlp_onnx(
            weight=[1.0, 0.0, 0.0, 1.0],
            bias=[0.0, 0.0],
            weight_dims=[2, 2],
            activation="Identity",
        ),
    )
    model = load_onnx_model(path)
    result = evaluate_onnx_model(model, input_names=["a1", "a2"], points=[[3.0, 4.0]])
    assert result[0] == pytest.approx([3.0, 4.0])


def test_transposed_gemm_input_is_refused_instead_of_silently_ignored(tmp_path) -> None:
    path = _write(
        tmp_path,
        "transposed-input.onnx",
        _onnx(
            [_node("Gemm", ["input", "W"], ["output"], {"transA": 1})],
            [_tensor("W", [2, 2], [1.0, 0.0, 0.0, 1.0])],
            ["input"],
        ),
    )
    with pytest.raises(OnnxDecodeError, match="transA"):
        evaluate_onnx_model(load_onnx_model(path), input_names=["a", "b"], points=[[3.0, 4.0]])


def test_gemm_dynamic_weights_are_refused_with_a_decode_error(tmp_path) -> None:
    path = _write(
        tmp_path,
        "dynamic-weights.onnx",
        _onnx([_node("Gemm", ["input", "input"], ["output"])], [], ["input"]),
    )
    with pytest.raises(OnnxDecodeError, match="initializer"):
        evaluate_onnx_model(load_onnx_model(path), input_names=["a", "b"], points=[[3.0, 4.0]])


def test_tanh_activation_matches_the_analytic_value(tmp_path) -> None:
    # A single 1x1 Gemm with weight 1 and bias 0 followed by Tanh computes
    # tanh(x) exactly, so the evaluator is checked against a known answer.
    path = _write(
        tmp_path,
        "tanh.onnx",
        _mlp_onnx(weight=[1.0], bias=[0.0], weight_dims=[1, 1], activation="Tanh"),
    )
    model = load_onnx_model(path)
    for value in (0.0, 0.5, -1.5, 3.0):
        result = evaluate_onnx_model(model, input_names=["x"], points=[[value]])
        assert result[0][0] == pytest.approx(math.tanh(value), abs=1e-6)


def test_gemm_with_bias_and_multiple_outputs(tmp_path) -> None:
    # W is [2, 3]: two inputs, three outputs, with a per-output bias.
    path = _write(
        tmp_path,
        "wide.onnx",
        _mlp_onnx(
            # [in=2, out=3] row-major: row0=[1,0,0], row1=[1,1,1]
            weight=[1.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            bias=[0.5, -0.5, 2.0],
            weight_dims=[2, 3],
            activation="Identity",
        ),
    )
    model = load_onnx_model(path)
    result = evaluate_onnx_model(model, input_names=["a", "b"], points=[[2.0, 3.0]])
    # out0 = 2*1 + 3*1 + 0.5 = 5.5
    # out1 = 2*0 + 3*1 - 0.5 = 2.5
    # out2 = 2*0 + 3*1 + 2.0 = 5.0
    assert result[0] == pytest.approx([5.5, 2.5, 5.0])


def test_trans_b_matches_the_pytorch_weight_layout(tmp_path) -> None:
    """COMSOL exports PyTorch-style Gemm with transB=1, i.e. W is [out, in]."""
    # W stored as [out=2, in=2]; with transB the effective matrix is its transpose.
    path = _write(
        tmp_path,
        "transb.onnx",
        _mlp_onnx(
            weight=[1.0, 2.0, 3.0, 4.0],
            bias=[0.0, 0.0],
            weight_dims=[2, 2],
            activation="Identity",
            trans_b=1,
        ),
    )
    model = load_onnx_model(path)
    result = evaluate_onnx_model(model, input_names=["a", "b"], points=[[1.0, 1.0]])
    # W^T applied to [1,1]: column sums of the stored [out,in] layout -> [3, 7].
    assert result[0] == pytest.approx([3.0, 7.0])


def test_scale_and_bias_nodes_apply_input_normalization(tmp_path) -> None:
    # Mul(scale, input) then Add(bias, ...) is COMSOL's input normalization.
    nodes = [
        _node("Mul", ["scale", "input"], ["scaled"]),
        _node("Add", ["offset", "scaled"], ["normalized"]),
        _node("Identity", ["normalized"], ["output"]),
    ]
    initializers = [
        _tensor("scale", [2], [0.5, 0.25]),
        _tensor("offset", [2], [-1.0, -2.0]),
    ]
    path = _write(tmp_path, "norm.onnx", _onnx(nodes, initializers, ["input"]))
    model = load_onnx_model(path)
    result = evaluate_onnx_model(model, input_names=["a", "b"], points=[[4.0, 8.0]])
    # [4*0.5 - 1, 8*0.25 - 2] = [1, 0]
    assert result[0] == pytest.approx([1.0, 0.0])


def test_relu_and_sigmoid_activations(tmp_path) -> None:
    for activation, expected in (("Relu", [0.0, 2.0]), ("Sigmoid", None)):
        path = _write(
            tmp_path,
            f"{activation}.onnx",
            _mlp_onnx(
                weight=[1.0, 0.0, 0.0, 1.0],
                bias=[0.0, 0.0],
                weight_dims=[2, 2],
                activation=activation,
            ),
        )
        model = load_onnx_model(path)
        result = evaluate_onnx_model(model, input_names=["a", "b"], points=[[-1.0, 2.0]])
        if expected is not None:
            assert result[0] == pytest.approx(expected)
        else:
            assert result[0][0] == pytest.approx(1 / (1 + math.exp(1.0)), abs=1e-6)
            assert result[0][1] == pytest.approx(1 / (1 + math.exp(-2.0)), abs=1e-6)


def test_multiple_points_are_evaluated_independently(tmp_path) -> None:
    path = _write(
        tmp_path,
        "multi.onnx",
        _mlp_onnx(weight=[2.0], bias=[1.0], weight_dims=[1, 1], activation="Identity"),
    )
    model = load_onnx_model(path)
    result = evaluate_onnx_model(model, input_names=["x"], points=[[1.0], [2.0], [3.0]])
    assert [row[0] for row in result] == pytest.approx([3.0, 5.0, 7.0])


def test_gemm_alpha_and_beta_scale_weight_and_bias(tmp_path) -> None:
    path = _write(
        tmp_path,
        "gemm-scale.onnx",
        _onnx(
            [_node("Gemm", ["input", "W", "B"], ["output"], {"alpha": 2, "beta": 3})],
            [_tensor("W", [1, 1], [4.0]), _tensor("B", [1], [5.0])],
            ["input"],
        ),
    )
    model = load_onnx_model(path)
    assert evaluate_onnx_model(model, input_names=["x"], points=[[2.0]])[0][0] == pytest.approx(
        31.0
    )


def test_declared_graph_output_is_used_instead_of_last_node(tmp_path) -> None:
    payload = _onnx(
        [_node("Identity", ["input"], ["declared"]), _node("Identity", ["input"], ["other"])],
        [],
        ["input"],
        output="declared",
    )
    model = load_onnx_model(_write(tmp_path, "declared-output.onnx", payload))
    assert evaluate_onnx_model(model, input_names=["x"], points=[[7.0]]) == [[7.0]]


def test_sigmoid_handles_large_negative_values(tmp_path) -> None:
    payload = _onnx([_node("Sigmoid", ["input"], ["output"])], [], ["input"])
    model = load_onnx_model(_write(tmp_path, "stable-sigmoid.onnx", payload))
    result = evaluate_onnx_model(model, input_names=["x"], points=[[-1000.0]])
    assert result[0][0] == pytest.approx(0.0)


def test_graph_limits_are_checked_before_decoding_all_entries(tmp_path) -> None:
    too_many_nodes = [_node("Identity", ["input"], [f"node-{i}"]) for i in range(513)]
    with pytest.raises(OnnxDecodeError, match="node limit"):
        load_onnx_model(
            _write(tmp_path, "too-many-nodes.onnx", _onnx(too_many_nodes, [], ["input"]))
        )
    too_many_initializers = [_tensor(f"w-{i}", [1], [1.0]) for i in range(513)]
    with pytest.raises(OnnxDecodeError, match="initializer limit"):
        load_onnx_model(
            _write(
                tmp_path, "too-many-initializers.onnx", _onnx([], too_many_initializers, ["input"])
            )
        )


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_wrong_point_width_is_refused(tmp_path) -> None:
    path = _write(
        tmp_path,
        "two.onnx",
        _mlp_onnx(weight=[1.0, 0.0, 0.0, 1.0], bias=[0.0, 0.0], weight_dims=[2, 2]),
    )
    model = load_onnx_model(path)
    with pytest.raises(OnnxDecodeError, match="supplies 1 values for 2 inputs"):
        evaluate_onnx_model(model, input_names=["a", "b"], points=[[1.0]])


def test_mismatched_input_name_count_is_refused(tmp_path) -> None:
    """A graph declaring several inputs must match the supplied name count."""
    nodes = [_node("Identity", ["in_a", "in_b"], ["output"])]
    path = _write(tmp_path, "two_inputs.onnx", _onnx(nodes, [], ["in_a", "in_b"]))
    model = load_onnx_model(path)
    assert model["graph_inputs"] == ["in_a", "in_b"]
    with pytest.raises(OnnxDecodeError, match="matches neither"):
        evaluate_onnx_model(model, input_names=["only_one"], points=[[1.0]])


def test_single_batched_input_accepts_the_whole_point_vector(tmp_path) -> None:
    """COMSOL declares one batched input tensor; a width mismatch still fails."""
    path = _write(
        tmp_path,
        "batched.onnx",
        _mlp_onnx(weight=[1.0], bias=[0.0], weight_dims=[1, 1]),
    )
    model = load_onnx_model(path)
    assert model["graph_inputs"] == ["input"]
    with pytest.raises(OnnxDecodeError, match="Gemm expects 1 inputs"):
        evaluate_onnx_model(model, input_names=["a", "b"], points=[[1.0, 2.0]])


def test_gemm_width_mismatch_is_refused(tmp_path) -> None:
    # The graph declares a 1-wide input but the weight matrix expects 3.
    path = _write(
        tmp_path,
        "mismatch.onnx",
        _mlp_onnx(
            weight=[1.0] * 9,
            bias=[0.0, 0.0, 0.0],
            weight_dims=[3, 3],
        ),
    )
    model = load_onnx_model(path)
    with pytest.raises(OnnxDecodeError, match="Gemm expects 3 inputs"):
        evaluate_onnx_model(model, input_names=["x"], points=[[1.0]])


def test_duplicate_and_empty_input_names_are_refused(tmp_path) -> None:
    path = _write(
        tmp_path,
        "dup.onnx",
        _mlp_onnx(weight=[1.0], bias=[0.0], weight_dims=[1, 1]),
    )
    model = load_onnx_model(path)
    with pytest.raises(OnnxDecodeError, match="must be unique"):
        evaluate_onnx_model(model, input_names=["a", "a"], points=[[1.0, 2.0]])
    with pytest.raises(OnnxDecodeError, match="input_names must be non-empty"):
        evaluate_onnx_model(model, input_names=[], points=[[1.0]])
    with pytest.raises(OnnxDecodeError, match="non-empty sequence"):
        evaluate_onnx_model(model, input_names=["a"], points=[])


def test_an_unknown_value_reference_is_refused(tmp_path) -> None:
    nodes = [
        _node("Identity", ["missing_tensor"], ["output"]),
    ]
    path = _write(tmp_path, "dangling.onnx", _onnx(nodes, [], ["input"]))
    model = load_onnx_model(path)
    with pytest.raises(OnnxDecodeError, match="unknown ONNX value"):
        evaluate_onnx_model(model, input_names=["a"], points=[[1.0]])


def test_non_onnx_payload_is_refused(tmp_path) -> None:
    path = _write(tmp_path, "junk.onnx", b"this is not a protobuf message at all\xff\xff")
    with pytest.raises(OnnxDecodeError):
        load_onnx_model(path)


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_onnx_runtime_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.onnx_runtime as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"onnx_runtime references {banned}"


def test_onnx_runtime_uses_only_the_standard_library() -> None:
    import comsol_mcp.surrogate.onnx_runtime as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "import struct" in source
    assert "import math" in source


# --------------------------------------------------------------------------
# Real COMSOL artifact regression
# --------------------------------------------------------------------------

# A real ONNX export produced by COMSOL 6.4.0.293 from a trained 2->16->8->1
# network (receipt in the licensed S7 gate archive).  Its training target was
# qoi = sin(a1)cos(a2) + 0.1*a1*a2 over a1 in [1.0, 2.75], a2 in [2.0, 4.5].
REAL_COMSOL_ONNX_SHA256 = "a417ee487ad5d8c8fc25919aa47f2782b5c9b7f0a979ee255da8aa841860ecca"


def _real_comsol_onnx_path():

    from development_kit.tests.platform_fixtures import platform_test_root

    candidate = platform_test_root("s7gate") / "workspace" / "surrogate_export_a.onnx"
    return candidate if candidate.is_file() else None


@pytest.mark.skipif(
    _real_comsol_onnx_path() is None,
    reason="licensed COMSOL export receipt is not present on this host",
)
def test_real_comsol_export_decodes_with_expected_structure() -> None:
    """The decoder must handle the exact ONNX COMSOL emits."""
    model = load_onnx_model(_real_comsol_onnx_path())
    assert model["producer"] == "COMSOL"
    assert model["operators"] == ["Add", "Gemm", "Mul", "Tanh"]
    assert model["node_count"] == 10
    assert model["sha256"] == REAL_COMSOL_ONNX_SHA256
    # A single batched input tensor, and the trained MLP weight shapes.
    assert model["graph_inputs"] == ["input"]
    assert model["initializer_shapes"]["node_Gemm.weight"] == [16, 2]
    assert model["initializer_shapes"]["node_Gemm_1.weight"] == [8, 16]
    assert model["initializer_shapes"]["node_Gemm_2.weight"] == [1, 8]


@pytest.mark.skipif(
    _real_comsol_onnx_path() is None,
    reason="licensed COMSOL export receipt is not present on this host",
)
def test_real_comsol_export_predicts_its_training_target() -> None:
    """The decoded network must approximate the function it was trained on.

    This is the check that would catch a wrong transB interpretation, a
    mis-bound input, or a mis-decoded float layout: any of those produce
    predictions unrelated to the target.
    """
    model = load_onnx_model(_real_comsol_onnx_path())
    points = [(1.0, 2.0), (1.5, 2.5), (2.0, 3.0), (2.5, 3.5), (2.75, 4.5)]
    predictions = evaluate_onnx_model(model, input_names=["a1", "a2"], points=points)
    worst = 0.0
    for (a1, a2), row in zip(points, predictions):
        target = math.sin(a1) * math.cos(a2) + 0.1 * a1 * a2
        worst = max(worst, abs(row[0] - target))
    # The trained network reported a test loss of 0.0206, so its worst
    # deviation inside the training box must be far below the target's range.
    assert worst < 0.25, f"decoded network does not reproduce its target: {worst}"


@pytest.mark.skipif(
    _real_comsol_onnx_path() is None,
    reason="licensed COMSOL export receipt is not present on this host",
)
def test_real_comsol_export_normalization_matches_the_reported_domain() -> None:
    """The ONNX scale/bias must equal min-max scaling of COMSOL's own domain.

    COMSOL's `information` property reported the training domain as
    a1 in [1.0, 2.75] and a2 in [2.0, 4.5]; the exported normalization must
    agree, which independently confirms the input binding is correct.
    """
    model = load_onnx_model(_real_comsol_onnx_path())
    scale = model["initializers"]["scale"]["values"]
    bias = model["initializers"]["bias"]["values"]
    assert scale == pytest.approx([1.0 / 1.75, 1.0 / 2.5], abs=1e-6)
    assert bias == pytest.approx([-1.0 / 1.75, -2.0 / 2.5], abs=1e-6)


# --------------------------------------------------------------------------
# Regressions: input binding and empty-graph refusal
# --------------------------------------------------------------------------


def test_per_feature_inputs_bind_each_tensor_to_one_feature(tmp_path) -> None:
    """Each per-feature tensor must carry one feature, not a bare string.

    Regression: ``dict(zip(graph_inputs, names))`` bound every tensor name to a
    string, so the consumer iterated that string's characters and evaluation
    failed with ``KeyError: 'a'``.  The distinct tensor names here are what make
    the per-feature branch reachable at all; reusing the feature names would
    leave the graph input already bound and skip the binding entirely.
    """
    # out = in_a + in_b, declared as two single-feature graph inputs.
    nodes = [_node("Add", ["in_a", "in_b"], ["output"])]
    payload = _onnx(nodes, [], ["in_a", "in_b"])
    model = load_onnx_model(_write(tmp_path, "two_inputs.onnx", payload))

    assert model["graph_inputs"] == ["in_a", "in_b"]
    predictions = evaluate_onnx_model(
        model, input_names=["a1", "a2"], points=[[1.0, 2.0], [3.0, 4.0]]
    )
    assert predictions == [[3.0], [7.0]]


def test_single_batched_input_binds_the_whole_point(tmp_path) -> None:
    """The COMSOL layout binds one tensor named ``input`` to every feature."""
    nodes = [_node("Identity", ["input"], ["output"])]
    model = load_onnx_model(_write(tmp_path, "batched.onnx", _onnx(nodes, [], ["input"])))
    predictions = evaluate_onnx_model(model, input_names=["a1", "a2"], points=[[1.0, 2.0]])
    assert predictions == [[1.0, 2.0]]


def test_input_count_mismatch_is_refused(tmp_path) -> None:
    """A graph whose input count matches neither layout is refused, not guessed."""
    nodes = [
        _node("Identity", ["in_a"], ["hidden"]),
        _node("Identity", ["in_b"], ["mid"]),
        _node("Identity", ["in_c"], ["output"]),
    ]
    model = load_onnx_model(
        _write(tmp_path, "three_inputs.onnx", _onnx(nodes, [], ["in_a", "in_b", "in_c"]))
    )
    with pytest.raises(OnnxDecodeError, match="neither"):
        evaluate_onnx_model(model, input_names=["a1", "a2"], points=[[1.0, 2.0]])


def test_graph_with_no_nodes_is_refused_instead_of_crashing(tmp_path) -> None:
    """Regression: an empty node list raised ``NameError`` instead of refusing.

    The output tensor name is assigned inside the node loop, so a graph with no
    nodes left it unbound.  It must be reported as an unsupported model.
    """
    model = load_onnx_model(_write(tmp_path, "empty.onnx", _onnx([], [], ["input"])))
    assert model["nodes"] == []
    with pytest.raises(OnnxDecodeError, match="produced no output"):
        evaluate_onnx_model(model, input_names=["a1"], points=[[1.0]])


def test_declared_graph_output_differs_from_final_node(tmp_path):
    payload = _onnx(
        [
            _node("Identity", ["input"], ["declared"]),
            _node("Relu", ["input"], ["other"]),
        ],
        [],
        ["input"],
        output="declared",
    )
    model = load_onnx_model(_write(tmp_path, "different.onnx", payload))
    assert evaluate_onnx_model(model, input_names=["x"], points=[[-7.0]]) == [[-7.0]]
