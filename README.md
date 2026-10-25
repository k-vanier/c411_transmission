# c411_transmission

Cron script that keeps a [Transmission](https://transmissionbt.com) daemon fed with torrents from a
Torznab indexer (C411), while keeping a minimum of free disk space.

The two files `c411_machine1.py` and `c411_machine2.py` are the **same code**: only the shebang and the
`CONFIGURATION` block at the top differ. Fix or improve the shared code in both files, change
settings only in the configuration block.

## What the script does, on each run

1. Reads the Transmission state over RPC (session, stats, torrents) and saves it as JSON in `BASE_DIR`.
2. Start watch: a torrent added by the script that has not started after `START_TIMEOUT_MIN` minutes
   is removed with its data, and its `.torrent` is deleted from `WATCH_DIR`.
3. With `--rm-stalled [MIN]`: a downloading torrent whose received bytes have not changed for MIN minutes
   (10 by default) is removed with its data. Progress is tracked between runs in `get_stalled_state.json`.
4. Orphans: files and folders in the download directory that belong to no torrent are deleted.
5. Free space: if less than `MIN_FREE_GB` is free on `SPACE_CHECK_PATH`, one torrent is removed
   according to `VICTIM_POLICY`, then free space is checked again.
6. Add: if there is enough space and `ADD_POLICY` allows it, the script searches the indexer and drops
   the first new candidate (smaller than `MAX_TORRENT_GB`, fewer than `MAX_SEEDERS` seeders, not already
   in Transmission or in the watch folder) into `WATCH_DIR`.
7. Writes a one-line summary (130 ASCII characters max) to `/var/log/torrents_status`, overwritten on each run:

   ```
   machine1 25/10 13:10 15t 1dl 14seed 0stop r1.40 libre 1200G | -2 orphelin, +Some.Movie.2024.1080p
   ```

   Torrent count (total, downloading, seeding, stopped), global ratio (Transmission cumulative stats),
   free space, then what the run did (`RAS` when nothing, `[sim]` in dry-run, `ERREUR …` if RPC fails).

## Configuration

| Setting | Meaning |
|---|---|
| `MACHINE` | name shown in the reports and the summary line |
| `HOST`, `PORT`, `USERNAME`, `PASSWORD` | Transmission RPC access |
| `C411_APIKEY`, `C411_CAT` | indexer API key and Torznab category |
| `MAX_TORRENT_GB`, `MAX_SEEDERS` | candidate filter: size below, seeders below |
| `BASE_DIR` | where the JSON state files are written |
| `WATCH_DIR` | folder watched by Transmission (`watch-dir` in its settings) |
| `DOWNLOAD_DIR` | download folder scanned for orphans; `None` = folders read from the torrents |
| `MIN_FREE_GB`, `SPACE_CHECK_PATH` | minimum free space and the path where it is measured |
| `DRY_RUN_DEFAULT` | `True` = deletions are simulated unless `--force-delete` is given |
| `VICTIM_POLICY` | `ratio`: finished torrent with the lowest ratio, idle ones first; `oldest`: inactive for the longest time |
| `ADD_POLICY` | `all_finished`: add only when every torrent is complete; `no_download`: add when nothing is downloading |
| `START_TIMEOUT_MIN` | start watch delay in minutes; `None` disables it |
| `STALL_MINUTES_DEFAULT` | value of `--rm-stalled` without an argument |

## Options

| Option | Effect |
|---|---|
| `--dry-run` | simulate every deletion (wins over `--force-delete`) |
| `--force-delete` | real deletions even if `DRY_RUN_DEFAULT = True` |
| `--skip-orphans` | do not scan or clean orphans |
| `--rm-stalled [MIN]` | remove downloads stuck for MIN minutes |
| `--rmall` | remove every torrent and its data, then exit |

`--dry-run` only blocks deletions: a `.torrent` can still be downloaded into the watch folder.

## Installation

Python 3 standard library only. Copy the file to the server (prefer `scp` over copy-paste), then check it:

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
