"""Refit the compact scope scorer offline from consumed evidence packs."""

import argparse
import json
from pathlib import Path

import numpy as np

from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.support import PassageSupport
from ledgerops_assistant.support_relation import RelationFeatures, cap_witness_score, relation_score
from scripts.evaluate_v13 import select_threshold


def fit_logistic(features, labels):
    """Fit L2-regularized logistic regression and convert to raw feature units."""
    center = features.mean(axis=0)
    scale = np.maximum(features.std(axis=0), 1e-5)
    design = np.column_stack([np.ones(len(features)), (features - center) / scale])
    coefficients = np.zeros(design.shape[1])
    ridge = np.array([1e-6] + [2.0] * features.shape[1])
    for _ in range(35):
        fitted = _sigmoid(design @ coefficients)
        curvature = design.T @ ((fitted * (1 - fitted))[:, None] * design) + np.diag(ridge)
        gradient = design.T @ (fitted - labels) + ridge * coefficients
        step = np.linalg.solve(curvature, gradient)
        coefficients -= step
        if np.max(np.abs(step)) < 1e-6:
            break
    weights = coefficients[1:] / scale
    return float(coefficients[0] - weights @ center), weights


def _sigmoid(logit):
    return 1 / (1 + np.exp(-np.clip(logit, -35, 35)))


def fit(questions_files, score_files):
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
    scorer = PassageSupport(DocumentCoverage(corpus, 0.597))
    features = []
    witnesses = []
    for row in rows:
        question = questions[row["id"]]
        if question["expected_route"] != row["expected_route"]:
            raise ValueError(f"label differs from the score record for {row['id']}")
        passages = [by_id[chunk_id] for chunk_id in row["passage_ids"]]
        values, candidates = scorer.relation_features(question["question"], passages)
        features.append(values)
        witnesses.append([feature for _, feature in candidates])

    features = np.array(features)
    labels = np.array([row["expected_route"] == "docs" for row in rows], dtype=float)
    admitted = np.array([row["reference_admitted"] for row in rows], dtype=bool)
    intercept, weights = fit_logistic(features[admitted], labels[admitted])

    def score(vector):
        return relation_score(vector, intercept=intercept, weights=weights)

    values = []
    for bundle, candidates in zip(features, witnesses, strict=True):
        witness = max(map(score, candidates), default=0.0)
        values.append(round(cap_witness_score(witness, score(bundle)), 8))
    selected, _ = select_threshold(
        [{**row, "score": float(value)} for row, value in zip(rows, values, strict=True)]
    )
    return {
        "corpus_digest": corpus.digest,
        "scored_rows": len(rows),
        "fitting_rows": int(admitted.sum()),
        "intercept": intercept,
        "weights": dict(zip(RelationFeatures._fields, weights.tolist(), strict=True)),
        "calibration": selected,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", nargs="+", type=Path, required=True)
    parser.add_argument("--scores", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(fit(args.questions, args.scores), indent=2))


if __name__ == "__main__":
    main()
