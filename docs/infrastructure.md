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
| `cloud` | A cloud provider account (one row per AWS account / GCP project) | Provision = identity check (`aws sts get-caller-identity` / GCP ADC token); lends exec-plugin credentials to `k8s` entries; `list_cloud_accounts` / `cloud_identity` in chat |

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
- **AWS credentials file** (`kind=k8s`, `kind=cloud`) — a
  `~/.aws/credentials`-style ini for profile users; materialized per call and
  exposed as `AWS_SHARED_CREDENTIALS_FILE`.
- **GCP service account JSON** (`kind=k8s`, `kind=cloud`) — a service-account
  key file; materialized per call and exposed as
  `GOOGLE_APPLICATION_CREDENTIALS` (honored by `gke-gcloud-auth-plugin`), with
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
new `case` arm. Forks that build their own images (see
[`production.md`](production.md)) pass the arg from their build pipeline.

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

## Cloud accounts (`kind=cloud`)

A **Cloud account** entry represents one AWS account or one GCP project as a
first-class registry row — independent of any cluster. Use one row per
account: `aws-hikmah`, `aws-stockopedia`, `gcp-main`, … Each row carries its
own encrypted credentials plus a little non-secret config:

| Provider | Secret (write-only, encrypted) | Non-secret config |
|---|---|---|
| `aws` | **AWS credentials file** (multi-profile ini) and/or **Auth env** (`AWS_ACCESS_KEY_ID=…` lines) | **Default profile** (used as `AWS_PROFILE` when nothing more specific is given), **Region** |
| `gcp` | **GCP service account JSON** | **Project** |

The AWS ini is the full `~/.aws/credentials` shape, so one account row can
hold several profiles — including role-assumption ones (remember the
`[default]` source-profile rule above):

```ini
[default]
aws_access_key_id = AKIA...
aws_secret_access_key = ...

[prod]
role_arn = arn:aws:iam::111111111111:role/aegis-ops
source_profile = default

[staging]
role_arn = arn:aws:iam::222222222222:role/aegis-ops
source_profile = default
```

**Provision** runs a pure identity check (allowed even on read-only entries,
like the k8s connectivity check):

- `aws` → `aws sts get-caller-identity` with `AWS_PROFILE` set to the default
  profile (when configured) and `AWS_DEFAULT_REGION` from the region field —
  the resulting account id / ARN are stored on the row (`cloud.identity`) and
  shown in the UI and `list_cloud_accounts`.
- `gcp` → `gcloud auth application-default print-access-token` with
  `GOOGLE_APPLICATION_CREDENTIALS` pointing at the materialized key (that is
  the gcloud variant that honors ADC); the token itself is discarded — only
  the project + service-account email are recorded.

Both require the matching CLI in the image. When it is missing, provisioning
(and the chat tools) fail with an explicit
`aws CLI not in image — build with --build-arg EXTRA_CLOUD_CLIS=aws`
(or `…=gcloud`) instead of a confusing exec error.

### k8s entries referencing a cloud account

A `k8s` entry can point at a cloud account instead of carrying its own copy
of the cloud credentials: pick it in the **Cloud account** dropdown (stored
as `cloud.cloud_slug`), optionally with an **AWS profile override** for that
cluster. Every kubectl call (and the provision connectivity check) then
resolves the account row's credentials at execution time, with

- `AWS_PROFILE` = the k8s entry's profile override, else the account's
  default profile;
- the account's credentials file / SA key winning over any inline copies the
  k8s entry still has (inline remains the fallback, so existing entries keep
  working unchanged — referencing an account is opt-in).

One AWS account row with `[prod]`/`[staging]` profiles can therefore back
several EKS clusters, each selecting its profile — rotate the keys in one
place. A dangling reference (account deleted later) fails the call with a
clear 400; the API refuses to save an unknown/non-cloud `cloud_slug` up
front.

### Chat

Pandora gets two read-only tools:

- `list_cloud_accounts` — slugs, provider, status, default profile / project,
  and the identity recorded at the last provision.
- `cloud_identity` — runs the identity check live for one slug (optional
  `profile` override), e.g. "which principal is `aws-hikmah`'s `staging`
  profile?". Errors (missing CLI, bad credentials, unknown slug) come back as
  plain tool errors, never crashes.

## System monitoring (`hosts_aegis`)

