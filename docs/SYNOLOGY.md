# Synology container deployment

The production layout keeps mutable data and secrets outside the image:

```text
~/.yonstudy/
├── compose.yaml
├── .env                    host paths used by Docker Compose
├── source/                 clean Git checkout used for builds
├── config/
│   ├── yonstudy.env        mode 0600; LearnUs and mail credentials
│   └── rclone.conf         local archive alias
├── logs/
└── store/                  SQLite, blobs, reports, cookies
```

The human-readable archive is mounted separately at `/archive`. The configured
`archive:` rclone alias writes there without sending files through SMB back to
the same NAS.

## Initial deployment

Install Synology Container Manager and place a clean checkout in `source/`.
Copy `compose.yaml` to the deployment directory and create `.env` from
`deploy/synology.env.example`. Create the two files in `config/` from their
examples and restrict the credential file. The first image is built locally;
there is no registry image to pull:

```bash
chmod 600 config/yonstudy.env config/rclone.conf
sudo docker compose build scheduler
sudo docker compose up -d
sudo docker compose ps
```

Run a read-only application check:

```bash
sudo docker exec yonstudy /app/deploy/run-job.sh keepalive
sudo docker exec yonstudy python /app/cli.py --store /data/store status
```

## Automatic updates

`.github/workflows/container.yml` runs the unit tests and verifies that the
container image builds after every push to `main`. The NAS updater checks the
public repository every five minutes and deploys a commit only after its exact
GitHub `test` check succeeds. It builds in the dedicated clean checkout and
keeps the previous image as `yonstudy:rollback`. The SQLite store, cookies,
logs, and archive are bind mounts, so replacing the application container does
not replace user data. No inbound NAS port, registry credential, or GitHub
deployment token is required.

Inspect deployment and scheduler logs with:

```bash
sudo docker compose logs --tail=100 scheduler updater
tail -n 100 logs/scheduler.log
```
