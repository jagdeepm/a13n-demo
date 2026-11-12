---
name: postmortem
description: Write a blameless postmortem file after an incident is resolved.
---

# Postmortem

Write `/workspace/postmortems/<YYYY-MM-DD>-<service>.md` with these sections:

- **Summary** (2 sentences)
- **Impact** (services, duration, error rate, latency)
- **Timeline** (deploy, detection, mitigation, verification)
- **Root cause** (the technical cause, not a person)
- **Action items** (3 max, each with an owner role, e.g. "checkout team", and a guardrail that prevents recurrence)

Blameless: name changes and systems, never people.
