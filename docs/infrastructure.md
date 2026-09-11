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
account: `aws-prod`, `aws-staging`, `gcp-main`, … Each row carries its
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
  `profile` override), e.g. "which principal is `aws-prod`'s `staging`
  profile?". Errors (missing CLI, bad credentials, unknown slug) come back as
  plain tool errors, never crashes.

## Service state (deploy and maintenance windows)

The problem hub (`services/hub.py`, spec
`docs/superpowers/specs/2026-09-07-problem-hub-design.md`) keeps a
`service_state` row per subject saying what is happening to it right now.
While a subject is `deploying` or in `maintenance`, every occurrence the hub
ingests for it is stored and counted but raises nothing; the problem it
creates sits in status `suppressed`. When the window passes without a
`resolved` event, `HubSweepFlow` (every 5 min) opens the problem: the deploy
did not make it go away. `degraded` is information only; `ok` clears the row.
`subject: "*"` is a wildcard for a whole kind (`subject_kind: node`) or, with
`subject_kind: "*"`, everything — a planned power cut.

Three writers, all of which land on the same row:

- **The deploy job.** `POST /api/hub/service-state`, authenticated like the
  alert webhook (`X-Alert-Token` or `Authorization: Bearer`, the
  `alert_webhook_secret`). The Ansible role that deploys AEGIS posts
  `deploying` for `aegis_core`, `aegis_worker` and `aegis_comms` before the
  stack deploy and `ok` after the services are up. A hand-run
  `docker service update` deserves the same two calls:

  ```bash
  curl -sS -X POST "$AEGIS_URL/api/hub/service-state" \
    -H "X-Alert-Token: $AEGIS_ALERT_WEBHOOK_SECRET" -H 'Content-Type: application/json' \
    -d '{"subject": "chatapp_app", "state": "deploying", "minutes": 15, "set_by": "operator", "note": "rolling latest"}'
  # ... docker --context swarm-baa service update --image ... --force chatapp_app ...
  curl -sS -X POST "$AEGIS_URL/api/hub/service-state" \
    -H "X-Alert-Token: $AEGIS_ALERT_WEBHOOK_SECRET" -H 'Content-Type: application/json' \
    -d '{"subject": "chatapp_app", "state": "ok", "set_by": "operator"}'
  ```

  `minutes` bounds the window; omit it for open-ended and rely on `ok`.
- **Chat.** `set_service_state(subject, state, minutes, note)`, granted to
  the `infra` capability holder (seeded for `pandoras-actor`; a running
  deployment adds it to `agents.metadata.tool_set`). Withheld from coding
  runs on the MCP mount: a run that could open a window could silence the
  alert about itself.
- **The heartbeat.** A `deploying` service row older than two ticks whose
  service is no longer below its desired replicas is cleared automatically —
  the safety net for a deploy job that crashed before posting `ok`.
  `maintenance` rows are never auto-cleared.

### Backfilling the hub from the open alert tasks

Once, after the PR that moved the alert producers onto the hub deploys, turn
every open `#alert` task AEGIS created before it into a problem that owns that
task — otherwise the next occurrence of a known alert creates a second task:

Run it in the **worker** container, not core: it imports `aegis_worker` for
the same `extract_service_name` the coding lane uses, and the core image does
not carry that package. It reads the database URL from the container's own
environment.

```bash
# on the node running the worker
W=$(docker ps --filter name=aegis_worker -q | head -1)
docker cp scripts/hub_backfill.py $W:/tmp/hub_backfill.py
docker exec $W python /tmp/hub_backfill.py            # dry run
docker exec $W python /tmp/hub_backfill.py --apply    # writes
```

Read the dry run before applying. A task whose class and subject it cannot
read gets an empty key and therefore its own problem — check that the ones it
DID key match what the producer computes, because a task keyed differently
from its producer will be duplicated by the next occurrence rather than
attached to.

It read the retired `alert_dedup_index` for recurrence counts while that table
existed. Migration 037 dropped it after the 2026-09-08 backfill, so a later run
seeds every problem it creates at one occurrence.

### Rolling the problem hub out

The hub replaces the old alert dedupe machinery in place, so the order matters.

1. **Deploy the images.** Migrations 030-035 apply on Core startup: the
   `problems` / `problem_events` / `problem_links` tables, `service_state`, the
   `work_sessions` widening, and the drops of `alert_mutes` and the digest
   buffer. Nothing else is needed for alerts to start flowing onto the hub.
