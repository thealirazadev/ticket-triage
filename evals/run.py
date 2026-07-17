"""Eval runner: execute the classification pipeline over a labeled dataset and
report per-field accuracy, per-label precision/recall/F1, a confusion summary,
and parse-failure/cost accounting.

Run offline with recorded fixtures (no provider key needed):

    uv run python -m evals.run --fixtures evals/fixtures.jsonl

Or against the real provider (costs money; used by the CI eval job):

    uv run python -m evals.run

The pipeline is the real one (real prompt, real client through TriageResult); a
throwaway in-memory database only exists so per-call cost/latency is recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.models import Base, LlmCall
from app.prompts import PROMPT_VERSION
from app.schemas.triage import INTENTS, PRIORITIES, SENTIMENTS
from app.services.classifier import TriageFailure, classify_ticket
from app.services.llm_client import LlmClient

FIELDS = ("intent", "priority", "sentiment")
LABELS: dict[str, tuple[str, ...]] = {
    "intent": INTENTS,
    "priority": PRIORITIES,
    "sentiment": SENTIMENTS,
}


class EvalConfigError(Exception):
    """A configuration or runtime problem that should exit with code 2."""


@dataclass
class RowResult:
    external_id: str
    gold: dict[str, str]
    pred: dict[str, str] | None
    failure: str | None  # parse_failed | provider_error | circuit_open


@dataclass
class RunOutput:
    results: list[RowResult]
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    elapsed_s: float
    provider_errors: int
    parse_failures: int


# --- data loading -------------------------------------------------------------


def load_dataset(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except OSError as exc:
        raise EvalConfigError(f"cannot read dataset {path}: {exc}") from exc
    if not rows:
        raise EvalConfigError(f"dataset {path} is empty")
    return rows


def dataset_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def load_fixtures(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle if line.strip()]
    except OSError as exc:
        raise EvalConfigError(f"cannot read fixtures {path}: {exc}") from exc
    return {entry["external_id"]: entry["content"] for entry in entries}


# --- pipeline execution -------------------------------------------------------


def _in_memory_session_factory() -> sessionmaker:
    # StaticPool keeps a single connection so the in-memory DB persists across
    # the per-row sessions used to record llm_calls.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _fixture_client(settings: Settings, content: str | None) -> LlmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        text = content if content is not None else ""
        approx_prompt = len(request.content) // 4
        body = {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": approx_prompt, "completion_tokens": 40},
        }
        return httpx.Response(200, json=body)

    return LlmClient(settings, transport=httpx.MockTransport(handler))


def run_pipeline(
    rows: list[dict],
    settings: Settings,
    *,
    fixtures: dict[str, str] | None,
    limit: int | None = None,
    progress: bool = False,
) -> RunOutput:
    session_factory = _in_memory_session_factory()
    shared_client = None if fixtures is not None else LlmClient(settings)
    selected = rows[:limit] if limit else rows
    results: list[RowResult] = []
    provider_errors = 0
    parse_failures = 0
    started = time.monotonic()

    for index, row in enumerate(selected, start=1):
        if progress:
            print(f"  {index}/{len(selected)}", end="\r", file=sys.stderr)
        gold = {f: row[f] for f in FIELDS}
        if fixtures is not None:
            client = _fixture_client(settings, fixtures.get(row["external_id"]))
        else:
            client = shared_client
        pred: dict[str, str] | None = None
        failure: str | None = None
        with session_factory() as db:
            try:
                outcome = classify_ticket(
                    db,
                    client,
                    ticket_id=None,
                    subject=row["subject"],
                    sender=row["sender"],
                    channel=row["channel"],
                    body=row["body"],
                )
                pred = {
                    "intent": outcome.result.intent,
                    "priority": outcome.result.priority,
                    "sentiment": outcome.result.sentiment,
                }
            except TriageFailure as exc:
                failure = exc.reason
                if exc.reason == "parse_failed":
                    parse_failures += 1
                else:
                    provider_errors += 1
            db.commit()
        if fixtures is not None:
            client.close()
        results.append(RowResult(row["external_id"], gold, pred, failure))

    if shared_client is not None:
        shared_client.close()

    calls, input_tokens, output_tokens, cost = _aggregate_calls(session_factory)
    if progress:
        print(" " * 20, end="\r", file=sys.stderr)
    return RunOutput(
        results=results,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        elapsed_s=time.monotonic() - started,
        provider_errors=provider_errors,
        parse_failures=parse_failures,
    )


def _aggregate_calls(session_factory: sessionmaker) -> tuple[int, int, int, float]:
    with session_factory() as db:
        calls = db.query(LlmCall).all()
        input_tokens = sum(c.input_tokens or 0 for c in calls)
        output_tokens = sum(c.output_tokens or 0 for c in calls)
        cost = float(sum(c.cost_usd for c in calls))
        return len(calls), input_tokens, output_tokens, cost


# --- metrics ------------------------------------------------------------------


def field_metrics(pairs: list[tuple[str, str | None]], labels: tuple[str, ...]) -> dict:
    """Accuracy, macro-F1, and per-label precision/recall/F1/support for one
    field. A None prediction (triage failure) counts as wrong everywhere."""
    total = len(pairs)
    correct = sum(1 for gold, pred in pairs if pred is not None and gold == pred)
    accuracy = correct / total if total else 0.0

    per_label: dict[str, dict] = {}
    f1_scores: list[float] = []
    for label in labels:
        tp = sum(1 for gold, pred in pairs if pred == label and gold == label)
        fp = sum(1 for gold, pred in pairs if pred == label and gold != label)
        fn = sum(1 for gold, pred in pairs if gold == label and pred != label)
        support = sum(1 for gold, _ in pairs if gold == label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_label[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
        if support > 0:
            f1_scores.append(f1)
    macro_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0
    return {"accuracy": accuracy, "macro_f1": macro_f1, "labels": per_label}


def confusion_top(
    pairs: list[tuple[str, str | None]], k: int = 5
) -> list[tuple[tuple[str, str], int]]:
    counter = Counter((gold, pred) for gold, pred in pairs if pred is not None and gold != pred)
    return counter.most_common(k)


def compute_metrics(output: RunOutput) -> dict:
    metrics: dict[str, dict] = {}
    for field in FIELDS:
        pairs = [(r.gold[field], (r.pred[field] if r.pred else None)) for r in output.results]
        metrics[field] = field_metrics(pairs, LABELS[field])
        metrics[field]["confusion"] = confusion_top(pairs)
    return metrics


def parse_failure_rate(output: RunOutput) -> float:
    n = len(output.results)
    return output.parse_failures / n if n else 0.0


# --- reporting ----------------------------------------------------------------


def _color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, code: str) -> str:
    if not _color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def render_report(
    output: RunOutput,
    metrics: dict,
    *,
    dataset_path: str,
    sha: str,
    model: str,
    quiet: bool,
    verbose: bool,
) -> str:
    lines: list[str] = []
    n = len(output.results)
    lines.append("ticket-triage eval")
    lines.append(f"  dataset   {dataset_path}  ({n} rows, sha256 {sha[:8]})")
    lines.append(f"  model     {model}     prompt v{PROMPT_VERSION}")
    lines.append("")

    if not quiet:
        for field in FIELDS:
            data = metrics[field]
            lines.append(
                f"{field:<11} accuracy {data['accuracy']:.2f}  macro-F1 {data['macro_f1']:.2f}"
            )
            lines.append("  label             prec   rec    f1     n")
            for label in LABELS[field]:
                stat = data["labels"][label]
                lines.append(
                    f"  {label:<16} {stat['precision']:.2f}   {stat['recall']:.2f}   "
                    f"{stat['f1']:.2f}   {stat['support']}"
                )
            if data["confusion"]:
                summary = ", ".join(
                    f"{gold} -> {pred} x{count}" for (gold, pred), count in data["confusion"]
                )
                lines.append(f"  confusion (top): {summary}")
            lines.append("")

    if verbose:
        for result in output.results:
            if result.pred is None:
                lines.append(f"  {result.external_id}  FAILED ({result.failure})")
                continue
            for field in FIELDS:
                if result.gold[field] != result.pred[field]:
                    lines.append(
                        f"  {result.external_id}  {field} gold={result.gold[field]} "
                        f"pred={result.pred[field]}"
                    )
        lines.append("")

    lines.append(
        f"parse failures: {output.parse_failures} of {n}   "
        f"provider errors: {output.provider_errors}"
    )
    lines.append(
        f"cost: {output.input_tokens:,} in / {output.output_tokens:,} out tokens   "
        f"${output.cost_usd:.4f}   elapsed {output.elapsed_s:.1f}s"
    )
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals.run", description="Run the classification eval.")
    parser.add_argument("--dataset", default="evals/dataset.jsonl")
    parser.add_argument("--fixtures", default=os.environ.get("EVAL_FIXTURES_PATH"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def _require_provider(settings: Settings, fixtures: dict | None) -> None:
    if fixtures is None and not (settings.llm_api_key and settings.llm_base_url):
        raise EvalConfigError(
            "set LLM_API_KEY and LLM_BASE_URL in .env or the environment, "
            "or pass --fixtures for an offline run"
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    try:
        rows = load_dataset(args.dataset)
        sha = dataset_sha256(args.dataset)
        fixtures = load_fixtures(args.fixtures) if args.fixtures else None
        _require_provider(settings, fixtures)
    except EvalConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    output = run_pipeline(
        rows, settings, fixtures=fixtures, limit=args.limit, progress=sys.stderr.isatty()
    )
    metrics = compute_metrics(output)
    report = render_report(
        output,
        metrics,
        dataset_path=args.dataset,
        sha=sha,
        model=settings.llm_model or "(none)",
        quiet=args.quiet,
        verbose=args.verbose,
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
