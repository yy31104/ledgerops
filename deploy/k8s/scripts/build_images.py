"""Build the two images kind needs from the unchanged ops/linux/Dockerfile.

The build context comes from `prepare_context` in ops/linux/stack.py: the committed
revision via `git archive` when the tree is clean, otherwise the working tree, plus
source-provenance.json. Only the `runtime` and `proxy` targets are built.

Behind a TLS-intercepting proxy, pip inside the build cannot verify PyPI. Pass
--extra-ca-cert: the Dockerfile *copy in the temporary context* (never the repository
file) is rewritten so every pip step reads that CA through a BuildKit secret mount.
The certificate is not written into any image layer, ENV or label.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ops" / "linux"))
from stack import prepare_context  # noqa: E402  (reuse the Linux build's source rules)

TARGETS = {"runtime": "ledgerops-local:k8s", "proxy": "ledgerops-local-proxy:k8s"}
PIP = "RUN python -m pip "


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra-ca-cert", type=Path, help="PEM bundle trusted by pip in the build")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ledgerops-k8s-build-") as temporary:
        context = Path(temporary) / "context"
        provenance, _ = prepare_context(ROOT, context, allow_dirty=True)
        secret = []
        if args.extra_ca_cert is not None:
            dockerfile = context / "Dockerfile"
            text = dockerfile.read_text()
            if text.count(PIP) != 4:
                raise SystemExit("unexpected Dockerfile shape; refusing to rewrite pip steps")
            dockerfile.write_text(
                text.replace(
                    PIP,
                    "RUN --mount=type=secret,id=extra-ca,required=true "
                    "PIP_CERT=/run/secrets/extra-ca python -m pip ",
                )
            )
            secret = ["--secret", f"id=extra-ca,src={args.extra_ca_cert.resolve()}"]
        for target, tag in TARGETS.items():
            subprocess.run(
                [
                    "docker", "build", "--target", target, "--tag", tag,
                    "--build-arg", "SOURCE_REVISION=" + provenance["revision"],
                    "--build-arg", "SOURCE_DIRTY=" + str(provenance["dirty"]).lower(),
                    *secret, str(context),
                ],
                check=True,
            )  # fmt: skip
    print(f"built {', '.join(TARGETS.values())} from {provenance['revision'][:12]}"
          f"{' (dirty)' if provenance['dirty'] else ''}")  # fmt: skip


if __name__ == "__main__":
    main()
