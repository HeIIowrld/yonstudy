# Synology container deployment

The production layout keeps mutable data and secrets outside the image:

```text
~/.yonstudy/
├── compose.yaml
├── .env                    host paths used by Docker Compose
├── source/                 clean Git checkout used for builds
├── config/
│   ├── yonstudy.env        mode 0600; LearnUs and mail credentials
│   ├── rclone.conf         local archive alias
│   └── timetable.toml      optional class schedule for recording matching
├── logs/
└── store/                  SQLite, blobs, reports, cookies

~/02_Personal/01_학교/
├── 00.녹음_넣기/           phone/PC recording hot folder
└── 10.학기/                human-readable course archive
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

Transcription has its own `compose.transcription.yaml`, independent from the
scheduler. Copy `deploy/transcription.env.example` to a private env file. Select
`COMPOSE_PROFILES=local` to mount a semester folder directly on a NAS or workstation.
Select `COMPOSE_PROFILES=remote` on a compute host to stage media through rclone and
upload only reviewed subtitles, reports, state and exact old-subtitle backups.

```bash
cp deploy/transcription.env.example ~/.yonstudy/transcription.env
chmod 600 ~/.yonstudy/transcription.env
$EDITOR ~/.yonstudy/transcription.env
sudo docker compose --env-file ~/.yonstudy/transcription.env \
  -f compose.transcription.yaml up -d --build
sudo docker compose --env-file ~/.yonstudy/transcription.env \
  -f compose.transcription.yaml logs -f
```

The local profile defaults to small/int8, two CPU threads and 2 GB. The remote
profile defaults to large-v3-turbo/int8, 12 CPU threads and 7 GB. Both keep model
cache, queue state and backups in host directories across image replacement. Read
`<archive>/<semester>/전사_현황.md` for progress. The heartbeat health check stays
active during long jobs. Suspect subtitles are processed before missing recordings
and videos, and a failed file does not stall the remaining queue.

An LXC without nested Docker can run `systemd/yonstudy-remote-transcribe.service`
directly with the same remote environment variables. Media decoding and ASR then
consume only that LXC's CPU and RAM. `--update` uploads preserve a newer NAS-side edit.

To enable recording classification, copy `deploy/timetable.toml.example` to
`config/timetable.toml`, replace its course IDs with values from `courses`, and
enable `YONSTUDY_TIMETABLE=/config/timetable.toml` in `config/yonstudy.env`.
Recordings uploaded to the host directory configured by
`YONSTUDY_RECORDING_HOST_DIR` are checked every three minutes. Keep this folder
outside the hidden deployment directory so it is easy to reach from a phone.
If the variable is omitted, Compose uses `00.녹음_넣기` beside the configured
archive directory.
A file must have the same size and mtime across scans for at least 120 seconds;
only after successful content-addressed storage is the uploaded original
consumed from the hot folder. Failed or incomplete files remain there for a
later retry. Matched recordings are linked into the course root under
`/archive`, while uncertain files go to `/archive/unmatched/recordings`.

```bash
mkdir -p /volume3/homes/USER/02_Personal/01_학교/00.녹음_넣기
cp source/deploy/timetable.toml.example config/timetable.toml
sudo docker exec yonstudy /app/deploy/run-job.sh recordings
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
