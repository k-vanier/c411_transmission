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
4. Orphans: files and folders in the download directory that belong to no torrent are deleted.
5. Free space: if less than `min_free_gb` is free on `space_check_path`, one torrent is removed
   according to `victim_policy`, then free space is checked again.
6. Add: `add_policy` gives the number of torrents that can be added now (with `max_downloads`:
   `max_downloads` minus the torrents downloading or queued, minus the `.torrent` files dropped but not yet
   picked up by Transmission). The script then searches the indexer and drops new candidates (smaller than
   `max_torrent_gb`, fewer than `max_seeders` seeders, not already in Transmission or in the watch folder)
   into `watch_dir`, as long as each one fits in the space budget:

   ```
   budget = free space - bytes still to download - min_free_gb
   ```

   so `min_free_gb` stays free once every download has finished. A candidate larger than the budget is
   skipped for a smaller one; each added torrent is taken off the budget.
7. Writes a one-line summary (130 ASCII characters max) to `/var/log/torrents_status`, overwritten on each run:

   ```
   machine1 25/10 13:10 15t 1dl 14seed 0stop r1.40 libre 1200G | -2 orphelin, +Some.Movie.2024.1080p
   ```

   Torrent count (total, downloading, seeding, stopped), global ratio (Transmission cumulative stats),
   free space, then what the run did (`RAS` when nothing, `[sim]` in dry-run, `ERREUR …` if RPC fails).

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
| `min_free_gb` | `30` | minimum free space; below it a torrent is removed and nothing is added |
| `space_check_path` | required | path where free space is measured |
| `dry_run_default` | `false` | `true` = deletions are simulated unless `--force-delete` is given |
| `victim_policy` | `ratio` | `ratio`: finished torrent with the lowest ratio, idle ones first; `oldest`: inactive for the longest time |
| `add_policy` | `max_downloads` | `max_downloads`: keep up to `max_downloads` downloads running; `all_finished`: add one only when every torrent is complete; `no_download`: add one when nothing is downloading |
| `max_downloads` | `3` | simultaneous downloads with `add_policy` `max_downloads` |
| `start_timeout_min` | `15` | start watch delay in minutes; `null` disables it |
| `stall_minutes_default` | `10` | value of `--rm-stalled` without an argument |

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
