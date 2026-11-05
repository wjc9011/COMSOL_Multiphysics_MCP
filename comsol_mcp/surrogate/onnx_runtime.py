"""Solver-free ONNX reader and forward evaluator for exported COMSOL DNNs.

This module never imports COMSOL, Java, MPh, or any third-party library.  It
decodes the ONNX protobuf that COMSOL's DNN ``export(path)`` produces and runs a
forward pass, so an exported surrogate can be evaluated and its predictions
compared with a fresh FEM result.

Why this exists: a trained COMSOL DNN function cannot be evaluated without a
resolved dataset, and a Point dataset cannot be created in a geometry-free
surrogate model.  Evaluating the exported artifact is therefore the path that
yields genuine model predictions, and it additionally proves the exported file
is a working network rather than inert bytes.

Only the protobuf subset ONNX actually uses here is implemented: a minimal
wire-format reader plus the ``ModelProto``/``GraphProto``/``NodeProto``/
``TensorProto`` fields COMSOL emits.  Unsupported operators are refused rather
than approximated.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"
MAX_MODEL_BYTES = 512 * 1024 * 1024
MAX_NODES = 512
MAX_INITIALIZERS = 512
MAX_TENSOR_ELEMENTS = 4 * 1024 * 1024
MAX_EVALUATION_POINTS = 4096

# ONNX TensorProto.DataType values used by COMSOL exports.
_TENSOR_DATA_TYPES = {1: "float32", 11: "float64"}

# Operators this evaluator implements.  COMSOL emits a plain MLP: an optional
# input scale, an optional input bias, then alternating Gemm/activation pairs.
SUPPORTED_OPERATORS = ("Mul", "Add", "Gemm", "Tanh", "Sigmoid", "Relu", "Identity")


class OnnxDecodeError(ValueError):
    """Raised when an ONNX payload cannot be decoded or evaluated."""


# --------------------------------------------------------------------------
# Minimal protobuf wire-format reader
# --------------------------------------------------------------------------


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(payload):
            raise OnnxDecodeError("truncated varint")
        byte = payload[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise OnnxDecodeError("varint is too long")


def _read_fields(payload: bytes) -> list[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)`` triples for one message."""
    fields: list[tuple[int, int, Any]] = []
    offset = 0
    length = len(payload)
    while offset < length:
        key, offset = _read_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 0x07
        # A varint field yields an int while the fixed and length-delimited wire
        # types yield bytes slices, so the value is typed as the union of both
        # rather than inferred from the first branch.
        value: int | bytes
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
        elif wire_type == 1:
            if offset + 8 > length:
                raise OnnxDecodeError("truncated fixed64")
            value = payload[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            size, offset = _read_varint(payload, offset)
            if offset + size > length:
                raise OnnxDecodeError("truncated length-delimited field")
            value = payload[offset : offset + size]
            offset += size
        elif wire_type == 5:
            if offset + 4 > length:
                raise OnnxDecodeError("truncated fixed32")
            value = payload[offset : offset + 4]
            offset += 4
        else:
            raise OnnxDecodeError(f"unsupported wire type {wire_type}")
        fields.append((field_number, wire_type, value))
    return fields


def _field_strings(fields: Sequence[tuple[int, int, Any]], number: int) -> list[str]:
    return [
        value.decode("utf-8", errors="strict")
        for field, wire, value in fields
        if field == number and wire == 2
    ]


def _field_ints(fields: Sequence[tuple[int, int, Any]], number: int) -> list[int]:
    values: list[int] = []
    for field, wire, value in fields:
        if field != number:
            continue
        if wire == 0:
            values.append(int(value))
        elif wire == 2:
            offset = 0
            while offset < len(value):
                item, offset = _read_varint(value, offset)
                values.append(item)
    return values


def _field_messages(fields: Sequence[tuple[int, int, Any]], number: int) -> list[bytes]:
    return [value for field, wire, value in fields if field == number and wire == 2]


def _decode_tensor(payload: bytes) -> dict[str, Any]:
    fields = _read_fields(payload)
    dims = _field_ints(fields, 1)
    data_types = _field_ints(fields, 2)
    names = _field_strings(fields, 8)
    raw = [value for field, wire, value in fields if field == 9 and wire == 2]
    data_type = data_types[0] if data_types else 1
    if data_type not in _TENSOR_DATA_TYPES:
        raise OnnxDecodeError(f"unsupported tensor data type {data_type}")
    typed_data: list[float] = []
    data_field, data_wire, data_format, data_width = (
        (4, 5, "<f", 4) if data_type == 1 else (10, 1, "<d", 8)
    )
    for field, wire, value in fields:
        if field == data_field and wire in (data_wire, 2):
            if len(value) % data_width:
                raise OnnxDecodeError("packed tensor data has an incomplete element")
            typed_data.extend(item[0] for item in struct.iter_unpack(data_format, value))
    count = 1
    for dimension in dims:
        count *= int(dimension)
    if count > MAX_TENSOR_ELEMENTS:
        raise OnnxDecodeError("tensor exceeds the element limit")
    if raw:
        element = _TENSOR_DATA_TYPES[data_type]
        fmt = "<f" if element == "float32" else "<d"
        width = 4 if element == "float32" else 8
        available = len(raw[0]) // width
        values = [
            struct.unpack_from(fmt, raw[0], index * width)[0]
            for index in range(min(available, max(count, 0)))
        ]
    elif typed_data:
        values = typed_data
    else:
        values = []
    if count and len(values) != count:
        raise OnnxDecodeError(f"tensor declares {count} elements but carries {len(values)}")
    if any(not math.isfinite(value) for value in values):
        raise OnnxDecodeError("tensor carries a non-finite value")
    return {
        "name": names[0] if names else "",
        "dims": [int(dimension) for dimension in dims],
        "data_type": _TENSOR_DATA_TYPES[data_type],
        "values": values,
    }


def _decode_attribute(payload: bytes) -> tuple[str, Any]:
    """Decode one AttributeProto into ``(name, value)``.

    Only the value kinds ONNX Gemm actually uses are decoded: float, int, and
    string.  A Gemm exported from PyTorch carries ``transB=1``, which changes
    whether the stored weight matrix is ``[in, out]`` or ``[out, in]``; reading
    it is required for a correct forward pass.
    """
    fields = _read_fields(payload)
    names = _field_strings(fields, 1)
    name = names[0] if names else ""
    for field, wire, value in fields:
        if field == 2 and wire == 5:
            return name, struct.unpack("<f", value)[0]
        if field == 3 and wire == 0:
            return name, int(value)
        if field == 4 and wire == 2:
            return name, value.decode("utf-8", errors="strict")
    return name, None


def _decode_node(payload: bytes) -> dict[str, Any]:
    fields = _read_fields(payload)
    op_types = _field_strings(fields, 4)
    names = _field_strings(fields, 3)
    attributes: dict[str, Any] = {}
    for attribute in _field_messages(fields, 5):
        name, value = _decode_attribute(attribute)
        if name:
            attributes[name] = value
    return {
        "op_type": op_types[0] if op_types else "",
        "name": names[0] if names else "",
        "inputs": _field_strings(fields, 1),
        "outputs": _field_strings(fields, 2),
        "attributes": attributes,
    }


def _decode_value_info(payload: bytes) -> dict[str, Any]:
    """Decode one ValueInfoProto; only the tensor name is needed here."""
    fields = _read_fields(payload)
    names = _field_strings(fields, 1)
    return {"name": names[0] if names else ""}


def _decode_graph(payload: bytes) -> dict[str, Any]:
    fields = _read_fields(payload)
    raw_nodes = _field_messages(fields, 1)
    if len(raw_nodes) > MAX_NODES:
        raise OnnxDecodeError("graph exceeds the node limit")
    nodes = [_decode_node(node) for node in raw_nodes]
    names = _field_strings(fields, 2)
    raw_initializers = _field_messages(fields, 5)
    if len(raw_initializers) > MAX_INITIALIZERS:
        raise OnnxDecodeError("graph exceeds the initializer limit")
    initializers = [_decode_tensor(item) for item in raw_initializers]
    graph_inputs = [_decode_value_info(item) for item in _field_messages(fields, 11)]
    graph_outputs = [_decode_value_info(item) for item in _field_messages(fields, 12)]
    return {
        "name": names[0] if names else "",
        "nodes": nodes,
        "initializers": initializers,
        "inputs": [item["name"] for item in graph_inputs if item["name"]],
        "outputs": [item["name"] for item in graph_outputs if item["name"]],
    }


def load_onnx_model(path: str | Path) -> dict[str, Any]:
    """Decode an ONNX model into a bounded, inspectable Python structure."""
    resolved = Path(path)
    if not resolved.is_file():
        raise OnnxDecodeError(f"onnx model not found: {resolved}")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_MODEL_BYTES:
        raise OnnxDecodeError(f"onnx model size {size} is outside the allowed range")
    payload = resolved.read_bytes()
    model_fields = _read_fields(payload)
    graphs = _field_messages(model_fields, 7)
    if not graphs:
        raise OnnxDecodeError("onnx payload has no graph")
    graph = _decode_graph(graphs[0])
    if len(graph["nodes"]) > MAX_NODES:
        raise OnnxDecodeError("graph exceeds the node limit")
    if len(graph["initializers"]) > MAX_INITIALIZERS:
        raise OnnxDecodeError("graph exceeds the initializer limit")

    producers = _field_strings(model_fields, 2)
    initializers: dict[str, dict[str, Any]] = {}
    for tensor in graph["initializers"]:
        if not tensor["name"]:
            raise OnnxDecodeError("an initializer has no name")
        initializers[tensor["name"]] = tensor

    operators = sorted({node["op_type"] for node in graph["nodes"]})
    unsupported = [op for op in operators if op not in SUPPORTED_OPERATORS]
    if unsupported:
        raise OnnxDecodeError(f"unsupported operators: {', '.join(unsupported)}")

    body = {
        "schema": "comsol_mcp.surrogate_onnx_model",
        "schema_version": SCHEMA_VERSION,
        "producer": producers[0] if producers else "",
        "graph_name": graph["name"],
        "operators": operators,
        "node_count": len(graph["nodes"]),
        "graph_inputs": graph["inputs"],
        "graph_outputs": graph["outputs"],
        "initializer_names": sorted(initializers),
        "initializer_shapes": {
            name: tensor["dims"] for name, tensor in sorted(initializers.items())
        },
        "size_bytes": size,
        "sha256": __import__("hashlib").sha256(payload).hexdigest(),
    }
    return {
        **body,
        "model_sha256": canonical_sha256_v1(body),
        "nodes": graph["nodes"],
        "initializers": initializers,
    }


# --------------------------------------------------------------------------
# Forward evaluation
# --------------------------------------------------------------------------


def _dense(
    input_vector: Sequence[float],
    weights: Sequence[float],
    bias: Sequence[float],
    weight_dims: Sequence[int],
    *,
    trans_b: bool,
    alpha: float = 1.0,
    beta: float = 1.0,
) -> list[float]:
    """Apply one Gemm: ``output = input @ W`` with an optional broadcast bias.

    With ``trans_b`` the stored weight tensor is ``[out, in]`` (PyTorch's
    convention, which COMSOL exports); otherwise it is ``[in, out]``.
    """
    if len(weight_dims) != 2:
        raise OnnxDecodeError("Gemm weights must be two-dimensional")
    first, second = int(weight_dims[0]), int(weight_dims[1])
    rows, columns = (second, first) if trans_b else (first, second)
    if rows != len(input_vector):
        raise OnnxDecodeError(f"Gemm expects {rows} inputs but received {len(input_vector)}")
    if len(weights) != rows * columns:
        raise OnnxDecodeError("Gemm weight tensor size does not match its shape")
    if bias and len(bias) not in (columns, 1):
        raise OnnxDecodeError("Gemm bias size does not match its output width")
    output = [0.0] * columns
    for row in range(rows):
        value = input_vector[row]
        if value == 0.0:
            continue
        for column in range(columns):
            weight = weights[column * rows + row] if trans_b else weights[row * columns + column]
            output[column] += alpha * value * weight
    if bias:
        if len(bias) == 1:
            offset = beta * bias[0]
            output = [item + offset for item in output]
        else:
            output = [item + beta * bias[index] for index, item in enumerate(output)]
    return output


def evaluate_onnx_model(
    model: Mapping[str, Any],
    *,
    input_names: Sequence[str],
    points: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Evaluate a decoded ONNX model at bounded points.

    ``input_names`` names the model's input features in order; each point must
    supply one value per name.  Only the MLP structure COMSOL emits is handled,
    so an unexpected graph is refused rather than silently mis-evaluated.
    """
    if not isinstance(model, Mapping):
        raise OnnxDecodeError("model must be a mapping")
    if "nodes" not in model or "initializers" not in model:
        raise OnnxDecodeError("model must come from load_onnx_model")
    if not input_names:
        raise OnnxDecodeError("input_names must be non-empty")
    names = [str(name) for name in input_names]
    if len(set(names)) != len(names):
        raise OnnxDecodeError("input_names must be unique")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)) or not points:
        raise OnnxDecodeError("points must be a non-empty sequence")
    if len(points) > MAX_EVALUATION_POINTS:
        raise OnnxDecodeError("too many evaluation points")

    initializers = model["initializers"]
    nodes = model["nodes"]
    # COMSOL emits a single batched input tensor named generically ("input")
    # whose width is the feature count, so the whole point vector is bound to
    # that one tensor.  A graph that instead declares one tensor per feature is
    # supported too.  Anything else is refused rather than silently mis-bound.
    graph_inputs = [str(name) for name in (model.get("graph_inputs") or [])]
    # ``binding`` maps a graph input tensor name to the caller feature names it
    # carries.  Every value is a list: the single batched tensor carries all
    # features, while in the per-feature layout each tensor carries exactly one.
    # Binding a bare string here would make the consumer iterate its characters.
    binding: dict[str, list[str]] = {}
    if len(graph_inputs) == 1:
        binding = {graph_inputs[0]: list(names)}
    elif len(graph_inputs) == len(names):
        binding = {tensor: [feature] for tensor, feature in zip(graph_inputs, names)}
    elif not graph_inputs:
        binding = {}
    else:
        raise OnnxDecodeError(
            f"graph declares {len(graph_inputs)} inputs, which matches neither a "
            f"single batched input nor {len(names)} per-feature inputs"
        )
    outputs: list[list[float]] = []
    for index, point in enumerate(points):
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)):
            raise OnnxDecodeError(f"points[{index}] must be a sequence")
        if len(point) != len(names):
            raise OnnxDecodeError(
                f"points[{index}] supplies {len(point)} values for {len(names)} inputs"
            )
        bound_values: dict[str, float] = {name: float(value) for name, value in zip(names, point)}
        values: dict[str, list[float]] = {name: [value] for name, value in bound_values.items()}
        # Assigned inside the loop below.  It is initialized so an empty graph is
        # refused as an unsupported model instead of raising ``NameError``.
        target: str | None = None
        for node in nodes:
            op = node["op_type"]
            inputs = node["inputs"]
            # ``target`` is pre-declared above the loop purely so an empty graph
            # cannot raise ``NameError``; it is reassigned here each iteration.
            target = node["outputs"][0] if node["outputs"] else None
            if target is None:
                raise OnnxDecodeError(f"node {node['name']!r} produces no output")

            def _resolve(name: str) -> list[float]:
                if name in values:
                    return values[name]
                if name in initializers:
                    return [float(item) for item in initializers[name]["values"]]
                # A graph input tensor is bound to caller features by position.
                bound = binding.get(name)
                if bound is not None:
                    return [bound_values[item] for item in bound]
                raise OnnxDecodeError(f"unknown ONNX value {name!r}")

            resolved = [_resolve(name) for name in inputs]
            if op == "Gemm":
                if len(resolved) < 2:
                    raise OnnxDecodeError("Gemm requires weights")
                if node.get("attributes", {}).get("transA", 0) != 0:
                    raise OnnxDecodeError("Gemm transA is unsupported for point-vector evaluation")
                weight_name = inputs[1]
                if weight_name not in initializers:
                    raise OnnxDecodeError("Gemm weights must come from an initializer")
                weight_dims = initializers[weight_name]["dims"]
                bias = resolved[2] if len(resolved) > 2 else []
                values[target] = _dense(
                    resolved[0],
                    resolved[1],
                    bias,
                    weight_dims,
                    trans_b=bool(int(node.get("attributes", {}).get("transB") or 0)),
                    alpha=float(node.get("attributes", {}).get("alpha", 1.0)),
                    beta=float(node.get("attributes", {}).get("beta", 1.0)),
                )
            elif op == "Mul":
                if len(resolved) != 2:
                    raise OnnxDecodeError("Mul requires two operands")
                left, right = resolved
                if len(left) == 1:
                    values[target] = [item * left[0] for item in right]
                elif len(right) == 1:
                    values[target] = [item * right[0] for item in left]
                elif len(left) == len(right):
                    values[target] = [a * b for a, b in zip(left, right)]
                else:
                    raise OnnxDecodeError("Mul operands are not broadcastable")
            elif op == "Add":
                if len(resolved) != 2:
                    raise OnnxDecodeError("Add requires two operands")
                left, right = resolved
                if len(left) == 1:
                    values[target] = [item + left[0] for item in right]
                elif len(right) == 1:
                    values[target] = [item + right[0] for item in left]
                elif len(left) == len(right):
                    values[target] = [a + b for a, b in zip(left, right)]
                else:
                    raise OnnxDecodeError("Add operands are not broadcastable")
            elif op == "Tanh":
                values[target] = [math.tanh(item) for item in resolved[0]]
            elif op == "Sigmoid":
                values[target] = [
                    (
                        1.0 / (1.0 + math.exp(-item))
                        if item >= 0
                        else math.exp(item) / (1.0 + math.exp(item))
                    )
                    for item in resolved[0]
                ]
            elif op == "Relu":
                values[target] = [max(0.0, item) for item in resolved[0]]
            elif op == "Identity":
                values[target] = list(resolved[0])
            else:
                raise OnnxDecodeError(f"unsupported operator {op!r}")

        # The guard inside the loop already refuses a node without an output, but
        # narrowing does not survive the loop, so the lookup is guarded again here.
        graph_outputs = [str(name) for name in (model.get("graph_outputs") or [])]
        if len(graph_outputs) > 1:
            raise OnnxDecodeError("multiple graph outputs are not supported")
        output_name = graph_outputs[0] if graph_outputs else target
        final = values.get(output_name) if output_name is not None else None
        if final is None:
            raise OnnxDecodeError("graph produced no output")
        if any(not math.isfinite(item) for item in final):
            raise OnnxDecodeError("evaluation produced a non-finite value")
        outputs.append(list(final))
    return outputs


__all__ = [
    "MAX_EVALUATION_POINTS",
    "OnnxDecodeError",
    "SCHEMA_VERSION",
    "SUPPORTED_OPERATORS",
    "evaluate_onnx_model",
    "load_onnx_model",
]