2. **Merge the Ansible hook** (homelab-gitops) that posts
   `POST /api/hub/service-state` at the top and bottom of a rollout. Held back
   until now on purpose: merging it can trigger a deploy, and the endpoint has
   to exist first.
3. **Run the backfill** (see above), dry run then `--apply`, in the worker
   container. Read the dry run first: a task it keys differently from its
   producer will be duplicated by the next occurrence rather than attached to.
4. **Grant the tools.** `config/seed/agents.yaml` only seeds an agent with no
   `metadata.tool_set`, so a running deployment needs the SQL below.
5. **Check it.** The admin **Problems** page is the fastest look: the live
   list, what each problem's timeline says, and the windows in force. The
   queries below answer the same questions in SQL.

```sql
-- 4. grant the hub's operator tools to every agent whose tool set is an array
UPDATE agents SET metadata = jsonb_set(metadata,'{tool_set}',
  (metadata->'tool_set') || '["task_context","report_progress","merge_problems"]'::jsonb)
WHERE active AND jsonb_typeof(metadata->'tool_set') = 'array'
  AND NOT (metadata->'tool_set' @> '["task_context"]'::jsonb);

-- 5a. every open alert task should belong to exactly one problem
SELECT t.id, t.content FROM todoist_tasks t
LEFT JOIN problems p ON p.todoist_task_id = t.id
WHERE t.source_tag = '#alert' AND NOT t.is_completed AND p.id IS NULL;

-- 5b. no live problem should hold two tasks
SELECT problem_id, count(*) FROM problem_links
WHERE link_kind = 'todoist_task' GROUP BY 1 HAVING count(*) > 1;

-- 5c. what the hub has seen since the deploy
SELECT status, count(*), sum(occurrences) FROM problems
WHERE last_seen_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC;

-- 5d. windows in force (should be empty outside a deploy)
SELECT subject, state, until_at, set_by FROM service_state ORDER BY updated_at DESC;
```

`alert_dedup_index` was dropped by migration 037 after the 2026-09-08
backfill, which was its last reader. The recurrence history it held is now
`problems.occurrences` and one `problem_events` row per occurrence.

### Groups: one problem for the same failure on many things

Six Postiz posts wedged in the same queue used to be six problems and six
Todoist tasks. `HubSweepFlow` now folds a cluster like that into one **group**
problem, and every later occurrence of the class joins it instead of opening
another task. Migration 040 adds the `problems.group_key` column and the
partial unique index that keeps one live group per class and kind.

**How a group happens.** Every five minutes the sweep looks for three or more
live, ungrouped problems sharing a class and a subject kind, seen in the last
72 hours. If it finds one, it spends a single `think()` call (purpose
`hub_group_judge`, so it shows on the admin **Models** page like any other)
asking whether they are one condition. Only a yes folds them. A no is cached
in `settings.hub_group_verdicts` for 24 hours and re-asked early only if the
cluster grew, so the sweep does not re-price the same question every tick —
most ticks make no call at all. At most two clusters are judged per tick.

The first run in production, 2026-09-08, is the shape to expect:

```
stuck_post:post              grouped=true  n=5
  "All posts are stuck in the same queue (Postiz QUEUE), indicating a single
   queue drainage issue rather than individual post failures."
swarmoverlayblackhole:service grouped=false n=3
  "Different hosts (pop-think-os, daal, meem), different overlay networks
   (monitoring vs traefik_public), and different endpoint counts suggest
   independent network partitioning issues requiring separate investigation."
```

Both judgements cost $0.0009 together.