The admin **System monitoring** page shows the live health of AEGIS's *own*
deployment — database latency, Temporal reachability, and the running container
services — so it needs to know where AEGIS itself runs. Flag the infra entry
for that machine with **This host runs AEGIS itself** (`hosts_aegis`). The page
lists services from that host, via its `docker_context` if set, otherwise over
SSH using its stored key.

On a shared Docker Swarm the host runs many stacks, so the service list is
**scoped to AEGIS's own stack** — it filters `docker service ls` by the
`com.docker.stack.namespace` label. The stack name comes from
**`aegis_stack_name`** (default `aegis`), editable under **Integrations →
System Monitoring**; leave it blank to show every service on the host (the
escape hatch). If AEGIS is deployed under a stack name other than `aegis`, set
this or the page will show nothing.

## Remote script / coding agents

The remote-script subsystem (chat's `run_script` infra tools, coding-CLI runs
via kimi/claude, workspace scans/mirrors, `gh pr create`) SSHes into one
designated host. That host is configured **from the admin UI**: any
`ssh_host`/`swarm`/`docker` entry has a collapsible **Coding agent (remote
script)** section, and the entry whose **Enabled** box is checked becomes the
remote-script host (the service layer enforces at most one). The
`RemoteScriptConnector` re-reads this configuration every ~30 s, so edits
apply without restarting core or the worker.

**SSH identity** comes from the entry itself: host, SSH user/port, and the
pasted (encrypted) **SSH private key** — decrypted and materialized to a
mode-0600 temp file per SSH invocation and deleted immediately after, exactly
like the kubeconfig/cloud credentials. No key file needs to live on any
volume. (`ssh_key_ref` still works as a bring-your-own-file fallback when no
key is pasted.)

### How-to: register the coding host

1. Create/edit the infra entry for the machine where your repos live
   (kind `ssh_host`), paste its SSH private key, and **Provision** to verify
   connectivity.
2. Open **Coding agent (remote script)** → **Configure** and fill in:
   - **Enabled** — makes this entry the remote-script host.
   - **Repo base** — the workspace root the fixed checkouts live under
     (e.g. `/home/deploy/Workspace`; repos are addressed as paths under it,
     like `acme/bcp`).
   - **Engine binary paths** — `claude` and/or `kimi` CLI paths on the host.
   - **Claude accounts** — named `CLAUDE_CONFIG_DIR`s for multiple Claude
     logins on the same host (e.g. `work → /home/deploy/.claude-work`,
     `personal → /home/deploy/.claude-personal`). **Default Claude account**
     is used by fallback (`engine_override`) runs; empty means the host's
     default `~/.claude`.
   - **Org routing** — rows of GitHub org → engine (+ account for claude).
     A repo whose org matches runs on that engine/account; everything else
     uses the **Default engine** (usually `kimi`). This replaces the old
     `AEGIS_REMOTE_SCRIPT_CLAUDE_ORGS` csv.
   - **tmux session / window cap** — live-attachable windows for agent runs.
   - **Kimi host (infra slug)** — optional: the slug of *another* infra entry
     whose machine runs kimi jobs (the canonical workspace host). It is
     probed before each run and **fails closed** to the base host when
     unreachable. Leave empty to run kimi on the base host.
   - **AEGIS self-repo path** / **Runbooks dir** — used by the
     `aegis_self_diagnose` chat tool and alert runbooks; usually fine left
     empty (env/image defaults apply).
3. Save. The entry shows a **coding host** badge; runs pick the config up
   within ~30 s.
