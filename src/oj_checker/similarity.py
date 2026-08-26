import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from itertools import combinations, repeat

from oj_checker.domain import Submission
from oj_checker.review_basis import ReviewBasis
from oj_checker.submission_store import SourceBundle, matches_submission_pattern


@dataclass(frozen=True, slots=True)
class BaselineDeltaFile:
    path: str
    added_text: str
    removed_text: str


@dataclass(frozen=True, slots=True)
class BaselineDelta:
    submission_id: str
    files: tuple[BaselineDeltaFile, ...]
    incomplete: bool
    digest: str


class BaselineDeltaBuilder:
    def build(self, bundle: SourceBundle, basis: ReviewBasis) -> BaselineDelta:
        baseline_by_path = {file.path: file.text() for file in basis.files}
        delta_files = []
        for source_file in bundle.files:
            if source_file.truncated:
                continue
            baseline = baseline_by_path.get(source_file.path, "")
            if source_file.content == baseline:
                continue
            added, removed = _changed_lines(baseline, source_file.content)
            if added or removed:
                delta_files.append(
                    BaselineDeltaFile(
                        path=source_file.path,
                        added_text=added,
                        removed_text=removed,
                    )
                )
        source_paths = {file.path for file in bundle.files}
        for baseline_file in basis.files:
            if baseline_file.path in source_paths:
                continue
            if any(
                matches_submission_pattern(baseline_file.path, pattern)
                for pattern in (*bundle.required_patterns, *bundle.allowed_patterns)
            ):
                delta_files.append(
                    BaselineDeltaFile(
                        path=baseline_file.path,
                        added_text="",
                        removed_text=baseline_file.text(),
                    )
                )
        delta_files.sort(key=lambda file: file.path)
        frozen_files = tuple(delta_files)
        return BaselineDelta(
            submission_id=bundle.submission_id,
            files=frozen_files,
            incomplete=any(file.truncated for file in bundle.files),
            digest=_delta_digest(frozen_files),
        )


class SimilaritySignal(StrEnum):
    EXACT_SUBMISSION = "exact_submission"
    EXACT_DELTA = "exact_delta"
    MINHASH = "minhash"


@dataclass(frozen=True, slots=True)
class SimilarityPolicy:
    jaccard_threshold: float = 0.7
    shingle_size: int = 5
    num_permutations: int = 64
    band_size: int = 4
    tokenizer_version: str = "code-tokens-v1"

    def __post_init__(self) -> None:
        if not 0 <= self.jaccard_threshold <= 1:
            raise ValueError("jaccard_threshold must be between zero and one")
        if self.shingle_size <= 0:
            raise ValueError("shingle_size must be positive")
        if self.num_permutations <= 0 or self.band_size <= 0:
            raise ValueError("MinHash dimensions must be positive")
        if self.num_permutations % self.band_size:
            raise ValueError("num_permutations must be divisible by band_size")


@dataclass(frozen=True, slots=True)
class SimilarityDocument:
    submission: Submission
    delta: BaselineDelta

    def __post_init__(self) -> None:
        if self.submission.id != self.delta.submission_id:
            raise ValueError("submission and baseline delta IDs do not match")


@dataclass(frozen=True, slots=True)
class SimilarityCandidate:
    lab_id: str
    submission_ids: tuple[str, str]
    signal: SimilaritySignal
    jaccard: float
    delta_digests: tuple[str, str]


@dataclass(frozen=True, slots=True)
class SimilarityExclusion:
    submission_id: str
    lab_id: str
    reason: str
    skipped_layers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidates: tuple[SimilarityCandidate, ...]
    exclusions: tuple[SimilarityExclusion, ...]