**What you see.** The surviving task is renamed for what it now covers ("5
posts stuck in Postiz QUEUE"), gets a comment naming the members folded in,
and its status block gains a `Group:` line and a `problem:` link per member.
Each swallowed task is completed with a note pointing at the survivor. A card
goes to the infra agent's channel saying what was grouped and why.

**What it will never do**, enforced in code rather than left to the judge:
group across classes; group the `manual` class (those problems ARE hand-written
`@code` tasks, and folding two would move one task's sessions and PR links onto
another); or group on the count alone.

**Recovery.** `hub_watch.reconcile_findings` resolves a group only when its
watchdog stops finding *any* member of the class — a group's `*` subject is
never among the findings, so without that it would resolve every tick.

```sql
-- which groups exist, and what each swallowed
SELECT id, group_key, title, status, occurrences, todoist_task_id FROM problems
WHERE group_key IS NOT NULL AND closed_at IS NULL;

SELECT payload FROM problem_events
WHERE payload->>'action' = 'grouped' ORDER BY id DESC LIMIT 5;

-- clusters the next sweep would consider (mirrors hub_group.candidates)
SELECT class, subject_kind, count(*) FROM problems
WHERE closed_at IS NULL AND status NOT IN ('resolved','closed') AND group_key IS NULL
  AND class <> '' AND class <> 'manual' AND subject <> '' AND subject_kind <> ''
  AND last_seen_at >= now() - interval '72 hours'
GROUP BY 1,2 HAVING count(*) >= 3;

-- what the judge has already decided
SELECT jsonb_pretty(value) FROM settings WHERE key = 'hub_group_verdicts';
```

**To unpick a group**, take the members from its timeline (the `grouped` event
lists their subjects, and `problem_links` holds their ids) and reopen the ones
that deserve their own problem. Nothing was deleted: each member is a closed
problem with its events moved onto the group and a link back.

**To make the sweep forget a verdict** — you disagree, or the situation
changed — delete that key from the cache and it asks again on the next tick:

```sql
UPDATE settings SET value = value - 'stuck_post:post', updated_at = now()
WHERE key = 'hub_group_verdicts';
```

**To stop it grouping one particular cluster**, write the "no" yourself. The
sweep honours a cached verdict, so a hand-written one keeps it away for 24
hours at a time, and for good while the member count stays at or below what
you record:

```sql
UPDATE settings SET value = value || jsonb_build_object(
  'stuck_post:post', jsonb_build_object(
    'decided_at', now()::text, 'grouped', false,
    'member_count', 999, 'reason', 'operator: keep these separate')),
  updated_at = now()
WHERE key = 'hub_group_verdicts';
```

There is no threshold knob on the sweep's `activities.config` row yet
(`HubSweepConfig` carries the fields; the registry does not populate them —
issue #448). Until it does, the verdict cache above is the control, and
turning the whole sweep off is not an alternative: it also stops suppression
promotion and projection.

### A problem and its task stay in step

The task is a view of the problem, and four rules keep the two agreeing
(#473):

- **Completing the task resolves the problem.** Every five minutes
  `HubSweepFlow` looks for live problems whose task the Todoist mirror shows
  completed — ticked off in Todoist, or through the `complete_task` chat tool
  — and resolves them. The timeline records it as a `resolve` with the reason
  "its Todoist task was completed by a person, not by the hub", and the task
  gets one comment saying so. If the problem comes back within the 24-hour
  reopen window, the problem reopens and so does the task; after that it is a
  new problem with a new task.

  Not every completed task on a live problem is a person's doing. When the
  completion is **older than the problem's latest return**, it is the hub's
  own close, and the reopen that followed never reached Todoist: the sweep
  reopens the task instead. That happens because TodoistSyncFlow applies
  Todoist's changes before it drains the outbox, so the mirror can be a tick
  stale when the projector reads it (prod problem 2140a366 kept a closed task
  for three days this way). A return the task has **not been told about yet**
  — under a mute, or inside a deploy window — is left alone until the
  projector may tell it.
- **A mute silences occurrences and returns, not recovery.** A muted problem
  that resolves still closes its task, within one sweep, with one comment.
  Occurrences under a mute are counted on the problem and never commented.
  A problem that comes back under a mute stays quiet — the Problems page and
  the digest still show it open — and its task reopens, with one comment,
  when the mute ends.
- **A problem that is over before it has a task never gets one.** Something
  that failed inside a deploy window and recovered before it ended is
  recorded and counted, and creates nothing. If it comes back, it gets its
  task then.
- **A backlog is told once.** When several resolves and returns wait for one
  projection (a long mute, Todoist down), the task gets one comment for where
  things ended up — "Resolved … It came back 4 times since the last update" —
  not one per turn. Occurrences were already collapsed to one "N more"
  comment per 30 minutes.

Two smaller gaps close with them. A task created through the outbox (Todoist
had a transient error at capture) is found by the real id the drain stores
on its `todoist_outbox` row; the projector used to wait for that id on the
capture idempotency row, where the drain never writes it. And a problem that
resolves before the next TodoistSyncFlow has mirrored its new task still has
that task closed; the close used to be refused for a task missing from the
mirror.

To check for drift by hand:

```sql
-- a live problem whose task is completed: the next sweep resolves it, or
-- reopens the task, unless a return is still waiting to reach the task
SELECT p.id, p.status, p.class, p.subject, t.completed_at FROM problems p
JOIN todoist_tasks t ON t.id = p.todoist_task_id
WHERE p.closed_at IS NULL AND p.status NOT IN ('resolved','closed') AND t.is_completed;

-- a resolved problem whose task is still open: expected only for a task
-- somebody claimed with @me
SELECT p.id, p.class, p.subject, p.resolved_at, t.assignee_label, p.muted_until
FROM problems p JOIN todoist_tasks t ON t.id = p.todoist_task_id
WHERE p.closed_at IS NULL AND p.status = 'resolved' AND NOT t.is_completed;
```

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

The remote-script subsystem (chat's `run_infra_script` and the other infra tools, coding-CLI runs
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

### Session inventory

Before starting a coding run, AEGIS can check whether one of your own Claude
Code sessions is already busy in the same repo, and skip rather than compete
with you. It reads `claude agents --json` — the documented, TTY-free listing —
once per configured account, over the SSH identity the coding host already uses.
Nothing is stored: the inventory is read fresh each time.

```json
"inventory": {
  "enabled": false,
  "skip_when_busy": true,
  "accounts": []
}
```

- `enabled` — off by default. Turn it on deliberately: it changes whether runs
  start. Off means not one extra SSH round trip.
- `skip_when_busy` — set false to log collisions without acting on them, so you
  can watch what it would do before letting it decide.
- `accounts` — restrict to some of `engines.claude.config_dirs`; blank means all.
  An account label that is not a `config_dirs` key is rejected when you save,
  because it would otherwise enumerate nothing and silently disable the check.

A busy session only blocks a run when it is human-owned. AEGIS's own runs live in
`<repo>-aegis-wt/<run_id>` worktrees and are recognised as its own, so runs never
block each other. Only `busy` sessions count — an idle session parked in a
directory is not someone mid-thought.

Any failure to read the inventory fails open and the run starts, which is the
behaviour without this feature at all.

Skipped runs appear in `workflow_runs` with `result_summary.reason = "repo_busy"`,
and log `coding_run_skipped_repo_busy`. Ask any agent holding the
`list_coding_sessions` tool what is currently open on the host.

One consequence worth knowing: a skipped **Todoist** task is retried after the
sweep's `cooldown_hours` (six by default), not on the next fifteen-minute tick,
because its workflow completed. Lower `cooldown_hours` if you want it sooner.
### How a run authenticates to AEGIS (mount tokens)

A claude run mounts AEGIS's own tools over MCP at
`{mcp_server_url}/api/mcp-server/{agent_id}`. The credential written into that
run's config file is a **mount token**: an HMAC over the agent id, the gated
flag and an expiry, signed with `AEGIS_SECRET_KEY`.

It is not the shared API key, and that is the point. A run reads untrusted
content by design, and an ungated one has a shell, so it can read its own config
file. A shared key found there would be full API access that never expires, and
could be used against any other agent's endpoint by changing one path segment.
A mount token instead:

- opens only its own `{agent_id}` — another agent's endpoint returns 403;
- opens only its own mode — a gated run cannot present its token at the ungated
  URL to escape the approval gate;
- expires, so a token printed into a transcript and delivered to chat ages out.
  The TTL follows the run's own deadline where the caller knows it, and is
  otherwise six hours.

Verification is stateless: Core recomputes the signature with the same secret.
No table, no lookup on the auth path, and nothing to revoke when a run dies with
the power.

Set `AEGIS_SECRET_KEY`. Without it no token can be signed and the mount falls
back to the shared API key, which is logged as
`mcp_mount_token_unavailable_using_shared_key` — the weaker posture, kept only
so such a deployment is not left with toolless runs.

### Driving runs from your own terminal (the operator mount)

`POST /api/mcp-server/{agent_id}/operator` is the mount for a human's session
rather than a run's. Same agent, same tool set, plus the tools a run mount
withholds — so you can start, inspect and stop coding work from whatever editor
session you are already in.

Add it to your CLI once:

```bash
claude mcp add --transport http aegis-operator \
  https://<your-core-url>/api/mcp-server/<agent>/operator \
  --header "X-API-Key: <your AEGIS API key>"
```

Then, in any session: *"what's running on the coding host?"* (`list_coding_sessions`),
*"have sebas look at this Todoist task"* (`dispatch_agent_run`), *"stop run
a1b2c3"* (`stop_agent_run`).

**This endpoint requires a real API key even when `AEGIS_AUTH_DISABLED=true`**,
and refuses a run's mount token outright. That asymmetry is the design: the
credential it needs is never written to the coding host, so a run cannot escalate
from "use my tools" to "start and stop runs" however much of its own filesystem
it reads. `stop_agent_run` is withheld from run mounts for the same reason — a
run able to stop runs could kill a sibling, or the run you are waiting on.

Passing `todoist_task_id` to `dispatch_agent_run` ties the run to that task with
a deterministic workflow id, so asking twice cannot start a second session on the
same work.

Stopping kills the run's tmux window. The flow notices on its next poll, reports
the run as failed, and cleans up the worktree — so there is no half-stopped
state. "No live tmux window" is a normal answer: the run may have finished, or
have been launched detached past the tmux window cap.

### Putting your own sessions on the record (the session hooks)

`work_sessions` is the registry of who is on which task: AEGIS's own coding
turns write to it, and your sessions write to it through `report_progress` on
the operator mount. While your row says `active`, AEGIS stays out of that task
and tells you in Slack that your comment is waiting for you in the session you
already have open.

That only works if something calls the tool. A tool nobody calls is a tool that
does not exist, so wire two hooks into your own `~/.claude/settings.json` —
they live in your dotfiles, not in this repo, because they are about your
machine:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/aegis-session.sh start"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/aegis-session.sh stop"
          }
        ]
      }
    ]
  }
}
```

The script decides whether the session is on an AEGIS task at all, and says
nothing when it is not:

```bash
#!/usr/bin/env bash
# ~/.claude/hooks/aegis-session.sh — tell AEGIS which task this session is on.
set -euo pipefail

