# Run z0live checks on Boat

## Set the spend controls before starting

Use the Studio CLI, `~/.ascii/bin/boat`. Check the billing scope and current usage:

```sh
boat org
boat billing --all
boat wallet status
boat limits --all
boat ci --state active
boat list
```

In the personal wallet, run `boat wallet auto-top-up off` and `boat billing extra-starts fail`. Verify both settings before starting paid work. The CLI credential used for this setup returned `403` for both changes because it lacked `account.admin`. Do not start paid work until an account admin verifies the settings.

- Use only `small` or `default` machines. Do not create sandboxes in batches.
- Count active sandboxes across CLI and CI. Start one at a time, and do not start if the account already has three active sandboxes or if a queued CI job could put it over three. Do not double-count CI sandboxes that also appear in `boat list`.
- Boat's plan limit is not the three-sandbox policy. No account-wide cap of three was verified. Coordinate starts across operators. Do not use this lane unattended.
- For manual work, pass `--ttl 600`, leave auto-stop enabled, and use `--no-env --no-snapshots`. Do not use `--no-auto-stop`, save snapshots, or attach local data.
- A prompt can move the auto-stop deadline. Interrupt overdue work and delete the sandbox. Do not rely on TTL as a hard billing cutoff.

## Enable the branch-only workflow

1. With Boat account-admin access, run `boat ci connect`. In GitHub, install the Boat CI App for **only** `kvnloo/z0live`.
2. Run `boat ci repos --account kvnloo` to confirm the repository is linked. For this public repository, run `boat ci public kvnloo on --users kvnloo`. Do not use `--anyone`.
3. Re-run the `Boat tests` workflow on `ci/boat` or push a new commit to that branch. Verify both test steps in GitHub and read the job receipt in Boat:

```sh
gh run list --repo kvnloo/z0live --branch ci/boat --workflow boat.yml
gh run view <run-id> --repo kvnloo/z0live --json conclusion,jobs,url
boat ci --repo kvnloo/z0live --all
boat ci job <job-id>
```

The workflow uses one `runs-on: boat` job, serializes its runs, and sets a 10-minute GitHub job timeout. Boat does not expose a `--ttl` flag for native CI runners. Boat documents cleanup when the job ends. Do not add `/debug` or a snapshot label. The workflow uses read-only checkout and does not request any GitHub Actions secrets.

## Run a manual sandbox

After the spend checks pass, start one fresh small sandbox. Use only a public GitHub ref. Do not copy a Studio worktree, attachments, model logins, Telegram data, or chat history.

```sh
boat new --personal --type small --ttl 600 --no-env --no-snapshots --name z0live-smoke
boat exec "$id" --timeout 60 -- git clone --depth 1 --single-branch --branch ci/boat https://github.com/kvnloo/z0live.git
boat usage "$id"
boat delete "$id" --yes
boat list
```

Set `id` to the ID returned by `boat new` before running the remaining commands. Delete the sandbox even if work fails. Confirm it no longer appears as active.

## Check agent credential requirements

Boat's [agent credential docs](https://docs.boat.dev/integrated-agents/keys.md) say `boat prompt "$id" --provider codex '…'` needs a configured model credential. A public clone does not need GitHub credentials. A sandbox-side push needs separate, repository-scoped GitHub write access. Commit authorship does not grant push access.

Do not inject personal credentials or turn on the default environment to make the smoke test pass. Stop at any model-authentication requirement. No agent or branch push from a sandbox was verified in this setup.

## Read the cost

- **Measured:** No Boat job or manual sandbox started during this setup, so there is no run-cost receipt. Use `boat ci job <job-id>` for CI and `boat usage "$id"` before deleting a manual sandbox. Record the billed time and cost separately from model charges.
- **Published rates:** [Boat pricing](https://docs.boat.dev/pricing.md) lists `$0.018/hour` for `small` and `$0.036/hour` for `default`.
- **Inferred:** Ten billed minutes cost `$0.003` on `small` or `$0.006` on `default`, excluding paid starts, snapshots, and model charges. These are estimates, not caps or measured spend.
