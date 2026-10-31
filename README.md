# c411_transmission

Cron script that keeps a [Transmission](https://transmissionbt.com) daemon fed with torrents from a
Torznab indexer (C411), while keeping a minimum of free disk space.

One script, `c411.py`, for every machine. Each machine's settings live in a JSON file: by default
`c411.json` next to the script, or any file given with `--config`. Two examples are in
[`exemples/`](exemples): `machine1.json` (dedicated data disk, fixed download folder) and
`machine2.json` (system disk, download folders read from the torrents).
`c411.json` is in `.gitignore`: it holds the real password and API key.

## What the script does, on each run

1. Reads the Transmission state over RPC (session, stats, torrents) and saves it as JSON in `base_dir`.
2. Start watch: a torrent added by the script that has received no data after `start_timeout_min`
   minutes is removed with its data, and its `.torrent` is deleted from `watch_dir`.
3. With `--rm-stalled [MIN]`: a downloading torrent whose received bytes have not changed for MIN minutes
   is removed with its data. Progress is tracked between runs in `get_stalled_state.json`.
4. Idle torrents: with `idle_remove_hours` set, every finished torrent (seeding or stopped) with no data
   exchanged for that many hours is removed with its data, even when there is enough free space.
   A torrent uploading right now is kept. Last activity is Transmission's `activityDate`, or the
   completion date, or the date added.
5. Orphans: files and folders in the download directory that belong to no torrent are deleted.
6. Free space, measured ahead: the space budget is

   ```
   budget = free space on space_check_path - bytes still to download - min_free_gb
   ```

   so `min_free_gb` stays free once every download has finished. If the budget is negative (the running
   downloads would end below `min_free_gb`), finished torrents are removed in `victim_policy` order until
   it is back to zero.
7. Add, with rotation: `add_policy` gives the number of torrents that can be added now (with
   `max_downloads`: `max_downloads` minus the torrents downloading or queued, minus the `.torrent` files
   dropped but not yet picked up by Transmission). The script searches the indexer for new candidates
   (smaller than `max_torrent_gb`, fewer than `max_seeders` seeders, not already in Transmission, in the
   watch folder or removed before) and drops them into `watch_dir`:
   - candidates that fit in the budget go first;
   - for one that does not fit, finished torrents are removed in `victim_policy` order until it fits, so
     downloads keep rotating even when the disk is at `min_free_gb`;
   - nothing is removed for a candidate that would not fit even after removing every finished torrent;
   - each added torrent is taken off the budget.

   Removal order with `victim_policy` `ratio`: finished torrents (seeding or stopped) that nobody is
   downloading from, lowest ratio first, then the others, lowest ratio first. Downloads in progress are
   never removed. Every removed torrent is remembered in `get_removed_torrents.json` (last 1000 names)
   and never taken again from the indexer, so the rotation cannot download and remove the same torrent
   in a loop. In `--dry-run`, a candidate that needs a simulated removal is not added.
8. Writes a one-line summary (130 ASCII characters max) to `/var/log/torrents_status`, overwritten on each run:

   ```
   machine1 31/10 14:01 DL 3/3 (87% 45% 20%) +1 att | v1.5M ^2.0M | libre 98G dispo 41G | r1.57 285t | DL complet
   ```

   | Part | Meaning |
   |---|---|
   | `DL 3/3` | downloads running / `max_downloads` |
   | `(87% 45% 20%)` | their progress, most advanced first (4 at most) |
   | `+1 att` | `.torrent` files dropped but not yet picked up by Transmission |
   | `v1.5M ^2.0M` | download and upload speed |
   | `libre 98G` | free space on `space_check_path` |
   | `dispo 41G` / `manque 17G` | room for new torrents (free − still to download − `min_free_gb`), or what the rotation must free |
   | `r1.57 285t` | global ratio (Transmission cumulative stats) and torrent count; `2err` added when torrents are in error |
   | last part | what the run did: `-2 rotation`, `-3 inactif`, `-1 orphelin`, `+Name`, `DL complet`… (`RAS` when nothing, `[sim]` in dry-run) |

   When the line would exceed 130 characters, the progress percentages go first, then the speeds, so
   the actions are cut last. If Transmission cannot be reached the line is
   `machine1 31/10 14:01 libre 98G | ERREUR …`.

## Configuration (JSON)

| Key | Default | Meaning |
|---|---|---|
| `machine` | required | name shown in the reports and the summary line |
| `host` | `localhost` | Transmission RPC host |
| `port` | required | Transmission RPC port |
| `username` | `transmission` | RPC user |
| `password` | required | RPC password |
| `c411_apikey` | required | indexer API key |
| `c411_cat` | required | Torznab category searched (text or number) |
| `max_torrent_gb` | required | candidate filter: size below |
| `max_seeders` | required | candidate filter: seeders below |
| `base_dir` | `/root/scripts` | where the JSON state files are written |
| `watch_dir` | required | folder watched by Transmission (`watch-dir` in its settings) |
| `download_dir` | `null` | download folder scanned for orphans; `null` = folders read from the torrents |
| `min_free_gb` | `30` | free space kept once every download has finished; finished torrents are removed to stay above it and to make room for new ones |
| `space_check_path` | required | path where free space is measured |
| `dry_run_default` | `false` | `true` = deletions are simulated unless `--force-delete` is given |
| `victim_policy` | `ratio` | `ratio`: finished torrent with the lowest ratio, idle ones first; `oldest`: inactive for the longest time |
| `add_policy` | `max_downloads` | `max_downloads`: keep up to `max_downloads` downloads running; `all_finished`: add one only when every torrent is complete; `no_download`: add one when nothing is downloading |
| `max_downloads` | `3` | simultaneous downloads with `add_policy` `max_downloads` |
| `start_timeout_min` | `15` | start watch delay in minutes; `null` disables it |
| `stall_minutes_default` | `10` | value of `--rm-stalled` without an argument |
| `idle_remove_hours` | `null` | remove finished torrents with no activity for that many hours (decimals allowed); `null` = never |

The file is checked before anything else: a missing required key, a wrong type, an unknown value or an
unknown key (typo) stops the script with a message and exit code 2.

## Options

| Option | Effect |
|---|---|
| `--config FILE` | JSON configuration of the machine (default: `c411.json` next to the script) |
| `--dry-run` | simulate every deletion (wins over `--force-delete`) |
| `--force-delete` | real deletions even if `dry_run_default` is `true` |
| `--skip-orphans` | do not scan or clean orphans |
| `--rm-stalled [MIN]` | remove downloads stuck for MIN minutes |
| `--rmall` | remove every torrent and its data, then exit |

`--dry-run` only blocks deletions: a `.torrent` can still be downloaded into the watch folder.

## Installation

Python 3 standard library only. Copy `c411.py` and the machine's JSON (renamed `c411.json`) to the server,
prefer `scp` over copy-paste, then check:

```sh
python3 -m py_compile /root/scripts/c411.py && echo OK
python3 /root/scripts/c411.py --dry-run
cat /var/log/torrents_status
```

Cron every 5 minutes, with a lock so two runs never overlap (`lockf -t 0` on FreeBSD, `flock -n` on Linux).
Use the full interpreter path: cron's `PATH` is short. `-u` writes the log as the script runs.

```sh
*/5 * * * * lockf -t 0 /tmp/c411.lock /usr/local/bin/python3 -u /root/scripts/c411.py --rm-stalled 60 >> /var/log/c411.log 2>&1
```