# A task session runs in `<repo>-aegis-wt/task-<id>`; anything else is not on
# a task unless AEGIS_TASK says so. Silence is the correct answer for an
# ordinary session in an ordinary checkout.
task="${AEGIS_TASK:-}"
if [ -z "$task" ]; then
  case "$PWD" in
    *-aegis-wt/task-*) task="${PWD##*/task-}" ;;
    *) exit 0 ;;
  esac
fi

case "${1:-start}" in
  start)  status=active; summary="opened a session here" ;;
  stop)   status=parked; summary="stepped away" ;;
esac

curl -fsS -X POST "$AEGIS_URL/api/mcp-server/pandoras-actor/operator" \
  -H "X-API-Key: $AEGIS_API_KEY" -H "Content-Type: application/json" \
  -d "$(jq -nc --arg t "$task" --arg s "$summary" --arg st "$status" \
        --arg sid "${CLAUDE_SESSION_ID:-}" --arg acct "$(basename "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")" \
        '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"report_progress",
          arguments:{task_id:$t,summary:$s,status:$st,session_id:$sid,account:$acct}}}')" \
  >/dev/null || true
```

Three things about it are deliberate. It **fails open** (`|| true`): a hook
that breaks your session because AEGIS is down is worse than an unrecorded
session. It sends the **account** (`CLAUDE_CONFIG_DIR`'s basename), which is
what lets a later AEGIS turn resume under the same login. And `stop` parks
rather than finishing: only you know whether the work is done, and
`report_progress(status='done')` from inside a session is how you say so.

Without the hooks the tools still work — call `task_context` and
`report_progress` by hand — but the registry then only knows what you remember
to tell it.

### Task sessions (comment-driven coding)

A `@code` Todoist task gets one persistent Claude Code session in its own git
worktree, driven by comments on the task. Three places configure it, none of
them code:

- **Coding block** on the coding-host infra entry: **Default engine** `claude`
  (`routing.default_engine`) and a **Default Claude account**
  (`engines.claude.default_account`). A turn resumes a session by id, which
  only works when every turn lands on the same engine and the same login.
- **`activities.config` for `agent-task-15min`** — `max_coding` (default 3),
  the ceiling on coding turns started per sweep, new and resumed together; and
  `turn_timeout_minutes` (default 60), after which a turn is killed and
  reported.
- **`activities.config` for `cleanup-daily`** — `task_session_days` (default
  7). A session whose task is completed or gone, and idle that long, has its
  worktree removed and its row deleted by `CleanupFlow`. The branch stays; it
  may back an open PR. Set to 0 to disable.

Optionally grant the `comment_on_task` tool. A turn does **not** need it — a
turn's own reply is posted by the flow's `comment` activity, and the tool is
deliberately withheld from run mounts so a run cannot comment its way into
triggering its own next turn. It is for **you and the chat agents**: it posts a
note in your voice on a task that already has a coding session, which is what
starts that session's next turn. It refuses any task without one.

Grant it to every active agent whose tool set is an array — an agent with no
`tool_set`, or one holding anything else, is left alone:

```sql
UPDATE agents SET metadata = jsonb_set(metadata,'{tool_set}',(metadata->'tool_set')||'["comment_on_task"]'::jsonb) WHERE active AND jsonb_typeof(metadata->'tool_set') = 'array' AND NOT (metadata->'tool_set' @> '["comment_on_task"]'::jsonb);
```

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

## The books (hledger)

Maou keeps double-entry books as an hledger journal in a private git repo. Money
mail becomes a journal block; bills and failed payments also become dated Todoist
tasks. Design:
[`superpowers/specs/2026-09-05-maou-books-design.md`](superpowers/specs/2026-09-05-maou-books-design.md).

Both the core and worker images ship `hledger` 1.52.3 and `git`. The working copy
lives at `books_path` (default `/app/config/books`) on the config volume core and
worker share — **one** checkout, not one per container. The worker's flows write
it; core installs the deploy key at boot and hosts the same `books.py`, so an
`flock` on `<books_path>/.aegis.lock` serialises writes across both processes.
Every write pulls with `--rebase --autostash`, runs `hledger check --strict`, and
reverts just the paths it touched if that fails. A push that fails is logged, not
raised: the commit stays local and the next write pushes it.

Configure on the admin **Integrations** page, group *Books*. These are DB-owned
settings, with the matching `AEGIS_BOOKS_*` env vars as first-boot fallback; core
and worker must both restart to pick a change up. `books_path` is the exception —
it is env-only (`AEGIS_BOOKS_PATH`), because it is a container path, not a choice.

| Key | What it is |
|---|---|
| `books_repo_url` | The books repo, SSH form (`git@github.com:<org>/books.git`). Empty = posting disabled: money mail is still parsed and indexed, never written to a journal |
| `books_deploy_key` | The private half of an ed25519 deploy key with write access on that repo. Paste the PEM or its base64 |
| `books_ignored_mailboxes` | Comma-separated mailbox labels whose money is not yours (an employer's account, say). Their mail is classified `ignore` |
| `books_mailbox_entities` | `label=entity,...` where entity is `personal` or `hikmah` — which set of books a mailbox's money belongs to. An unlisted mailbox is `personal` |
| `books_todoist_projects` | `personal=<project id>,hikmah=<project id>` — where dated dues are captured. Unset = the Inbox |

The whole money lane, books included, is gated on **Money Hygiene**
(`money_hygiene_enabled` / `AEGIS_MONEY_HYGIENE_ENABLED`). With that off no money
flow is scheduled and `MoneyActivities` is never constructed, so setting a repo
URL alone does nothing.

Each `MoneyProcessFlow` run reports what happened to its one email: `posted`,
`linked` (enriched the counterpart's block instead of writing a second one),
`indexed`, `ignored` or `duplicate`. Five outcomes leave the receipt below
`parsed.version = 2` so the weekly sweep re-drives it: `load_failed`,
`extract_failed`, `parse_failed`, `books_disabled` and `post_failed`. The last
two are deliberate. `books_disabled`: with no repo and no checkout the event
reaches the index but never a journal, so the row is not finished, and
configuring a repo later replays the whole backlog through the sweep.
`post_failed`: the books refused the block or could not accept it, so the event
is indexed with no `journal_file` and the weekly sweep retries it. The usual
cause is `hledger check --strict` on a chart mismatch — an account or a
commodity nobody declared — but the same status covers a books repo that could
not be pulled or an hledger that is missing or broken, which is why it is named
for the outcome and not for the check. Whatever the cause, the activity returns
this rather than raising: an uncaught error burned all three attempts and failed
the run every week without ever posting. Grep the worker log for
`money_post_failed`, which names the msgid and the underlying error. The status
is in `workflow_runs.result_summary`.

The admin **Money** page carries two review counters, and each one means
something narrower than its label. *Unexplained* counts transactions still
sitting in an `:unknown` account, over a rolling 60 days. *Dues open* counts
bills and failed payments nothing has been linked to — **excluding a
zero-amount invoice**, which is not an obligation and which nothing can ever
close (a payment matches a due on its amount, and no ₹0 payment mail arrives).
`capture_due` already refuses to raise a task for one, so counting it as
outstanding was the index disagreeing with that. A due whose amount the
extractor never got is a different thing and IS still counted: a bill of
unknown size is still a bill, and it is the counter's job to say so. Both rows
stay in the events table either way — the index records what arrived.

### The deploy key

Generate a key pair, register the public half, paste the private half:

```bash
ssh-keygen -t ed25519 -N "" -f books_deploy_key -C aegis-books
gh api repos/<org>/books/keys -f title=aegis \
  -f key="$(cat books_deploy_key.pub)" -F read_only=false
