# Infrastructure Registry

The **Infrastructure** page in the admin panel (backed by the `infra` table,
`core/src/aegis/services/infra.py`, and `/api/admin/infra`) is a registry of
machines and clusters AEGIS can reach: SSH hosts, the Docker Swarm, and
Kubernetes clusters. Everything an entry needs — including its secrets — is
entered in the UI and stored in the database, so registering new infrastructure
never requires mounting files into containers or redeploying.

| Kind | What it is | Executable ops |
|------|------------|----------------|
| `ssh_host` | Any machine reachable over SSH | Provisioning (push files, run a setup command) |
| `swarm` | A Docker Swarm manager, reached over SSH | Provisioning; the `hosts_aegis` service probe; maps chat's `swarm` context onto the read-only gate |
| `docker` | A plain Docker host | Same as `swarm` |
| `k8s` | A Kubernetes cluster, reached via kubeconfig | Provision = connectivity check; list pods/deployments, pod logs, rolling restart — from the UI **and** chat |

## Credentials — how secrets are handled

All per-entry secrets are **write-only**: you paste them in the form, they are
encrypted with `AEGIS_SECRET_KEY` (Fernet; see `core/src/aegis/crypto.py`) into
the `infra.credentials` jsonb column, and the API only ever returns
`has_ssh_key` / `has_kubeconfig` / `has_auth_env` / `has_aws_credentials` /
`has_gcp_service_account` booleans. When editing, a blank secret field **keeps** the stored value;
pasting new material replaces it.

At execution time secrets are decrypted and materialized to mode-0600 temp
files (SSH key, kubeconfig, AWS credentials file, GCP service account JSON)
that are deleted as soon as the call finishes — nothing secret persists on
disk.

> If `AEGIS_SECRET_KEY` is unset, values are stored plaintext with an
> `encrypted: false` flag (the single-user self-hosted default). Set the key in
> production. Turning it on later only affects newly-saved secrets.

Per-entry secret fields:

- **SSH private key** — used for provisioning and the `hosts_aegis` probe.
  Wins over `ssh_key_ref` (a path on the core host, kept as a
  bring-your-own-file fallback).
- **Kubeconfig** (`kind=k8s`) — must be self-contained; see below.
- **Auth env** (`kind=k8s`) — `KEY=value` lines injected into the environment
  of every kubectl call for this entry. This is how exec-plugin kubeconfigs
  (EKS, GKE) get their cloud credentials.
- **AWS credentials file** (`kind=k8s`) — a `~/.aws/credentials`-style ini for
  profile users; materialized per call and exposed as
  `AWS_SHARED_CREDENTIALS_FILE`.
- **GCP service account JSON** (`kind=k8s`) — a service-account key file;
  materialized per call and exposed as `GOOGLE_APPLICATION_CREDENTIALS`
  (honored by `gke-gcloud-auth-plugin`), with
  `CLOUDSDK_CORE_DISABLE_PROMPTS=1` set so gcloud never blocks on a prompt.

## The read-only flag

Every entry has a **Read-only** checkbox — a per-entry mutation gate enforced
in the service layer, so the admin UI, the REST API, and chat tools all hit the
same check:

| Operation | `read_only=true` |
|---|---|
| k8s `restart_deployment` (UI + chat) | refused (403 / chat error) |
| swarm `restart_service` (UI + chat) | refused when a registered swarm/docker entry maps to the requested context (by `slug` or `docker_context`) |
| SSH provisioning | refused (it writes files / runs commands) |
| k8s provisioning (connectivity check), all list/logs/inspect ops | allowed |

Unregistered contexts fail open — the flag only governs infrastructure that is
actually in the registry.

## How-to: register the Docker Swarm

