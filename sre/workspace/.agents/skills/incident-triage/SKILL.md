---
name: incident-triage
description: Standard procedure for triaging a production incident (latency, errors, outage). Read before any diagnosis.
---

# Incident triage

1. **Scope**: run `fleet_status`. List unhealthy services; separate root-cause candidates from victims
   (a gateway is usually a victim of its upstream).
2. **What changed**: run `recent_deploys`. A deploy within ~1h of symptom onset is the prime suspect.
3. **Evidence**: delegate log analysis to the `log-analyst` subagent for each suspect service.
4. **Decide**: prefer the smallest reversible fix. Rollback of a suspect deploy beats scaling or restarting.
   Never restart as a first action; it destroys evidence.
5. **Act**: call exactly one remediation tool. It requires human approval; explain why in one line first.
6. **Verify**: re-run `fleet_status`. Do not declare resolved until p99 < 200ms and error rate < 1%.
7. Offer to write a postmortem (see the `postmortem` skill).
