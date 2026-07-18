"""Pure, bounded secret-safe egress decisions for every model-visible payload."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import Enum


class EgressDecision(str, Enum):
    ALLOW = "allow"
    REDACT_RANGES = "redact_ranges"
    WITHHOLD_SNIPPET = "withhold_snippet"
    REJECT_RESULT = "reject_result"


class EgressContentClass(str, Enum):
    SOURCE_SNIPPET = "source_snippet"
    FINDING = "finding"
    LOG = "log"
    DIAGNOSTIC = "diagnostic"
    RECORDING = "recording"
    TRACE = "trace"
    ATTESTATION = "attestation"
    STRUCTURED_FIELD = "structured_field"


class EgressDestination(str, Enum):
    MODEL = "model"
    OPERATOR_UI = "operator_ui"
    TRACE = "trace"
    RECORDING = "recording"
    DIAGNOSTIC = "diagnostic"
    ATTESTATION = "attestation"


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    max_input_bytes: int = 1_000_000
    max_output_chars: int = 8_000
    max_output_lines: int = 500
    withhold_private_keys: bool = True
    reject_binary: bool = True

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_input_bytes", self.max_input_bytes, 20_000_000),
            ("max_output_chars", self.max_output_chars, 1_000_000),
            ("max_output_lines", self.max_output_lines, 20_000),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if not isinstance(self.withhold_private_keys, bool):
            raise ValueError("withhold_private_keys must be boolean")
        if not isinstance(self.reject_binary, bool):
            raise ValueError("reject_binary must be boolean")


@dataclass(frozen=True, slots=True)
class EgressRequest:
    content: str | bytes
    content_class: EgressContentClass
    destination: EgressDestination
    source_path: str | None = None
    source_symbol: str | None = None
    snapshot_identity: str | None = None
    encoding: str = "utf-8"
    source_denied: bool = False
    explicit_secrets: tuple[str, ...] = ()
    allow_values: tuple[str, ...] = ()
    policy: EgressPolicy = field(default_factory=EgressPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.content, (str, bytes)):
            raise ValueError("content must be text or bytes")
        if not isinstance(self.content_class, EgressContentClass):
            raise ValueError("content_class must be an EgressContentClass")
        if not isinstance(self.destination, EgressDestination):
            raise ValueError("destination must be an EgressDestination")
        if not isinstance(self.source_denied, bool):
            raise ValueError("source_denied must be boolean")
        for name, value, limit in (
            ("source_path", self.source_path, 4_096),
            ("source_symbol", self.source_symbol, 1_024),
            ("snapshot_identity", self.snapshot_identity, 256),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > limit
            ):
                raise ValueError(f"{name} must be null or a bounded non-empty string")
        if not isinstance(self.encoding, str) or not self.encoding or len(self.encoding) > 64:
            raise ValueError("encoding must be a bounded non-empty string")
        for name, values in (
            ("explicit_secrets", self.explicit_secrets),
            ("allow_values", self.allow_values),
        ):
            if not isinstance(values, tuple) or len(values) > 128:
                raise ValueError(f"{name} must be a bounded tuple")
            if any(not isinstance(item, str) or not item or len(item) > 16_384 for item in values):
                raise ValueError(f"{name} contains an invalid value")


@dataclass(frozen=True, slots=True, order=True)
class EgressRange:
    start: int
    end: int
    category: str
    finding_id: str


@dataclass(frozen=True, slots=True, order=True)
class EgressFinding:
    finding_id: str
    category: str
    confidence: str
    start: int | None
    end: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class EgressResult:
    decision: EgressDecision
    content: str | None
    findings: tuple[EgressFinding, ...]
    redaction_ranges: tuple[EgressRange, ...]
    redaction_count: int
    withheld_lines: int
    truncated: bool
    source_digest: str
    reason: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    category: str
    confidence: str
    reason: str


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9])")),
    ("openai", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])")),
    ("aws", re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")),
    ("slack", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{16,}(?![A-Za-z0-9])")),
    ("npm", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{20,}(?![A-Za-z0-9])")),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>authorization|control_plane_api_key|api[_-]?key|access[_-]?token|token|secret|password|passwd|credential)\b"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]+)")
_URL_CREDENTIALS = re.compile(r"(?i)https?://[^/@\s:]+:(?P<value>[^@/\s]+)@")
_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9_./+-]{32,}={0,2})(?![A-Za-z0-9])")
_SHA_HEX = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
_INTEGRITY = re.compile(r"sha(?:256|384|512)-[A-Za-z0-9+/=]{20,}")
_PUBLIC_URL_BODY = re.compile(
    r"[A-Za-z0-9.-]+\.(?:com|org|net|io|dev|test|local|ai|app)(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?"
)
_SAFE_SELECTOR = re.compile(
    r"(?:check-run|operation|task|workspace|restore|backup)-[A-Za-z0-9:._-]{1,128}"
)
# Matches RepoForge's own internal compound identifiers (e.g. workspace_id
# "<task-slug>-<hex-suffix>", plan_id "plan-<hex>", operation_id "op-<hex>"):
# lowercase-alnum-and-hyphen only, so it cannot match base64/JWT/mixed-case
# secret shapes. A long slug-plus-hex identifier can otherwise cross the
# high-entropy threshold and be falsely redacted as a secret.
_COMPOUND_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SANITIZED_MARKER = re.compile(
    r"<(?:redacted(?::[A-Za-z0-9_+.-]+)?|withheld:[A-Za-z0-9_+.-]+|reject_result:[^<>\r\n]+)>"
)
_SENSITIVE_KEYS = {
    "authorization",
    "control_plane_api_key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
}
_SAFE_IDENTITY_KEYS = {
    "checksum",
    "digest",
    "fingerprint",
    "hash",
    "head_sha",
    "manifest_checksum",
    "oid",
    "plan_digest",
    "selector",
    "sha",
    "sha256",
    "source_digest",
    "tool_surface_hash",
    "workspace_fingerprint",
}


def _source_bytes(request: EgressRequest) -> tuple[bytes, str | None]:
    if isinstance(request.content, bytes):
        return request.content, None
    try:
        return request.content.encode(request.encoding, errors="strict"), request.content
    except (LookupError, UnicodeError):
        return request.content.encode("utf-8", errors="replace"), None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finding_id(source_digest: str, candidate: _Candidate) -> str:
    identity = (
        f"{source_digest}:{candidate.category}:{candidate.start}:{candidate.end}:"
        f"{candidate.confidence}:{candidate.reason}"
    ).encode()
    return "secret-" + hashlib.sha256(identity).hexdigest()[:24]


def _synthetic_finding(
    source_digest: str,
    *,
    category: str,
    reason: str,
    confidence: str = "high",
) -> EgressFinding:
    identity = hashlib.sha256(
        f"{source_digest}:{category}:{confidence}:{reason}".encode()
    ).hexdigest()[:24]
    return EgressFinding(f"secret-{identity}", category, confidence, None, None, reason)


def _looks_high_entropy(value: str) -> bool:
    if len(value) < 32 or len(set(value)) < 12:
        return False
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    if classes < 2:
        return False
    counts = {character: value.count(character) for character in set(value)}
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value)) for count in counts.values()
    )
    return entropy >= 4.5 and len(counts) / len(value) >= 0.3


def _safe_candidate(value: str, allow_values: frozenset[str]) -> bool:
    url_body = value[2:] if value.startswith("//") else value
    return (
        value in allow_values
        or _SHA_HEX.fullmatch(value) is not None
        or _UUID.fullmatch(value) is not None
        or _INTEGRITY.fullmatch(value) is not None
        or _PUBLIC_URL_BODY.fullmatch(url_body) is not None
        or _SAFE_SELECTOR.fullmatch(value) is not None
    )


def _add_candidate(
    candidates: list[_Candidate],
    *,
    start: int,
    end: int,
    category: str,
    confidence: str,
    reason: str,
    allow_values: frozenset[str],
    text: str,
) -> None:
    if start < 0 or end <= start or end > len(text):
        return
    value = text[start:end]
    if value in allow_values or _SANITIZED_MARKER.fullmatch(value) is not None:
        return
    candidates.append(_Candidate(start, end, category, confidence, reason))


def _detect(text: str, request: EgressRequest) -> tuple[_Candidate, ...]:
    allow_values = frozenset(request.allow_values)
    candidates: list[_Candidate] = []

    for match in _PRIVATE_KEY.finditer(text):
        _add_candidate(
            candidates,
            start=match.start(),
            end=match.end(),
            category="private_key",
            confidence="high",
            reason="private-key marker",
            allow_values=allow_values,
            text=text,
        )
    for provider, pattern in _PROVIDER_PATTERNS:
        for match in pattern.finditer(text):
            _add_candidate(
                candidates,
                start=match.start(),
                end=match.end(),
                category="provider_token",
                confidence="high",
                reason=f"approved {provider} token pattern",
                allow_values=allow_values,
                text=text,
            )
    for match in _BEARER.finditer(text):
        start, end = match.span("value")
        _add_candidate(
            candidates,
            start=start,
            end=end,
            category="authorization",
            confidence="high",
            reason="bearer authorization value",
            allow_values=allow_values,
            text=text,
        )
    for match in _URL_CREDENTIALS.finditer(text):
        start, end = match.span("value")
        _add_candidate(
            candidates,
            start=start,
            end=end,
            category="credential_url",
            confidence="high",
            reason="credential-bearing URL",
            allow_values=allow_values,
            text=text,
        )
    for match in _SECRET_ASSIGNMENT.finditer(text):
        start, end = match.span("value")
        key = match.group("key").casefold().replace("-", "_")
        category = (
            "authorization"
            if key == "authorization"
            else "password"
            if key in {"password", "passwd"}
            else "sensitive_config"
        )
        _add_candidate(
            candidates,
            start=start,
            end=end,
            category=category,
            confidence="high",
            reason=f"sensitive assignment key: {key}",
            allow_values=allow_values,
            text=text,
        )
    for secret in sorted(set(request.explicit_secrets), key=len, reverse=True):
        if secret in allow_values:
            continue
        cursor = 0
        while True:
            index = text.find(secret, cursor)
            if index < 0:
                break
            _add_candidate(
                candidates,
                start=index,
                end=index + len(secret),
                category="explicit_secret",
                confidence="high",
                reason="caller-provided secret identity",
                allow_values=allow_values,
                text=text,
            )
            cursor = index + max(1, len(secret))
    for match in _TOKEN_CANDIDATE.finditer(text):
        candidate = match.group(1)
        if _safe_candidate(candidate, allow_values) or not _looks_high_entropy(candidate):
            continue
        start, end = match.span(1)
        _add_candidate(
            candidates,
            start=start,
            end=end,
            category="high_entropy",
            confidence="medium",
            reason="contextual high-entropy token",
            allow_values=allow_values,
            text=text,
        )

    return tuple(
        sorted(
            set(candidates),
            key=lambda item: (item.start, item.end, item.category, item.reason),
        )
    )


def _merge_ranges(
    candidates: tuple[_Candidate, ...], source_digest: str
) -> tuple[tuple[EgressRange, ...], tuple[EgressFinding, ...]]:
    findings = tuple(
        EgressFinding(
            _finding_id(source_digest, item),
            item.category,
            item.confidence,
            item.start,
            item.end,
            item.reason,
        )
        for item in candidates
    )
    if not candidates:
        return (), findings

    merged: list[tuple[int, int, set[str], list[str]]] = []
    for candidate, finding in zip(candidates, findings, strict=True):
        if not merged or candidate.start >= merged[-1][1]:
            merged.append(
                (candidate.start, candidate.end, {candidate.category}, [finding.finding_id])
            )
            continue
        start, end, categories, ids = merged[-1]
        categories.add(candidate.category)
        ids.append(finding.finding_id)
        merged[-1] = (start, max(end, candidate.end), categories, ids)

    ranges = tuple(
        EgressRange(
            start,
            end,
            "+".join(sorted(categories)),
            hashlib.sha256(":".join(sorted(ids)).encode("utf-8")).hexdigest()[:24],
        )
        for start, end, categories, ids in merged
    )
    return ranges, findings


def _redact(text: str, ranges: tuple[EgressRange, ...]) -> str:
    if not ranges:
        return text
    parts: list[str] = []
    cursor = 0
    for item in ranges:
        parts.append(text[cursor : item.start])
        parts.append(f"<redacted:{item.category}>")
        cursor = item.end
    parts.append(text[cursor:])
    return "".join(parts)


def _bound_lines(value: str, maximum: int) -> tuple[str, int, bool]:
    lines = value.splitlines(keepends=True)
    if len(lines) <= maximum:
        return value, 0, False
    head_count = max(1, maximum // 2)
    tail_count = max(0, maximum - head_count - 1)
    omitted = len(lines) - head_count - tail_count
    marker = f"... <{omitted} lines omitted> ...\n"
    tail = lines[-tail_count:] if tail_count else []
    return "".join((*lines[:head_count], marker, *tail)), omitted, True


def _bound_chars(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    marker = "... <characters omitted> ..."
    available = max(0, maximum - len(marker))
    head = available // 2
    tail = available - head
    bounded = value[:head] + marker + (value[-tail:] if tail else "")
    return bounded[:maximum], True


def evaluate_egress(request: EgressRequest) -> EgressResult:
    """Return one deterministic decision without exposing detected secret values."""

    data, encoded_text = _source_bytes(request)
    source_digest = _digest(data)
    if len(data) > request.policy.max_input_bytes:
        finding = _synthetic_finding(
            source_digest,
            category="size_limit",
            reason="input exceeds the reviewed byte bound",
        )
        return EgressResult(
            EgressDecision.REJECT_RESULT,
            None,
            (finding,),
            (),
            0,
            0,
            False,
            source_digest,
            "input exceeds policy bound",
        )
    if request.source_denied:
        finding = _synthetic_finding(
            source_digest,
            category="denied_source",
            reason="source path is denied by repository policy",
        )
        return EgressResult(
            EgressDecision.WITHHOLD_SNIPPET,
            None,
            (finding,),
            (),
            0,
            0,
            False,
            source_digest,
            "source is denied",
        )

    text = encoded_text
    if text is None:
        try:
            text = data.decode(request.encoding, errors="strict")
        except (LookupError, UnicodeError):
            text = None
    if text is None or "\x00" in text:
        finding = _synthetic_finding(
            source_digest,
            category="binary_data",
            reason="content is binary or invalid for the declared encoding",
        )
        decision = (
            EgressDecision.REJECT_RESULT
            if request.policy.reject_binary
            else EgressDecision.WITHHOLD_SNIPPET
        )
        return EgressResult(
            decision,
            None,
            (finding,),
            (),
            0,
            0,
            False,
            source_digest,
            "binary or invalid encoding",
        )

    candidates = _detect(text, request)
    ranges, findings = _merge_ranges(candidates, source_digest)
    if request.policy.withhold_private_keys and any(
        item.category == "private_key" for item in findings
    ):
        return EgressResult(
            EgressDecision.WITHHOLD_SNIPPET,
            None,
            findings,
            ranges,
            len(ranges),
            text.count("\n") + 1,
            False,
            source_digest,
            "private-key material is withheld",
        )

    redacted = _redact(text, ranges)
    line_bounded, withheld_lines, lines_truncated = _bound_lines(
        redacted, request.policy.max_output_lines
    )
    bounded, chars_truncated = _bound_chars(line_bounded, request.policy.max_output_chars)
    decision = EgressDecision.REDACT_RANGES if ranges else EgressDecision.ALLOW
    return EgressResult(
        decision,
        bounded,
        findings,
        ranges,
        len(ranges),
        withheld_lines,
        lines_truncated or chars_truncated,
        source_digest,
        "secret ranges redacted" if ranges else "content allowed",
    )


def _normalized_key(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def _safe_identity_field(key: str, value: str) -> bool:
    if key not in _SAFE_IDENTITY_KEYS and not key.endswith(("_sha", "_hash", "_digest", "_id")):
        return False
    return (
        _SHA_HEX.fullmatch(value) is not None
        or _UUID.fullmatch(value) is not None
        or _INTEGRITY.fullmatch(value) is not None
        or _PUBLIC_URL_BODY.fullmatch(value) is not None
        or _SAFE_SELECTOR.fullmatch(value) is not None
        or (0 < len(value) <= 128 and _COMPOUND_IDENTIFIER.fullmatch(value) is not None)
    )


def _structured_path_field(key: str, value: str) -> bool:
    return (
        (key == "path" or key.endswith(("_path", "_paths")))
        and 0 < len(value) <= 4_096
        and not any(ord(character) < 32 for character in value)
        and "://" not in value
    )


def sanitize_egress_data(
    value: object,
    *,
    destination: EgressDestination,
    content_class: EgressContentClass = EgressContentClass.STRUCTURED_FIELD,
    explicit_secrets: tuple[str, ...] = (),
    allow_values: tuple[str, ...] = (),
    policy: EgressPolicy | None = None,
    _key: str = "",
    _depth: int = 0,
) -> object:
    """Recursively sanitize one bounded structured payload before serialization."""

    active_policy = policy or EgressPolicy()
    if _depth > 16:
        return "<withheld:structure-depth>"
    if isinstance(value, dict):
        sanitized_mapping: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 2_000:
                sanitized_mapping["items_truncated"] = True
                break
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS:
                sanitized_mapping[str(key)] = "<redacted:sensitive_config>"
            else:
                sanitized_mapping[str(key)] = sanitize_egress_data(
                    item,
                    destination=destination,
                    content_class=content_class,
                    explicit_secrets=explicit_secrets,
                    allow_values=allow_values,
                    policy=active_policy,
                    _key=normalized,
                    _depth=_depth + 1,
                )
        return sanitized_mapping
    if isinstance(value, (list, tuple)):
        return [
            sanitize_egress_data(
                item,
                destination=destination,
                content_class=content_class,
                explicit_secrets=explicit_secrets,
                allow_values=allow_values,
                policy=active_policy,
                _key=_key,
                _depth=_depth + 1,
            )
            for item in value[:2_000]
        ]
    if isinstance(value, bytes):
        evaluation = evaluate_egress(
            EgressRequest(
                value,
                content_class,
                destination,
                explicit_secrets=explicit_secrets,
                allow_values=allow_values,
                policy=active_policy,
            )
        )
        return (
            evaluation.content
            if evaluation.content is not None
            else f"<{evaluation.decision.value}:{evaluation.reason}>"
        )
    if isinstance(value, str):
        if _safe_identity_field(_key, value):
            return value
        evaluation = evaluate_egress(
            EgressRequest(
                value,
                content_class,
                destination,
                explicit_secrets=explicit_secrets,
                allow_values=allow_values,
                policy=active_policy,
            )
        )
        if (
            _structured_path_field(_key, value)
            and evaluation.findings
            and all(item.category == "high_entropy" for item in evaluation.findings)
        ):
            return value
        return (
            evaluation.content
            if evaluation.content is not None
            else f"<{evaluation.decision.value}:{evaluation.reason}>"
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_egress_data(
        repr(value),
        destination=destination,
        content_class=content_class,
        explicit_secrets=explicit_secrets,
        allow_values=allow_values,
        policy=active_policy,
        _key=_key,
        _depth=_depth + 1,
    )
