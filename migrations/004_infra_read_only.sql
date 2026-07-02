-- Per-entry mutation gate: when true, AEGIS refuses mutating operations on
-- this infra (k8s deployment restarts, SSH provisioning). Read-only ops
-- (list pods/deployments, logs, k8s connectivity check) stay available.
-- Default false keeps existing entries behaving as before.
ALTER TABLE infra ADD COLUMN read_only boolean NOT NULL DEFAULT false;
