"""Record evaluations in local MLflow; no credentials or remote tracking server."""

from pathlib import Path


def log_experiment(report: dict, directory: Path) -> str:
    from mlflow import MlflowClient

    directory.mkdir(parents=True, exist_ok=True)
    client = MlflowClient(tracking_uri=f"sqlite:///{directory.resolve() / 'tracking.db'}")
    name = "ledgerops-retrieval"
    experiment = client.get_experiment_by_name(name)
    experiment_id = (
        experiment.experiment_id
        if experiment
        else client.create_experiment(
            name, artifact_location=(directory.resolve() / "artifacts").as_uri()
        )
    )
    run_id = client.create_run(experiment_id).info.run_id
    try:
        for key, value in {
            "corpus_digest": report["corpus"]["digest"],
            "revision": report["corpus"]["revision"],
            "questions_sha256": report["questions"]["sha256"],
            "embedder": report["environment"]["embedder"],
            "backend": report["environment"]["backend"],
            "split": report["questions"]["evaluated_split"],
            "quality_signal": report["environment"]["quality_signal"],
        }.items():
            client.log_param(run_id, key, value)
        if "fusion" in report:
            import json

            client.log_param(run_id, "fusion", json.dumps(report["fusion"], sort_keys=True))
        if "context" in report:
            client.log_param(run_id, "context_policy", report["context"]["policy"])
            client.log_param(run_id, "context_max_json_bytes", report["context"]["max_json_bytes"])
            for name, value in report["context"].get("stats", {}).items():
                client.log_metric(run_id, f"context.{name}", value)
        for method, result in report["retrieval"].items():
            for split, metrics in result["by_split"].items():
                if not metrics["n"]:
                    continue
                for metric, value in metrics.items():
                    client.log_metric(
                        run_id, f"{method}.{split}.{metric.replace('@', '_at_')}", value
                    )
        client.log_dict(run_id, report, "evaluation.json")
        client.set_terminated(run_id)
    except Exception:
        client.set_terminated(run_id, status="FAILED")
        raise
    return run_id
