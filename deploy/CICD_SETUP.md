# CI/CD Setup — one-time steps

The GitHub Actions deploy workflow is already committed at
`.github/workflows/deploy.yml`. It won't fire until you complete
these one-time steps.

## 1. Generate a deploy-only SSH keypair on your Mac

```bash
ssh-keygen -t ed25519 -C "gh-deploy" -f ~/.ssh/jcc_deploy -N ""
```

This makes two files:
- `~/.ssh/jcc_deploy` (private key — goes into GitHub Secrets)
- `~/.ssh/jcc_deploy.pub` (public key — goes on the droplet)

## 2. Add the public key to the droplet

```bash
cat ~/.ssh/jcc_deploy.pub | ssh root@143.198.188.116 'cat >> ~/.ssh/authorized_keys'
```

Test the key works:
```bash
ssh -i ~/.ssh/jcc_deploy root@143.198.188.116 'echo works'
```

You should see `works`.

## 3. Add 3 GitHub Secrets

Go to https://github.com/ramvamshi019/job-control-center/settings/secrets/actions
→ **New repository secret** → add these three:

| Name              | Value                                    |
|-------------------|------------------------------------------|
| `DEPLOY_SSH_KEY`  | `cat ~/.ssh/jcc_deploy` (whole file)     |
| `DEPLOY_HOST`     | `143.198.188.116`                        |
| `DEPLOY_USER`     | `root`                                   |

## 4. Test the pipeline

Push any commit to `main`:

```bash
git commit --allow-empty -m "test: kick auto-deploy"
git push origin main
```

Watch the run at:
https://github.com/ramvamshi019/job-control-center/actions

You should see:
1. **tests** workflow — pytest runs, green
2. **deploy** workflow — auto-fires when tests pass, SSHs, pulls,
   rebuilds, runs `./deploy/verify.sh`

## What happens on failure

- **Tests red** → deploy doesn't fire at all. Fix, push again.
- **Deploy fails at `docker compose up`** → droplet is likely in a mixed
  state; the "Notify on failure" step prints a rollback command.
  Runs `verify.sh` last so you know exactly what's broken.
- **Verify red** (containers up but health endpoints failing) → same:
  action fails, red X on the commit, rollback command shown.

## Rollback (manual, ~10 seconds)

```bash
ssh root@143.198.188.116 'cd /root/job-control-center && git reset --hard HEAD~1 && docker compose up -d --build'
```

## What changed vs the manual flow

Before (every deploy this session):
1. Local edits
2. `scp` file to droplet
3. `docker cp` into container
4. `docker restart`
5. Hope nothing broke
6. Manually check `/health`

After (just push):
1. Local edits
2. `git push origin main`
3. Actions runs tests → deploys → verifies → alerts on failure

Same wall-clock time, way less error-prone. And it works even when
you're not at your Mac (phone-triggered pushes deploy fine too).
