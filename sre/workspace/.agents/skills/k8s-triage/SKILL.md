---
name: k8s-triage
description: Procedure for triaging Kubernetes workload incidents (crashes, pending pods, errors, no traffic). Read before diagnosing a cluster.
---

# Kubernetes triage

1. **Objectives**: list `ServiceLevelObjective` resources (apiVersion `sre.demo.io/v1`) in `shop`.
   They tell you each service's tier, owner and remediation notes. Use `api_resources_list` if unsure of the kind.
2. **Scope**: `pods_list_in_namespace` for `shop`. Note STATUS, READY, RESTARTS per pod.
   Healthy pods are not proof of health: also check step 4.
3. **Why**: for each unhealthy pod, `pods_get` (look at `state.waiting.reason`, `lastState.terminated.reason`,
   `exitCode`, conditions) and `events_list` for the namespace. Map to a cause:
   - CrashLoopBackOff: read `pods_log` (use `previous: true` for the crashed container)
   - OOMKilled / exit 137: compare memory limit vs `pods_top` usage
   - ImagePullBackOff / ErrImagePull: the image tag in the pod spec
   - Pending + FailedScheduling: requests vs node capacity
   - Running but not Ready: readiness probe failing
4. **Customer view**: read the `loadgen` pod's logs. Each line is one request with status code + latency.
   `000` = connection failed (e.g. Service has no endpoints: compare Service selector vs pod labels with
   `resources_get`); `5xx` = application errors (read the app's logs).
5. **What changed**: `resources_get` on the Deployment; `metadata.annotations["deployment.kubernetes.io/revision"]`,
   recent ReplicaSets (`resources_list` apps/v1 ReplicaSet) show the rollout history.
6. **Fix**: smallest reversible change via `kubectl_fix`. A bad rollout -> `rollout_undo`.
   Never restart first; it destroys evidence. One fix at a time.
7. **Verify**: re-list pods and re-read loadgen logs. Report resolved only when pods are Ready and loadgen shows 200s.
