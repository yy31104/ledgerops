"""Refit compact scope scoring offline from consumed evidence packs."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.support import PassageSupport, _question_parts
from ledgerops_assistant.support_relation import (
    FOCUSED_WITNESS_LOG_ODDS_SHIFT,
    RelationFeatures,
    relation_score,
    witness_score,
)
from ledgerops_assistant.support_veto import (
    CONDITIONAL_FOCUSED_WITNESS_LOG_ODDS_SHIFT,
    VetoFeatures,
    has_fronted_or_coordinated_inquiry,
)
from scripts.evaluate_v13 import ROOT, select_threshold


def fit_logistic(features, labels, *, weights=None, intercept_ridge=1e-6, ridge=2.0, prior=None):
    """Fit L2-regularized logistic regression and convert to raw feature units."""
    if weights is None:
        center = features.mean(axis=0)
        scale = np.maximum(features.std(axis=0), 1e-5)
        weights = np.ones(len(features))
    else:
        center = np.average(features, axis=0, weights=weights)
        variance = np.average((features - center) ** 2, axis=0, weights=weights)
        scale = np.sqrt(np.maximum(variance, 1e-8))
    design = np.column_stack([np.ones(len(features)), (features - center) / scale])
    coefficients = np.zeros(design.shape[1])
    penalties = np.array([intercept_ridge] + [ridge] * features.shape[1])
    target = np.zeros_like(coefficients)
    if prior is not None:
        intercept, slopes = prior
        target = np.r_[intercept + slopes @ center, slopes * scale]
    for _ in range(35):
        fitted = _sigmoid(design @ coefficients)
        curvature = design.T @ ((weights * fitted * (1 - fitted))[:, None] * design)
        curvature += np.diag(penalties)
        gradient = design.T @ ((fitted - labels) * weights) + penalties * (coefficients - target)
        step = np.linalg.solve(curvature, gradient)
        coefficients -= step
        if np.max(np.abs(step)) < 1e-6:
            break
    slopes = coefficients[1:] / scale
    return float(coefficients[0] - slopes @ center), slopes


def _sigmoid(logit):
    return 1 / (1 + np.exp(-np.clip(logit, -35, 35)))


def _hashes(files):
    hashes = {}
    for path in files:
        absolute = path.resolve()
        name = absolute.relative_to(ROOT) if absolute.is_relative_to(ROOT) else absolute
        hashes[name.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def fit(questions_files, score_files, *, mode="compact-relation"):
    questions = {
        row["id"]: row
        for path in questions_files
        for row in map(json.loads, path.read_text().splitlines())
    }
    rows = [row for path in score_files for row in json.loads(path.read_text())["rows"]]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("score rows must have unique question IDs")
    corpus = load_corpus()
    by_id = corpus.by_id()
    scorer = PassageSupport(DocumentCoverage(corpus, 0.597), mode=mode, corpus=corpus)
    conditional = mode == "conditional-veto"
    features, veto_features, witnesses, contextual = [], [], [], []
    for row in rows:
        question = questions[row["id"]]
        if question["expected_route"] != row["expected_route"]:
            raise ValueError(f"label differs from the score record for {row['id']}")
        text = question["question"]
        passages = [by_id[chunk_id] for chunk_id in row["passage_ids"]]
        values, candidates = scorer.relation_features(text, passages)
        features.append(values)
        witnesses.append([(feature, focused) for _, feature, focused in candidates])
        if scorer.veto:
            inquiry = _question_parts(text)[1]
            veto_features.append(scorer.veto.features(values, inquiry, passages))
            if conditional:
                contextual.append(has_fronted_or_coordinated_inquiry(inquiry))

    features = np.array(features)
    labels = np.array([row["expected_route"] == "docs" for row in rows], dtype=float)
    eligibility_field = "candidate_eligible" if conditional else "reference_admitted"
    admitted = np.array([row[eligibility_field] for row in rows], dtype=bool)
    intercept_ridge = 1e-5 if scorer.veto else 1e-6
    intercept, weights = fit_logistic(
        features[admitted], labels[admitted], intercept_ridge=intercept_ridge
    )
    bundle_intercept, bundle_weights = intercept, weights
    bundle_vectors, bundle_fields = features, RelationFeatures._fields
    if scorer.veto:
        bundle_vectors = np.array(veto_features)
        bundle_fields = VetoFeatures._fields
        historical_weight = 0.7 if conditional else 0.5
        bundle_ridge = 50.0 if conditional else 2.0
        sample_weights = np.array(
            [1.0 if row["id"].startswith("v13-r") else historical_weight for row in rows]
        )
        bundle_intercept, bundle_weights = fit_logistic(
            bundle_vectors[admitted],
            labels[admitted],
            weights=sample_weights[admitted],
            intercept_ridge=intercept_ridge,
            ridge=bundle_ridge,
        )

    odds_shift = FOCUSED_WITNESS_LOG_ODDS_SHIFT
    if conditional:
        context_rows = admitted & np.array(contextual, dtype=bool)
        context_intercept, context_weights = fit_logistic(
            bundle_vectors[context_rows],
            labels[context_rows],
            weights=sample_weights[context_rows],
            intercept_ridge=2.0,
            ridge=2 * bundle_ridge,
            prior=(bundle_intercept, bundle_weights),
        )
        odds_shift = CONDITIONAL_FOCUSED_WITNESS_LOG_ODDS_SHIFT
    odds_multiplier = math.exp(odds_shift)
    values, winning_witnesses = [], []
    for index, (bundle, candidates) in enumerate(zip(bundle_vectors, witnesses, strict=True)):
        raw = [
            (relation_score(feature, intercept=intercept, weights=weights), focused)
            for feature, focused in candidates
        ]
        witness, raw_witness, focused = max(
            (
                (
                    witness_score(score, focused=focus, log_odds_shift=odds_shift),
                    round(score, 8),
                    focus,
                )
                for score, focus in raw
            ),
            key=lambda item: item[0],
            default=(0.0, 0.0, False),
        )
        if conditional and contextual[index]:
            veto = relation_score(bundle, intercept=context_intercept, weights=context_weights)
        else:
            veto = relation_score(bundle, intercept=bundle_intercept, weights=bundle_weights)
        values.append(round(min(witness, veto), 8))
        winning_witnesses.append((raw_witness, focused))
    selected, _ = select_threshold(
        [
            {**row, "score": value, "candidate_eligible": bool(eligible)}
            for row, value, eligible in zip(rows, values, admitted, strict=True)
        ]
    )
    report = {
        "corpus_digest": corpus.digest,
        "scored_rows": len(rows),
        "fitting_rows": int(admitted.sum()),
        "intercept": bundle_intercept,
        "weights": dict(zip(bundle_fields, bundle_weights.tolist(), strict=True)),
        "calibration": selected,
    }
    if scorer.veto:
        report.update(
            mode=mode,
            development_hashes=_hashes(questions_files),
            evidence_pack_hashes=_hashes(score_files),
            ridge=bundle_ridge,
            intercept_ridge=intercept_ridge,
            historical_v12_bundle_sample_weight=historical_weight,
            focused_witness_log_odds_shift=odds_shift,
            witness_intercept=intercept,
            witness_weights=dict(zip(RelationFeatures._fields, weights.tolist(), strict=True)),
        )
    if conditional:
        cutoff = selected["threshold"]
        admitted_witnesses = [
            witness
            for eligible, score, witness in zip(admitted, values, winning_witnesses, strict=True)
            if eligible and score >= cutoff
        ]
        report.update(
            context_fitting_rows=int(context_rows.sum()),
            context_intercept=context_intercept,
            context_weights=dict(zip(bundle_fields, context_weights.tolist(), strict=True)),
            context_ridge=2 * bundle_ridge,
            context_intercept_ridge=2.0,
            witness_ridge=2.0,
            effective_raw_witness_cutoff={
                "focused": cutoff / (odds_multiplier - (odds_multiplier - 1) * cutoff),
                "other": cutoff,
            },
            minimum_admitted_raw_witness={
                label: min(
                    (raw for raw, focused in admitted_witnesses if focused == flag), default=None
                )
                for label, flag in (("focused", True), ("other", False))
            },
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", nargs="+", type=Path, required=True)
    parser.add_argument("--scores", nargs="+", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("compact-relation", "compact-veto", "conditional-veto"),
        default="compact-relation",
    )
    args = parser.parse_args()
    print(json.dumps(fit(args.questions, args.scores, mode=args.mode), indent=2))


if __name__ == "__main__":
    main()
