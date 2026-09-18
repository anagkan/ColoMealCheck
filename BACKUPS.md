# Postgres backups and recovery

This runbook covers the Docker Compose deployment in this repository. It uses
`pg_dump` from the running `db` container, so Postgres does not need to be
published on a host port and the dump tool always matches the server version.

The short version is: take a logical database dump every night, copy it off the
server, and test restoring it. A copy of the running container is **not** a
database backup.

## What must be backed up

| Item | Why | Suggested schedule |
| --- | --- | --- |
| Postgres database | Members, credentials, attendance, settings, accounts, and audit history | Nightly and before an upgrade that changes the schema |
| `photos` volume | Enrollment photographs are files, not database rows | Nightly, or at least weekly if enrollment is infrequent |
| `.env` and the pinned image tag/commit | Needed to reproduce the deployment | Whenever either changes; store separately and securely |

The `db` and `api` containers themselves are disposable. They can be recreated
from the Compose file and image. Do not use `docker export`, and do not copy
`/var/lib/postgresql/data` while Postgres is running. A raw copy of `pgdata` is
only safe with Postgres stopped and is tied to a compatible Postgres major
version; the logical dump below is the primary backup.

These backups contain student PUIDs, card identifiers, account data, attendance
history, and possibly photographs. Restrict access and encrypt every off-server
copy.

## One-time setup

Run commands from the repository directory containing `.env`. Choose the same
Compose file used to start the deployment:

```bash
cd /home/colonial/ColoMealCheck
COMPOSE_FILE=docker-compose.yml
# Use docker-compose.deploy.yml above if this server pulls the published image.

BACKUP_DIR=/var/backups/colomealcheck
sudo install -d -m 700 -o "$USER" -g "$(id -gn)" "$BACKUP_DIR"
umask 077
```

Shell variables above last only for the current shell. The automation example
later in this document defines its own values.

## Take and verify a database backup

The application may remain online. `pg_dump` takes a consistent snapshot even
while check-ins are being written.

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DB_BACKUP="$BACKUP_DIR/database-$STAMP.sql.gz"
DB_PARTIAL="$DB_BACKUP.partial"

set -o pipefail
docker compose -f "$COMPOSE_FILE" exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  | gzip -9 > "$DB_PARTIAL"

gunzip -t "$DB_PARTIAL"
gunzip -c "$DB_PARTIAL" | grep "PostgreSQL database dump complete" > /dev/null
mv "$DB_PARTIAL" "$DB_BACKUP"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$DB_BACKUP")" > "$(basename "$DB_BACKUP").sha256")
echo "Verified $DB_BACKUP"
```

Important details:

- `-T` disables TTY allocation so Docker does not alter the dump stream.
- The single quotes make the database name and user expand inside the
  container from its environment; this continues to work if `.env` changes.
- `--clean --if-exists` lets a restore replace existing database objects.
- `--no-owner --no-privileges` avoids carrying deployment-specific ownership
  and grants to a replacement server.
- `pipefail` makes a failed `pg_dump` fail the pipeline instead of leaving a
  seemingly successful gzip file. The marker check also catches a truncated
  dump.

On macOS, use `shasum -a 256` instead of `sha256sum`.

## Back up enrollment photos

Photos are held in a different named volume. Discover its actual Docker name
instead of assuming that it is `colomealcheck_photos`; Compose derives the name
from its project name.

```bash
API_CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps -q api)
PHOTO_VOLUME=$(docker inspect "$API_CONTAINER" --format \
  '{{range .Mounts}}{{if eq .Destination "/srv/data/photos"}}{{.Name}}{{end}}{{end}}')
test -n "$PHOTO_VOLUME"

PHOTO_BACKUP="$BACKUP_DIR/photos-$STAMP.tar.gz"
PHOTO_PARTIAL="$PHOTO_BACKUP.partial"
docker run --rm \
  --mount "type=volume,src=$PHOTO_VOLUME,dst=/photos,readonly" \
  --mount "type=bind,src=$BACKUP_DIR,dst=/backup" \
  alpine:3.22 tar -czf "/backup/$(basename "$PHOTO_PARTIAL")" -C /photos .

gzip -t "$PHOTO_PARTIAL"
mv "$PHOTO_PARTIAL" "$PHOTO_BACKUP"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$PHOTO_BACKUP")" > "$(basename "$PHOTO_BACKUP").sha256")
```

The first run may pull the small Alpine image. Run this outside enrollment
hours. For a strictly point-in-time-consistent database-and-photo pair, briefly
stop writes with `docker compose -f "$COMPOSE_FILE" stop api`, take both
backups, and then run `docker compose -f "$COMPOSE_FILE" start api`.

Keep `.env` in an encrypted password manager or encrypted backup system rather
than copying it into the same unencrypted directory. Record the exact deployed
image tag or Git commit with it.

## Automate it

Create `/usr/local/sbin/colomealcheck-db-backup` with the following contents,
editing `APP_DIR`, `BACKUP_DIR`, and `COMPOSE_FILE` for the server:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR=/home/colonial/ColoMealCheck
BACKUP_DIR=/var/backups/colomealcheck
COMPOSE_FILE=docker-compose.yml
RETENTION_DAYS=30

cd "$APP_DIR"
mkdir -p "$BACKUP_DIR"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
final="$BACKUP_DIR/database-$stamp.sql.gz"
partial="$final.partial"
trap 'rm -f "$partial"' EXIT

docker compose -f "$COMPOSE_FILE" exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  | gzip -9 > "$partial"

gunzip -t "$partial"
gunzip -c "$partial" | grep "PostgreSQL database dump complete" > /dev/null
mv "$partial" "$final"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$final")" > "$(basename "$final").sha256")

# Only completed database backups matching this exact naming pattern are pruned.
find "$BACKUP_DIR" -type f -name 'database-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'database-*.sql.gz.sha256' -mtime +"$RETENTION_DAYS" -delete
```

