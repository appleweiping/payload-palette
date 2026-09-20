"""A bounded, explicitly tagged oneOf import profile."""

from __future__ import annotations

import asyncio
import copy
import json
from io import BytesIO, StringIO, TextIOWrapper
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from payload_palette import (
    AsyncGenerationRunner,
    GeneratedResponse,
    GenerationPolicy,
    GenerationRequest,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    RuleBinding,
    RuleContext,
    RuleResult,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    TokenUsage,
    ValidationPipeline,
    export_output_config,
    export_output_schema,
    load_output_config,
    load_output_schema,
    load_output_schema_json_bytes,
)
from payload_palette.cli import run


def _tagged_document() -> dict[str, object]:
    return {
        "$defs": {
            "Cat": {
                "title": "Cat",
                "type": "object",
                "properties": {
                    "kind": {"title": "Kind", "type": "string", "const": "cat"},
                    "meows": {"title": "Meows", "type": "integer"},
                },
                "required": ["kind", "meows"],
            },
            "Dog": {
                "title": "Dog",
                "type": "object",
                "properties": {
                    "kind": {"title": "Kind", "type": "string", "const": "dog"},
                    "barks": {"title": "Barks", "type": "number"},
                },
                "required": ["kind", "barks"],
            },
        },
        "oneOf": [{"$ref": "#/$defs/Cat"}, {"$ref": "#/$defs/Dog"}],
        "discriminator": {
            "propertyName": "kind",
            "mapping": {"cat": "#/$defs/Cat", "dog": "#/$defs/Dog"},
        },
    }


def test_pydantic_shaped_tagged_union_imports_and_validates() -> None:
    document = _tagged_document()
    original = copy.deepcopy(document)
    schema = load_output_schema(document)
    assert document == original
    assert schema.validate({"kind": "cat", "meows": 2}) == ()
    assert schema.validate({"kind": "dog", "barks": 2.5}) == ()
    assert [issue.code for issue in schema.validate({"kind": "other"})] == ["schema_one_of"]
    assert schema.discriminator_property == "kind"
    assert dict(schema.discriminator_map) == {"cat": 0, "dog": 1}
    document["discriminator"]["mapping"]["cat"] = "#/$defs/Dog"  # type: ignore[index]
    assert schema.validate({"kind": "cat", "meows": 2}) == ()


def test_tagged_one_of_matches_draft_2020_12_and_normalized_export() -> None:
    document = _tagged_document()
    tagged = load_output_schema(document)
    normalized = export_output_schema(tagged)
    assert "discriminator" not in normalized
    assert "title" not in json.dumps(normalized)
    reloaded = load_output_schema(normalized)
    assert reloaded.discriminator_property is None
    candidates: list[object] = [None, [], 0, "cat"]
    for kind in ("cat", "dog", "other", 1, None):
        for fields in (
            {},
            {"meows": 2},
            {"meows": "2"},
            {"barks": 2.5},
            {"barks": "bad"},
            {"meows": 2, "barks": 2.5},
            {"meows": 2, "private": "x"},
        ):
            candidates.append({"kind": kind, **fields})
    candidates.extend([{}, {"meows": 2}, {"kind": "cat", "meows": 2.0}])
    original_oracle = Draft202012Validator(document)
    normalized_oracle = Draft202012Validator(normalized)
    accepted = rejected = 0
    for candidate in candidates:
        expected = original_oracle.is_valid(candidate)
        assert normalized_oracle.is_valid(candidate) is expected
        assert (tagged.validate(candidate) == ()) is expected
        assert (reloaded.validate(candidate) == ()) is expected
        accepted += expected
        rejected += not expected
    assert len(candidates) >= 30 and accepted >= 3 and rejected >= 20


