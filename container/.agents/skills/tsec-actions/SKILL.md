---
name: tsec-actions
description: Use when submitting a flag for a challenge in a tsec CTF or intelligent penetration testing competition.
---

## Usage

* Call the API directly with `curl`. TSEC_SERVER_HOST and TSEC_AGENT_TOKEN are already set as environment variables.
* All flags use the `flag{...}` format.

Submit a flag:

```bash
curl -X POST "http://${TSEC_SERVER_HOST}/api/submit" \
  -H "Agent-Token: ${TSEC_AGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"code": "<challenge_code>", "flag": "<flag>"}'
```

## Rules

- Submit a flag only when confidence is high. Do not use this endpoint for brute forcing.
- Determine whether submission succeeded from the API response. Do not assume success.