class SimilarityDetector:
    def __init__(self, *, max_workers: int = 1) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self._max_workers = max_workers

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def detect(
        self,
        documents: Iterable[SimilarityDocument],
        policy: SimilarityPolicy,
    ) -> CandidateSet:
        ordered = tuple(
            sorted(
                documents,
                key=lambda item: (
                    item.submission.lab_id,
                    item.submission.submitted_at,
                    item.submission.id,
                ),
            )
        )
        shingles = tuple(_delta_shingles(document.delta, policy) for document in ordered)
        selected: dict[tuple[str, str], SimilarityCandidate] = {}

        self._add_exact_candidates(
            ordered,
            shingles,
            selected,
            key=lambda item: item.submission.input_digest,
            eligible=lambda item: bool(item.delta.files) or item.delta.incomplete,
            require_shingles=False,
            signal=SimilaritySignal.EXACT_SUBMISSION,
        )
        self._add_exact_candidates(
            ordered,
            shingles,
            selected,
            key=lambda item: item.delta.digest,
            eligible=lambda item: bool(item.delta.files) and not item.delta.incomplete,
            require_shingles=True,
            signal=SimilaritySignal.EXACT_DELTA,
        )

        signatures = self._signatures(ordered, shingles, policy.num_permutations)
        buckets: dict[tuple[str, int, tuple[int, ...]], list[int]] = defaultdict(list)
        band_count = policy.num_permutations // policy.band_size
        for index, signature in enumerate(signatures):
            if not signature:
                continue
            for band in range(band_count):
                start = band * policy.band_size
                key = (
                    ordered[index].submission.lab_id,
                    band,
                    signature[start : start + policy.band_size],
                )
                buckets[key].append(index)

        approximate_pairs: set[tuple[int, int]] = set()
        for members in buckets.values():
            for left_index, right_index in combinations(sorted(set(members)), 2):
                approximate_pairs.add((left_index, right_index))

        for first_index, second_index in sorted(approximate_pairs):
            first = ordered[first_index]
            second = ordered[second_index]
            if first.submission.owner == second.submission.owner:
                continue
            pair_key = _pair_key(first.submission, second.submission)
            if pair_key in selected:
                continue
            score = _jaccard(shingles[first_index], shingles[second_index])
            if score >= policy.jaccard_threshold:
                selected[pair_key] = _candidate(
                    first,
                    second,
                    SimilaritySignal.MINHASH,
                    score,
                )

        candidates = tuple(
            sorted(
                selected.values(),
                key=lambda item: (item.lab_id, item.submission_ids),
            )
        )
        exclusions = tuple(
            SimilarityExclusion(
                submission_id=document.submission.id,
                lab_id=document.submission.lab_id,
                reason="incomplete_baseline_delta",
                skipped_layers=("exact_delta", "minhash_lsh"),
            )
            for document in ordered
            if document.delta.incomplete
        )
        return CandidateSet(
            candidates,
            exclusions,
        )

    def _signatures(
        self,
        documents: tuple[SimilarityDocument, ...],
        shingles: tuple[frozenset[tuple[str, ...]], ...],
        num_permutations: int,
    ) -> tuple[tuple[int, ...], ...]:
        work = tuple(
            (index, value)
            for index, (document, value) in enumerate(
                zip(documents, shingles, strict=True)
            )
            if value and not document.delta.incomplete
        )
        if not work:
            return tuple(() for _ in documents)

        worker_count = min(self._max_workers, len(work))
        values = tuple(value for _, value in work)
        if worker_count == 1:
            computed = tuple(
                _minhash_signature(value, num_permutations) for value in values
            )
        else:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                computed = tuple(
                    executor.map(
                        _minhash_signature,
                        values,
                        repeat(num_permutations),
                        chunksize=1,
                    )
                )

        signatures: list[tuple[int, ...]] = [() for _ in documents]
        for (index, _), signature in zip(work, computed, strict=True):
            signatures[index] = signature
        return tuple(signatures)

    @staticmethod
    def _add_exact_candidates(
        documents: tuple[SimilarityDocument, ...],
        shingles: tuple[frozenset[tuple[str, ...]], ...],
        selected: dict[tuple[str, str], SimilarityCandidate],
        *,
        key: Callable[[SimilarityDocument], str],
        eligible: Callable[[SimilarityDocument], bool],
        require_shingles: bool,
        signal: SimilaritySignal,
    ) -> None:
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for index, document in enumerate(documents):
            identity = key(document)
            if identity and eligible(document) and (shingles[index] or not require_shingles):
                groups[(document.submission.lab_id, identity)].append(index)

        for members in groups.values():
            representatives: dict[str, int] = {}
            for index in members:
                representatives.setdefault(documents[index].submission.owner, index)
            for first_index, second_index in combinations(representatives.values(), 2):
                first = documents[first_index]
                second = documents[second_index]
                pair_key = _pair_key(first.submission, second.submission)
                if pair_key not in selected:
                    selected[pair_key] = _candidate(
                        first,
                        second,
                        signal,
                        _jaccard(shingles[first_index], shingles[second_index]),
                    )


