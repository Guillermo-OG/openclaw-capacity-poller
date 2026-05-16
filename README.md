# openclaw-capacity-poller

GitHub Actions cron that polls Oracle Cloud every 30 min for `VM.Standard.A1.Flex` ARM Always-Free capacity in `us-ashburn-1`. Launches the OpenClaw gateway VM the moment a slot frees up, pings Telegram, then auto-disables itself.

## Files

- `poll_capacity.py` — OCI SDK script. Iterates ADs, attempts `LaunchInstance`, handles `Out of host capacity` silently, succeeds + sends Telegram on launch.
- `.github/workflows/poll.yml` — `*/30 * * * *` cron + `workflow_dispatch`. Disables itself via GitHub API on success.
- `requirements.txt` — `oci`, `requests`.

## Required secrets (repo Settings → Secrets → Actions)

| Name | Source |
|---|---|
| `OCI_USER_OCID` | Oracle Console → User Settings |
| `OCI_FINGERPRINT` | API key fingerprint |
| `OCI_TENANCY_OCID` | Tenancy Information |
| `OCI_COMPARTMENT_OCID` | Compartment to launch into (root tenancy OCID OK) |
| `OCI_REGION` | e.g. `us-ashburn-1` |
| `OCI_SUBNET_OCID` | Public subnet OCID |
| `OCI_PRIVATE_KEY_PEM` | Full PEM contents of API private key |
| `SSH_PUBLIC_KEY` | `ssh-ed25519 AAAA... openclaw` |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID |

## Smoke test

Actions tab → "OCI ARM Capacity Poller" → Run workflow. Expect:

- Logs: auth OK, image OCID resolved, 3 ADs attempted, all "out of capacity".
- Telegram: smoke-test ping with shape config.

## On success

Telegram: `*OPENCLAW VM CREATED*` with public IP + SSH command. Workflow disabled automatically.

## Re-enable

Actions tab → workflow → "Enable workflow".