@pytest.mark.parametrize(
    "case",
    [
        "wrong_reference",
        "missing_map_entry",
        "extra_map_entry",
        "unknown_reference",
        "remote_reference",
        "inline_branch",
        "repeated_branch",
        "overlapping_tag",
        "optional_tag",
        "nonliteral_tag",
        "numeric_tag",
        "wrong_property_name",
        "mapping_list",
        "mapping_257",
        "branches_18",
        "branch_all_of",
        "unsupported_sibling",
        "unsupported_description",
        "recursive_branch",
        "title_surrogate",
        "title_too_long",
    ],
)
def test_tagged_profile_rejects_ambiguous_or_unsupported_documents(case: str) -> None:
    document: dict[str, Any] = _tagged_document()
    definitions = document["$defs"]
    discriminator = document["discriminator"]
    mapping = discriminator["mapping"]
    if case == "wrong_reference":
        mapping["cat"] = "#/$defs/Dog"
    elif case == "missing_map_entry":
        del mapping["dog"]
    elif case == "extra_map_entry":
        mapping["other"] = "#/$defs/Cat"
    elif case == "unknown_reference":
        mapping["cat"] = "#/$defs/Unknown"
    elif case == "remote_reference":
        mapping["cat"] = "https://example.test/Cat"
    elif case == "inline_branch":
        document["oneOf"][0] = definitions["Cat"]
    elif case == "repeated_branch":
        document["oneOf"][1] = {"$ref": "#/$defs/Cat"}
    elif case == "overlapping_tag":
        definitions["Dog"]["properties"]["kind"]["const"] = "cat"
    elif case == "optional_tag":
        definitions["Dog"]["required"] = ["barks"]
    elif case == "nonliteral_tag":
        del definitions["Dog"]["properties"]["kind"]["const"]
    elif case == "numeric_tag":
        definitions["Dog"]["properties"]["kind"] = {"type": "integer", "const": 1}
    elif case == "wrong_property_name":
        discriminator["propertyName"] = "species"
    elif case == "mapping_list":
        discriminator["mapping"] = []
    elif case == "mapping_257":
        discriminator["mapping"] = {str(index): "#/$defs/Cat" for index in range(257)}
    elif case == "branches_18":
        document["oneOf"] = document["oneOf"] * 9
    elif case == "branch_all_of":
        document["oneOf"][0] = {"allOf": [{"$ref": "#/$defs/Cat"}, {"type": "object"}]}
    elif case == "unsupported_sibling":
        document["type"] = "object"
    elif case == "unsupported_description":
        definitions["Cat"]["description"] = "not in profile"
    elif case == "recursive_branch":
        definitions["Cat"]["properties"]["child"] = {"$ref": "#/$defs/Cat"}
    elif case == "title_surrogate":
        definitions["Cat"]["title"] = "\ud800"
    elif case == "title_too_long":
        definitions["Cat"]["title"] = "x" * 2_049
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(document)


def test_nested_tagged_union_and_multiple_tags_for_one_branch() -> None:
    base: dict[str, Any] = _tagged_document()
    base["$defs"]["Dog"]["properties"]["kind"] = {
        "type": "string",
        "enum": ["dog", "hound"],
    }
    base["discriminator"]["mapping"]["hound"] = "#/$defs/Dog"
    document = {
        "$defs": base["$defs"],
        "type": "object",
        "properties": {"pet": {"oneOf": base["oneOf"], "discriminator": base["discriminator"]}},
        "required": ["pet"],
    }
    schema = load_output_schema(document)
    oracle = Draft202012Validator(document)
    for candidate in (
        {"pet": {"kind": "dog", "barks": 1}},
        {"pet": {"kind": "hound", "barks": 2}},
        {"pet": {"kind": "cat", "meows": 3}},
        {"pet": {"kind": "hound", "meows": 3}},
    ):
        assert (schema.validate(candidate) == ()) is oracle.is_valid(candidate)
    issues = schema.validate({"pet": {"kind": "private-tag", "private-key": "value"}})
    assert [(issue.code, issue.path) for issue in issues] == [("schema_one_of", '$["pet"]')]
    assert "private-tag" not in str(issues) and "private-key" not in str(issues)


def test_invalid_mapping_reference_error_path_omits_tag_value() -> None:
    document: dict[str, Any] = _tagged_document()
    document["discriminator"]["mapping"]["private-tag"] = "https://example.test/remote"
    with pytest.raises(SchemaDefinitionError) as caught:
        load_output_schema(document)
    assert "private-tag" not in str(caught.value)


