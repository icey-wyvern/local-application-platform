# Platform CLI

The `lap` command uses only the public platform API. Install pinned workspace
dependencies with `uv sync --package lap-cli`, then run commands with
`uv run --package lap-cli lap ...` from the repository root. The API endpoint is
`https://api.lab.test`; the lab root CA must be trusted by the operating system
or supplied as `LAP_CA_BUNDLE=/path/to/lab-root-ca.crt` (or `--ca-bundle`).
The workstation or WSL resolver must send `lab.test` queries to the lab DNS
VIP (`10.20.0.12` in the reference lab); `api.lab.test` resolves to the Envoy
Gateway address. WSL's default generated resolver may need local configuration.

Set `LAP_API_TOKEN` in the environment, or place the raw token in
`~/.config/local-application-platform/credentials` with mode `0600` on Unix.
`LAP_CREDENTIALS_FILE` selects another file. The provisioned token is at
`~/.local/share/local-application-platform/secrets/platform-api-token` on the
lab workstation; copy it to the credentials file with restrictive permissions.
The token is sent only as a bearer header. There is no token command-line
option, so it does not appear in shell history or process arguments.

`LAP_API_URL` or `--api-url` changes the HTTPS API origin. TLS verification and
hostname checking remain enabled. Connect timeout is 5 seconds and each read is
bounded at 20 seconds. Operation polling defaults to every 2 seconds for at most
20 minutes; `--poll-interval` and `--poll-timeout` can adjust those bounds.

```sh
uv run --package lap-cli lap deploy https://github.com/example/app.git --revision main
uv run --package lap-cli lap deploy https://github.com/example/app.git --revision refs/tags/v1
uv run --package lap-cli lap deploy https://github.com/example/app.git --revision <40-character-sha>
uv run --package lap-cli lap list
uv run --package lap-cli lap status example-api
uv run --package lap-cli lap history example-api
uv run --package lap-cli lap rollback example-api --release <healthy-release-uuid>
uv run --package lap-cli lap delete example-api
uv run --package lap-cli lap operation <operation-uuid> --wait
```

Branch is the default revision type. Use `--revision-type tag` or `commit`
when the value itself does not identify the type. `delete` waits by default;
`--no-wait` returns the accepted Operation. `--json` emits API-shaped JSON
instead of human-readable output. Exit codes are `0` for success, `2` for API
rejection or failed Operation, `3` for configuration, connectivity, timeout,
or response errors, and `130` for Ctrl+C.

The `status` runtime line summarizes the latest persisted Deployment. For
detailed controller conditions, use the platform's existing operational checks.
`rollback` selects a previously healthy Release from that Application, creates
a new Deployment record, and waits for the same controller and HTTPS health
gates as `deploy`. It does not contact Git or rebuild the image.
