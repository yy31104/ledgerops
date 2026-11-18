"""Refit compact scope scoring offline from consumed evidence packs."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.support import PassageSupport, _question_parts
from ledgerops_assistant.support_relation import RelationFeatures, relation_score, witness_score
from ledgerops_assistant.support_veto import VetoFeatures
from scripts.evaluate_v13 import ROOT, select_threshold


def fit_logistic(features, labels, *, weights=None, intercept_ridge=1e-6):
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
    ridge = np.array([intercept_ridge] + [2.0] * features.shape[1])
    for _ in range(35):
        fitted = _sigmoid(design @ coefficients)
        curvature = design.T @ ((weights * fitted * (1 - fitted))[:, None] * design)
        curvature += np.diag(ridge)
        gradient = design.T @ ((fitted - labels) * weights) + ridge * coefficients
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
    features, veto_features, witnesses = [], [], []
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
            veto_features.append(scorer.veto.features(values, _question_parts(text)[1], passages))

    features = np.array(features)
    labels = np.array([row["expected_route"] == "docs" for row in rows], dtype=float)
    admitted = np.array([row["reference_admitted"] for row in rows], dtype=bool)
    intercept_ridge = 1e-5 if scorer.veto else 1e-6
    intercept, weights = fit_logistic(
        features[admitted], labels[admitted], intercept_ridge=intercept_ridge
    )
    bundle_intercept, bundle_weights = intercept, weights
    bundle_vectors, bundle_fields = features, RelationFeatures._fields
    if scorer.veto:
        bundle_vectors = np.array(veto_features)
        bundle_fields = VetoFeatures._fields
        # Give the older v1.2 development rows half the weight of the v1.3 cohorts.
        sample_weights = np.array([1.0 if row["id"].startswith("v13-r") else 0.5 for row in rows])
        bundle_intercept, bundle_weights = fit_logistic(
            bundle_vectors[admitted],
            labels[admitted],
            weights=sample_weights[admitted],
            intercept_ridge=intercept_ridge,
        )

    values = []
    for bundle, candidates in zip(bundle_vectors, witnesses, strict=True):
        witness = max(
            (
                witness_score(
                    relation_score(feature, intercept=intercept, weights=weights),
                    focused=focused,
                )
                for feature, focused in candidates
            ),
            default=0.0,
        )
        veto = relation_score(bundle, intercept=bundle_intercept, weights=bundle_weights)
        values.append(round(min(witness, veto), 8))
    selected, _ = select_threshold(
        [{**row, "score": value} for row, value in zip(rows, values, strict=True)]
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
            ridge=2.0,
            intercept_ridge=intercept_ridge,
            historical_v12_bundle_sample_weight=0.5,
            focused_witness_log_odds_shift=0.5,
            witness_intercept=intercept,
            witness_weights=dict(zip(RelationFeatures._fields, weights.tolist(), strict=True)),
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", nargs="+", type=Path, required=True)
    parser.add_argument("--scores", nargs="+", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("compact-relation", "compact-veto"), default="compact-relation"
    )
    args = parser.parse_args()
    print(json.dumps(fit(args.questions, args.scores, mode=args.mode), indent=2))


if __name__ == "__main__":
    main()
