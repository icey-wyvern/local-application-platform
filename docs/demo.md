# v1.0.0 public demo (target: 3 minutes)

Show the application lifecycle, not lab setup. Keep the ten beats below in order;
cut idle polling time if the two builds make the recording longer than four
minutes. Do not cut away from an error or imply a command finished earlier than
it did.

## Before recording, off screen

- **After the repository is public:** publish a dedicated public HTTPS Git fixture
  repository with `success` and `unhealthy` branches. Use the root-level
  `Dockerfile` and `platform.yaml` from the matching
  [`examples/build-agent-fixture`](../examples/build-agent-fixture) directories.
  Give both manifests the same fresh, DNS-safe application name (the existing
  fixture generator's `--name-suffix` convention is suitable). Freeze the two
  branch commits and confirm anonymous `git ls-remote` access. Set
  `FIXTURE_REPO` to that public `.git` URL and `APP` to the manifest name before
  capture. This checkout has no published fixture URL yet.
- Use a fresh fixture repository and name for each take that follows a delete:
  LAP retains the repository/name identity after soft deletion. Keep the fixture
  public through recording, then delete only the disposable LAP application.
- Preconfigure the CLI's credentials and trusted lab CA, DNS, `kubectl` context,
  and a signed-in Grafana tab. Warm the build image and Trivy cache. Rehearse
  timing and confirm the healthy branch passes the current scan. Clear terminal
  history and unrelated browser tabs; never display tokens, credentials, local
  paths, tunnel URLs, raw logs, or Grafana administration pages.

## On-screen sequence

| Beat | Show | Approximate screen time |
| --- | --- | ---: |
| 1 | README architecture diagram and one sentence on the CLI → API → build agent → controller flow. | 10 s |
| 2 | `uv run --package lap-cli lap deploy "$FIXTURE_REPO" --revision success` | 10 s |
| 3 | While it polls, show a prefiltered Grafana Explore view of `{service_name="platform-build-agent"} |= "stage completed"` for the last five minutes: `clone`, `build` (BuildKit), `scan` (Trivy), `publish`, `verify`. Once deploy finishes, show the resolved commit, Release ID, and immutable digest with the command below. | 35–60 s |
| 4 | `uv run --package lap-cli lap status "$APP"`, then `kubectl -n "app-${APP}" get deployment application` showing `2/2` Ready. | 15 s |
| 5 | `curl --fail --silent --show-error "https://${APP}.apps.lab.test/health"` returning `ok` with normal TLS verification. | 10 s |
| 6 | `uv run --package lap-cli lap history "$APP"` showing one successful Build, Release, and healthy Deployment. | 15 s |
| 7 | `uv run --package lap-cli lap deploy "$FIXTURE_REPO" --revision unhealthy` (expected exit code 2). | 10 s |
| 8 | Show its failed Operation and `Automatic rollback: succeeded`, then `uv run --package lap-cli lap history "$APP"` showing the failed Deployment and healthy `automatic_rollback` Deployment restoring the prior Release. | 35–60 s |
| 9 | If the view has been screened, show only a relevant Platform Health panel or the filtered build stage evidence in Grafana. Skip this beat if it would show unrelated internal details. | 10 s |
| 10 | End on `uv run --package lap-cli lap status "$APP"`, `kubectl -n "app-${APP}" get deployment application` (`2/2`), and the trusted HTTPS `curl` above returning `ok` again. | 15 s |

For beat 3, after the successful deploy returns:

```sh
uv run --package lap-cli lap --json history "$APP" |
  jq -r '.releases[0] | "Commit: \(.gitCommit)\nRelease: \(.id)\nDigest: \(.imageDigest)"'
```

The unhealthy deploy intentionally returns a nonzero exit status. Its failure
is the proof of the automatic rollback, not a reason to hide that output.
After capture, run `uv run --package lap-cli lap delete "$APP"` off screen and
verify it disappears from `lap list`. Do not reuse that deleted name or fixture
repository for another take.
