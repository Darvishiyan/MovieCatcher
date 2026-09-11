# MovieCatcher

MovieCatcher is a self-hosted Telegram download service designed to complement a
[Jellyfin](https://jellyfin.org/) media server. Send the bot a file, choose a
destination folder from Telegram, and MovieCatcher writes the download into a host
directory that Jellyfin can scan.

Despite its name and its primary Jellyfin use case, **MovieCatcher is not limited to
movies**. It can save any file that Telegram accepts as a document or common media
attachment. It intentionally does not download URLs; the file itself must be sent to
the bot through Telegram.

## Features

- Downloads Telegram documents, videos, audio, animations, voice messages, video
  notes, and photos.
- Queues confirmed downloads and processes them one at a time in FIFO order.
- Provides an inline folder browser before each download.
- Opens the folder browser at each user's most recently selected destination.
- Creates destination folders directly from Telegram.
- Shows percentage, transferred size, and speed as concise text without a graphical
  progress bar.
- Supports large Telegram downloads through Pyrogram/MTProto.
- Restricts access with allowlisted Telegram user IDs and/or chat IDs.
- Prevents destination navigation outside the configured download root.
- Stores credentials and server-specific paths only in environment variables.
- Runs as a non-root user in a portable Docker Compose stack.
- Persists the Pyrogram session without adding it to the image or Git repository.

## Architecture and how it works

```text
Telegram file attachment
        |
        v
MovieCatcher bot -- access allowlist
        |
        v
Folder selection (last destination is remembered)
        |
        v
Single FIFO download queue
        |
        v
Pyrogram/MTProto download worker
        |
        v
Selected directory under DOWNLOAD_ROOT
        |
        v
Host DOWNLOAD_PATH <----> Jellyfin library mount and scan
```

`DOWNLOAD_PATH` is a path on the Docker host. Docker mounts it at `DOWNLOAD_ROOT`
inside the MovieCatcher container. Jellyfin should mount the same host directory as
one of its library paths; its path *inside the Jellyfin container* does not have to
match MovieCatcher's internal path.

## Requirements

- Docker Engine with Docker Compose v2
- A Telegram bot token from [BotFather](https://t.me/BotFather)
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org/)
- At least one allowed Telegram user ID or chat ID
- A writable host directory for downloads
- Network access to Telegram

## Quick start

```bash
git clone https://github.com/Darvishiyan/MovieCatcher.git
cd MovieCatcher

cp .env.example .env
mkdir -p downloads data
```

Edit `.env` and replace the Telegram credential and allowlist placeholders. For a
real Jellyfin library, set `DOWNLOAD_PATH` to that library's host path, for example:

```dotenv
DOWNLOAD_PATH=/srv/media
DOWNLOAD_ROOT=/downloads
```

Validate and start the stack:

```bash
docker compose config
docker compose up -d --build
```

Then send the bot a file or attachment. Select an existing folder—or create a new
one—and press **Download here**. For uncommon file types, send the item as a Telegram
document to preserve its original name and extension.

You can send another file while a download is running. MovieCatcher immediately lets
you choose its destination, adds it to the queue, and starts it after earlier items
finish. The next folder browser opens at your last selected folder, which is useful
when downloading several episodes into the same series directory.

## Queue behavior

- The queue is global and FIFO: only one file downloads at a time.
- Folder selection remains responsive while the worker downloads another file.
- Each user has an independent last-folder preference.
- The graphical progress bar is intentionally omitted; percentage, transferred
  size, speed, and lifecycle status remain available as text.
- The queue and last-folder preferences are held in memory. Restarting or redeploying
  the container clears waiting items and resets last-folder preferences; completed
  files remain on the mounted host directory.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token issued by BotFather. |
| `TELEGRAM_API_ID` | Yes | — | Numeric Telegram application API ID. |
| `TELEGRAM_API_HASH` | Yes | — | Telegram application API hash. |
| `ALLOWED_USER_IDS` | Conditional | Empty | Comma-separated Telegram user IDs. At least this or `ALLOWED_CHAT_IDS` must be set. |
| `ALLOWED_CHAT_IDS` | Conditional | Empty | Comma-separated Telegram chat IDs; group IDs are commonly negative. |
| `DOWNLOAD_PATH` | Yes for Compose | `./downloads` | Download directory on the Docker host. Use an absolute path in production/Portainer. |
| `DOWNLOAD_ROOT` | Yes | `/downloads` in the example | Path inside the MovieCatcher container and root of its folder browser. |
| `SESSION_PATH` | No | `./data` | Host directory containing the sensitive Pyrogram session database. |
| `PUID` | No | `1000` | UID used to run the container. Match the owner of the mounted host directories. |
| `PGID` | No | `1000` | GID used to run the container. Match the group of the mounted host directories. |
| `MAX_FILE_SIZE_GB` | No | `10` | Maximum accepted Telegram file size in GiB. |
| `LOG_LEVEL` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

Values in `.env.example` are placeholders only. Copy the file to `.env`; never put
real credentials or private IDs in `.env.example`.

## Volumes and download paths

The Compose stack uses two bind mounts:

- `${DOWNLOAD_PATH}:${DOWNLOAD_ROOT}` stores downloaded files.
- `${SESSION_PATH}:/data` stores the Pyrogram session database.

On Linux, create both host directories and make sure `PUID:PGID` can write to them.
For example, when using UID/GID `1000:1000`:

```bash
sudo chown -R 1000:1000 /srv/media ./data
```

Use the narrowest suitable media directory rather than mounting an entire disk or
filesystem root. If Jellyfin runs in Docker, mount the same host `DOWNLOAD_PATH` into
the Jellyfin container and add that Jellyfin-side path to a library.

## Portainer

MovieCatcher can also be deployed as a Portainer Git stack:

1. Create a stack from a Git repository.
2. Use `https://github.com/Darvishiyan/MovieCatcher.git` and branch `main`.
3. Set the Compose path to `docker-compose.yml`.
4. Define every required environment variable in Portainer's stack environment.
5. Use absolute host paths for `DOWNLOAD_PATH` and `SESSION_PATH`.
6. Deploy the stack, then inspect its logs for the `MovieCatcher started` message.

Because secrets are intentionally absent from the repository, a Git-based Portainer
deployment will not start until its required environment variables are configured.

## Logs

Follow the service logs:

```bash
docker compose logs --follow moviecatcher
```

Show the latest 200 lines:

```bash
docker compose logs --tail 200 moviecatcher
```

Logs are written to standard output and are managed by Docker. Set `LOG_LEVEL=DEBUG`
temporarily for diagnosis; switch back to `INFO` afterward to reduce verbosity.

## Updating

Pull the newest source, rebuild, and recreate only what changed:

```bash
git pull --ff-only
docker compose up -d --build
```

The files under `DOWNLOAD_PATH` and the session under `SESSION_PATH` remain on the
host during container replacement. Review release changes before updating a service
that has write access to a media library.

## Security notes

- Never commit `.env`, `config.json`, Telegram session files, logs, tokens, API
  credentials, passwords, private IDs, or server-specific paths.
- Set at least one narrow allowlist. Anyone allowed to use the bot can write files
  and create directories under `DOWNLOAD_ROOT`.
- Treat `SESSION_PATH` as a secret. Back it up securely and do not share it.
- Rotate the bot token/API credentials and recreate the session if they are exposed.
- The container runs without root privileges and with `no-new-privileges`, but the
  mounted download directory is intentionally writable.
- Keep Docker, the base image, Python dependencies, and Jellyfin updated.

## Troubleshooting

### Compose reports a missing variable

Copy `.env.example` to `.env`, replace all Telegram placeholders, and set at least
one allowlist variable. Run `docker compose config` again before starting.

### `Permission denied` while creating a folder or downloading

Confirm `DOWNLOAD_PATH` and `SESSION_PATH` exist on the host and are writable by
`PUID:PGID`. Avoid fixing this with world-writable permissions; correct ownership or
group access instead.

### The bot replies `Not authorized`

Verify the sender's numeric user ID or the chat's numeric ID is present in the
corresponding comma-separated allowlist. Recreate the container after editing `.env`:

```bash
docker compose up -d
```

### A Telegram download fails or is rejected as too large

Check the container logs and `MAX_FILE_SIZE_GB`. Telegram-side limits and account/API
restrictions still apply even when the local limit is higher.

### Jellyfin does not see a downloaded file

Confirm Jellyfin mounts the same host `DOWNLOAD_PATH`, has read permission, and has
that container-side directory configured as a library. Trigger a Jellyfin library
scan or wait for its scheduled scan.

### The Pyrogram session is locked

Run only one MovieCatcher container against a given `SESSION_PATH`. Stop duplicate
containers before restarting the service.

## Disclaimer

MovieCatcher is a general-purpose, self-hosted downloader. It is not affiliated with
Telegram or Jellyfin. You are responsible for complying with applicable laws,
copyright, privacy rules, and storage/security requirements. Download only content
you are authorized to access and retain.
