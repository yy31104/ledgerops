"""Start or stop only this local demo stack; generated credentials stay outside Git."""

import argparse
import os
import secrets
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("up", "down", "check"))
    parser.add_argument(
        "--hashing", action="store_true", help="stand-in retrieval for CI plumbing only"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if not env_file.exists():
        if args.command != "up":
            raise SystemExit("Start the local stack first to generate its private configuration.")
        contents = (
            f"ASSISTANT_OWNER_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"ASSISTANT_RUNTIME_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"LEDGEROPS_ASSISTANT_DEMO_TOKEN=lo1_{secrets.token_urlsafe(32)}\n"
        )
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(root / "compose.yml")]
    env = dict(os.environ)
    if args.hashing:
        env["LEDGEROPS_ASSISTANT_EMBEDDER"] = "hashing"
    operation = {
        "up": ["up", "--build", "-d", "--wait", "--wait-timeout", "240"],
        "down": ["down"],
        "check": ["config", "--quiet"],
    }[args.command]
    subprocess.run([*command, *operation], check=True, env=env)
    if args.command == "up":
        print("Synthetic operator demo: http://127.0.0.1:8088 — choose Try synthetic demo.")
        if args.hashing:
            print("Hashing embeddings: wiring check only, not retrieval-quality evidence.")


if __name__ == "__main__":
    main()