@pytest.mark.parametrize("placement", ["discriminator", "branch"])
def test_nonstring_nested_schema_keys_are_rejected_without_hashing_again(
    placement: str,
) -> None:
    class HostileKey:
        armed = False
        calls = 0

        def __init__(self, collision_hash: int) -> None:
            self.collision_hash = collision_hash

        def __hash__(self) -> int:
            if self.armed:
                self.calls += 1
                raise RuntimeError("untrusted key was hashed after admission")
            return self.collision_hash

        def __eq__(self, other: object) -> bool:
            if self.armed:
                self.calls += 1
                raise RuntimeError("untrusted key was compared after admission")
            return self is other

    document: dict[str, Any] = _tagged_document()
    key = HostileKey(hash("mapping" if placement == "discriminator" else "$ref"))
    if placement == "discriminator":
        document["discriminator"][key] = "unexpected"
    else:
        document["oneOf"][0][key] = "unexpected"
    key.armed = True
    with pytest.raises(SchemaDefinitionError, match="schema_definition_discriminator"):
        load_output_schema(document)
    assert key.calls == 0


def test_tagged_branch_and_title_budgets_are_shared_and_exact() -> None:
    document = _tagged_document()
    schema = load_output_schema(document)
    assert (
        schema.validate({"kind": "cat", "meows": 2}, limits=OutputLimits(max_schema_steps=4)) == ()
    )
    with pytest.raises(OutputContractError, match="work"):
        schema.validate({"kind": "cat", "meows": 2}, limits=OutputLimits(max_schema_steps=3))
    assert [
        issue.code
        for issue in schema.validate({"kind": "unknown"}, limits=OutputLimits(max_schema_steps=1))
    ] == ["schema_one_of"]
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(document, limits=SchemaDefinitionLimits(max_nodes=2))
    with pytest.raises(SchemaDefinitionError, match="branches"):
        load_output_schema(
            {**document, "oneOf": document["oneOf"] * 2},
            limits=SchemaDefinitionLimits(max_branches=2),
        )
    assert (
        load_output_schema(
            {"type": "string", "title": "abc"}, limits=SchemaDefinitionLimits(max_characters=3)
        ).kind
        == "string"
    )
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(
            {"type": "string", "title": "abc"}, limits=SchemaDefinitionLimits(max_characters=2)
        )


def test_tagged_constructor_rejects_forged_map_and_copies_valid_map() -> None:
    branches = load_output_schema(_tagged_document()).one_of
    with pytest.raises(ValueError, match="match"):
        OutputSchema(
            "one_of",
            one_of=branches,
            discriminator_property="kind",
            discriminator_map={"cat": 1, "dog": 0},
        )
    mapping = {"cat": 0, "dog": 1}
    schema = OutputSchema(
        "one_of", one_of=branches, discriminator_property="kind", discriminator_map=mapping
    )
    mapping["cat"] = 1
    assert schema.validate({"kind": "cat", "meows": 1}) == ()
    with pytest.raises(TypeError):
        schema.discriminator_map["cat"] = 1  # type: ignore[index]


def test_direct_tagged_constructor_never_compares_untrusted_mapping_key() -> None:
    class HostileKey:
        armed = False
        calls = 0

        def __hash__(self) -> int:
            if self.armed:
                self.calls += 1
                raise RuntimeError("untrusted mapping key was hashed")
            return hash("dog")

        def __eq__(self, other: object) -> bool:
            if self.armed:
                self.calls += 1
                raise RuntimeError("untrusted mapping key was compared")
            return self is other

    branches = load_output_schema(_tagged_document()).one_of
    key = HostileKey()
    mapping: dict[object, object] = {"cat": 0, key: 1}
    key.armed = True
    with pytest.raises(ValueError, match="discriminator_map"):
        OutputSchema(
            "one_of",
            one_of=branches,
            discriminator_property="kind",
            discriminator_map=mapping,  # type: ignore[arg-type]
        )
    assert key.calls == 0