```

Put the private key in `books_deploy_key` on the Integrations page and restart
core and worker. At boot each process writes it to
`<gmail_token_dir>/books_deploy_key` with mode 0600 and points its SSH command at
it. The value is never logged. Rotate by replacing the setting and restarting.

A malformed key does not fail boot. It logs `books_deploy_key_install_failed` and
the process carries on with no key on disk, after which the checkout cannot
authenticate and every journal write raises instead of posting. Grep the boot log
for that line after setting or rotating the key.

### Backfill

`ReceiptIngestFlow` is the backfill vehicle. Its weekly run already sweeps every
`finance.receipt_email` row below `parsed.version = 2` back through the books
pipeline, oldest first; a manual run with a wider window and a bigger batch
drains an existing backlog:

```bash
curl -X POST https://<aegis>/api/admin/money/receipt_scan/run \
  -H "X-API-Key: $AEGIS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query_window": "after:2026/06/30", "max_per_account": 600, "sweep_limit": 500}'
```

The body is the flow's input, so any `ReceiptIngestInput` field works. Two things
to know. The endpoint returns 409 when Money Hygiene is off. And leave
`sender_filter` alone unless you mean to change it — the default is the bank and
vendor sender list, and setting it to an **empty string** does not disable
filtering, it produces an unfiltered whole-mailbox query. Give a real filter or
omit the key.

A manual run gets no `aegis_ui_url` (only the scheduled builder injects it), so
if a mailbox's Gmail token has expired the re-auth card it raises carries a
relative, unusable link. Re-authorise from the Google accounts block on the admin
**Flows** page first, or pass `aegis_ui_url` in the body.

### Ledger tools

Four chat tools work the books (`core/src/aegis/services/tools/ledger.py`). Ask
Maou in chat; there is no separate UI.

| Tool | What it does |
|---|---|
| `ledger_query` | A read-only hledger report — `bal`, `reg`, `is`, `bs`, `cf`, `print`, `accounts`, `payees`, `tags`, `stats`, `activity`, `aregister`, `check`. Text, JSON or CSV, capped at 12,000 characters |
| `ledger_post` | Records one transaction by hand. Two or more postings, at most one without an amount |
| `ledger_reclassify` | Moves one posting to another account by its msgid, and optionally renames the payee |
| `ledger_add_rule` | Appends a rule to `rules/accounts.yaml` and, unless you say otherwise, refiles the postings already sitting in an `:unknown` account that it matches |

Every write goes through `books.py`: one flock, `hledger check --strict`, and a
revert of just the paths it touched when the check fails. All three writers
refuse an account the chart does not declare — the chart is yours, and the
strict check would reject the block anyway.

The write itself does not happen in the chat turn. The three writers validate
what they can, hand the write to `BooksWriteFlow` on the worker, and wait 20
seconds. Nearly always that is enough and you get the answer in the reply. When
it is not — usually because another write holds the flock — Maou says the write
is still running and names its workflow id, and the flow sends you the outcome
when it lands. Two consequences worth knowing: asking twice for the same write
attaches to the one already running rather than starting a second, and with
Temporal unreachable a write is refused outright rather than half-done.

Three behaviours are worth knowing before you use them.

- **A re-post is a retry, not a second transaction.** `ledger_post` derives the
  msgid from the rendered block, so calling it again with the same date, payee,
  postings and note finds the first block and writes nothing. That is what makes
  a timed-out write safe to repeat. A genuine second identical payment needs a
  distinguishing `note`.
- **`ledger_add_rule` refuses a slow regex.** The pattern is persisted and the
  worker then runs it against every money event, in another process, forever, so
  a pattern that repeats a group, stacks quantifiers or simply measures slow is
  turned away with a message saying what to change. The first three of those
  bounds are applied AGAIN when `rules/accounts.yaml` is read, so a rule you
  hand-edit into the file is held to the same standard as one the tool wrote:
  it is skipped, with a warning naming it (`books_rule_skipped`), rather than
  run. The timing probe is write-time only — it forks a process, which is not a
  price the ingest lane can pay per rule per email. The same loader skips a
  rule whose optional `direction` is neither `in` nor `out`.
- **A rule may name a direction, and by default does not.** `direction: in` or
  `direction: out` makes the rule fire only on money moving that way; leaving
  it out means either way, which is what every rule written before the field
  existed means. Reach for it when the same name moves money both ways and the
  two belong in different accounts — a person you both pay and are paid by —
  so a payment is not filed into the income account you picked for a credit.
  It is never derived from the account, because an account says what a posting
  is *for*, not which way the money went. The chart says so itself:
  `equity:transfers` is declared "between own accounts when the far side is
  unknown", and a transfer moves either way. Deriving `out` from an expense
  account would also narrow 26 of the 28 rules in the live file at a stroke,
  none of whose authors asked for it. The curiosity answer hook is the
  exception and always stamps one, because it knows which way its card asked
  and nobody reviews what it writes.
- **One sweep is capped at 200 postings**, written as a single commit. Past that
  the tool asks to be run again rather than rewriting the whole backlog in one
  unreviewable change.

`ledger_query` is the only one a coding run can reach; the MCP server withholds
the three writers. The operator mount serves all four.

**Granting them is a database write.** `config/seed/agents.yaml` grants all four
to Maou and `ledger_query` to Sebas, but the seed merges only keys the agent's
`metadata` does not already have — and any deployment that has run once already
has a `tool_set`. So the yaml is a first-boot default, and an existing
deployment needs the grant applied itself:

```sql
UPDATE agents SET metadata = jsonb_set(metadata, '{tool_set}',
  (metadata->'tool_set') || '["ledger_query","ledger_post","ledger_reclassify","ledger_add_rule"]'::jsonb)
