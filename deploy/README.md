# Deploy: native systemd (no Docker)

We run the hunter directly on this host via `uv run`, supervised by two
systemd services — one per profile. Docker is *not* used here; the
`docker-compose.yml` and `Dockerfile` in the repo root are just an
alternative deploy path we don't take.

## Layout

- `bfh-single.service` — `profiles/single.yaml` (≤€600, 1 room)
- `bfh-wg.service`     — `profiles/wg.yaml` (≤€1100, 2 rooms)

Each service runs a `BerlinHunter` loop; both share the same Python venv
(`.venv/`) but have isolated DBs and schema-monitor state under `data/<profile>/`.

## One-time install

```bash
# Deps already installed via `uv sync` — re-run if pyproject.toml changes.
sudo install -m 644 deploy/bfh-single.service deploy/bfh-wg.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bfh-single.service bfh-wg.service
```

## Operate

```bash
sudo journalctl -u bfh-single -u bfh-wg -f          # tail both
sudo systemctl status bfh-single bfh-wg             # state
sudo systemctl restart bfh-single bfh-wg            # after editing a profile
sudo systemctl stop bfh-single bfh-wg               # stop both
```

## Dry-test before going live (recommended once)

`auto_apply.dry_run` is `false` in the profiles, so applications submit for
real. To do a single fill-but-don't-submit test cycle, edit
`profiles/single.yaml` to set `auto_apply.dry_run: true`, then:

```bash
cd /root/berlin-flat-hunter
/root/.local/bin/uv run python main.py --config profiles/single.yaml --once
```

Watch the logs for `auto_apply: dry_run filled fields ...` lines — those mean
selectors still match. Flip `dry_run` back to `false` and start the systemd
unit.

## Telegram

Per-listing Telegram is OFF (`notifiers: []` in both profiles). Telegram only
fires when a crawler returns 0 results for 3 consecutive cycles, or when more
than 50% of fields are missing — i.e. "the crawler is broken, fix me".
Routed via `monitoring.alert_notifiers: [telegram]`.

## Uninstall

```bash
sudo systemctl disable --now bfh-single bfh-wg
sudo rm /etc/systemd/system/bfh-single.service /etc/systemd/system/bfh-wg.service
sudo systemctl daemon-reload
```