1. Create a dedicated keypair and install it on a swarm manager:

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/aegis_swarm -C aegis-infra -N ""
   ssh <user>@<manager> "cat >> ~/.ssh/authorized_keys" < ~/.ssh/aegis_swarm.pub
   ```

   Use a dedicated key, not your personal one — it lives (encrypted) in the
   AEGIS database. Optionally restrict it in `authorized_keys` with
   `from="<subnet>"`.

2. **+ Add infrastructure** with:
   - **Name**: `swarm` — the slug becomes the identity the read-only gate
     matches against chat's `restart_service` context.
   - **Kind**: `swarm`; **Host**: the manager's IP (containers usually can't
     resolve LAN hostnames); **SSH user/port**.
   - **SSH private key**: paste the private key.
   - **Docker context**: leave **empty** — if set, the System-Monitoring probe
     tries `docker --context …` inside the core container (which has no
     contexts) instead of SSHing with your key.
   - **This host runs AEGIS**: check it so System Monitoring lists the swarm's
     services through this entry.

3. **Provision** — with no setup files/command this is an SSH connectivity
   check; expect status `ready`. Failures show the actual ssh stderr in the
   per-step log (**View log**).

## How-to: register a Kubernetes cluster

The pasted kubeconfig must be **self-contained** and the API server must be
reachable from wherever core runs. From a working local kubeconfig:

```bash
kubectl config view --minify --flatten --context=<ctx> > /tmp/aegis-kubeconfig.yaml
```

(`--flatten` inlines cert files referenced by path.) Paste the contents into
the **Kubeconfig** field, then delete the temp file.

- **Name**: whatever you'll say in chat — the slug **is** the chat context
  ("list pods on `homelab-k8s`").
- **Read-only**: check it if AEGIS should only observe this cluster.
- **Provision** runs `kubectl get nodes` and reports "N node(s) reachable".
- After provisioning, the row gets a **Cluster** button: namespace picker,
  deployments (with confirm-guarded Restart unless read-only), pods with Logs.

### Static-credential kubeconfigs (token / client cert)

If the kubeconfig embeds a token or client cert, that's all you need. For a
least-privilege setup, mint a ServiceAccount instead of pasting an admin
config:

```bash
kubectl create sa aegis -n kube-system
kubectl create clusterrole aegis-ops \
  --verb=get,list --resource=pods,deployments,nodes,pods/log \
  --verb=patch --resource=deployments        # patch = rollout restart; drop for read-only