WHERE id = 'maou' AND jsonb_typeof(metadata->'tool_set') = 'array'
  AND NOT (metadata->'tool_set' @> '["ledger_post"]'::jsonb);
UPDATE agents SET metadata = jsonb_set(metadata, '{tool_set}', (metadata->'tool_set') || '["ledger_query"]'::jsonb)
WHERE id = 'sebas' AND jsonb_typeof(metadata->'tool_set') = 'array' AND NOT (metadata->'tool_set' @> '["ledger_query"]'::jsonb);
```

Both statements skip an agent that already has the grant, so they are safe to
re-run. The admin **Agents → Behavior** tab does the same thing by hand. Core
reads `tool_set` per request, so no restart is needed.

**The persona is a second database write.** Granting the tools does not tell
Maou they exist. Her prompt is the `agent_personalities` row, and
`personalities/maou/*.md` in this repo only seeds it on a deployment's first
boot — so an existing deployment still describes the v1 subscription tracker
this release deleted, and describes no books, no journal and no ledger tool.
Copy the updated `personalities/maou/SOUL.md` into the **soul** document on the
admin **Agents → Maou** page (or `PUT /api/admin/agents/maou/personality`).
Without it she has four tools she has never been told about, and the model
picks them up only if a message happens to name one.

## Chat

Pandora's infra tools work against registry clusters by slug:

- `list_pods` / `list_deployments` / `get_pod_logs` — pass a registry entry's
  slug as `context` (script-host contexts keep working unchanged; those run on
  the remote script host, not through the registry).
- `restart_deployment` — registry-only, refused for read-only entries.
- `restart_service` (swarm) — refused when the matching registry entry is
  read-only.
- ArgoCD tools are script-host only (they need the `argocd` CLI, not just a
  kubeconfig) — `context` must be one of the names configured in
  `AEGIS_SCRIPT_HOST_K8S_CONTEXTS`.
- `list_cloud_accounts` / `cloud_identity` — registered cloud accounts (see
  the Cloud accounts section above).

### Script-host k8s contexts

`list_pods` / `list_deployments` / `get_pod_logs` / `run_infra_script` and the
ArgoCD tools accept a `context` that is either the slug of a registered
`kind=k8s` infra entry (routed through the registry, no script-host setup
needed) **or** the name of a "context" that exists on the remote script host
— i.e. a `kubectl config get-contexts` entry the `scripts/infra/*.sh` scripts
know how to map, on the host `AEGIS_REMOTE_SCRIPT_HOST` SSHes into. The
script-host names are not auto-discovered; declare them via
`AEGIS_SCRIPT_HOST_K8S_CONTEXTS` (comma-separated, e.g.
`AEGIS_SCRIPT_HOST_K8S_CONTEXTS=prod,staging`). Blank (the default) means no
script-host k8s contexts exist — pod/deployment/log tools then only resolve
via registered `kind=k8s` slugs, and the ArgoCD tools have no valid context
until you configure at least one (they only run against the argocd CLI on the
script host, never a bare kubeconfig). When adding a name here, also update
the matching `case` branches in `scripts/infra/*.sh` (e.g.
`infra_list_pods.sh`, `infra_list_argocd_apps.sh`) so the script host actually
knows how to route that context name.

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
