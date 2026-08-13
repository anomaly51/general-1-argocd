---
status: verifying
trigger: "бот дублирует почему то сообщение . можешь плиз разобраться. мне кажется как бдуто 2 инстагнация слоняры бота запущен"
created: 2026-08-14T01:42:00+03:00
updated: 2026-08-14T01:55:44+03:00
---

## Current Focus

hypothesis: CONFIRMED — the General1 migration left the legacy Workload1 Argo CD application active; two Telethon clients authenticated to the same Telegram account independently handle every inbound link and each sends a response.
test: With the legacy Workload1 deployment GitOps-scaled to zero, send a representative Telegram link and correlate exactly one inbound event and one response from General1 only.
expecting: Workload1 keeps its rollback-safe Application/PVC but has Deployment replicas 0 and no pod; General1 remains Healthy/Synced with exactly one ready pod; each test link produces exactly one response.
next_action: Await a human test link and confirmation that Sloniara sends exactly one response; if confirmed, record end-to-end verification and archive the session.
reasoning_checkpoint:
  hypothesis: The migration left the legacy Workload1 deployment active, so two independently authenticated Telethon clients receive and handle the same inbound links.
  confirming_evidence:
    - General1 and Workload1 each have one simultaneously ready Sloniara deployment and pod under separate Argo CD applications.
    - Runtime logs show exact 3/3 overlap of the same inbound-link events across both pods, while non-secret configuration fingerprints match.
    - Source inspection finds a single handler, ruling out duplicate handler registration within one process.
  falsification_test: If duplicate responses continue while Workload1 has no pod and logs show only the General1 pod receiving each link once, this root-cause hypothesis is false.
  fix_rationale: Setting the obsolete Workload1 deployment to zero replicas eliminates the second event consumer at its GitOps source while retaining its Application, PVC, and rollback path.
  blind_spots: Cluster and Argo state are verified, but end-to-end Telegram behavior cannot be verified until the user sends a representative link.
tdd_checkpoint: null

## Symptoms

expected: One bot response for each incoming Telegram message.
actual: The Sloniara bot sends a duplicate response; the user suspects two running instances.
errors: No explicit error message reported.
reproduction: Send a message or command to the Sloniara Telegram bot.
started: Recently observed; exact first occurrence is unknown.

## Eliminated

- hypothesis: Two responses are caused by overlapping pods during a General1 rolling deployment.
  evidence: The General1 Deployment uses strategy Recreate, declares replicas 1, and has exactly one ready pod; the second active pod is in Workload1 under another Deployment.
  timestamp: 2026-08-14T01:51:00+03:00

- hypothesis: One process registers the message handler twice.
  evidence: Source inspection finds one handler, while logs independently show the same three inbound-link events processed by both cluster pods.
  timestamp: 2026-08-14T01:51:00+03:00

- hypothesis: The new General1 chart is absent from committed GitOps state because values.yaml is untracked.
  evidence: Raw git status shows only .planning/ is untracked; git ls-files and commit d4dedeb confirm all three downloader chart files, including values.yaml, are committed. The earlier RTK-filtered status line was misread.
  timestamp: 2026-08-14T01:51:00+03:00

## Evidence

- timestamp: 2026-08-14T01:55:44+03:00
  checked: Final Workload1 GitOps reconciliation at revision 656259c81e4b352c25f81aa3a3395b7b19ac2e75 and current General1 state.
  found: Argo operation succeeded; the Workload1 application is Synced to the final revision, its Deployment is Synced with replicas 0, and its legacy pod is absent. General1 remains Synced and Healthy with one ready pod. Workload1 application health is Degraded only because three pre-existing ExternalSecrets cannot reach their provider through the existing DNS/Vault failure.
  implication: Only the intended General1 Telethon client remains active, so the infrastructure cause of duplicate responses is removed. The unrelated ExternalSecret condition does not re-create or run a second Sloniara client.

- timestamp: 2026-08-14T01:52:00+03:00
  checked: Complete downloader-sloniara-bot Helm template and repository-wide references.
  found: The committed chart declares replicas 1 with Deployment strategy Recreate; the only exact repository references are within the chart itself.
  implication: The intended chart cannot create two simultaneous rollout pods, so another deployment/environment/process is required to explain a true duplicate-instance symptom.

- timestamp: 2026-08-14T01:49:00+03:00
  checked: Read-only inventory across connect-cluster-discovered kubeconfigs.
  found: General1 runs apps/downloader-sloniara-bot at 1/1 with image sha-e0bb85d0, while Workload1 simultaneously runs downloader-sloniara-bot/downloader-sloniara-bot at 1/1 with legacy image sha-c1b60d1338c8 and a running pod aged 38 days.
  implication: The multiple-instance branch is supported directly: migration created the General1 instance without decommissioning the active legacy Workload1 instance.

- timestamp: 2026-08-14T01:51:00+03:00
  checked: Argo CD ownership and source for both live deployments.
  found: General1 is tracked by Application apps-downloader-sloniara-bot from anomaly51/general-1-argocd path apps/downloader-sloniara-bot; Workload1 is tracked by a distinct Application downloader-sloniara-bot from anomaly51/workload-1-k3s-argocd path apps/downloader-sloniara-bot.
  implication: Both instances are persistent GitOps-managed workloads; restarting or deleting a pod would not fix the cause because Argo CD would recreate it.

- timestamp: 2026-08-14T01:51:00+03:00
  checked: Non-secret runtime identity fingerprints, pod logs, and application source handler registration.
  found: The two pods have matching non-secret account configuration fingerprints and different session fingerprints; both logs contain the same 3 of 3 inbound-link events; source contains one handler.
  implication: Two independently authenticated sessions for the same Telegram account consume every inbound link, and each process emits its own response. Duplicate handler registration inside one process is ruled out.

## Resolution

root_cause: The August migration added downloader-sloniara-bot to General1 but left its legacy Workload1 Argo CD application at one replica. Both deployments authenticated separate Telethon sessions to the same Telegram account, received the same inbound link events, and each sent a response.
fix: Applied through Workload1 GitOps revision 656259c81e4b352c25f81aa3a3395b7b19ac2e75 — declare app.replicaCount 0, vendor the application chart to bypass broken OCI DNS, preserve the legacy emptyDir shape to avoid an SSA volume-type conflict, and explicitly retain the existing session PVC for rollback. No live resource was deleted manually; General1 remains the sole running instance.
verification: Infrastructure verified: Workload1 Argo operation succeeded, Application and Deployment are Synced to revision 656259c, Deployment replicas are 0, and the legacy pod is absent; General1 is Synced/Healthy with one ready pod. Pending human end-to-end confirmation that a representative Telegram link yields exactly one response. The Workload1 Application remains Degraded solely due a pre-existing ExternalSecret provider DNS/Vault failure unrelated to this fix.
files_changed:
  - anomaly51/workload-1-k3s-argocd:apps/downloader-sloniara-bot/values.yaml
  - anomaly51/workload-1-k3s-argocd:apps/downloader-sloniara-bot/Chart.yaml
  - anomaly51/workload-1-k3s-argocd:apps/downloader-sloniara-bot/Chart.lock
  - anomaly51/workload-1-k3s-argocd:apps/downloader-sloniara-bot/charts/app-0.4.3.tgz
  - anomaly51/workload-1-k3s-argocd:apps/downloader-sloniara-bot/charts-src/app/**