kubectl create clusterrolebinding aegis-ops --clusterrole=aegis-ops --serviceaccount=kube-system:aegis
TOKEN=$(kubectl create token aegis -n kube-system --duration=8760h)
```

Build a kubeconfig with the cluster CA + server URL + that token.

### Exec-plugin kubeconfigs (EKS, GKE)

Managed-cloud kubeconfigs usually authenticate via an exec plugin — EKS runs
`aws eks get-token`, GKE runs `gke-gcloud-auth-plugin` — which needs (a) the
CLI binary in the core image and (b) cloud credentials in the environment.

**(a) The binary** — the core image installs cloud CLIs behind a build arg
(default empty, so the standard image stays slim):

```bash
docker build --build-arg EXTRA_CLOUD_CLIS=aws -f core/Dockerfile .
# or both:
docker build --build-arg EXTRA_CLOUD_CLIS="aws gcloud" -f core/Dockerfile .
```

Supported values live in the `EXTRA_CLOUD_CLIS` step of `core/Dockerfile`
(currently `aws` and `gcloud` — the latter installs the Google Cloud CLI plus
`gke-gcloud-auth-plugin` from Google's apt repo); adding another CLI is one
new `case` arm. Forks that build their own images (see the deployment docs)
pass the arg from their build pipeline.

**(b) The credentials** — per entry, in the **Auth env** field. For EKS:

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

or, if you use **profiles**, set `AWS_PROFILE=myprofile` in Auth env (or leave
it to the kubeconfig — EKS exec blocks often carry `env: AWS_PROFILE=...`
themselves) and paste the relevant section of your `~/.aws/credentials` into
the **AWS credentials file** field — it is materialized per call as
`AWS_SHARED_CREDENTIALS_FILE`. The IAM principal must be mapped in the
cluster's `aws-auth` ConfigMap (it is, if `kubectl` works for you locally with
the same credentials). The region comes from the exec block's `--region` arg
in the kubeconfig itself.

> **Role-assumption profiles:** if your profile looks like
>
> ```ini
> [prod]
> role_arn = arn:aws:iam::...:role/...
> source_profile = default
> ```
>
> you must paste the **`[default]` section too** (it holds the actual keys) —
> the materialized file is the *only* credentials file the CLI sees, so a
> role profile alone fails with
> `The source_profile "default" referenced in the profile "prod" does not exist`.

Full EKS recipe:

```bash
# 1. self-contained kubeconfig for the context (exec block included, verbatim)
kubectl config view --minify --flatten --context=<eks-ctx> > /tmp/aegis-kubeconfig.yaml
# 2. add a k8s entry: paste the kubeconfig + AWS keys (or profile + credentials file)
# 3. Provision → "N node(s) reachable"
```

For **GKE**, paste a **service account JSON key** into the **GCP service
account JSON** field instead — it is materialized per call as
`GOOGLE_APPLICATION_CREDENTIALS`, which `gke-gcloud-auth-plugin` honors
directly (no `gcloud auth login` needed; `CLOUDSDK_CORE_DISABLE_PROMPTS=1` is
set so nothing ever blocks on a prompt). The service account needs at least
`roles/container.viewer` on the cluster's project (plus RBAC inside the
cluster for what AEGIS should do). Requires the image built with
`--build-arg EXTRA_CLOUD_CLIS="aws gcloud"` (or just `gcloud`).

Full GKE recipe:

```bash
# 1. service account + key
gcloud iam service-accounts create aegis-infra --project <project>
gcloud projects add-iam-policy-binding <project> \
  --member serviceAccount:aegis-infra@<project>.iam.gserviceaccount.com \
  --role roles/container.viewer
gcloud iam service-accounts keys create /tmp/aegis-gke-key.json \
  --iam-account aegis-infra@<project>.iam.gserviceaccount.com
# 2. self-contained kubeconfig (the gke-gcloud-auth-plugin exec block comes along)
gcloud container clusters get-credentials <cluster> --region <region> --project <project>
kubectl config view --minify --flatten --context=<gke-ctx> > /tmp/aegis-kubeconfig.yaml
# 3. add a k8s entry: paste the kubeconfig + the JSON key; delete both temp files
# 4. Provision → "N node(s) reachable"
```

## Chat

Pandora's infra tools work against registry clusters by slug:

- `list_pods` / `list_deployments` / `get_pod_logs` — pass a registry entry's
  slug as `context` (script-host contexts keep working unchanged; those run on
  the remote script host, not through the registry).
- `restart_deployment` — registry-only, refused for read-only entries.
- `restart_service` (swarm) — refused when the matching registry entry is
  read-only.
- ArgoCD tools are script-host only (they need the `argocd` CLI, not just a
  kubeconfig).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Provision error `exec plugin: executable aws not found` | Image built without `EXTRA_CLOUD_CLIS=aws` |
| Provision error `exec plugin: executable gke-gcloud-auth-plugin not found` | Image built without `gcloud` in `EXTRA_CLOUD_CLIS` |
| Provision error mentioning `getting credentials` / `ExpiredToken` | Auth env keys missing/wrong for this entry |
| Provision error `Unable to connect to the server` | API endpoint not reachable from the core container (VPN-only endpoint?) |
| `hosts_aegis` probe says `docker --context` failed | The entry has `docker_context` set — clear it so the probe uses SSH |
| `entry is read-only …` | Working as intended; uncheck Read-only to allow mutations |

Every provision failure records the failing step's stdout/stderr in the row's
provision log (**View log** in the UI).
