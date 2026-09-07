"""Public API for Payload Palette."""

from payload_palette.annotation_adapter import (
    AnnotationAdapter,
    FieldConstraints,
    JSONSerializationOptions,
    schema_for_annotation,
)
from payload_palette.audit import AuditReceipt, audit_manifest
from payload_palette.batch import (
    BatchRecord,
    BatchReport,
    manifest_schema,
    normalize_batch,
    normalize_jsonl,
)
from payload_palette.diff import ManifestDifference, compare_manifests
from payload_palette.errors import OutputError, PayloadValidationError, ValidationIssue
from payload_palette.generation import (
    AsyncGenerationRunner,
    AsyncModelProvider,
    GeneratedResponse,
    GenerationAttempt,
    GenerationFeedback,
    GenerationPolicy,
    GenerationReport,
    GenerationRequest,
    TokenUsage,
)
from payload_palette.ingress import (
    DEFAULT_MAX_INPUT_BYTES,
    MAX_INGRESS_INPUT_BYTES,
    decode_json_bytes,
    normalize_json_bytes,
)
from payload_palette.jsonstream import StreamStatistics
from payload_palette.models import (
    ENVELOPE_NAMES,
    LARGE_VALUE_PREFIX_CHARACTERS,
    MAX_MODEL_INTEGER_DIGITS,
    EnvelopeName,
    LargeValue,
    Manifest,
    NormalizedPart,
)
from payload_palette.normalizer import normalize, validate
from payload_palette.output_schema import (
    JSONScalar,
    JSONValue,
    OutputContractError,
    OutputLimits,
    OutputPath,
    OutputSchema,
)
from payload_palette.output_validation import (
    OutputReport,
    OutputValidator,
    RuleBinding,
    RuleContext,
    RuleOutcome,
    RuleResult,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
)
from payload_palette.policy import NormalizationPolicy, RemoteURLPolicy
from payload_palette.schema_io import (
    SCHEMA_DIALECT,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    export_output_schema,
    load_output_schema,
    load_output_schema_json_bytes,
)
from payload_palette.streaming import (
    decode_stream,
    normalize_path,
    normalize_stream,
)

__all__ = [
    "DEFAULT_MAX_INPUT_BYTES",
    "ENVELOPE_NAMES",
    "LARGE_VALUE_PREFIX_CHARACTERS",
    "MAX_INGRESS_INPUT_BYTES",
    "MAX_MODEL_INTEGER_DIGITS",
    "SCHEMA_DIALECT",
    "AnnotationAdapter",
    "AsyncGenerationRunner",
    "AsyncModelProvider",
    "AuditReceipt",
    "BatchRecord",
    "BatchReport",
    "EnvelopeName",
    "FieldConstraints",
    "GeneratedResponse",
    "GenerationAttempt",
    "GenerationFeedback",
    "GenerationPolicy",
    "GenerationReport",
    "GenerationRequest",
    "JSONScalar",
    "JSONSerializationOptions",
    "JSONValue",
    "LargeValue",
    "Manifest",
    "ManifestDifference",
    "NormalizationPolicy",
    "NormalizedPart",
    "OutputContractError",
    "OutputError",
    "OutputLimits",
    "OutputPath",
    "OutputReport",
    "OutputSchema",
    "OutputValidator",
    "PayloadValidationError",
    "RemoteURLPolicy",
    "RuleBinding",
    "RuleContext",
    "RuleOutcome",
    "RuleResult",
    "SchemaDefinitionError",
    "SchemaDefinitionLimits",
    "StreamStatistics",
    "StringChoices",
    "TokenUsage",
    "TrimmedString",
    "ValidationIssue",
    "ValidationPipeline",
    "audit_manifest",
    "compare_manifests",
    "decode_json_bytes",
    "decode_stream",
    "export_output_schema",
    "load_output_schema",
    "load_output_schema_json_bytes",
    "manifest_schema",
    "normalize",
    "normalize_batch",
    "normalize_json_bytes",
    "normalize_jsonl",
    "normalize_path",
    "normalize_stream",
    "schema_for_annotation",
    "validate",
]

__version__ = "0.5.0"
