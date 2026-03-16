#!/usr/bin/env python3
"""Build and push GrowthBook fork image to Anthropic GAR.

Builds an amd64 image from the local Dockerfile and pushes to GAR.

Single-arch because argon (the only deploy target) is 100% amd64 nodes.
The upstream image is multi-arch, but that's for their public distribution —
we don't need arm64, and local multi-arch buildx needs a privileged buildkit
container that this dev environment can't spawn (sysfs mount denied).

GAR only: all four Helm releases in infra-manifests reference the same
GAR path, argon is GKE so pulls are GCP-native, and no ECR reference exists
anywhere in the deploy config.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

REGISTRY = "us-docker.pkg.dev/anthropic-artifact-registry/main/growthbook/growthbook"
PLATFORM = "linux/amd64"
FORK_REPO_URL = "https://github.com/anthropics/growthbook.git"


def run(cmd: list[str], *, dry_run: bool = False, **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
    return subprocess.run(cmd, check=True, **kwargs)


def docker_cmd(args: list[str], *, docker_config: str) -> list[str]:
    return ["docker", "--config", docker_config] + args


def git_sha(short: bool = False) -> str:
    args = ["git", "rev-parse"]
    if short:
        args.append("--short=7")
    args.append("HEAD")
    result = subprocess.run(args, capture_output=True, text=True, check=True, cwd=SCRIPT_DIR)
    return result.stdout.strip()


def git_is_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, check=True, cwd=SCRIPT_DIR,
    )
    return bool(result.stdout.strip())


def gcp_tag_exists(tag: str) -> bool:
    """True iff the tag already exists in GAR.

    Treats only NOT_FOUND as "absent" — auth expiry, permission errors, or
    transient failures abort rather than silently returning False (which
    would bypass the overwrite guard and clobber an existing tag).
    """
    result = subprocess.run(
        ["gcloud", "artifacts", "docker", "images", "describe", f"{REGISTRY}:{tag}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    if "NOT_FOUND" in result.stderr:
        return False
    sys.exit(f"ERROR: tag existence check failed (not a NOT_FOUND):\n{result.stderr}")

def write_buildinfo(full_sha: str, *, dry_run: bool = False) -> None:
    """Write buildinfo/SHA and buildinfo/DATE — COPY buildinfo* in Dockerfile needs these."""
    if dry_run:
        print(f"    Would write buildinfo/SHA={full_sha} buildinfo/DATE=<now>")
        return
    buildinfo = SCRIPT_DIR / "buildinfo"
    buildinfo.mkdir(exist_ok=True)
    (buildinfo / "SHA").write_text(full_sha + "\n")
    (buildinfo / "DATE").write_text(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-r", "--revision", type=int, default=1,
                        help="Revision number (default: 1)")
    parser.add_argument("-t", "--tag",
                        help="Full image tag (overrides auto-generated anthropic-YYYYMMDD-rN)")
    parser.add_argument("--no-push", action="store_true",
                        help="Build only, do not push")
    parser.add_argument("--force", action="store_true",
                        help="Push even if tag already exists in GAR")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Build even with uncommitted changes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing")
    args = parser.parse_args()

    os.chdir(SCRIPT_DIR)

    if not args.dry_run and not args.allow_dirty and git_is_dirty():
        sys.exit("ERROR: working tree is dirty. Commit changes or pass --allow-dirty.")

    dirty = not args.dry_run and git_is_dirty()
    suffix = "-dirty" if dirty else ""
    full_sha = (git_sha() + suffix) if not args.dry_run else "0" * 40
    short_sha = (git_sha(short=True) + suffix) if not args.dry_run else "0000000"

    tag = args.tag or f"anthropic-{datetime.now(UTC):%Y%m%d}-r{args.revision}"
    remote_image = f"{REGISTRY}:{tag}"
    print(f"Git SHA:   {short_sha}")
    print(f"Image tag: {tag}")
    print(f"Target:    {remote_image}")

    # Fail fast before the ~7min build if the tag is already taken.
    if not args.dry_run and not args.no_push:
        print("\n=== Checking for existing tag ===")
        if gcp_tag_exists(tag):
            msg = f"Tag {tag} already exists at {REGISTRY}"
            if args.force:
                print(f"WARNING: {msg}. Proceeding (--force).")
            else:
                sys.exit(f"ERROR: {msg}. Use --force to overwrite or -r to bump revision.")
        else:
            print(f"    Tag {tag} is available.")

    print("\n=== Writing buildinfo/ ===")
    write_buildinfo(full_sha, dry_run=args.dry_run)

    build_args = [
        "--build-arg", f"DD_GIT_COMMIT_SHA={full_sha}",
        "--build-arg", f"DD_GIT_REPOSITORY_URL={FORK_REPO_URL}",
        "--build-arg", f"DD_VERSION={tag}",
    ]

    print(f"\n=== Building ({PLATFORM}) ===")
    local_image = f"growthbook:{tag}"
    run(
        ["docker", "build", "--platform", PLATFORM, *build_args, "-t", local_image, "."],
        dry_run=args.dry_run,
    )

    if args.no_push:
        print(f"\n=== Build complete (push skipped) ===\nLocal image: {local_image}")
        return

    # Isolated docker config dir avoids credsStore helpers in
    # ~/.docker/config.json that don't work in this environment.
    with tempfile.TemporaryDirectory(prefix="growthbook-docker-") as docker_config:
        Path(docker_config, "config.json").write_text(json.dumps({}))
        env = {**os.environ, "DOCKER_CONFIG": docker_config}

        print("\n=== Authenticating to GCP Artifact Registry ===")
        run(
            ["gcloud", "auth", "configure-docker", "us-docker.pkg.dev", "--quiet"],
            dry_run=args.dry_run, env=env,
        )

        print("\n=== Tagging and pushing ===")
        run(docker_cmd(["tag", local_image, remote_image], docker_config=docker_config),
            dry_run=args.dry_run)
        run(docker_cmd(["push", remote_image], docker_config=docker_config),
            dry_run=args.dry_run)

    print("\n=== Push complete ===")
    print(f"Image: {remote_image}")
    print("\nNext: update infra-manifests values.yaml (3 files, 2 lines each):")
    print("  apps/growthbook/base/values.yaml")
    print("  apps/growthbook-staging/base/values.yaml")
    print("  apps/growthbook-portcullis/base/values.yaml")
    print(f"  sed -i 's|tag: \"[^\"]*\"|tag: \"{tag}\"|g' <files>")


if __name__ == "__main__":
    main()
