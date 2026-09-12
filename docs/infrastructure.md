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

-- 5e. no task should be owned by two live problems (#472). A closed problem
-- is history, so a task may have one closed problem and one live one.
SELECT o.task_id, count(*) AS problems,
       string_agg(p.id::text || ' ' || p.correlation_key || ' ' || p.status, ' ; '
                  ORDER BY p.first_seen_at) AS which
FROM (SELECT problem_id, ref AS task_id FROM problem_links WHERE link_kind = 'todoist_task'
      UNION SELECT id, todoist_task_id FROM problems WHERE todoist_task_id IS NOT NULL) o
JOIN problems p ON p.id = o.problem_id
WHERE p.closed_at IS NULL
GROUP BY 1 HAVING count(*) > 1;

-- 5c. what the hub has seen since the deploy
SELECT status, count(*), sum(occurrences) FROM problems
WHERE last_seen_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC;

-- 5d. windows in force (should be empty outside a deploy)
SELECT subject, state, until_at, set_by FROM service_state ORDER BY updated_at DESC;
```

`alert_dedup_index` was dropped by migration 037 after the 2026-09-08
backfill, which was its last reader. The recurrence history it held is now
`problems.occurrences` and one `problem_events` row per occurrence.

### What can change a problem's status and severity

- **Only the alert source makes a problem live again.** An investigation
  adds notes; it does not decide. If the alert clears while an investigation
  is still running, the verdict (and its decision card, if it has one) still
  arrive and the
  verdict is on the problem's timeline, but the problem stays `resolved` and
  its task stays closed. The worker logs `hub_status_held` when this happens.
  If the thing breaks again within a day, the next occurrence reopens the
  problem and its task; later than that, it opens a new problem. Either way a
  fresh investigation starts (#484).
- **Severity only goes up.** Each occurrence keeps its own severity, and the
  problem takes the worst one it has seen. That is what the status block on
  the task shows. A milder occurrence later never lowers it (#486). Hub tasks
  get no Todoist priority from severity, so nothing else changes on the task.

Before #484 a late verdict reopened the problem. To find any still live from
that time, run the query below. Resolve each one from the admin **Problems**
page, unless its timeline shows an occurrence after the reopen.

```sql
SELECT p.id, p.status, p.class, p.subject, e.occurred_at AS reopened_at
FROM problem_events e JOIN problems p ON p.id = e.problem_id
WHERE e.source = 'hub' AND e.kind = 'state_change' AND e.payload->>'action' = 'reopen'
  AND e.external_id LIKE 'investigation:%'
  AND p.closed_at IS NULL AND p.status NOT IN ('resolved', 'closed');
```

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

**Strays.** A later post of the same class joins the group as it comes in.
One whose own problem was still live when the group formed — it recovered
during the fold, then came back and reopened — used to keep that problem and
its task for good, because a single stray is never three of a kind again
(#474). The sweep now folds such a stray into its group before it looks for
new clusters, with no model call: the group already stands for the class. The
stray's task is completed through the outbox with a note pointing at the
group, and the group's timeline gets a `grouped` event whose `reason` says it
was a stray. It folds only into a group that is live and not suppressed, and
only a stray seen in the last 72 hours.

**What it will never do**, enforced in code rather than left to the judge:
group across classes; group the `manual` class (those problems ARE hand-written
`@code` tasks, and folding two would move one task's sessions and PR links onto
another); group the `task` subject kind (a report keyed on the Todoist task it
came from — see below); group a money finding; or group on the count alone.

**Reports keyed on a task.** Clarify's content-route investigations carry a
class from the route (`alert_overrides`) and often no service. Such an event
used to be keyed `{class}::`, so every later one of that class attached to the
first problem and was never investigated (#472). `event_from_alert` now keys
an alert that names a Todoist task and nothing else on that task:
`nodedown:task:<task id>`. An alert with no subject and no task — an aggregate
alertmanager rule, a Sentry issue with no project — keeps its one key per
class. Clarify also no longer starts an investigation for a task the hub owns
(`#alert`, or any problem holds it): it records `hub_owned` and stamps `@next`.
A user's comment on such a task still starts one, on the task's own problem.

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
  AND class <> '' AND class <> 'manual' AND subject <> '' AND subject_kind NOT IN ('', 'task')
  AND last_seen_at >= now() - interval '72 hours'
GROUP BY 1,2 HAVING count(*) >= 3;

-- strays the next sweep will fold into a group (mirrors hub_group.absorb_strays)
SELECT g.group_key, p.id, p.subject, p.status, p.todoist_task_id FROM problems g
JOIN problems p ON p.class = g.class AND p.subject_kind = g.subject_kind AND p.id <> g.id
WHERE g.group_key IS NOT NULL AND g.closed_at IS NULL
  AND g.status NOT IN ('resolved', 'closed', 'suppressed')
  AND p.group_key IS NULL AND p.closed_at IS NULL AND p.status NOT IN ('resolved', 'closed')
  AND p.class <> 'manual' AND p.subject_kind <> 'task' AND p.subject <> ''
  AND p.last_seen_at >= now() - interval '72 hours'
  AND NOT EXISTS (SELECT 1 FROM problem_events e
                  WHERE e.problem_id IN (p.id, g.id) AND e.source = 'money');

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

**To keep one particular cluster apart for a day**, write the "no" yourself.
The sweep honours a cached verdict, so a hand-written one keeps it away — but
only for 24 hours from its `decided_at`, like any other verdict. After that it
has expired and the sweep asks the judge again; a cluster that grows past the
`member_count` you record is asked again sooner. There is no permanent opt-out:
to keep a cluster apart for longer, write the verdict again each day.

```sql
UPDATE settings SET value = value || jsonb_build_object(
  'stuck_post:post', jsonb_build_object(
    'decided_at', now()::text, 'grouped', false,
    'member_count', 999, 'reason', 'operator: keep these separate')),
  updated_at = now()
WHERE key = 'hub_group_verdicts';
```

**To raise the bar on a noisy class**, set the thresholds on the sweep's
`activities.config` row (#448). `group_min_members` is how many live problems
of one class and kind make a cluster worth judging, and `group_window_hours`
is how far back "live" reaches. Either at `0` keeps the defaults — 3 members,
seen in the last 72 hours. `schedule_sync` picks the change up within about
300 seconds, with no redeploy.

```sql
-- only judge a cluster of five or more, seen in the last day
UPDATE activities SET config = config || '{"group_min_members": 5, "group_window_hours": 24}'::jsonb,
  updated_at = now()
WHERE workflow_type = 'HubSweepFlow';
```

Raising the bar is not the same control as the verdict cache above: the
threshold decides what is ever *asked*, the cache decides what the answer was.
Turning the whole sweep off is not an alternative to either — it also stops
suppression promotion and projection.

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
     `aegis_self_diagnose` chat tool and the built-in alert runbook files;
     usually fine left empty (env/image defaults apply). Runbooks you write
     yourself go in the database instead: see "The runbook an investigation
     reads" below.
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

### Which repo an alert is investigated in

`AlertInvestigationFlow` picks the repo in this order:

1. **A label claim.** A repository with **Enable alert / Sentry
   investigation** on can claim alerts by label. Put it in the resource's
   **Additional metadata (JSON)** box:

   ```json
   {"alert_labels": {"code_location": ["analytics"]}}
   ```

   That claims every alert whose `code_location` label is `analytics`. List
   more labels to claim on any of them. Label names match exactly; values
   match case-insensitively. A claimed alert is investigated in that repo as
   application code: the run may stage a fix branch, it is not told it is
   looking at a swarm problem, and Gate-0 is skipped because the mapping is
   yours. This holds even when the alertname is on the infra list below. If
   two repos claim one alert, neither gets it, and the worker logs
   `alert_label_claim_ambiguous`.
2. **Infra alerts** go to the infra repo, investigate-only, after the one safe
   force-restart (once per problem per hour; see
   [below](#when-pandora-asks-you-and-the-automatic-restart)).
3. **Everything else** goes down the ladder: Sentry project slug, service
   name, token match, then the LLM, with Gate-0 confirming the pick.

#### The infra list and the infra repo

Both live in the `infra_alert_routing` settings row. Read and replace it over
the admin API:

```bash
curl -sS -H "X-API-Key: $AEGIS_API_KEY" "$AEGIS_URL/api/admin/infra-alert-routing"
curl -sS -X PUT "$AEGIS_URL/api/admin/infra-alert-routing" \
  -H "X-API-Key: $AEGIS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"extra_alertnames": ["Dagster Pipeline Failure", "ClickHouseDown"],
       "repo": "acme/infra-gitops",
       "platform_hint": "This cluster is Docker Swarm, three managers. Read it with `docker --context swarm node ls` and `docker --context swarm service ps <service>`; a stuck service usually takes `docker --context swarm service update --force <service>`."}'
```

- `extra_alertnames` are added to the built-in list, which `GET` returns as
  `default_alertnames`: the alerts AEGIS's heartbeat raises (`NodeDown`,
  `DockerServiceDown`, `ServiceDownProlonged`, `HeartbeatCollectFailed`) plus
  common host, container and monitoring-stack alerts. Anything specific to
  your setup goes here. Names are compared lowercased.
- `repo` is the `owner/name` (the resource's GitHub repo) that infra alerts
  are investigated in. Unset means infra alerts get an LLM-only investigation.
  It is also the repo that a connector or service alert expands to, on the
  grounds that the config which deploys a thing is as likely to be at fault as
  the thing. Before #505 that expansion looked for a repo whose path ended in
  `infra-gitops`, so it never fired for anyone who named theirs otherwise.
- `platform_hint` is one or two sentences saying what the cluster IS and how to
  read it. The instructions AEGIS puts in front of an infra investigation name
  no orchestrator, because it has no way to know whether you run Swarm, k8s,
  Nomad or a few systemd units — this is where you tell it. Up to 1000
  characters; it goes in front of everything else the agent reads. Leave it
  empty and the agent works it out from the infra repo.
- `PUT` replaces the whole row and answers 400 on a bad value. The worker
  picks a change up within 30 seconds; no restart.

#### Example: Dagster pipeline failures

Every Dagster job in every code location raises the same alertname, so the
alertname cannot say which repo broke. Keep it on the infra list, because a
run that dies before any step runs (user code unreachable, run worker killed)
is an infra problem. Then let each pipeline repo claim its own failures.

The label to claim on is the code location. Dagster records it on every run
as the `.dagster/repository` tag (`__repository__@<location>`). Have your
alert rule export it, but only when a step failed, so a run-level failure
keeps going to the infra repo. For a Grafana rule that selects from `runs r`
with the `STEP_FAILURE` event joined as `e`, add to the `SELECT`:

```sql
COALESCE(
  CASE WHEN e.step_key IS NOT NULL THEN
    (SELECT split_part(rt.value, '@', 2) FROM run_tags rt
      WHERE rt.run_id = r.run_id AND rt.key = '.dagster/repository' LIMIT 1)
  END,
  '-'
) AS code_location
```

Then claim each location on its repo:

```sql
UPDATE resources
SET metadata = metadata || '{"alert_labels": {"code_location": ["analytics"]}}'::jsonb
WHERE kind = 'repository' AND metadata->>'github_repo' = 'acme/analytics-pipeline';
```

Before the rule exports the location, a repo can claim by job name instead,
`{"alert_labels": {"pipeline_name": ["etl_daily", "etl_weekly"]}}`. That list
needs updating as jobs are added, and it cannot claim `__ASSET_JOB`, which
has the same name in every code location.

### The runbook an investigation reads

Every alert investigation starts with the runbook for its alert name, put in
front of the prompt, followed by past verdicts on similar alerts
(`AlertActivities.gather_alert_knowledge`; see
[what Pandora remembers](#what-pandora-remembers-from-your-decisions)). The
worker looks for the runbook in this order:

1. **The `runbooks` table.** Runbooks you write about your own setup: which
   machines share a power supply, which service is pinned to which node, what
   must never be restarted. Edit them on the admin **Runbooks** page or over
   the API below.
2. **`runbooks/<AlertName>.md`** from this repo, baked into the worker image
   at `/app/runbooks` (or the coding host's **Runbooks dir**). These are
   generic and host-free. A file that still says `TODO: fill in` is a stub
   and counts as no runbook.

A database error, or a read that takes longer than 5 seconds, falls through
to the file, with a `runbook_db_read_failed` warning in the worker log. A
runbook is context for an investigation, never a gate on it.

**Setup-specific runbooks belong in the table, not in `runbooks/`.** This repo
is public, and a fork should not inherit your machine names or topology. A
stored runbook replaces the file for that alert completely, so copy in any
generic steps you want to keep.

- **Names.** A runbook is keyed on its alert name with case and punctuation
  removed, so `NodeDown`, `node-down`, `Node Down` and `node_down` are one
  runbook. Use the alertname Prometheus sends, or the rule title for a Grafana
  alert (`Dagster Pipeline Failure`).
- **Limits.** A save answers 400 when the body is blank, still contains
  `TODO: fill in`, or is longer than 16,000 characters. Every runbook is
  prepended to a prompt, so keep it short: what the alert usually means on
  your setup, the first few read-only checks, what not to do, and when to hand
  it to a human.
- **When it applies.** The worker reads the table on every investigation, so
  a change applies to the next one; no restart. Saves and deletes are in the
  audit log (`runbook_saved`, `runbook_deleted`).

```bash
# List them (names and sizes, no bodies)
curl -sS -H "X-API-Key: $AEGIS_API_KEY" "$AEGIS_URL/api/admin/runbooks"

# Read one. URL-encode the name: "Dagster Pipeline Failure" is Dagster%20Pipeline%20Failure
curl -sS -H "X-API-Key: $AEGIS_API_KEY" "$AEGIS_URL/api/admin/runbooks/NodeDown"

# Create or replace one from a Markdown file
jq -Rs '{body: ., updated_by: "me"}' NodeDown.md |
  curl -sS -X PUT "$AEGIS_URL/api/admin/runbooks/NodeDown" \
    -H "X-API-Key: $AEGIS_API_KEY" -H 'Content-Type: application/json' --data-binary @-

# Delete one. The built-in file, if there is one, applies again
curl -sS -X DELETE -H "X-API-Key: $AEGIS_API_KEY" "$AEGIS_URL/api/admin/runbooks/NodeDown"
```

To load several at once from a file shaped `[{"name": "...", "body": "..."}]`:

```bash
jq -c '.[]' runbooks.json | while IFS= read -r rb; do
  name=$(jq -r .name <<<"$rb")
  code=$(jq '{body, updated_by: "runbooks.json"}' <<<"$rb" |
    curl -sS -o /tmp/runbook-resp.json -w '%{http_code}' -X PUT \
      "$AEGIS_URL/api/admin/runbooks/$(jq -rn --arg n "$name" '$n|@uri')" \
      -H "X-API-Key: $AEGIS_API_KEY" -H 'Content-Type: application/json' --data-binary @-)
  echo "$code $name"; [ "$code" = 200 ] || cat /tmp/runbook-resp.json
done
```

Two things with similar names are not this. The `update_runbook` chat tool
stores text in the knowledge store, where an investigation may find it through
the prior-incident search, but it is never the runbook. And resources of kind
`runbook` are not read by investigations at all.

### When Pandora asks you, and the automatic restart

**A decision card only when there is a decision (#500).** After a verdict,
`AlertInvestigationFlow` posts a Gate-2 card only when the card can do
something:

- the investigation staged a fix branch (**Open PR**),
- the verdict is `actionable` and the investigation proposed commands
  (**Run fix**),
- the alert escalates (a node down, the heartbeat unable to reach the swarm),
  which nags until you ack it, or
- the problem came back right after an automatic restart (below).

Any other verdict is told, not asked. It goes on the problem's task as a
comment, on the problem's timeline, and to chat as the usual verdict ping,
and the problem waits for you the way it did after an **Acknowledge**. The
task comment says no card was sent. To silence a problem that keeps coming
back, use **Mute** on the admin **Problems** page, the same 24-hour mute the
card had; for a longer window use `set_service_state`.

Commands earn a card only on an `actionable` verdict (#518). On an
`inconclusive` verdict they are a guess, and on a "no action needed" one they
contradict it: in the two weeks before this rule, 22 such cards drew 17 bare
acks and one **Run fix**. They go on the task comment instead, marked as not
run, so you can still run them by hand. Without commands the status earns no
card at all: an `actionable` verdict with no branch is work for you, but
nothing a card could approve.

`workflow_runs.result_summary` says what happened: `decision_card` (true or
false) and `restart_repeat`. To count cards per investigation:

```sql
SELECT result_summary->>'decision_card' AS card, count(*)
FROM workflow_runs
WHERE workflow_type = 'AlertInvestigationFlow' AND started_at > now() - interval '14 days'
  AND result_summary ? 'decision_card'
GROUP BY 1;
```

**One automatic restart per problem per window (#501).** A `DockerServiceDown`
or `ServiceDownProlonged` alert gets one `docker service update --force`
before anyone is asked. The flow records each attempt on the problem, with
what `docker service ps` said straight after it, recovered or not. If the
same problem comes back inside the window, it is not restarted again: a
restart that did not hold will not hold the second time either. Instead the
task gets a comment with the first restart's evidence and what changed since
(the tasks that are new, such as one the scheduler could not place), the
investigation is told not to propose the same restart, and one card goes out
whatever the verdict says. The problem is the identity, not the service
name, except that a restart of one service in a group problem does not count
against another.

The window is 60 minutes. Change it, or set `0` to restart every time as
before, in the `alert_remediation` settings row. The worker reads it on every
restart, so a change applies to the next alert; no restart. A value that is
not a whole number of minutes counts as 60.

```sql
INSERT INTO settings (key, value)
VALUES ('alert_remediation', '{"repeat_window_minutes": 60}')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
```

Both changes are behind `workflow.patched` ids, `gate2-only-for-decisions`
and `auto-restart-once-per-window`, so a run that was waiting on its card
when the worker was redeployed finishes the way it started.

### After Open PR: following the fix to a verified fix

**Pandora follows the PRs it opens (#502).** When you pick **Open PR(s)** on a
card, the flow pushes the fix branch, opens a draft PR, links it to the problem
(`problem_links`, kind `github_pr`) and leaves the problem in `fixing`. The
task comment says it is being followed. From there:

| What happens | Problem moves to | What the task says |
|---|---|---|
| The PR merges | `verifying` | Fix PR merged, and the alert is now being watched |
| The PR is closed without merging | `waiting_human` | The fix was not taken, so it is back with you |
| No occurrence for `fix_verify_hours` after the merge | `resolved` (the task closes) | The alert stayed clear for that long |
| The alert comes back later than `fix_grace_hours` after the merge | `open` | It came back, how long after the merge, and which PR |

- **What is followed.** Only a PR an investigation opened: the problem
  carries an `investigation` event naming it in `pr_urls`. A PR a coding
  session links with `report_progress` is not followed — its merge says
  nothing about whether an alert is fixed, and a `@code` task has no alert to
  stay clear.
- **Several PRs.** While any of the problem's fix PRs is open, it stays
  `fixing`. Once none is, one merge is enough for `verifying`; none merged
  means `waiting_human`.
- **The alert cleared first.** A problem the alert already resolved stays
  resolved (#488's rule: the alert source owns whether a problem is live). The
  merge is written on its timeline, and a return inside the 24-hour reopen
  window reopens it the usual way, with a fresh investigation.
- **The grace.** Right after a merge the old code is usually still running,
  so an occurrence inside `fix_grace_hours` is put down to it, not to the fix.
  An occurrence inside a deploy or maintenance window never counts. If your
  deploys land hours after a merge, raise the grace.
- **You still decide.** Completing the task resolves the problem at any point,
  as always.

How it gets there: GitHub's `pull_request` webhook (`/api/webhooks/github`)
starts `GitHubAlertFlow`, which hands a `closed` PR to
`HubActivities.follow_fix_pr` (`hub_fix.record_pr_closed`). The problem hub's
five-minute `HubSweepFlow` settles `verifying` problems
(`HubActivities.verify_fixes`). The webhook must be set up and reachable
(it already is if you get PR-opened pings in chat); without it nothing moves
past `fixing`, which is how it behaved before.

Both windows live on the `hub-sweep-5m` activity row, with generic defaults of
24 hours and 1 hour. `schedule_sync` picks a change up within five minutes; no
restart.

```sql
UPDATE activities
SET config = config || '{"fix_verify_hours": 24, "fix_grace_hours": 1}'::jsonb
WHERE workflow_type = 'HubSweepFlow';
```

To see fixes in flight and how they ended:

```sql
SELECT p.id, p.status, p.title, l.ref AS pr
FROM problems p JOIN problem_links l ON l.problem_id = p.id AND l.link_kind = 'github_pr'
WHERE p.closed_at IS NULL AND p.status IN ('fixing', 'verifying')
ORDER BY p.last_seen_at DESC;

SELECT e.occurred_at, e.source, e.payload->>'text'
FROM problem_events e
WHERE e.problem_id = '<problem id>' AND e.source IN ('github', 'hub') AND e.kind = 'investigation'
ORDER BY e.id;
```

`pending_prs` is not part of this. It is the hand-off between the two
activities that open a PR (`stage_pending_pr` writes the title, body and
branch; `create_github_pr` reads them back and marks the row `opened` or
`failed`), written only when someone picks Open PR, and pruned after 30 days
by `CleanupFlow`. That is why it is usually empty: in prod, Open PR was picked
twice (2026-07-31 and 2026-08-10), and the one row those left was pruned on
2026-08-31. A PR can stay open longer than 30 days, so the follow-up reads
the problem's own link and events instead.

### What Pandora remembers from your decisions

**The verdict is stored after you decide, tagged with what you did (#502).**
Each investigation's verdict and transcript go to the knowledge store as an
`alert_investigation` document, so the next investigation of a similar alert
can read how the last one ended. The document carries an outcome, in its
metadata and as an `outcome:<x>` tag:

| Outcome | Meaning |
|---|---|
| `opened_pr` | you picked Open PR(s) and a PR opened |
| `pr_failed` | you picked Open PR(s), but none could be opened |
| `run_fix` | you picked Run fix |
| `discarded` | you picked Discard |
| `muted` | you picked Mute 24h |
| `acknowledged` | you picked Acknowledge |
| `expired` | nobody answered the card in 48 hours |
| `self_resolved` | the alert cleared while the card was open |
| `no_card` | nothing to decide, so no card was sent |

When an investigation starts, `gather_alert_knowledge` searches these
documents for verdicts on similar alerts and puts the best three in front of
it, each with its outcome. A fix you took (`opened_pr`, `pr_failed`,
`run_fix`) comes first. A fix you discarded is never recalled. Each past alert
is one line: its verdicts are one document per outcome, under the alert's own
address, so a discard today does not overwrite the fix you took last week.
Verdicts stored before #502 have no outcome; they are recalled, after the
taken ones.

Before #502 the verdict was stored before the card went out, so a discarded
fix was recalled next time exactly like one you acted on. The move is behind
the `kg-verdict-after-decision` patch id, so a run that was waiting on its card
across the deploy stores its verdict the old way.

To see what was stored and how it ended:

```sql
SELECT metadata->>'outcome' AS outcome, count(*)
FROM knowledge_content WHERE source_type = 'alert_investigation'
GROUP BY 1 ORDER BY 2 DESC;
```

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

The mount is POST-only. The client also sends a GET to ask for a server
stream; that answers 405, which is how the MCP transport says "no stream". A
404 there would mean "your session is gone" — which is what the GET got until
#476, when it fell through to the admin panel's catch-all.

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
does not exist, so wire two hooks into your Claude settings — they live in your
dotfiles, not in this repo, because they are about your machine.

**Which settings file.** Claude Code reads its settings, and finds its hooks,
in its config directory: `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`. That is
`~/.claude` only when `CLAUDE_CONFIG_DIR` is unset. If you run more than one
login — say `CLAUDE_CONFIG_DIR=~/.claude-personal` for one account and the
default for another — each directory has its own `settings.json`, and the
hooks must go into every one you use, or the sessions under the others are
never recorded. So the file to edit is
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json`, once per config
directory, and the script goes in that directory's `hooks/`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/aegis-session.sh\" start"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/aegis-session.sh\" stop"
          }
        ]
      }
    ]
  }
}
```

Use `SessionEnd`, not `Stop`, for the second hook. `Stop` fires at the end of
every reply, so it would mark a session you are still sitting in as `parked`
after its first answer, and AEGIS would stop keeping out of that task.

**Where the script gets `AEGIS_URL` and `AEGIS_API_KEY`.** A hook sees only
the environment of the `claude` process that runs it, so both must be set
there — exported from your shell profile, or from a private file the profile
sources. Never put them in the repo or in this settings file.

- `AEGIS_URL` is the Core URL you gave `claude mcp add` above, without the
  `/api/mcp-server/...` path.
- `AEGIS_API_KEY` is the key in that entry's `X-API-Key` header: Core's API
  key (`AEGIS_API_KEY` on Core, or the one generated under Integrations →
  **API Key** in the admin panel). It has to be a real key. The operator mount
  refuses to run without one even when Core has `AEGIS_AUTH_DISABLED=true`,
  and refuses a run's mount token outright.

If either is missing the script exits quietly rather than failing your session.

- `AEGIS_ACCOUNT` is the **account name** AEGIS uses for this config
  directory — the key under `engines.claude.config_dirs` in the coding-host
  block (for example `personal` for `~/.claude-personal`). It is not the
  directory's name: AEGIS resumes a session under an account by that key, so
  sending `.claude-personal` would match nothing. Set it per config directory.

**When it does anything.** The script records a session only when it starts
inside a task worktree — a directory matching `*-aegis-wt/task-*`, which is
where AEGIS's own coding sessions run — or when `AEGIS_TASK` is set. Anywhere
else it says nothing, so an ordinary session in your own checkout never lands
in the registry. To put one on the record, start it with
`AEGIS_TASK=<todoist task id> claude`.

```bash
#!/usr/bin/env bash
# ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/aegis-session.sh
# Tell AEGIS which task this session is on.
set -euo pipefail

# Without a URL and a real key there is nothing to tell; stay out of the way.
[ -n "${AEGIS_URL:-}" ] && [ -n "${AEGIS_API_KEY:-}" ] || exit 0

# Claude Code hands a hook its session id and working directory as JSON on
# stdin; there is no CLAUDE_SESSION_ID variable.
input=$(cat)
cwd=$(jq -r '.cwd // empty' <<<"$input"); cwd=${cwd:-$PWD}
sid=$(jq -r '.session_id // empty' <<<"$input")

# A task session runs in `<repo>-aegis-wt/task-<id>`; anything else is not on
# a task unless AEGIS_TASK says so. Silence is the correct answer for an
# ordinary session in an ordinary checkout.
task="${AEGIS_TASK:-}"
if [ -z "$task" ]; then
  case "$cwd" in
    *-aegis-wt/task-*) task="${cwd##*-aegis-wt/task-}"; task="${task%%/*}" ;;
    *) exit 0 ;;
  esac
fi

case "${1:-start}" in
  start)  status=active; summary="opened a session here" ;;
  stop)   status=parked; summary="stepped away" ;;
  *)      exit 0 ;;
esac

body=$(jq -nc --arg t "$task" --arg s "$summary" --arg st "$status" --arg sid "$sid" \
  --arg acct "${AEGIS_ACCOUNT:-$(basename "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")}" \
  '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"report_progress",
    arguments:{task_id:$t,summary:$s,status:$st,session_id:$sid,account:$acct}}}')

# The key goes to curl on stdin (-K -), not on the command line, where any
# local user could read it from the process list.
printf 'header = "X-API-Key: %s"\n' "$AEGIS_API_KEY" |
  curl -fsS -m 5 -K - -X POST "$AEGIS_URL/api/mcp-server/pandoras-actor/operator" \
    -H "Content-Type: application/json" -d "$body" >/dev/null || true
```

Three things about it are deliberate. It **fails open** (`|| true`): a hook
that breaks your session because AEGIS is down is worse than an unrecorded
session. It sends the **account** (`AEGIS_ACCOUNT`, the coding block's name
for this login), which is what lets a later AEGIS turn resume under the same
login. And `stop` parks
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
  may back an open PR. Set to 0 to disable. The same row carries the flow's
  other windows, each 0 to disable: `problem_close_days` (7, fractions
  allowed — how long a resolved problem keeps its key before it closes),
  `interaction_orphan_days` (7) and `dispatch_days` (30).

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

## The research lane (Raphael)

Raphael researches with five chat tools and one flow (#509).

| Tool | What it does |
|---|---|
| `web_search` | SearxNG results — title, url, snippet — returned raw, not summarised |
| `read_url` | One page's readable text, bounded. Public http(s) hosts only |
| `paper_search` | arXiv and Semantic Scholar together: title, authors, date, abstract, citation count, and an id for `paper_read`. One engine failing still returns the other's papers |
| `paper_read` | A paper's text from its PDF, by arXiv id, `s2:<id>` or PDF URL |
| `research_topic` | Hands the question to `ResearchFlow` and waits up to 45s for the answer |

The four reads fetch and return; nothing is stored. They are on the MCP gated
endpoint's read-only list, and they stay there: fetching is not writing.
`research_topic` is not, because it starts a flow that saves its answer. The
text `read_url` and `paper_read` return comes labelled as untrusted fetched
content (an `untrusted` note is the first key), and the synthesis prompt
labels its numbered sources the same way, so a page cannot pass itself off
as an instruction.

**`ResearchFlow`** gathers from the knowledge store, a web search and — when the
question looks academic — the two paper engines; reads the best pages (a task's
own links first); asks the smart tier for one answer that cites its numbered
sources (`llm_calls.purpose = 'research_synthesis'`); and saves that answer to
the knowledge store under `aegis://research/<hash of the question>`, so asking
the same question again replaces the old answer. Only a real answer is saved: a
run whose synthesis failed says so and stores nothing.

- **From chat**, the run's id is `research-<hash of the question>`, so a retried
  turn re-attaches to the run in flight. Past 45s the tool answers "still
  researching", and the flow posts the answer to the agent's channel when it
  lands.
- **A `#research` task** assigned to an agent goes to the `research` verb
  (`agent_task_verbs`): the task gets a hub problem (`ensure_problem_for_task`,
  as a `@code` task does), the answer is posted as one comment with its numbered
  sources, and the task parks at `@waiting`. To send `#research` back to the old
  chat path, set `"#research": "ask"` in the `agent_task_verbs` setting.

**Granting the four reads on a running deployment is a DB write** — the seed only
applies to an agent that has no tool set yet. Tick them on Admin → Agents →
Raphael → Behavior, or:

```sql
UPDATE agents
   SET metadata = jsonb_set(
         metadata, '{tool_set}',
         (metadata->'tool_set') || '["web_search","read_url","paper_search","paper_read"]'::jsonb)
 WHERE id = 'raphael'
   AND NOT (metadata->'tool_set' ? 'web_search');
```

Every fetch of a URL that a model chose, a page named or a feed publishes goes
through `services/url_guard.py`. The first request and every redirect must
resolve to a public address, so a page the agent has just read cannot steer it
at the stack's own services, not even by redirecting inward. The one exception
is the admin knowledge route, where you seed a URL by hand
(`allow_private=True`). What is not caught: a host whose DNS answer changes
between the check and the connect (DNS rebinding). Bodies are read as a stream
and cut at 10 MB.

## Feeds (Raphael)

AEGIS owns the RSS list (#511). `channels(kind='rss')` is what `RssIngestFlow`
polls every hour at :30, and nothing seeds it: the Miniflux seeder is gone. It
read Miniflux's feed list once at core startup, Miniflux sat dead from
2026-03-28 for five and a half months, and nobody noticed. Add and drop feeds
on Admin → Channels, or ask Raphael (`subscribe_feed`, `unsubscribe_feed`).

The reading list Miniflux used to offer is **Admin → Channels → Recent items**
(`GET /api/admin/channels/feed-items`, `feeds.recent_items`). It shows the
newest entries across the feeds, or one feed, newest first: title linking to
the article, feed, when it came in, how it was stored (`full` / `abstract` /
`failed`), a short excerpt, and a tick when a chat prompt or a research run
has used it. It pages by a keyset cursor, so an entry that arrives between
pages never shifts or repeats one, and it only ever reads. A link that is not
`http`/`https` is dropped before it reaches the page, because a feed is
untrusted input. The Miniflux stack itself was removed on 2026-09-12; its
database on lam and its Portainer definition were kept.

### What each feed is worth

`feed_entries` (migration 046) records every entry a run stored or failed,
with the knowledge row it produced. Joining that to
`knowledge_injection_log.content_ids` tells you which feeds' documents a chat
prompt actually used. Admin → Channels shows it per feed, and so do
`list_feeds` and `GET /api/admin/channels/feed-stats`:

- entries and stored documents in the last 30 days, plus how many were
  abstract only;
- documents used in the last 30 and 90 days;
- the last entry, the backlog and consecutive fetch failures.

Chat turns and research runs write the injection log (`source` `chat` and
`research`). A briefing or a rollup does not log what it reads, so "used" is a
floor.

On the 1st of each month, Raphael's briefing names the active feeds that have
90 days of history and no use in that time. The migration backfills history
by host (an arXiv entry lives on arxiv.org). A feed whose links point
elsewhere, like Hacker News, starts its history at the deploy.

### Ingest modes (#512)

`channels.config.ingest` is set per feed:

| Mode | What a new entry costs |
|---|---|
| `full` (default) | The page or PDF is fetched and stored (`process_content`). |
| `abstract` | One row from the title and summary the feed already carries. Nothing is fetched. |
| `gate` | Full text when the title or summary names a topic term, the abstract otherwise. |

The topic terms are every active intel scan's `topics` plus the topics tracked
from chat (`intelligence_topics`), matched as whole words and case-insensitive.
No LLM is involved.

`full` is the default because of what the measurements showed, not by
accident. Over the 30 days to 2026-09-12:

- **arXiv:** 1,889 papers and 89,669 chunks, which is 90% of all RSS chunks.
  Prompts used 14 of the papers.
- **The topic gate on arXiv:** it would pass 41% of papers, only about 2.3x
  fewer chunks.
- **The topic gate on the other feeds:** it would have kept the full text of
  only 2 of the 10 documents a prompt used.

So gating is opt-in, and **arXiv is the feed to set to `abstract`**. That is
one chunk per paper, about 47x fewer chunks, and the full paper stays one
`paper_read` away.

```sql
UPDATE channels SET config = config || '{"ingest": "abstract"}'::jsonb
WHERE kind = 'rss' AND identifier = 'https://arxiv.org/rss/cs.AI';
```

An abstract row is cheap, so on that feed you can also raise
`max_entries_per_run` (30 today) to clear the arXiv backlog.

Switching a feed from `abstract` to `full` is one-way for entries already seen:
their claim is kept, so a later `full` run treats them as duplicates and only
new entries get their full text. To read one of those in full, use `read_url`
or `paper_read`.

### When a feed breaks

- **Failing:** three fetches in a row that fail (an HTTP error, a response
  that is not a feed, or a fetch the URL guard refused) are a `feeds` hub
  finding of class `feed_failing`. The feed itself is fetched through
  `url_guard` (every redirect checked, 30 s, 20 MB), and only the bytes go to
  feedparser. The finding records an occurrence when the feed crosses that
  line and once a day at the review hour; the hourly runs in between only keep
  the problem open, so a dead feed does not post 24 comments a day on its
  task. Before #511, feedparser turned all of these into an empty parse, which
  looked like a quiet feed. An empty feed that feedparser still recognised as
  a feed, with a benign complaint such as an encoding override, is quiet, not
  failing.
- **Stale:** no entry stored for `channels.config.stale_after_days` days
  (default 30) is a `feed_stale` finding, checked once a day at 03:30 UTC. It
  is measured from the newest entry the store kept (`feed_entries.seen_at`),
  not from the cursor, which also moves past duplicates. A feed that never
  stored an entry is measured from when AEGIS began tracking it: its first
  recorded entry, else its first poll (`channels.config.tracking_since`).
  That is `feeds.tracking_since`, the same date the feed stats show.
- **Recovery:** a stale finding resolves when the feed stores an entry again.
  A failing one resolves only after two good fetches in a row
  (`feeds.RECOVERED_AFTER`, counted in `channels.config.fetch_successes`).
  One good fetch, a failure under the threshold, or a run whose feed record
  could not be written keeps the problem open without adding an occurrence
  (`hub_watch.reconcile_findings`, `record: False`). The problem's subject is
  the feed URL, and the research agent owns the `feeds` source, so these stay
  out of the infra digest (`hub.DIGEST_SKIPPED_SOURCES`).

A `process_content` that returns `status: error` now counts as a failure. The
entry's claim is released and the cursor is fenced, so the next run retries it
instead of counting it as ingested.

### Retention (dry run only)

`GET /api/admin/channels/retention-preview?older_than_days=N` counts what one
rule would remove, and changes nothing. The rule: a PDF that no prompt used,
ingested more than N days ago, keeps its first chunk and drops the rest.

On 2026-09-12 with N=30 that is 8,297 PDFs and 362,153 of 500,055 chunks,
freeing about 530 MB of text and 1 GB of vectors from a 4.8 GB table. Every
PDF dates from 2026-07-01 or later, so nothing is older than 90 days yet.
Deleting anything is a separate, explicit decision.

It is an operator endpoint, kept on purpose: no admin page and no flow calls
it. Call it with the admin credentials the other `/api/admin` routes take,
when you want the numbers before deciding on a retention rule.

### Setting it up on an existing deployment

1. Grant the three tools. The DB `tool_set` wins over the seed:
   ```sql
   UPDATE agents SET metadata = jsonb_set(metadata, '{tool_set}',
     (metadata->'tool_set') || '["list_feeds","subscribe_feed","unsubscribe_feed"]'::jsonb)
   WHERE id = 'raphael' AND NOT (metadata->'tool_set' ? 'list_feeds');
   ```
2. Set arXiv to `abstract` (the SQL above).
3. The Miniflux cleanup is done on this deployment: the stack was removed on
   2026-09-12, and none of its settings rows are left (checked 2026-09-13).
   Another deployment that still has them can keep them, since they are
   harmless, or remove them:
   ```sql
   DELETE FROM settings WHERE key IN
     ('integration:miniflux_url', 'integration:miniflux_api_key', 'connector_health:miniflux');
   ```

## The Calibre library (Raphael)

Raphael reads your Calibre library through calibre-web's OPDS catalogue (#510):
`library_search`, `library_book`, `library_read` and `library_suggest`, and
`ResearchFlow` quotes a passage from the closest book when one speaks to the
question. **Calibre is the record; the knowledge store is only an index of it**
— one `source_type='book'` row per book (title, authors, tags, description),
never the text. A book's text is read on demand, bounded, cited and not stored:
arXiv PDFs were 93% of the corpus's chunks and 78 of 10,284 were ever used, so
bulk text is exactly what not to index.

- **Code:** `connectors/calibre.py` (the OPDS client), `services/library.py`
  (everything the tools and flows share: EPUB chapters and PDF pages, passage
  search, the index row), `services/tools/library.py`, and the worker's
  `CalibreActivities` + `CalibreSyncFlow`.
- **Never the public host.** `calibre.hikmahtech.in` is behind Cloudflare
  Access: every path, `/opds` included, 302s to a login page — the trap that
  broke Miniflux (#70). The connector refuses that host and treats any redirect
  as an error. Use the internal address `http://calibre-web_calibre-web:8083`:
  aegis-core and the worker both sit on the `traefik_public` overlay with
  calibre-web.
- **Reading:** EPUB is read by chapter (the book's own table of contents names
  them), PDF by page (at most 30 pages a read; a query scans the first 150, or
  the first 60 inside research). MOBI and AZW3 cannot be read. Files over 80 MB
  are refused.
- **The index:** `CalibreSyncFlow` runs daily at 03:41 UTC (`calibre-sync-daily`).
  It adds new books, re-embeds a book only when its metadata changed (a
  fingerprint in the row's metadata), and removes the row of a book that left
  Calibre, naming it in the run summary — unless the catalogue came back less
  than half the size of the index, which it refuses to trust
  (`removal_withheld`).

### Setting it up

1. In calibre-web (Admin → Users → Add new user), create a user for AEGIS:
   allow **download**; do not allow upload, edit, delete or admin. Basic auth
   and OPDS are on by default.
2. On AEGIS's Integrations page, group **Calibre (library)**: set the user and
   password, and leave the URL at the internal default. Core uses the new
   values at once; restart the worker for `CalibreSyncFlow`.
3. Grant Raphael the four tools. The DB `tool_set` wins over the seed:

   ```sql
   UPDATE agents SET metadata = jsonb_set(metadata, '{tool_set}',
     (metadata->'tool_set') || '["library_search","library_book","library_read","library_suggest"]'::jsonb)
   WHERE id = 'raphael' AND NOT (metadata->'tool_set' ? 'library_search');
   ```
4. Build the index once without waiting for 03:41: trigger the
   `calibre-sync-daily` schedule (Temporal UI, or `temporal schedule trigger
   --schedule-id calibre-sync-daily`). The summary reports `books`, `added`,
   `updated`, `unchanged` and `failed`.

Until step 2 the tools answer "not configured" and the flow reports
`not_configured`; both are the intended inert state.

## Tracked topics (Raphael)

A topic you ask Raphael to track (`track_topic`, or "yes" to a "track this?"
card) is two things (#513, spec
`docs/superpowers/specs/2026-09-12-research-hub-design.md`):

- **Its search terms**, in the `intelligence_topics` settings row. The intel
  scans search them and the RSS gate matches on them.
- **Its round of news**, a hub problem: class `topic`, source `research`,
  owned by Raphael. Every intel-scan item or stored feed entry that names one
  of the terms (whole word, any case) is an occurrence, keyed on the URL, so
  one story arriving by two paths counts once.

A round stays in the hub and Raphael's briefing ("Your topics") until it
holds enough items: 2 for a `high` topic, 3 for `medium`, 5 for `low`. Then
it becomes one `#research @raphael @next` task listing the items; later items
are collapsed comments. Ticking the task off means "seen": the round resolves
and closes, and the next matching article opens a fresh one.

Feed findings (`feed_failing`, `feed_stale`) are Raphael's too, as
`#feeds @raphael @next` tasks. The agent sweep never works them; you fix or
drop the feed.

Operations:

```sql
-- What is tracked
SELECT value FROM settings WHERE key = 'intelligence_topics';
-- Live rounds, their item counts and whether they earned a task
SELECT p.metadata->>'topic' AS topic, p.todoist_task_id,
       count(*) FILTER (WHERE e.payload->>'item' = 'true') AS items
FROM problems p JOIN problem_events e ON e.problem_id = p.id
WHERE p.class = 'topic' AND p.closed_at IS NULL GROUP BY p.id;
```

Stop tracking with `untrack_topic` (it closes the live round and its task).
The intel scans no longer capture a `#research` Inbox task per worthy item;
Raindrop bookmarks still do.

**A fresh deployment tracks nothing.** Prod had no `intelligence_topics` row
on 2026-09-12, so no round opens until you track a topic or answer "yes" to a
"track this?" card. Until then the intel items still reach the knowledge store
and the briefing; only the auto-closed `@reference` tasks are gone. Seeding the
registry from the intel scans' own topics was considered and deliberately not
done: those are single broad words (`ai`, `world`, `tech`, `macro`), and as
whole-word terms they would cross the threshold on every scan and raise a task
each round — the noise #513 removed.

## The vault (Raphael)

The user's Obsidian vault (`arshadansari27/arshad-workspace`) is Raphael's
record; the knowledge store is only its index (#514, spec
`docs/superpowers/specs/2026-09-12-raphael-notes-design.md`).

- **Reads:** `NotesSyncFlow` (`notes-sync-hourly`, minute :19) pulls the vault
  and indexes every changed `.md` note as `source_type='note'`, skipping
  `.obsidian/`, `_templates/`, `backups/`, `_attachments/` and `.trash/`, at
  most `max_files` (300) per run. Encrypted meld-encrypt blocks are stripped
  before anything is stored. Notes rank above raw documents (`rank_boost`
  1.25). Progress is `settings.notes_index_state`. `raphael/questions/` is
  not indexed: `ResearchFlow` already keeps each answer in the store as
  `aegis://research/<hash>`, and indexing the note too put every answer in
  retrieval twice. Any row an earlier run made for such a note is removed on
  the next run.
- **Writes, insert-only:** only under `raphael/` (research answers in
  `raphael/questions/`, and whatever Raphael writes with `note_write` /
  `note_link`) and the daylog's journal notes. A write creates a note or
  inserts one block into it; nothing the user wrote is ever changed or moved
  (`is_one_insertion` refuses anything else). Each block carries a hidden
  `%% aegis:<key> %%` marker, so a re-run adds nothing twice.
- **The journal:** notes are filed as the vault files its own. With the vault
  configured, the nightly daylog writes to
  `journal/<YYYY>/<NN. Mon>/DD MMM YY.md`, the weekly rollup to the week's
  `W<ww> MMM YY.md` in its Monday's month folder (the vault's weeks start on
  Monday, with ISO numbers) and the monthly one to the month folder's own note,
  `journal/<YYYY>/<NN. Mon>/<NN. Mon>.md`. If the user already has the day's or
  week's note open at the `journal/` root, where periodic-notes creates it,
  Raphael writes into that one instead. The entry is a `- #raphael day log`
  bullet (`week in review`, `month in review`) with the text as an indented
  outline under it: one bullet per prose paragraph, and in the daylog's
  fallback format each `Label:` line with its items nested under it. It is
  placed at the end of the note's own section: `Journal` for a day,
  `Review` for a week or a month (an older month note's `Month Review`),
  found by its heading text at any level. The section ends at the next
  heading, a `---` line or a code fence, so the month note's folder card stays
  last. A note without the section gets `## Journal` / `## Review` and the
  block at its end. A new note is rendered from the vault's own
  template, without its open checkboxes and without the empty `- ` placeholder
  in that section. No `daylog` knowledge row is filed then; if the vault write
  fails the row is filed as before and the run reports `vault_error`.
- **Conflicts:** `obsidian-git` commits from the phone and laptop. A push
  rejected as not a fast-forward, or a conflicting rebase, drops Raphael's own
  unpushed commit, pulls fresh and retries once; a second failure is reported
  and nothing is kept. Raphael never force-pushes. Any other git failure — no
  network, a refused deploy key, a missing repository — is reported at once
  without a retry, with a short reason such as "the remote refused the deploy
  key" and no URL or git output in it.
- **Dates:** a dated heading (`note_write` with no heading, a research
  answer's section) and the time on a journal note Raphael creates are on the
  user's clock, the `user_timezone` settings row, not the container's UTC.
- **What insert-only rules out:** Raphael cannot fill in a placeholder that
  is already in a note, such as an empty `- ` bullet the template left. It
  inserts its own block instead. Only a note Raphael creates from a template
  loses its empty placeholders.

### Setting it up

1. Make an ed25519 key pair and add the public half to the vault repo as a
   deploy key **with write access** (GitHub → Settings → Deploy keys). Keep
   the private half out of chat and out of the repo.
2. On AEGIS's Integrations page, group **Notes (vault)**: set
   `notes_repo_url` (`git@github.com:arshadansari27/arshad-workspace.git`) and
   paste the private key into `notes_deploy_key`. Restart core and the worker
   (the key is written to disk, mode 0600, at boot). The checkout is
   `/app/config/notes`, beside the books; no infra change is needed.
3. Grant Raphael the four tools. The DB `tool_set` wins over the seed:

   ```sql
   UPDATE agents SET metadata = jsonb_set(metadata, '{tool_set}',
     (metadata->'tool_set') || '["note_search","note_read","note_write","note_link"]'::jsonb)
   WHERE id = 'raphael' AND NOT (metadata->'tool_set' ? 'note_search');
   ```
4. Build the index without waiting for :19: `temporal schedule trigger
   --schedule-id notes-sync-hourly`. The first pass over ~1,000 notes takes a
   few runs (`remaining` in the summary counts down).
5. The daylog's knowledge rows reach the journal through `NotesBackfillFlow`,
   which runs weekly (`notes-backfill-weekly`, Sunday 04:47 UTC; the schedule
   appears on its own through `schedule_sync`). It files any day whose vault
   write failed and fell back to its knowledge row, and it uses the live
   markers, so a week with nothing missing writes nothing. The schedule looks
   only at rows filed in the last `since_days` (14) days: the pre-vault rows
   are still in the store, and rereading them every week would put back a
   block you deleted from an old journal note. To move every old row (the
   first time, or after a vault outage longer than two weeks), start it by
   hand, where `since_days` defaults to 0 (every row): `temporal workflow
   start --type NotesBackfillFlow --task-queue aegis-main --workflow-id
   notes-backfill-journal --input '{"agent_id": "raphael"}'`.

Until step 2 every part reports `not_configured` and the daylog files its
knowledge rows exactly as before.

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