4. **Register the repos the agent works on.** On the **Resources** page add a
   `repository` resource per repo. Its first-class fields (all saved under
   `metadata`, so no hand-editing JSON):
   - **Workspace path** — the checkout's path *relative to the coding host's
     repo base* (e.g. `acme/bcp` for `/home/deploy/Workspace/acme/bcp`). This
     is the directory the CLI `cd`s into and runs.
   - **GitHub repo** — `owner/repo`. Its **org** is the default engine/account
     selector (matched against the coding block's **Org routing**) and what
     alert investigation matches an incoming issue to.
   - **Coding-agent routing** (repository resources only):
     - **Enable alert / Sentry investigation** — the **allow-list gate**. Alert
       investigation only ever runs a coding agent on repos with this checked;
       everything else is ignored (an unknown GitHub repo seen in an alert is
       auto-added here *disabled*, for you to review and opt in). This is what
       "only the listed repos are included" means.
     - **Engine override** — pin this repo to `claude` or `kimi`, regardless of
       org routing. Blank = decide by org.
     - **Claude account** — a `CLAUDE_CONFIG_DIR` account label from the coding
       block's `engines.claude.config_dirs`; the claude run for this repo uses
       that profile. Wins over org routing. **Kimi ignores it** (no profile).
     - **Sentry project slug** — maps a Sentry issue (by its project slug)
       straight to this repo, deterministically, before any LLM guess.

   The fixed checkouts under the repo base are provisioned/mirrored by
   `WorkspaceRepoSyncFlow`, never cloned per-run — a missing path is a hard
   error, not a silent clone. Sentry alerts are additionally narrowed at fetch
   time by the `sentry_projects` setting (**Integrations → Sentry**,
   comma-separated project ids; blank = all) — that controls which issues are
   *pulled*; the per-resource **Sentry project slug** controls which repo an
   issue *routes to*.

   > Upgrading an existing deployment: mark your active repos
   > **Enable alert / Sentry investigation**, or alert investigation resolves
   > nothing (the allow-list starts closed). One-shot for the repos that already
   > have a workspace checkout:
   > `UPDATE resources SET metadata = jsonb_set(metadata,'{coding_enabled}','true') WHERE kind='repository' AND metadata->>'path' IS NOT NULL;`

### Verify the coding host

Drive the live connector from inside the running worker — it uses the same
DB-resolved config, decrypted key material, and SSH path as real agent runs:

```bash
docker exec -i <aegis_worker_container> python - <<'PY'
import asyncio, os
from aegis.db import create_pool          # registers the jsonb->dict codec the connector needs
from aegis.connectors.remote_script import RemoteScriptConnector

async def main():
    pool = await create_pool(os.environ["AEGIS_DATABASE_URL"].replace("+asyncpg", ""))
    c = RemoteScriptConnector(db_pool=pool, secret_key=os.environ["AEGIS_SECRET_KEY"])
    await c.ensure_config()
    print(await c.coding_settings())                    # -> source=db:<slug>, host, repo_base, binaries
    print(await c.run_on_host("", "whoami; hostname"))  # SSH reachability + key materialization
    print(await c.run_on_host("", "claude --version; kimi --version"))
asyncio.run(main())
PY
```

A healthy host prints `source: db:<slug>`, lands as the SSH user you configured,
and returns both CLI versions. `source: env` with an empty host means no entry
has the coding block enabled — or (when scripting your own check) that you used
a raw `asyncpg` pool instead of `aegis.db.create_pool`, which returns the
`coding` jsonb as a string and makes the connector silently fall back to env.

### Env fallback

When **no** entry has the coding block enabled, the connector behaves exactly
as before using the `AEGIS_REMOTE_SCRIPT_*` / `AEGIS_KIMI_CLI_BINARY_PATH` /
`AEGIS_CLAUDE_CLI_BINARY_PATH` env settings (including the env key-file path)
— existing deployments keep working unchanged. Once you enable a row, the row
wins wholesale for the SSH identity and coding settings; disable it to fall
back to env again.

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
- `list_cloud_accounts` / `cloud_identity` — registered cloud accounts (see
  the Cloud accounts section above).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Provision error `exec plugin: executable aws not found` | Image built without `EXTRA_CLOUD_CLIS=aws` |
| `aws CLI not in image — build with --build-arg EXTRA_CLOUD_CLIS=aws` (cloud entry / chat) | Same cause — the cloud-account gate reports it up front |
| Provision error `exec plugin: executable gke-gcloud-auth-plugin not found` | Image built without `gcloud` in `EXTRA_CLOUD_CLIS` |
| Provision error mentioning `getting credentials` / `ExpiredToken` | Auth env keys missing/wrong for this entry |
| Provision error `Unable to connect to the server` | API endpoint not reachable from the core container (VPN-only endpoint?) |
| `hosts_aegis` probe says `docker --context` failed | The entry has `docker_context` set — clear it so the probe uses SSH |
| `entry is read-only …` | Working as intended; uncheck Read-only to allow mutations |

Every provision failure records the failing step's stdout/stderr in the row's
provision log (**View log** in the UI).
