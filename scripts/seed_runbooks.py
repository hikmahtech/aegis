#!/usr/bin/env python3
"""Seed operational runbooks into the knowledge service.

Usage:
    python scripts/seed_runbooks.py --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import sys

import httpx

RUNBOOKS = [
    {
        "source_url": "aegis://runbook/alert_type/ServiceDown",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "alert_type:ServiceDown",
                "predicate": "has_runbook",
                "object": (
                    "1. Check service status via systemctl or docker ps. "
                    "2. Check recent deployments in audit_log. "
                    "3. Inspect service logs for crash loops or OOM. "
                    "4. Verify dependencies (DB, Redis, upstream). "
                    "5. Restart if transient; escalate if persistent."
                ),
                "confidence": 1.0,
            },
        ],
    },
    {
        "source_url": "aegis://runbook/alert_type/DiskCritical",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "alert_type:DiskCritical",
                "predicate": "has_runbook",
                "object": (
                    "1. Identify largest directories with du -sh /*. "
                    "2. Check Docker volumes and images: docker system df. "
                    "3. Prune unused images: docker image prune -a. "
                    "4. Check log rotation (journald, app logs). "
                    "5. If /var/lib/docker, consider volume cleanup. "
                    "6. Expand disk if above measures insufficient."
                ),
                "confidence": 1.0,
            },
        ],
    },
    {
        "source_url": "aegis://runbook/alert_type/PipelineFailure",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "alert_type:PipelineFailure",
                "predicate": "has_runbook",
                "object": (
                    "1. Check pipeline logs in CI/CD (GitHub Actions, n8n). "
                    "2. Identify failing step and error message. "
                    "3. Check if upstream API or dependency is down. "
                    "4. Retry if transient network error. "
                    "5. Fix code and re-run if build/test failure."
                ),
                "confidence": 1.0,
            },
        ],
    },
    {
        "source_url": "aegis://runbook/project/infra-gitops",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "project:infra-gitops",
                "predicate": "has_context",
                "object": (
                    "Homelab GitOps manages Docker Swarm infrastructure via Ansible. "
                    "Primary manager: mgr-1 (10.20.0.11), services on node-a (10.20.0.20). "
                    "Stack file: aegis-stack.yml.j2. Deploy via ansible-playbook with aegis role. "
                    "Secrets in group_vars/all.yml (vault-encrypted)."
                ),
                "confidence": 1.0,
            },
        ],
    },
    {
        "source_url": "aegis://runbook/project/bcp",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "project:bcp",
                "predicate": "has_context",
                "object": (
                    "BCP (Business Content Platform) is a Acme application. "
                    "PHP-based with data-securities-php-sdk. "
                    "Sentry project for error tracking. "
                    "Check PHP logs and SDK request traces for issues."
                ),
                "confidence": 1.0,
            },
        ],
    },
    {
        "source_url": "aegis://runbook/verification_delay/ServiceDown",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "alert_type:ServiceDown",
                "predicate": "has_verification_delay",
                "object": "300 seconds — wait 5 minutes before investigating ServiceDown alerts to allow auto-recovery.",
                "confidence": 1.0,
            },
        ],
    },
    {
        "source_url": "aegis://runbook/verification_delay/PipelineFailure",
        "source_type": "runbook",
        "extractor": "seed_script",
        "knowledge": [
            {
                "knowledge_type": "Claim",
                "subject": "alert_type:PipelineFailure",
                "predicate": "has_verification_delay",
                "object": "600 seconds — wait 10 minutes before investigating pipeline failures to allow retries.",
                "confidence": 1.0,
            },
        ],
    },
]


def main():
    parser = argparse.ArgumentParser(description="Seed operational runbooks into knowledge service")
    parser.add_argument("--url", default="http://localhost:8000", help="Knowledge service base URL")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    client = httpx.Client(timeout=30)

    success = 0
    failed = 0

    for runbook in RUNBOOKS:
        subject = runbook["knowledge"][0]["subject"]
        predicate = runbook["knowledge"][0]["predicate"]
        label = f"{subject} ({predicate})"

        try:
            resp = client.post(f"{base_url}/api/claims", json=runbook)
            resp.raise_for_status()
            print(f"  OK  {label}")
            success += 1
        except httpx.HTTPError as exc:
            print(f"  FAIL  {label}: {exc}")
            failed += 1

    print(f"\nDone: {success} succeeded, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
