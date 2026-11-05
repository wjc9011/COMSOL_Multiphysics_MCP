"""A deterministic fake COMSOL backend for adapter contract and parity tests.

Contract and parity tests must run with no COMSOL, no JVM, and no solver lease, so
they need a backend whose behaviour is fully specified.  This fake is that
backend: it records every call, returns scripted values, and can be told to fail
at any operation so the protocol's error translation and rollback paths are
exercised for real rather than mocked away.

It deliberately implements the *same* protocol as the MPh backends by inheriting
:class:`MphBackendBase` rules where they are lane-independent, and it never
imports ``mph``.
"""

from __future__ import annotations

from typing import Any, Sequence

from comsol_mcp.adapter.conversion import convert_explicitly, normalize_evaluation_result
from comsol_mcp.adapter.protocol import (
    DNN_FEATURE_METHODS,
    JAVA_WRITE_KINDS,
    AdapterError,
    ConvertedValue,
    EvaluationRequest,
    JavaTypedRead,
    JavaTypedWrite,
    ModelIdentity,
    NodeRef,
    PropertyWritePlan,
    PropertyWriteReceipt,
    SessionIdentity,
    SessionRequest,
)


class FakeComsolBackend:
    """A scripted backend that satisfies :class:`ComsolAdapter`.

    Parameters
    ----------
    lane:
        Reported lane identity, so a parity test can present two fakes as two
        lanes while keeping everything else identical.
    properties:
        Initial ``{tag: {name: value}}`` content.
    failures:
        ``{operation: reason_code}`` making that operation raise.  Used to prove
        error translation and rollback rather than to simulate success.
    """

    def __init__(
        self,
        *,
        lane: str = "fake",
        properties: dict[str, dict[str, Any]] | None = None,
        failures: dict[str, str] | None = None,
        mph_version: str = "0.0.0-fake",
        comsol_version: str | None = "6.4.0.293-fake",
    ) -> None:
        self.lane = lane
        self.backend_name = f"fake-{lane}"
        self._properties: dict[str, dict[str, Any]] = {
            tag: dict(values) for tag, values in (properties or {}).items()
        }
        self._created_nodes: dict[tuple[str, ...], NodeRef] = {}
        self._failures = dict(failures or {})
        #: Scripted raw evaluation results, keyed by expression.
        self._evaluations: dict[str, Any] = {}
        self._mph_version = mph_version
        self._comsol_version = comsol_version
        self._session: SessionIdentity | None = None
        self._model: ModelIdentity | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Every property write that was actually applied, in order, so a test
        #: can prove a failed write was rolled back rather than merely reported.
        self.applied_writes: list[tuple[str, str, Any]] = []
        #: Scripted ``{kind: [tags]}`` container content, for ``container_tags``.
        self._containers: dict[str, list[str]] = {}
        #: Scripted ``{(tag, name): (accessor, value)}`` typed Java reads.
        self._java_values: dict[tuple[str, str], tuple[str, Any]] = {}
        #: Scripted ``{(tag, name): declared_type}`` results for ``java_value_type``.
        self._java_types: dict[tuple[str, str], str] = {}
        #: Scripted ``{(tag, name): [allowed]}`` results for ``read_allowed_values``.
        self._allowed_values: dict[tuple[str, str], list[str]] = {}
        #: Every typed Java write the backend was asked to perform, in order.
        self.java_writes: list[tuple[str, str, str, Any]] = []
        #: Every feature lifecycle call the backend was asked to perform, in order.
        self.feature_calls: list[tuple[str, str]] = []
        #: Every export the backend was asked to perform, in order.
        self.exports: list[tuple[str, str]] = []
        self.attached = False
        self._client: Any = None
        #: Clients this backend actually cleared, so a test can prove an adopted
        #: client was released rather than cleared.
        self.cleared_clients: list[Any] = []

    # -- test helpers --------------------------------------------------------
    def _record(self, operation: str, **detail: Any) -> None:
        self.calls.append((operation, detail))

    def _maybe_fail(self, operation: str) -> None:
        reason = self._failures.get(operation)
        if reason is not None:
            raise AdapterError(reason, f"scripted failure for {operation}", operation=operation)

    def operations_called(self) -> list[str]:
        return [operation for operation, _ in self.calls]

    # -- step 1: session/client and model identity ---------------------------
    def open_session(self, request: SessionRequest) -> SessionIdentity:
        self._record(
            "session_open",
            cores=request.cores,
            version=request.version,
            host=request.host,
            port=request.port,
        )
        self._maybe_fail("session_open")
        if self._session is not None:
            raise AdapterError(
                "session_already_open",
                "a session is already open",
                operation="session_open",
            )
        self._session = SessionIdentity(
            mph_version=self._mph_version,
            comsol_version=self._comsol_version,
            host=request.host,
            port=request.port,
            standalone=request.standalone,
        )
        return self._session

    def close_session(self) -> None:
        self._record("session_close")
        self._maybe_fail("session_close")
        client = self._client
        attached = self.attached
        self._client = None
        self.attached = False
        self._session = None
        if attached:
            # An adopted client belongs to its creator, so it is dropped without
            # being cleared; the fake records that so the ownership rule is
            # assertable rather than implied.
            return
        clear = getattr(client, "clear", None)
        if callable(clear):
            clear()
            self.cleared_clients.append(client)

    def session_identity(self) -> SessionIdentity | None:
        return self._session

    def load_model(self, path: str) -> ModelIdentity:
        self._record("model_load", path=path)
        self._maybe_fail("model_load")
        self._require_session("model_load")
        self._model = ModelIdentity(name="fake-model", file=path, content_sha256=None)
        return self._model

    def save_model(self, path: str) -> ModelIdentity:
        self._record("model_save", path=path)
        self._maybe_fail("model_save")
        identity = self._require_model("model_save")
        return ModelIdentity(name=identity.name, file=path, content_sha256=None)

    def _require_session(self, operation: str) -> SessionIdentity:
        if self._session is None:
            raise AdapterError("session_not_open", "no session is open", operation=operation)
        return self._session

    def _require_model(self, operation: str) -> ModelIdentity:
        self._require_session(operation)
        if self._model is None:
            raise AdapterError("model_load_failed", "no model is loaded", operation=operation)
        return self._model

    # -- step 2: tag/list/node lookup ---------------------------------------
    def find_node(self, path: Sequence[str]) -> NodeRef:
        self._record("node_lookup", path=list(path))
        self._maybe_fail("node_lookup")
        self._require_model("node_lookup")
        if not path:
            raise AdapterError(
                "node_not_found", "a node path must not be empty", operation="node_lookup"
            )
        # The fake uses the same (kind, tag) convention as the real backends so a
        # test cannot pass against one convention and fail on COMSOL.
        key = path[-1]
        if key not in self._properties:
            raise AdapterError(
                "node_not_found",
                f"no node tagged {key!r}",
                operation="node_lookup",
            )
        return NodeRef(tag=key, path=tuple(path), node_type=path[0] if path else None)

    def children(self, node: NodeRef) -> Sequence[NodeRef]:
        self._record("node_children", tag=node.tag)
        self._maybe_fail("node_children")
        self._require_model("node_children")
        return [
            child for path, child in sorted(self._created_nodes.items()) if path[:-1] == node.path
        ]

    # -- step 3: explicit conversion ----------------------------------------
    def convert(self, value: Any, *, form: str, target: str) -> ConvertedValue:
        self._record("convert_value", form=form, target=target)
        self._maybe_fail("convert_value")
        return convert_explicitly(value, form=form)

    # -- step 4: typed property access with rollback ------------------------
    def read_property(self, node: NodeRef, name: str, *, form: str) -> ConvertedValue:
        self._record("property_read", tag=node.tag, name=name, form=form)
        self._maybe_fail("property_read")
        self._require_model("property_read")
        values = self._properties.get(node.tag)
        if values is None or name not in values:
            raise AdapterError(
                "property_not_found",
                f"no property {name!r} on {node.tag}",
                operation="property_read",
            )
        try:
            return convert_explicitly(values[name], form=form)
        except AdapterError as exc:
            raise AdapterError(
                "property_type_mismatch",
                f"property {name!r} on {node.tag} is not a {form}",
                operation="property_read",
            ) from exc

    def plan_write(self, node: NodeRef, name: str, value: ConvertedValue) -> PropertyWritePlan:
        self._record("property_write", tag=node.tag, name=name, phase="plan")
        self._require_model("property_write")
        # Capturing the previous value is what makes the write reversible; a
        # missing property is therefore not writable at all.
        previous = self.read_property(node, name, form=value.form)
        return PropertyWritePlan(
            node=node, name=name, value=value, previous=previous, rollback_required=True
        )

    def apply_write(self, plan: PropertyWritePlan) -> PropertyWriteReceipt:
        self._record("property_write", tag=plan.node.tag, name=plan.name, phase="apply")
        if "property_write" in self._failures:
            # Emulate a backend that fails mid-write: the value must be restored.
            self._properties[plan.node.tag][plan.name] = plan.previous.value
            return PropertyWriteReceipt(
                node=plan.node,
                name=plan.name,
                applied=False,
                rolled_back=True,
                reason_code=self._failures["property_write"],
            )
        self._properties[plan.node.tag][plan.name] = plan.value.value
        self.applied_writes.append((plan.node.tag, plan.name, plan.value.value))
        return PropertyWriteReceipt(node=plan.node, name=plan.name, applied=True, rolled_back=False)

    def property_value(self, tag: str, name: str) -> Any:
        """Read the fake's raw stored value, for rollback assertions."""
        return self._properties[tag][name]

    # -- step 5: evaluate and dataset/solution access -----------------------
    def evaluate(self, request: EvaluationRequest) -> ConvertedValue:
        self._record("model_evaluate", expression=request.expression)
        self._maybe_fail("model_evaluate")
        self._require_model("model_evaluate")
        # Real MPh returns a numpy array even for a scalar expression, so the fake
        # stores the scripted *shape* and runs the same normalizer the real
        # backends use. That keeps the fake honest about the licensed behaviour
        # instead of returning a convenient bare float.
        value = self._evaluations.get(request.expression, 0.0)
        return normalize_evaluation_result(value)

    def set_evaluation(self, expression: str, value: Any) -> None:
        """Script a raw evaluation result, in the shape a real lane would return."""
        self._evaluations[expression] = value

    def dataset_names(self) -> Sequence[str]:
        self._record("dataset_access")
        self._maybe_fail("dataset_access")
        self._require_model("dataset_access")
        return ["study1//solution1"]

    def solution_names(self) -> Sequence[str]:
        self._record("solution_access")
        self._maybe_fail("solution_access")
        self._require_model("solution_access")
        return ["sol1"]

    # -- step 6: typed Java access and feature operations -------------------
    def attach_client(self, model: Any, *, client: Any = None) -> None:
        """Adopt a caller-owned client, exactly as the MPh backend does.

        The fake records the adoption and opens a synthetic session so node
        operations after an attach behave as they do on a real adopted client.
        Ownership stays with the caller: ``close_session`` must not clear it.
        The client is explicit for the same reason as in the MPh backend: a real
        MPh 1.3.1 model has no back-reference to its owner.
        """
        self._record("session_open", attach=True, model=type(model).__name__)
        self._maybe_fail("session_open")
        if client is None:
            raise AdapterError(
                "adapter_unavailable",
                "attach_client requires the client that owns the model",
                operation="session_open",
            )
        if self._session is not None and not self.attached:
            # Parity with the real backend: attaching over an open
            # self-owned session would leak that client, so it is refused.
            raise AdapterError(
                "adapter_unavailable",
                "attach_client cannot adopt a client while this backend holds "
                "its own open session; close the owned session first",
                operation="session_open",
            )
        self._client = client
        self.attached = True
        if self._session is None:
            self._session = SessionIdentity(
                mph_version=self._mph_version,
                comsol_version=self._comsol_version,
                host=None,
                port=None,
                standalone=True,
            )

    def adopted_client(self) -> Any:
        return self._client if self.attached else None

    @staticmethod
    def _tag_of(node: Any) -> str:
        """Address a node by tag, accepting both reference forms."""
        tag = getattr(node, "tag", None)
        if not isinstance(tag, str):
            tag = getattr(node, "label", None)
        if not isinstance(tag, str):
            raise AdapterError(
                "node_not_found",
                f"the fake cannot address a {type(node).__name__}",
                operation="node_lookup",
            )
        return tag

    def container_tags(self, kind: str) -> Sequence[str]:
        self._record("node_children", kind=kind)
        self._maybe_fail("node_children")
        self._require_model("node_children")
        if kind not in self._containers:
            raise AdapterError(
                "node_not_found",
                f"the fake does not enumerate {kind!r} tags",
                operation="node_children",
            )
        return list(self._containers[kind])

    def set_container(self, kind: str, tags: Sequence[str]) -> None:
        """Script one enumerable container's content."""
        self._containers[kind] = [str(tag) for tag in tags]

    def create_node(
        self,
        kind: str,
        tag: str,
        *,
        parent_tag: str | None = None,
        feature_type: str | None = None,
    ) -> NodeRef:
        self._record("node_lookup", phase="create", kind=kind, tag=tag)
        self._maybe_fail("node_lookup")
        self._require_model("node_lookup")
        if kind not in {"study", "function", "study_step"}:
            raise AdapterError(
                "node_not_found",
                f"the fake does not create {kind!r} nodes",
                operation="node_lookup",
            )
        if kind in {"function", "study_step"} and feature_type is None:
            raise AdapterError(
                "conversion_not_representable",
                f"creating a {kind} requires an explicit feature type",
                operation="node_lookup",
            )
        path: tuple[str, ...]
        if kind == "study_step":
            if parent_tag is None:
                raise AdapterError(
                    "conversion_not_representable",
                    "creating a study step requires a parent study",
                    operation="node_lookup",
                )
            path = ("study", parent_tag, tag)
        else:
            path = (kind, tag)
            self._containers.setdefault(kind, []).append(tag)
        self._properties.setdefault(tag, {})
        node = NodeRef(tag=tag, path=path, node_type=feature_type)
        self._created_nodes[path] = node
        return node

    def remove_node(self, kind: str, tag: str) -> None:
        self._record("node_lookup", phase="remove", kind=kind, tag=tag)
        self._maybe_fail("node_lookup")
        self._require_model("node_lookup")
        if kind not in {"study", "function", "study_step"}:
            raise AdapterError(
                "node_not_found",
                f"the fake does not remove {kind!r} nodes",
                operation="node_lookup",
            )
        # COMSOL's ``remove`` is tolerant of an absent tag; the fake matches that
        # so a rollback of an already-removed node is not reported as a failure.
        if kind == "study_step":
            parent, separator, child = tag.partition("/")
            if separator and parent in self._containers.get("study", []):
                self._properties.pop(child, None)
                self._created_nodes.pop(("study", parent, child), None)
            return
        prefix = (kind, tag)
        for path in list(self._created_nodes):
            if path[:2] == prefix:
                removed_node = self._created_nodes.pop(path)
                self._properties.pop(removed_node.tag, None)
        if tag in self._containers.get(kind, []):
            self._containers[kind].remove(tag)
        self._properties.pop(tag, None)

    def java_typed_write(self, request: JavaTypedWrite) -> None:
        self._record("java_typed_write", name=request.name, kind=request.kind)
        self._maybe_fail("java_typed_write")
        if request.kind not in JAVA_WRITE_KINDS:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported Java write kind: {request.kind}",
                operation="java_typed_write",
            )
        if request.kind == "string_entry" and not isinstance(request.value, dict):
            raise AdapterError(
                "conversion_not_representable",
                "string_entry requires a mapping of key to value",
                operation="java_typed_write",
            )
        tag = self._tag_of(request.node)
        self.java_writes.append((tag, request.name, request.kind, request.value))
        self._java_values[(tag, request.name)] = ("set", request.value)

    def java_typed_read(self, node: Any, name: str, *, form: str = "auto") -> JavaTypedRead:
        self._record("property_read", name=name, kind="java_typed")
        self._maybe_fail("property_read")
        tag = self._tag_of(node)
        scripted = self._java_values.get((tag, name))
        if scripted is None:
            raise AdapterError(
                "property_not_found",
                f"no typed accessor accepted {name!r} on {tag}",
                operation="property_read",
                rejected=("getString: JException", "getDouble: JException"),
            )
        accessor, value = scripted
        accessors = {
            "string": "getString",
            "boolean": "getBoolean",
            "int": "getInt",
            "double": "getDouble",
            "string_array": "getStringArray",
            "int_array": "getIntArray",
            "double_array": "getDoubleArray",
            "string_matrix": "getStringMatrix",
        }
        if form != "auto" and form not in accessors:
            raise AdapterError(
                "conversion_not_representable", f"unsupported typed read form: {form}"
            )
        if form != "auto" and accessor != accessors[form]:
            raise AdapterError(
                "property_not_found",
                f"{name!r} on {tag} is not a string-array property",
                operation="property_read",
                rejected=(f"{accessor}: wrong form",),
            )
        return JavaTypedRead(node=node, name=name, accessor=accessor, value=value, rejected=())

    def java_string_array(self, node: Any, name: str) -> list[str]:
        read = self.java_typed_read(node, name, form="string_array")
        value = read.value
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    def set_java_value(self, tag: str, name: str, accessor: str, value: Any) -> None:
        """Script one typed Java read result."""
        self._java_values[(tag, name)] = (accessor, value)

    def set_java_type(self, tag: str, name: str, declared_type: str) -> None:
        """Script one property's declared Java value type."""
        self._java_types[(tag, name)] = declared_type

    def java_value_type(self, node: Any, name: str) -> str | None:
        self._record("property_read", name=name, kind="value_type")
        self._maybe_fail("property_read")
        return self._java_types.get((self._tag_of(node), name))

    def read_allowed_values(self, node: Any, name: str) -> list[str] | None:
        self._record("property_read", name=name, kind="allowed_values")
        self._maybe_fail("property_read")
        allowed = self._allowed_values.get((self._tag_of(node), name))
        return None if allowed is None else list(allowed)

    def set_allowed_values(self, tag: str, name: str, allowed: Sequence[str]) -> None:
        """Script one enumerated property's allowed values."""
        self._allowed_values[(tag, name)] = [str(item) for item in allowed]

    def run_feature(self, node: Any, *, method: str) -> None:
        self._record("dnn_feature_run", method=method)
        self._maybe_fail("dnn_feature_run")
        if method not in DNN_FEATURE_METHODS:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported feature method: {method}",
                operation="dnn_feature_run",
            )
        self.feature_calls.append((self._tag_of(node), method))

    def export_feature(self, node: Any, path: str) -> None:
        self._record("dnn_feature_export", path=path)
        self._maybe_fail("dnn_feature_export")
        self.exports.append((self._tag_of(node), str(path)))


__all__ = ["FakeComsolBackend"]