Make it root-owned and executable, then run it once by hand:

```bash
sudo chown root:root /usr/local/sbin/colomealcheck-db-backup
sudo chmod 750 /usr/local/sbin/colomealcheck-db-backup
sudo /usr/local/sbin/colomealcheck-db-backup
sudo ls -lh /var/backups/colomealcheck
```

Add this to root's crontab with `sudo crontab -e` to run it every night at
04:15. `flock` prevents overlapping runs:

```cron
15 4 * * * /usr/bin/flock -n /run/lock/colomealcheck-backup /usr/local/sbin/colomealcheck-db-backup >> /var/log/colomealcheck-backup.log 2>&1
```

Automate the photo command separately at a quiet time, or include it in an
encrypted file-backup tool that can read Docker volumes. Check the backup log
and alert on a missed or failed run. Retention is not a substitute for an
off-server copy: synchronize completed, verified files to encrypted storage on
another machine or provider.

A practical baseline is 30 nightly database dumps, 8 weekly photo archives,
and at least one copy off the server. Adjust this to the club's written data
retention policy.

## Restore the live deployment

Restoring replaces the current database. Confirm the filename and take a fresh
safety backup first if the current database may still be useful.

```bash
cd /home/colonial/ColoMealCheck
COMPOSE_FILE=docker-compose.yml       # or docker-compose.deploy.yml
DB_BACKUP=/secure/path/database-20260909T081500Z.sql.gz
PHOTO_BACKUP=/secure/path/photos-20260909T081500Z.tar.gz

(cd "$(dirname "$DB_BACKUP")" && sha256sum -c "$(basename "$DB_BACKUP").sha256")
gunzip -t "$DB_BACKUP"

docker compose -f "$COMPOSE_FILE" stop api
docker compose -f "$COMPOSE_FILE" up -d db
gunzip -c "$DB_BACKUP" \
  | docker compose -f "$COMPOSE_FILE" exec -T db sh -c \
      'psql -1 -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Restore photos if needed. This extracts over the existing volume; files not in
the archive remain but are harmless because the database does not refer to
them.

```bash
docker compose -f "$COMPOSE_FILE" create api
API_CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps -aq api)
PHOTO_VOLUME=$(docker inspect "$API_CONTAINER" --format \
  '{{range .Mounts}}{{if eq .Destination "/srv/data/photos"}}{{.Name}}{{end}}{{end}}')
test -n "$PHOTO_VOLUME"

PHOTO_DIR=$(dirname "$PHOTO_BACKUP")
PHOTO_FILE=$(basename "$PHOTO_BACKUP")
docker run --rm \
  --mount "type=volume,src=$PHOTO_VOLUME,dst=/photos" \
  --mount "type=bind,src=$PHOTO_DIR,dst=/backup,readonly" \
  alpine:3.22 tar -xzf "/backup/$PHOTO_FILE" -C /photos

docker compose -f "$COMPOSE_FILE" start api
docker compose -f "$COMPOSE_FILE" logs --tail=100 api
```

The API entrypoint applies any newer Alembic migrations when it starts. Restore
first and start the API second.

Verify all of the following:

1. `/healthz` returns `{"ok":true,"database":"up"}`.
2. `/admin` shows the expected members and recent attendance.
3. A member with an enrollment photo still displays that photo.
4. The kiosk can perform a test check-in, and the test record is removed if it
   should not remain in attendance history.

## Test a restore without touching production

Do this after first configuring backups and at least quarterly. The explicit
project name creates separate containers and volumes; port 8001 avoids the live
application.

```bash
cd /home/colonial/ColoMealCheck
COMPOSE_FILE=docker-compose.yml       # or docker-compose.deploy.yml
DB_BACKUP=/secure/path/database-20260909T081500Z.sql.gz

COMPOSE_PROJECT_NAME=colomealrestore \
  docker compose -f "$COMPOSE_FILE" up -d db

gunzip -c "$DB_BACKUP" \
  | COMPOSE_PROJECT_NAME=colomealrestore \
      docker compose -f "$COMPOSE_FILE" exec -T db sh -c \
        'psql -1 -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

COMPOSE_PROJECT_NAME=colomealrestore API_PORT=127.0.0.1:8001 \
  docker compose -f "$COMPOSE_FILE" up -d api

curl -fsS http://127.0.0.1:8001/healthz
COMPOSE_PROJECT_NAME=colomealrestore \
  docker compose -f "$COMPOSE_FILE" logs --tail=100 api
```

Inspect the scratch admin UI at `http://127.0.0.1:8001/admin`. If photographs
are part of the test, restore the photo archive into the
`colomealrestore_photos` volume before starting `api`.

After the test passes, remove only the explicitly named scratch project:

```bash
COMPOSE_PROJECT_NAME=colomealrestore \
  docker compose -f "$COMPOSE_FILE" down -v
```

Never add `-v` when bringing down the live project: it deletes the live named
volumes.

## Recovery checklist for a new server

1. Install Docker and check out the intended application version.
2. Restore `.env` securely and select the correct Compose file.
3. Start only Postgres with `docker compose -f "$COMPOSE_FILE" up -d db`.
4. Restore the database dump.
5. Create/identify the photos volume and restore its archive.
6. Start the API, allow migrations to finish, and run the verification checks.
7. Re-enable the backup schedule and confirm that the next off-server copy
   succeeds.

CSV exports from the admin UI are reports, not backups: they omit credentials,
photos, accounts, settings, and parts of the audit history, and cannot recreate
the application database.