@pytest.mark.parametrize("candidate", [{"kind": "private-tag"}, {"kind": "cat"}])
def test_tagged_error_under_dynamic_property_redacts_to_declared_ancestor(
    candidate: dict[str, object],
) -> None:
    tagged = load_output_schema(_tagged_document())
    schema = OutputSchema(
        "object",
        properties={
            "events": OutputSchema(
                "object",
                additional_properties=OutputSchema(
                    "object", properties={"detail": tagged}, required=("detail",)
                ),
            )
        },
        required=("events",),
    )
    raw = {"events": {"private-key": {"detail": candidate}}}
    issues = schema.validate(raw)
    assert [(issue.code, issue.path) for issue in issues] == [("schema_one_of", '$["events"]')]
    report = ValidationPipeline(schema).validate(raw)
    assert not report.valid and report.output is None
    assert "private-key" not in str(report.to_dict())
    assert "private-tag" not in str(report.to_dict())


def test_tagged_branch_must_target_direct_object_definition_not_ref_alias() -> None:
    document: dict[str, Any] = _tagged_document()
    document["$defs"]["AliasCat"] = {"$ref": "#/$defs/Cat"}
    document["oneOf"][0] = {"$ref": "#/$defs/AliasCat"}
    document["discriminator"]["mapping"]["cat"] = "#/$defs/AliasCat"
    with pytest.raises(SchemaDefinitionError, match="schema_definition_discriminator"):
        load_output_schema(document)


def test_tagged_failure_is_private_and_generation_hint_is_portable() -> None:
    class Provider:
        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            assert "oneOf" in request.schema
            assert "discriminator" not in request.schema
            return GeneratedResponse(
                '{"kind":"topsecret","private-key":"private-value"}', TokenUsage(0, 0)
            )

    schema = load_output_schema(_tagged_document())
    candidate = {"kind": "topsecret", "private-key": "private-value"}
    report = ValidationPipeline(schema).validate(candidate)
    assert not report.valid and report.output is None
    assert [(issue.code, issue.path) for issue in report.issues] == [("schema_one_of", "$")]
    assert "topsecret" not in str(report.to_dict())
    assert "private-key" not in str(report.to_dict())
    result = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(schema), Provider(), GenerationPolicy(max_attempts=1)
        ).run("go")
    )
    assert [(feedback.code, feedback.path) for feedback in result.attempts[0].feedback] == [
        ("schema_one_of", "$")
    ]
    assert "topsecret" not in str(result.to_dict())
    assert "private-key" not in str(result.to_dict())


def test_tagged_final_revalidation_and_config_cli_roundtrip(tmp_path: Any) -> None:
    class Switch:
        def __init__(self, replacement: dict[str, object]) -> None:
            self.replacement = replacement

        def check(self, value: Any, context: RuleContext) -> RuleResult:
            if value == {"kind": "cat", "meows": 1}:
                return RuleResult(False, "switch", "switch branch", self.replacement)
            return RuleResult(True)

    schema = load_output_schema(_tagged_document())
    for replacement, accepted in (({"kind": "dog", "barks": 1.5}, True), ({"kind": "dog"}, False)):
        pipeline = ValidationPipeline(
            schema, (RuleBinding("switch", (), Switch(replacement), "fix"),)
        )
        report = pipeline.validate({"kind": "cat", "meows": 1})
        assert report.valid is accepted
        if not accepted:
            assert any(issue.code == "schema_one_of" for issue in report.issues)
    config = export_output_config(ValidationPipeline(schema))
    reloaded = load_output_config(config)
    assert reloaded.validate({"kind": "dog", "barks": 1.5}).valid
    path = tmp_path / "tagged.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    for raw, accepted in ((b'{"kind":"cat","meows":1}', True), (b'{"kind":"dog"}', False)):
        stdin = TextIOWrapper(BytesIO(raw), encoding="ascii")
        stdout, stderr = StringIO(), StringIO()
        status = run(["validate-output", str(path), "-"], stdin=stdin, stdout=stdout, stderr=stderr)
        assert status == (0 if accepted else 1)
        assert stderr.getvalue() == ""
        assert json.loads(stdout.getvalue())["valid"] is accepted


def test_tagged_json_byte_ingress_rejects_duplicate_mapping_key() -> None:
    document = _tagged_document()
    payload = (
        json.dumps(document)
        .encode("utf-8")
        .replace(b'"cat": "#/$defs/Cat"', b'"cat": "#/$defs/Cat", "cat": "#/$defs/Dog"')
    )
    with pytest.raises(ValueError):
        load_output_schema_json_bytes(payload)