def _changed_lines(baseline: str, submitted: str) -> tuple[str, str]:
    baseline_lines = baseline.splitlines(keepends=True)
    submitted_lines = submitted.splitlines(keepends=True)
    matcher = SequenceMatcher(None, baseline_lines, submitted_lines, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    for tag, baseline_start, baseline_end, submitted_start, submitted_end in matcher.get_opcodes():
        if tag in {"replace", "insert"}:
            added.extend(submitted_lines[submitted_start:submitted_end])
        if tag in {"replace", "delete"}:
            removed.extend(baseline_lines[baseline_start:baseline_end])
    return "".join(added), "".join(removed)


def _delta_digest(files: tuple[BaselineDeltaFile, ...]) -> str:
    payload = [
        {
            "path": file.path,
            "added_text": file.added_text,
            "removed_text": file.removed_text,
        }
        for file in files
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


_TOKEN_PATTERN = re.compile(
    r"[A-Za-z_]\w*|0[xX][0-9A-Fa-f]+|\d+(?:\.\d+)?|==|!=|<=|>=|->|::|&&|\|\||<<|>>|[^\s]"
)


def _delta_shingles(
    delta: BaselineDelta,
    policy: SimilarityPolicy,
) -> frozenset[tuple[str, ...]]:
    tokens: list[str] = []
    for file in delta.files:
        added_tokens = _TOKEN_PATTERN.findall(file.added_text)
        if not added_tokens:
            continue
        tokens.extend(("__file__", file.path, *added_tokens))
    if not tokens:
        return frozenset()
    if len(tokens) < policy.shingle_size:
        return frozenset({tuple(tokens)})
    return frozenset(
        tuple(tokens[index : index + policy.shingle_size])
        for index in range(len(tokens) - policy.shingle_size + 1)
    )


_MINHASH_PRIME = 18_446_744_073_709_551_557


def _minhash_signature(
    shingles: frozenset[tuple[str, ...]],
    num_permutations: int,
) -> tuple[int, ...]:
    values = tuple(
        int.from_bytes(
            hashlib.blake2b("\0".join(shingle).encode(), digest_size=8).digest(),
            "big",
        )
        for shingle in shingles
    )
    signature = []
    for index in range(num_permutations):
        multiplier = (0x9E3779B185EBCA87 * (index + 1)) % _MINHASH_PRIME or 1
        offset = (0xC2B2AE3D27D4EB4F * (index + 1)) % _MINHASH_PRIME
        signature.append(
            min((multiplier * value + offset) % _MINHASH_PRIME for value in values)
        )
    return tuple(signature)


def _jaccard(
    first: frozenset[tuple[str, ...]],
    second: frozenset[tuple[str, ...]],
) -> float:
    union = first | second
    if not union:
        return 0.0
    return len(first & second) / len(union)


def _pair_key(first: Submission, second: Submission) -> tuple[str, str]:
    if first.id <= second.id:
        return first.id, second.id
    return second.id, first.id


def _candidate(
    first: SimilarityDocument,
    second: SimilarityDocument,
    signal: SimilaritySignal,
    jaccard: float,
) -> SimilarityCandidate:
    ordered = sorted(
        (first, second),
        key=lambda item: (item.submission.submitted_at, item.submission.id),
    )
    return SimilarityCandidate(
        lab_id=first.submission.lab_id,
        submission_ids=(ordered[0].submission.id, ordered[1].submission.id),
        signal=signal,
        jaccard=jaccard,
        delta_digests=(ordered[0].delta.digest, ordered[1].delta.digest),
    )
