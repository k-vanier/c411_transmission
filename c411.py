#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Automatisation Transmission + C411.
#
# Un seul script pour toutes les machines : les réglages de chacune sont dans un
# fichier JSON (c411.json à côté du script, ou --config FICHIER).
#
# - relève l'état de Transmission (session, stats, torrents) dans BASE_DIR
# - surveille le démarrage des torrents ajoutés (START_TIMEOUT_MIN)
# - supprime les torrents terminés sans activité depuis IDLE_REMOVE_HOURS heures
# - nettoie les fichiers orphelins du dossier de téléchargement
# - garde MIN_FREE_GB libres une fois tous les téléchargements finis : sinon supprime
#   des torrents terminés selon VICTIM_POLICY
# - rotation : si ADD_POLICY le permet, dépose de nouveaux torrents C411 dans WATCH_DIR,
#   en supprimant des torrents terminés (VICTIM_POLICY) quand il faut faire de la place
# - écrit un résumé d'une ligne (130 caractères max) dans STATUS_FILE
#
# Options :
#   --config FICHIER   configuration JSON de la machine (défaut : c411.json à côté du script)
#   --dry-run          simule les suppressions
#   --force-delete     suppressions réelles, même si DRY_RUN_DEFAULT = True
#   --skip-orphans     n'analyse pas / ne nettoie pas les orphelins disque
#   --rm-stalled [MIN] supprime les torrents bloqués en download depuis plus de MIN minutes
#   --rmall            supprime tous les torrents et leurs données locales

import argparse
import atexit
import base64
import json
import os
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION : lue dans un fichier JSON propre à chaque machine
# ═══════════════════════════════════════════════════════════════════════════════

# Par défaut c411.json à côté du script ; un autre fichier avec --config CHEMIN.
DEFAULT_CONFIG = Path(__file__).resolve().with_name('c411.json')

REQUIRED = object()

# clé JSON : (valeur par défaut, type attendu) ; la clé devient la constante en majuscules
CONFIG_KEYS = {
    'machine':               (REQUIRED, str),           # nom affiché dans les rapports et le résumé
    'host':                  ('localhost', str),        # Transmission RPC
    'port':                  (REQUIRED, int),
    'username':              ('transmission', str),
    'password':              (REQUIRED, str),
    'c411_apikey':           (REQUIRED, str),           # C411
    'c411_cat':              (REQUIRED, 'cat'),         # catégorie Torznab recherchée
    'max_torrent_gb':        (REQUIRED, 'number'),      # taille max d'un torrent candidat
    'max_seeders':           (REQUIRED, int),           # candidat retenu s'il a moins de N seeders
    'base_dir':              ('/root/scripts', 'path'), # fichiers JSON d'état
    'watch_dir':             (REQUIRED, 'path'),        # dossier surveillé par Transmission
    'download_dir':          (None, 'path'),            # null = dossiers lus dans les torrents
    'min_free_gb':           (30, 'number'),            # sous ce seuil libre, un torrent est supprimé
    'space_check_path':      (REQUIRED, 'path'),        # où l'espace libre est mesuré
    'dry_run_default':       (False, bool),             # true = suppressions simulées sauf --force-delete
    'victim_policy':         ('ratio', ('ratio', 'oldest')),
    'add_policy':            ('max_downloads', ('max_downloads', 'all_finished', 'no_download')),
    'max_downloads':         (3, int),                  # téléchargements simultanés (add_policy max_downloads)
    'start_timeout_min':     (15, int),                 # null = surveillance du démarrage désactivée
    'stall_minutes_default': (10, int),                 # valeur de --rm-stalled sans argument
    'idle_remove_hours':     (None, 'number'),          # supprime les terminés inactifs depuis N h, null = jamais
}
NULLABLE = {'download_dir', 'start_timeout_min', 'idle_remove_hours'}


def config_error(path, message):
    print(f"Erreur configuration {path}: {message}", file=sys.stderr)
    sys.exit(2)


def check_value(path, key, value, kind):
    if value is None:
        if key in NULLABLE:
            return None
        config_error(path, f"'{key}' ne peut pas être null")

    if isinstance(kind, tuple):
        if value not in kind:
            config_error(path, f"'{key}' doit valoir {' ou '.join(kind)}, pas {value!r}")
        return value
    if kind == 'path':
        if not isinstance(value, str) or not value:
            config_error(path, f"'{key}' doit être un chemin (texte)")
        return Path(value)
    if kind == 'cat':
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            config_error(path, f"'{key}' doit être un nombre ou un texte")
        return str(value)
    if kind == 'number':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            config_error(path, f"'{key}' doit être un nombre")
        return value
    # bool est un int en Python : on refuse true/false là où un nombre est attendu
    if isinstance(value, bool) != (kind is bool) or not isinstance(value, kind):
        config_error(path, f"'{key}' doit être de type {kind.__name__}")
    return value


def load_config(path):
    """Lit le JSON de la machine et crée les constantes (MACHINE, PORT, ...)."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        config_error(path, "fichier introuvable (copier l'exemple en c411.json ou passer --config)")
    except (OSError, ValueError) as e:
        config_error(path, e)
    if not isinstance(data, dict):
        config_error(path, "le fichier doit contenir un objet JSON { ... }")

    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        config_error(path, f"clé(s) inconnue(s) : {', '.join(unknown)}")

    cfg = {}
    for key, (default, kind) in CONFIG_KEYS.items():
        if key not in data:
            if default is REQUIRED:
                config_error(path, f"clé obligatoire absente : '{key}'")
            value = default
        else:
            value = data[key]
        cfg[key.upper()] = check_value(path, key, value, kind)

    g = globals()
    g.update(cfg)
    g['CONFIG_PATH'] = Path(path)
    g['URL_RPC'] = f"http://{HOST}:{PORT}/transmission/rpc"
    for name, filename in OUTPUT_FILES.items():
        g[name] = BASE_DIR / filename

# ═══════════════════════════════════════════════════════════════════════════════
# CODE COMMUN
# ═══════════════════════════════════════════════════════════════════════════════

URL_C411 = 'https://c411.org/api'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0'
TORZNAB_ATTR = '{http://torznab.com/schemas/2015/feed}attr'

# fichiers d'état écrits dans BASE_DIR (chemins créés par load_config)
OUTPUT_FILES = {
    'OUTPUT_TRANS': 'get_transmission.json',
    'OUTPUT_TRANS_FULL': 'get_transmission_full.json',
    'OUTPUT_TRANS_SUMMARY': 'get_transmission_summary.json',
    'OUTPUT_SESSION': 'get_transmission_session.json',
    'OUTPUT_STATS': 'get_transmission_stats.json',
    'OUTPUT_C411': 'get_c411.json',
    'OUTPUT_ORPHANS': 'get_orphans.json',
    'OUTPUT_STALL_STATE': 'get_stalled_state.json',
    'OUTPUT_PENDING': 'get_pending_torrents.json',
    'OUTPUT_REMOVED': 'get_removed_torrents.json',
}

STATUS_FILE = Path('/var/log/torrents_status')
STATUS_MAX_LEN = 130

BYTES_PER_GB = 1024 ** 3

STATUS_MAP = {
    0: 'Stoppé',
    1: 'Vérif en file',
    2: 'Vérification',
    3: 'DL en file',
    4: 'Téléchargement',
    5: 'Seed en file',
    6: 'Seed'
}

DOWNLOADING_STATUS = {3, 4}
INACTIVE_STATUS = {0, 5, 6}     # stoppé / seed en file / seed

# Transmission : uploadRatio vaut -1 (rien envoyé ni reçu) ou -2 (ratio infini)
RATIO_NA = -1
RATIO_INF = -2

TORRENT_FIELDS = [
    "id", "hashString", "name", "comment", "creator", "dateCreated",
    "addedDate", "doneDate", "activityDate", "startDate", "editDate",
    "status", "error", "errorString", "eta", "etaIdle",
    "leftUntilDone", "percentDone", "percentComplete", "metadataPercentComplete",
    "sizeWhenDone", "totalSize", "desiredAvailable", "haveUnchecked", "haveValid",
    "downloadedEver", "uploadedEver", "corruptEver",
    "rateDownload", "rateUpload", "uploadRatio",
    "seedRatioLimit", "seedRatioMode", "seedIdleLimit", "seedIdleMode",
    "downloadDir", "downloadLimit", "downloadLimited",
    "uploadLimit", "uploadLimited", "honorsSessionLimits",
    "bandwidthPriority", "queuePosition", "isFinished", "isPrivate", "isStalled",
    "labels", "group", "pieceCount", "pieceSize",
    "file-count", "files", "fileStats", "wanted", "priorities",
    "peers", "peersFrom", "peersConnected", "peersGettingFromUs", "peersSendingToUs",
    "webseeds", "webseedsSendingToUs",
    "trackers", "trackerStats", "trackerList",
    "magnetLink", "torrentFile", "primary-mime-type",
    "manualAnnounceTime", "maxConnectedPeers"
]


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def human_size(num):
    if num is None:
        return "N/A"
    value = float(num)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if value < 1024.0:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}EB"


def human_speed(num):
    if num is None:
        return "N/A"
    return f"{human_size(num)}/s"


def human_ratio(r):
    if r == RATIO_INF:
        return "inf"
    if r is None or r == RATIO_NA:
        return "-"
    return f"{r:.2f}"


def ts_to_str(ts):
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return "-"


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def read_json(path, default):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def is_done(t):
    return (t.get('percentDone') or 0) >= 1


REMOVED_KEEP = 1000     # noms gardés dans OUTPUT_REMOVED


def remember_removed(names):
    # un torrent supprimé ne doit pas être repris sur C411 : sinon la rotation
    # le retélécharge, le resupprime, etc.
    removed = read_json(OUTPUT_REMOVED, [])
    now = int(time.time())
    removed += [{'name': n, 'removed_at': now} for n in names if n]
    write_json(OUTPUT_REMOVED, removed[-REMOVED_KEEP:])


def ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    WATCH_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RÉSUMÉ UNE LIGNE (STATUS_FILE)
# ═══════════════════════════════════════════════════════════════════════════════

STATUS = {'torrents': None, 'stats': None, 'actions': [], 'rotated': 0, 'error': None, 'dry_run': False}


def note(action):
    STATUS['actions'].append(action)


def to_ascii(text):
    # 1 caractère = 1 octet : les accents des noms de torrents sont retirés
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')


def global_totals():
    # octets envoyés et reçus depuis toujours (statistiques cumulées de Transmission),
    # sinon ceux des torrents présents
    cum = (STATUS['stats'] or {}).get('cumulative-stats') or {}
    up, down = cum.get('uploadedBytes') or 0, cum.get('downloadedBytes') or 0
    if not down:
        torrents = STATUS['torrents'] or []
        up = sum(t.get('uploadedEver') or 0 for t in torrents)
        down = sum(t.get('downloadedEver') or 0 for t in torrents)
    return up, down


def short_size(num):
    # 1.5M, 98G : tailles compactes pour la ligne de résumé
    value = float(num or 0)
    for unit in ['', 'K', 'M', 'G', 'T']:
        if value < 1024 or unit == 'T':
            return f"{value:.1f}{unit}" if unit and value < 10 else f"{value:.0f}{unit}"
        value /= 1024


def gt_size(num):
    # compteurs en Go / To : 850G, 44.0T
    value = float(num or 0) / BYTES_PER_GB
    return f"{value / 1024:.1f}T" if value >= 1024 else f"{value:.0f}G"


def top_flop(torrents):
    """Torrents terminés le plus et le moins rentables (ratio), ou (None, None)."""
    finished = [t for t in torrents if is_done(t)]
    if not finished:
        return None, None
    key = lambda t: ratio_key(t)[0]
    return max(finished, key=key), min(finished, key=key)


def build_status_line():
    # DL 3/3 (87% 45% 20%) +1 att | libre 98G dispo 42G | env 44.0T rec 28.0T r1.57
    #   | top 6.95 Some.Movie.2022 | flop 0.07 Other.Movie.20 | RAS
    torrents = STATUS['torrents']

    if STATUS['error'] or torrents is None:
        try:
            free = f"libre {short_size(shutil.disk_usage(SPACE_CHECK_PATH).free)}"
        except OSError:
            free = "libre ?"
        line = f"{free} | ERREUR {STATUS['error'] or 'inconnue'}"
    else:
        # téléchargements : nombre / maximum, avancement du plus avancé au moins avancé, ajouts pas encore pris
        downloading = sorted((t for t in torrents if t.get('status') in DOWNLOADING_STATUS),
                             key=lambda t: t.get('percentDone') or 0, reverse=True)
        dl = f"DL {len(downloading)}"
        if ADD_POLICY == 'max_downloads':
            dl += f"/{MAX_DOWNLOADS}"
        percents = ' '.join(f"{(t.get('percentDone') or 0) * 100:.0f}%" for t in downloading[:4])
        waiting = len(waiting_pending(torrents))
        att = f" +{waiting} att" if waiting else ""

        # compteurs depuis toujours : envoyé, reçu, ratio
        up, down = global_totals()
        totals = f"env {gt_size(up)} rec {gt_size(down)} " + (f"r{up / down:.2f}" if down else "r-")

        # dispo = place pour un ajout sans passer sous MIN_FREE_GB ; manque = à libérer par la rotation
        try:
            free, _, budget = projected_space(torrents)
            room = f"dispo {short_size(budget)}" if budget >= 0 else f"manque {short_size(-budget)}"
            space = f"libre {short_size(free)} {room}"
        except OSError:
            space = "libre ?"

        errors = sum(1 for t in torrents if t.get('error'))
        err = f" {errors}err" if errors else ""

        rotated = [f"-{STATUS['rotated']} rotation"] if STATUS['rotated'] else []
        actions = ', '.join(rotated + STATUS['actions']) or 'RAS'
        if STATUS['dry_run']:
            actions = f"[sim] {actions}"

        # torrents terminés le plus et le moins rentables
        top, flop = top_flop(torrents)

        def ranked(width):
            if top is None:
                return ""
            if not width:
                return f" | top {human_ratio(top.get('uploadRatio'))} flop {human_ratio(flop.get('uploadRatio'))}"
            return (f" | top {human_ratio(top.get('uploadRatio'))} {(top.get('name') or '?')[:width].rstrip()}"
                    f" | flop {human_ratio(flop.get('uploadRatio'))} {(flop.get('name') or '?')[:width].rstrip()}")

        # trop long : on retire d'abord les pourcentages, puis on raccourcit les noms top/flop,
        # puis on les enlève, avant de couper les actions
        dl_full = f"{dl}{f' ({percents})' if percents else ''}{att}"
        variants = [(dl_full, 16), (f"{dl}{att}", 16), (f"{dl}{att}", 12), (f"{dl}{att}", 8), (f"{dl}{att}", 0)]
        lines = [f"{d} | {space} | {totals}{err}{ranked(w)} | {actions}" for d, w in variants]
        line = next((v for v in lines if len(to_ascii(v)) <= STATUS_MAX_LEN), lines[-1])

    line = to_ascii(line)
    if len(line) > STATUS_MAX_LEN:
        line = line[:STATUS_MAX_LEN - 3] + '...'
    return line


def write_status():
    line = build_status_line()
    try:
        STATUS_FILE.write_text(line + '\n', encoding='ascii')
    except OSError as e:
        print(f"Erreur écriture {STATUS_FILE}: {e}", file=sys.stderr)
        return
    print(f"\n>> Résumé ({len(line)} car.) -> {STATUS_FILE}\n{line}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT TRANSMISSION
# ═══════════════════════════════════════════════════════════════════════════════

class Transmission:
    def __init__(self):
        self.session_id = None

    def _request(self, payload, retry=True):
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(URL_RPC, data=data)
        req.add_header('Content-Type', 'application/json')

        if USERNAME and PASSWORD:
            creds = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
            req.add_header('Authorization', f'Basic {creds}')

        if self.session_id:
            req.add_header('X-Transmission-Session-Id', self.session_id)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.session_id = resp.getheader('X-Transmission-Session-Id') or self.session_id
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 409 and retry:
                new_id = e.headers.get('X-Transmission-Session-Id')
                if new_id:
                    self.session_id = new_id
                    return self._request(payload, retry=False)
            print(f"Erreur HTTP {e.code}: {e.reason}", file=sys.stderr)
            STATUS['error'] = f"RPC HTTP {e.code}"
            sys.exit(1)
        except Exception as e:
            print(f"Erreur RPC: {e}", file=sys.stderr)
            STATUS['error'] = f"RPC {e}"
            sys.exit(1)

    def call(self, method, arguments=None):
        payload = {"method": method}
        if arguments is not None:
            payload["arguments"] = arguments

        data = self._request(payload)
        if data.get("result") != "success":
            raise RuntimeError(f"Erreur RPC {method}: {data.get('result')}")
        return data.get("arguments", {})

    def remove(self, torrents, remember=True):
        self.call("torrent-remove", {"ids": [t.get('id') for t in torrents], "delete-local-data": True})
        if remember:
            remember_removed(t.get('name') for t in torrents)


def get_session_info(trans):
    data = trans.call("session-get")
    write_json(OUTPUT_SESSION, data)
    return data


def get_session_stats(trans):
    data = trans.call("session-stats")
    write_json(OUTPUT_STATS, data)
    STATUS['stats'] = data
    return data


def get_free_space(trans, path):
    try:
        return trans.call("free-space", {"path": str(path)})
    except Exception as e:
        print(f"Erreur free-space: {e}", file=sys.stderr)
        return None


def get_torrents(trans):
    torrents = trans.call(
        "torrent-get",
        {"fields": TORRENT_FIELDS, "format": "objects"}
    ).get("torrents", [])

    write_json(OUTPUT_TRANS, torrents)
    write_json(OUTPUT_TRANS_FULL, torrents)
    STATUS['torrents'] = torrents
    print_torrents_summary(build_summary(torrents))
    return torrents


# ═══════════════════════════════════════════════════════════════════════════════
# RAPPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def build_summary(torrents):
    summary_torrents = []

    for t in torrents:
        files = t.get("files", [])
        summary_torrents.append({
            "id": t.get("id"),
            "hash": t.get("hashString"),
            "name": t.get("name"),
            "status": t.get("status"),
            "status_text": STATUS_MAP.get(t.get("status"), "Inconnu"),
            "error": t.get("error"),
            "errorString": t.get("errorString"),
            "percentDone": t.get("percentDone"),
            "percentComplete": t.get("percentComplete"),
            "metadataPercentComplete": t.get("metadataPercentComplete"),
            "isFinished": t.get("isFinished"),
            "isStalled": t.get("isStalled"),
            "downloadDir": t.get("downloadDir"),
            "totalSize": t.get("totalSize"),
            "totalSize_human": human_size(t.get("totalSize", 0)),
            "leftUntilDone": t.get("leftUntilDone"),
            "leftUntilDone_human": human_size(t.get("leftUntilDone", 0)),
            "downloadedEver": t.get("downloadedEver"),
            "uploadedEver": t.get("uploadedEver"),
            "uploadRatio": t.get("uploadRatio"),
            "rateDownload": t.get("rateDownload"),
            "rateUpload": t.get("rateUpload"),
            "peersConnected": t.get("peersConnected"),
            "fileCount": len(files),
            "trackerCount": len(t.get("trackerStats", [])),
            "peerCount": len(t.get("peers", [])),
            "addedDate": ts_to_str(t.get("addedDate")),
            "doneDate": ts_to_str(t.get("doneDate")),
            "activityDate": ts_to_str(t.get("activityDate"))
        })

    total_size = sum(t.get("totalSize", 0) for t in torrents)
    result = {
        "torrentCount": len(torrents),
        "totalFilesInActiveTorrents": sum(s["fileCount"] for s in summary_torrents),
        "totalSizeBytes": total_size,
        "totalSizeHuman": human_size(total_size),
        "torrents": summary_torrents
    }

    write_json(OUTPUT_TRANS_SUMMARY, result)
    return result


def print_torrents_summary(summary):
    torrents = summary["torrents"]

    print(f"\nOK {len(torrents)} torrents écrits dans {OUTPUT_TRANS} (+ full, summary)")

    if not torrents:
        print("Aucun torrent trouvé.")
        return

    print(f"\nRésumé des torrents ({MACHINE}, port {PORT}) :\n")
    print(f"{'ID':<4} {'Nom':<38} {'Statut':<14} {'%':<7} {'Taille':<10} {'Fich':<5} {'Peers':<5} {'DL':<10} {'UL':<10} {'Ratio':<6} {'Activité'}")
    print("-" * 140)

    for t in torrents:
        print(
            f"{t['id']:<4} "
            f"{(t['name'] or 'N/A')[:38]:<38} "
            f"{t['status_text']:<14} "
            f"{(t['percentDone'] or 0) * 100:>5.1f}%  "
            f"{t['totalSize_human']:<10} "
            f"{t['fileCount']:<5} "
            f"{(t['peersConnected'] or 0):<5} "
            f"{human_speed(t['rateDownload']):<10} "
            f"{human_speed(t['rateUpload']):<10} "
            f"{human_ratio(t['uploadRatio']):<6} "
            f"{t['activityDate']}"
        )
        if t.get("error"):
            print(f"     ERREUR: {t.get('errorString') or 'inconnue'}")

    print(f"\n>> Nombre total de torrents : {summary['torrentCount']}")
    print(f">> Nombre total de fichiers dans les torrents actifs : {summary['totalFilesInActiveTorrents']}")
    print(f">> Taille totale de tous les torrents : {summary['totalSizeHuman']}")


def print_session_report(session, stats, free_space_info, dry_run, args):
    print("\nInformations session Transmission:\n")
    print(f"Machine                : {MACHINE} (port {PORT})")
    print(f"Configuration          : {CONFIG_PATH}")
    print(f"Mode suppression       : {'DRY-RUN' if dry_run else 'DELETE'}")
    print(f"Mode rmall             : {args.rmall}")
    print(f"DL bloqués (rm-stalled): {f'{args.rm_stalled} min' if args.rm_stalled is not None else 'désactivé'}")
    print(f"Surveillance démarrage : {f'{START_TIMEOUT_MIN} min' if START_TIMEOUT_MIN else 'désactivée'}")
    print(f"Inactifs supprimés     : {f'après {IDLE_REMOVE_HOURS} h' if IDLE_REMOVE_HOURS else 'jamais'}")
    print(f"Version                : {session.get('version', 'N/A')}")
    print(f"RPC version            : {session.get('rpc-version', 'N/A')} / {session.get('rpc-version-semver', 'N/A')}")
    print(f"Répertoire download    : {session.get('download-dir', 'N/A')}")
    print(f"Répertoire incomplete  : {session.get('incomplete-dir', 'N/A')}")
    print(f"Incomplete activé      : {session.get('incomplete-dir-enabled', False)}")
    print(f"Start added torrents   : {session.get('start-added-torrents', False)}")
    print(f"Rename partial files   : {session.get('rename-partial-files', False)}")
    print(f"Alt speed enabled      : {session.get('alt-speed-enabled', False)}")
    print(f"DHT enabled            : {session.get('dht-enabled', False)}")
    print(f"PEX enabled            : {session.get('pex-enabled', False)}")
    print(f"LPD enabled            : {session.get('lpd-enabled', False)}")
    print(f"uTP enabled            : {session.get('utp-enabled', False)}")
    print(f"Queue stalled enabled  : {session.get('queue-stalled-enabled', False)}")
    print(f"Seed queue enabled     : {session.get('seed-queue-enabled', False)}")
    print(f"Download queue enabled : {session.get('download-queue-enabled', False)}")

    print("\nStatistiques session:\n")
    print(f"Torrent count          : {stats.get('torrentCount', 0)}")
    print(f"Active torrent count   : {stats.get('activeTorrentCount', 0)}")
    print(f"Paused torrent count   : {stats.get('pausedTorrentCount', 0)}")
    print(f"Download speed         : {human_speed(stats.get('downloadSpeed', 0))}")
    print(f"Upload speed           : {human_speed(stats.get('uploadSpeed', 0))}")

    cur = stats.get('current-stats', {})
    cum = stats.get('cumulative-stats', {})

    print(f"Session DL total       : {human_size(cur.get('downloadedBytes', 0))}")
    print(f"Session UL total       : {human_size(cur.get('uploadedBytes', 0))}")
    print(f"Cumulated DL total     : {human_size(cum.get('downloadedBytes', 0))}")
    print(f"Cumulated UL total     : {human_size(cum.get('uploadedBytes', 0))}")

    if free_space_info:
        print("\nEspace disque (RPC free-space):\n")
        print(f"Path                   : {free_space_info.get('path', '-')}")
        print(f"Free                   : {human_size(free_space_info.get('size-bytes', 0))}")
        print(f"Total                  : {human_size(free_space_info.get('total_size', 0))}")


# ═══════════════════════════════════════════════════════════════════════════════
# SURVEILLANCE DÉMARRAGE DES TORRENTS AJOUTÉS (START_TIMEOUT_MIN)
# ═══════════════════════════════════════════════════════════════════════════════

def register_pending(c411_id, name, size, torrent_file):
    pending = [p for p in read_json(OUTPUT_PENDING, []) if p.get('c411_id') != c411_id]
    pending.append({
        'c411_id': c411_id,
        'name': name,
        'size': size,
        'torrent_file': str(torrent_file) if torrent_file else None,
        'added_at': int(time.time())
    })
    write_json(OUTPUT_PENDING, pending)


def _torrent_matches(t, name):
    tname = (t.get('name') or '').strip().lower()
    wanted = (name or '').strip().lower()
    if not tname or not wanted:
        return False
    return tname == wanted or wanted in tname or tname in wanted


def _torrent_started(t):
    # démarré = a reçu des données ; le statut Téléchargement seul ne suffit pas (0 octet possible)
    return (
        (t.get('haveValid') or 0) + (t.get('haveUnchecked') or 0) > 0
        or (t.get('percentDone') or 0) > 0
        or (t.get('downloadedEver') or 0) > 0
        or (t.get('rateDownload') or 0) > 0
    )


def check_pending(trans, torrents, dry_run):
    """Supprime tout torrent ajouté qui n'a pas démarré après START_TIMEOUT_MIN minutes.
    Retourne True si au moins une suppression réelle a eu lieu."""
    pending = read_json(OUTPUT_PENDING, [])
    if not pending:
        return False

    now = int(time.time())
    remaining = []
    deleted_any = False
    removed = 0

    print(f"\n>> Surveillance démarrage ({START_TIMEOUT_MIN} min) : {len(pending)} en attente")

    for p in pending:
        name = p.get('name', 'N/A')
        age_min = (now - p.get('added_at', now)) / 60
        match = next((t for t in torrents if _torrent_matches(t, name)), None)

        if match and _torrent_started(match):
            print(f"  Démarré : {name} (il y a {age_min:.0f} min)")
            continue

        if age_min < START_TIMEOUT_MIN:
            print(f"  En attente ({age_min:.0f}/{START_TIMEOUT_MIN} min) : {name}")
            remaining.append(p)
            continue

        if match:
            print(f"  NON DÉMARRÉ après {age_min:.0f} min : {name} (id={match.get('id')})")
            if dry_run:
                print("     [DRY-RUN] suppression simulée")
                remaining.append(p)
                continue
            try:
                trans.remove([match])
                print("     [DELETE] torrent supprimé avec ses données")
                deleted_any = True
                removed += 1
            except Exception as e:
                print(f"     [ERREUR] suppression: {e}", file=sys.stderr)
                remaining.append(p)
                continue
        else:
            print(f"  Jamais apparu dans Transmission après {age_min:.0f} min : {name}")
            if dry_run:
                print("     [DRY-RUN] nettoyage simulé")
                remaining.append(p)
                continue
            deleted_any = True

        # le nom C411 peut différer de celui de Transmission : on retient les deux
        remember_removed([name])

        torrent_file = p.get('torrent_file')
        if torrent_file and Path(torrent_file).exists():
            try:
                Path(torrent_file).unlink()
                print(f"     .torrent retiré : {torrent_file}")
            except Exception as e:
                print(f"     [ERREUR] .torrent non supprimé: {e}", file=sys.stderr)

    write_json(OUTPUT_PENDING, remaining)
    if removed:
        note(f"-{removed} non demarre")
    return deleted_any


# ═══════════════════════════════════════════════════════════════════════════════
# TORRENTS TERMINÉS INACTIFS (IDLE_REMOVE_HOURS)
# ═══════════════════════════════════════════════════════════════════════════════

def rm_idle(trans, torrents, hours, dry_run):
    """Supprime les torrents terminés sans activité (ni envoi ni réception) depuis
    `hours` heures, même s'il reste de la place. Un torrent qui envoie en ce
    moment est gardé. Retourne True si au moins une suppression réelle a eu lieu."""
    now = int(time.time())
    limit = hours * 3600
    idle = []

    for t in torrents:
        if t.get('status') not in INACTIVE_STATUS or not is_done(t):
            continue
        if t.get('rateUpload') or t.get('peersGettingFromUs'):
            continue
        # activityDate = dernier échange de données ; à défaut fin du DL, puis ajout
        last = t.get('activityDate') or t.get('doneDate') or t.get('addedDate') or now
        if now - last >= limit:
            idle.append((t, (now - last) // 3600))

    print(f"\n>> Torrents terminés inactifs depuis plus de {hours} h : {len(idle)}")

    removed = 0
    for t, idle_h in idle:
        tid = t.get('id')
        label = (f"ID {tid} {(t.get('name') or 'N/A')[:60]} "
                 f"(ratio {human_ratio(t.get('uploadRatio'))}, inactif depuis {idle_h} h)")

        if dry_run:
            print(f"[DRY-RUN] Suppression de {label}")
            continue

        try:
            trans.remove([t])
            print(f"[DELETE] {label}")
            removed += 1
        except Exception as e:
            print(f"[ERREUR] suppression ID {tid}: {e}", file=sys.stderr)

    count = len(idle) if dry_run else removed
    if count:
        note(f"-{count} inactif")
    return removed > 0


# ═══════════════════════════════════════════════════════════════════════════════
# TORRENTS BLOQUÉS EN DOWNLOAD (--rm-stalled)
# ═══════════════════════════════════════════════════════════════════════════════

def rm_stalled(trans, torrents, minutes, dry_run):
    """Supprime tout torrent en download (status 4) dont la quantité reçue n'a pas
    progressé depuis `minutes` minutes. L'état est gardé entre deux exécutions.
    Retourne True si au moins une suppression réelle a eu lieu."""
    now = int(time.time())
    limit = minutes * 60
    old_state = read_json(OUTPUT_STALL_STATE, {})
    new_state = {}
    stalled = []

    for t in torrents:
        h = t.get('hashString')
        if not h:
            continue
        # seuls les torrents en cours de download et non terminés sont suivis
        if t.get('status') != 4 or is_done(t):
            continue

        have = (t.get('haveValid') or 0) + (t.get('haveUnchecked') or 0)
        prev = old_state.get(h)

        if prev is None or have != prev.get('have'):
            since = now
        else:
            since = prev.get('since', now)

        new_state[h] = {'have': have, 'since': since, 'name': t.get('name')}

        if now - since >= limit:
            stalled.append(t)

    print(f"\n>> Torrents bloqués en download depuis plus de {minutes} min : {len(stalled)} ({len(new_state)} suivis)")

    removed = 0
    for t in stalled:
        tid = t.get('id')
        h = t.get('hashString')
        name = (t.get('name') or 'N/A')[:60]
        blocked_min = (now - new_state[h]['since']) // 60
        pct = (t.get('percentDone') or 0) * 100
        label = f"ID {tid} {name} ({pct:.1f}%, bloqué depuis {blocked_min} min)"

        if dry_run:
            print(f"[DRY-RUN] Suppression de {label}")
            continue

        try:
            trans.remove([t])
            print(f"[DELETE] {label}")
            new_state.pop(h, None)
            removed += 1
        except Exception as e:
            print(f"[ERREUR] suppression ID {tid}: {e}", file=sys.stderr)

    write_json(OUTPUT_STALL_STATE, new_state)
    if removed:
        note(f"-{removed} bloque")
    return removed > 0


# ═══════════════════════════════════════════════════════════════════════════════
# ORPHELINS DISQUE
# ═══════════════════════════════════════════════════════════════════════════════

def download_dirs(torrents):
    if DOWNLOAD_DIR is not None:
        return [DOWNLOAD_DIR]
    return sorted({Path(t['downloadDir']) for t in torrents if t.get('downloadDir')})


def active_top_names(torrents):
    names = set()
    for t in torrents:
        for f in t.get("files", []):
            name = f.get("name", "")
            if name:
                names.add(name.split("/", 1)[0])
        if t.get("name"):
            names.add(t["name"])

    # rename-partial-files : un torrent mono-fichier en cours s'appelle "nom.part"
    return names | {n + ".part" for n in names}


def list_disk_orphans(torrents):
    active = active_top_names(torrents)
    orphans = []

    for ddir in download_dirs(torrents):
        if not ddir.exists():
            print(f"Répertoire absent: {ddir}")
            continue

        for entry in ddir.iterdir():
            if entry.name in active:
                continue
            try:
                is_dir = entry.is_dir()
                st = entry.stat()
                size_bytes = 0 if is_dir else st.st_size
                orphans.append({
                    "name": entry.name,
                    "path": str(entry),
                    "downloadDir": str(ddir),
                    "is_dir": is_dir,
                    "size": size_bytes,
                    "size_human": "dir" if is_dir else human_size(size_bytes),
                    "mtime": ts_to_str(int(st.st_mtime))
                })
            except Exception as e:
                orphans.append({
                    "name": entry.name,
                    "path": str(entry),
                    "downloadDir": str(ddir),
                    "is_dir": None,
                    "size": None,
                    "size_human": "N/A",
                    "mtime": f"Erreur: {e}"
                })

    write_json(OUTPUT_ORPHANS, {"orphans": orphans})
    print(f"\n>> Eléments sur disque absents des torrents actifs : {len(orphans)}")
    print(f">> Détail JSON : {OUTPUT_ORPHANS}")
    return orphans


def delete_orphans(orphans, dry_run=True):
    deleted_count = 0
    deleted_bytes = 0
    errors = 0

    if not orphans:
        print(">> Aucun orphelin à supprimer")
        return deleted_count, deleted_bytes, errors

    print(f"\n>> Nettoyage orphelins [{'DRY-RUN' if dry_run else 'DELETE'}]")

    for item in orphans:
        path = Path(item["path"])
        try:
            if not path.exists():
                continue

            if dry_run:
                print(f"[DRY-RUN] Suppression de {path}")
                deleted_count += 1
                if path.is_file():
                    deleted_bytes += path.stat().st_size
                continue

            if path.is_dir():
                shutil.rmtree(path)
                print(f"[DELETE] Dossier supprimé: {path}")
            else:
                size_bytes = path.stat().st_size
                path.unlink()
                deleted_bytes += size_bytes
                print(f"[DELETE] Fichier supprimé: {path}")

            deleted_count += 1

        except Exception as e:
            errors += 1
            print(f"[ERREUR] {path}: {e}", file=sys.stderr)

    if deleted_count:
        note(f"-{deleted_count} orphelin")
    print(f"\n>> Orphelins traités : {deleted_count}")
    print(f">> Taille fichiers supprimés/simulés : {human_size(deleted_bytes)}")
    print(f">> Erreurs : {errors}")

    return deleted_count, deleted_bytes, errors


# ═══════════════════════════════════════════════════════════════════════════════
# ESPACE ET SUPPRESSION D'UN TORRENT
# ═══════════════════════════════════════════════════════════════════════════════

def check_space(torrents):
    total_gb = sum(t.get('totalSize') or 0 for t in torrents) / BYTES_PER_GB
    print(f"\n\033[1;92m>> Taille totale des torrents : {total_gb:.2f} GB\033[0m")

    try:
        free_gb = shutil.disk_usage(SPACE_CHECK_PATH).free / BYTES_PER_GB
    except OSError as e:
        print(f"Erreur espace disque sur {SPACE_CHECK_PATH}: {e}", file=sys.stderr)
        return False

    free_ok = free_gb >= MIN_FREE_GB
    print(f"\033[1;92m>> Libre sur {SPACE_CHECK_PATH} : {free_gb:.1f} GB (minimum {MIN_FREE_GB}) {'OK' if free_ok else 'KO'}\033[0m")
    return free_ok


def ratio_key(t):
    r = t.get('uploadRatio')
    if r is None or r == RATIO_NA:
        r = 0.0             # rien envoyé : aucun intérêt à le garder
    elif r == RATIO_INF:
        r = float('inf')    # ratio infini : à garder en dernier
    return (r, t.get('activityDate') or 0)


def victim_order(torrents):
    """Torrents supprimables pour faire de la place, dans l'ordre de VICTIM_POLICY."""
    if VICTIM_POLICY == 'ratio':
        # torrents terminés, en seed ou arrêtés ; d'abord ceux que personne ne télécharge
        finished = [t for t in torrents if t.get('status') in INACTIVE_STATUS and is_done(t)]
        idle = [t for t in finished if not t.get('rateUpload') and not t.get('peersGettingFromUs')]
        busy = [t for t in finished if t not in idle]
        return sorted(idle, key=ratio_key) + sorted(busy, key=ratio_key)
    return sorted((t for t in torrents if t.get('status') in INACTIVE_STATUS), key=oldest_key)


def torrent_bytes(t):
    # place rendue au budget : données présentes + reste à télécharger (déjà retenu par space_budget)
    return t.get('sizeWhenDone') or t.get('totalSize') or 0


def oldest_key(t):
    return (
        t.get('activityDate') or 0,     # le plus anciennement inactif d'abord
        t.get('peersConnected') or 0,   # le moins seedé
        t.get('rateUpload') or 0,       # le moins actif en upload
        t.get('rateDownload') or 0,     # le moins actif en download
        t.get('addedDate') or 0         # le plus ancien en dernier critère
    )


def remove_victim(trans, victim, dry_run):
    """Supprime un torrent choisi par victim_order().
    Retourne None si la suppression a échoué, False en dry-run, True si supprimé."""
    tid = victim.get('id')
    ratio = ratio_key(victim)[0]

    print(f"""
+--------------------------------------------------+
|       SUPPRESSION AUTOMATIQUE ({VICTIM_POLICY:<6})            |
+--------------------------------------------------+
 ID       : {tid}
 NOM      : {(victim.get('name') or 'N/A')[:60]}
 STATUT   : {STATUS_MAP.get(victim.get('status'), '?')}
 TAILLE   : {(victim.get('totalSize') or 0) / BYTES_PER_GB:.1f} GB
 RATIO    : {'inf' if ratio == float('inf') else f'{ratio:.2f}'}
 AJOUTÉ   : {ts_to_str(victim.get('addedDate'))}
 ACTIVITÉ : {ts_to_str(victim.get('activityDate'))}
 PEERS    : {victim.get('peersConnected') or 0}
 MODE     : {'DRY-RUN' if dry_run else 'DELETE'}
+--------------------------------------------------+
""")

    if dry_run:
        print(">> DRY-RUN: aucun torrent supprimé")
        STATUS['rotated'] += 1
        return False

    try:
        trans.remove([victim])
    except Exception as e:
        print(f"ERREUR suppression: {e}", file=sys.stderr)
        note("echec suppression")
        return None

    # le résumé compte les suppressions de rotation (-N rotation), le détail reste dans le log
    STATUS['rotated'] += 1

    print(f">> REUSSI : torrent {tid} + fichiers supprimés, {(victim.get('totalSize') or 0) / BYTES_PER_GB:.1f} GB libérés")
    return True


def make_room(trans, room, needed, dry_run):
    """Supprime des torrents, dans l'ordre de VICTIM_POLICY, jusqu'à ce que
    room['budget'] >= needed. Ne supprime rien si même tous les supprimables ne
    suffiraient pas. Retourne True si la place est faite (ou simulée en dry-run)."""
    if room['budget'] >= needed:
        return True

    plan, budget = [], room['budget']
    for t in room['victims']:
        if budget >= needed:
            break
        plan.append(t)
        budget += torrent_bytes(t)
    if budget < needed:
        return False

    print(f">> Rotation : {len(plan)} torrent(s) à supprimer pour libérer {(needed - room['budget']) / BYTES_PER_GB:.1f} GB")
    for t in plan:
        if remove_victim(trans, t, dry_run) is None:
            return False
        room['victims'].remove(t)
        room['budget'] += torrent_bytes(t)
        room['removed'] += 1
        if dry_run:
            room['simulated'] = True
    return True


def rm_all_torrents(trans, torrents, dry_run):
    if not torrents:
        print("Aucun torrent trouvé.")
        return False

    ids = [t.get("id") for t in torrents if t.get("id") is not None]
    total_size = sum(t.get("totalSize", 0) for t in torrents)

    print(f"""
+--------------------------------------------------+
|           SUPPRESSION DE TOUS LES TORRENTS       |
+--------------------------------------------------+
 NOMBRE : {len(ids)}
 TAILLE : {human_size(total_size)}
 MODE   : {'DRY-RUN' if dry_run else 'DELETE'}
+--------------------------------------------------+
""")

    for t in torrents:
        print(f" - ID {t.get('id'):<4} {(t.get('name') or 'N/A')[:60]:<60} {human_size(t.get('totalSize', 0))}")

    note(f"rmall {len(ids)} torrents")

    if dry_run:
        print("\n>> DRY-RUN: aucun torrent supprimé")
        return True

    try:
        trans.remove([t for t in torrents if t.get('id') is not None], remember=False)
    except Exception as e:
        print(f"ERREUR --rmall: {e}", file=sys.stderr)
        return False

    print(f"\n>> REUSSI : {len(ids)} torrents + données locales supprimés, {human_size(total_size)} libérés")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# AJOUT D'UN TORRENT C411
# ═══════════════════════════════════════════════════════════════════════════════

def waiting_pending(torrents):
    # .torrent déposés dans WATCH_DIR mais pas encore vus dans Transmission
    return [
        p for p in read_json(OUTPUT_PENDING, [])
        if not any(_torrent_matches(t, p.get('name')) for t in torrents)
    ]


def free_slots(torrents):
    """Nombre de torrents qu'on peut ajouter maintenant selon ADD_POLICY."""
    if ADD_POLICY == 'all_finished':
        # une liste vide compte comme terminée : on peut injecter un nouveau torrent
        if not all(is_done(t) for t in torrents) or waiting_pending(torrents):
            print("\033[1;33m>> Tous les torrents ne sont pas terminés : pas d'ajout\033[0m")
            note("attente fin DL")
            return 0
        return 1

    active = sum(1 for t in torrents if t.get('status') in DOWNLOADING_STATUS) + len(waiting_pending(torrents))

    if ADD_POLICY == 'max_downloads':
        slots = max(0, MAX_DOWNLOADS - active)
        print(f"\033[1;33m>> Téléchargements en cours : {active}/{MAX_DOWNLOADS}, places libres : {slots}\033[0m")
        if not slots:
            note("DL complet")
        return slots

    if active:
        print("\033[1;33m>> Téléchargement en cours : pas d'ajout\033[0m")
        note("attente fin DL")
        return 0
    return 1


def projected_space(torrents):
    """(libre, reste à télécharger, budget) en octets ; budget = libre - reste - MIN_FREE_GB.
    Lève OSError si SPACE_CHECK_PATH est illisible."""
    free = shutil.disk_usage(SPACE_CHECK_PATH).free
    left = sum(t.get('leftUntilDone') or 0 for t in torrents if not is_done(t))
    left += sum(p.get('size') or 0 for p in waiting_pending(torrents))
    return free, left, free - left - MIN_FREE_GB * BYTES_PER_GB


def space_budget(torrents):
    """Octets qu'on peut encore engager sans passer sous MIN_FREE_GB une fois
    tous les téléchargements terminés : libre - reste à télécharger - minimum."""
    try:
        free, left, budget = projected_space(torrents)
    except OSError as e:
        print(f"Erreur espace disque sur {SPACE_CHECK_PATH}: {e}", file=sys.stderr)
        return 0

    print(f">> Libre {free / BYTES_PER_GB:.1f} GB - reste à télécharger {left / BYTES_PER_GB:.1f} GB"
          f" - minimum {MIN_FREE_GB} GB = disponible pour ajout {budget / BYTES_PER_GB:.1f} GB")
    return budget


def c411_url(params):
    return f"{URL_C411}?{urllib.parse.urlencode({'apikey': C411_APIKEY, **params})}"


def fetch_c411():
    try:
        req = urllib.request.Request(c411_url({'t': 'search', 'cat': C411_CAT}))
        req.add_header('User-Agent', USER_AGENT)
        with urllib.request.urlopen(req, timeout=30) as resp:
            root = ET.fromstring(resp.read().decode('utf-8'))
    except ET.ParseError as e:
        print(f"ERREUR XML C411: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Erreur C411: {e}", file=sys.stderr)
        return None

    channel = root.find('channel')
    candidates = []

    for item in (channel.findall('item') if channel is not None else []):
        c = {
            'id': item.findtext('guid', default=''),
            'name': item.findtext('title', default='N/A'),
            'size': int(item.findtext('size', default='0') or 0),
            'seeders': 0,
            'peers': 0
        }
        for attr in item.findall(f".//{TORZNAB_ATTR}"):
            if attr.get('name') in ('seeders', 'peers'):
                c[attr.get('name')] = int(attr.get('value', 0))
        candidates.append(c)

    write_json(OUTPUT_C411, {'torrents': candidates})
    print(f"OK: {OUTPUT_C411} créé ({len(candidates)} torrents C411 cat={C411_CAT})")
    return candidates


def pick_candidates(candidates, torrents):
    known_names = {t.get('name') for t in torrents if t.get('name')}
    pending_ids = {p.get('c411_id') for p in read_json(OUTPUT_PENDING, [])}
    removed_names = {r.get('name') for r in read_json(OUTPUT_REMOVED, [])}

    for c in candidates:
        cid = c.get('id')
        if not cid:
            continue
        # déjà dans Transmission, déjà supprimé, en attente de démarrage ou déjà dans le dossier watch
        if (c['name'] in known_names or c['name'] in removed_names or cid in pending_ids
                or (WATCH_DIR / f'c411_{cid}.torrent').exists()):
            continue
        if c['size'] < MAX_TORRENT_GB * BYTES_PER_GB and c['seeders'] < MAX_SEEDERS:
            yield c


def download_torrent(c411_id):
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/x-bittorrent,*/*;q=0.5',
        'Accept-Language': 'fr,en-US;q=0.7,en;q=0.3',
        'Referer': 'https://c411.org/',
        'Cache-Control': 'max-age=0'
    }
    torrent_file = WATCH_DIR / f'c411_{c411_id}.torrent'

    try:
        print('Téléchargement du torrent C411...')
        req = urllib.request.Request(c411_url({'t': 'get', 'id': c411_id}), headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        torrent_file.write_bytes(data)
        print(f'OK: {torrent_file} ({len(data)} bytes)')
        return torrent_file
    except Exception as e:
        print(f'Erreur téléchargement: {e}', file=sys.stderr)
        return None


def add_torrents(trans, torrents, slots, room, dry_run):
    """Ajoute jusqu'à `slots` torrents. Ceux qui tiennent dans room['budget'] passent
    d'abord ; pour les autres, make_room() supprime des torrents terminés (rotation)."""
    candidates = fetch_c411()
    if candidates is None:
        note("C411 injoignable")
        return 1

    eligible = list(pick_candidates(candidates, torrents))
    if not eligible:
        print(f"Aucun nouveau torrent avec taille < {MAX_TORRENT_GB}Go et seeders < {MAX_SEEDERS}")
        note("aucun candidat C411")
        return 1

    # sans suppression d'abord, puis en faisant de la place
    fitting = [c for c in eligible if c['size'] <= room['budget']]
    ordered = fitting + [c for c in eligible if c not in fitting]

    added = 0
    for c in ordered:
        if added >= slots:
            break
        if c['size'] > room['budget'] and not make_room(trans, room, c['size'], dry_run):
            continue
        if room['simulated']:
            # place libérée seulement en simulation : ajouter dépasserait MIN_FREE_GB
            print(f"[DRY-RUN] ajout de {c['name'][:60]} non fait : place libérée seulement en simulation")
            note(f"+{c.get('name', '')[:22]} (apres rotation)")
            break

        print(f"Trouvé: {c['name'][:60]} ({c['size'] / BYTES_PER_GB:.1f}G, {c['seeders']} seeders)")
        torrent_file = download_torrent(c['id'])
        if torrent_file is None:
            note("echec telechargement .torrent")
            return 1

        note(f"+{c.get('name', '')[:22]}")
        added += 1
        room['budget'] -= c['size']

        if START_TIMEOUT_MIN:
            register_pending(c['id'], c.get('name', ''), c['size'], torrent_file)
            print(f">> Torrent surveillé : supprimé s'il ne démarre pas en {START_TIMEOUT_MIN} min")

    if not added and not room['simulated']:
        print(f"Aucun candidat ne tient, même en supprimant tous les torrents terminés (minimum {MIN_FREE_GB} Go)")
        note("rien a supprimer pour faire place")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    # --config d'abord : les valeurs par défaut des autres options viennent du JSON
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        metavar="FICHIER",
        help=f"configuration JSON de la machine (défaut {DEFAULT_CONFIG})"
    )
    known, _ = pre.parse_known_args()

    asking_help = bool({'-h', '--help'} & set(sys.argv[1:]))
    if asking_help and not known.config.exists():
        # l'aide s'affiche même sans fichier de configuration
        globals().update(MACHINE='?', STALL_MINUTES_DEFAULT=CONFIG_KEYS['stall_minutes_default'][0])
    else:
        load_config(known.config)

    parser = argparse.ArgumentParser(description=f"Transmission + C411 manager ({MACHINE})", parents=[pre])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="simule les suppressions sans rien effacer"
    )
    parser.add_argument(
        "--force-delete",
        action="store_true",
        help="autorise les suppressions réelles (ignoré avec --dry-run)"
    )
    parser.add_argument(
        "--skip-orphans",
        action="store_true",
        help="n'analyse pas / ne nettoie pas les orphelins disque"
    )
    parser.add_argument(
        "--rmall",
        action="store_true",
        help="supprime tous les torrents de la liste ainsi que les données locales"
    )
    parser.add_argument(
        "--rm-stalled",
        nargs="?",
        type=int,
        const=STALL_MINUTES_DEFAULT,
        default=None,
        metavar="MIN",
        help=f"supprime les torrents bloqués en download depuis plus de MIN minutes (défaut {STALL_MINUTES_DEFAULT})"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.dry_run:
        dry_run = True
    elif args.force_delete:
        dry_run = False
    else:
        dry_run = DRY_RUN_DEFAULT

    # écrit le résumé à la sortie, quel que soit le sys.exit() atteint
    STATUS['dry_run'] = dry_run
    atexit.register(write_status)

    if sys.stdout.isatty():
        os.system('clear')
    ensure_dirs()

    trans = Transmission()
    session = get_session_info(trans)
    stats = get_session_stats(trans)
    free_space_info = get_free_space(trans, session.get('download-dir') or DOWNLOAD_DIR or SPACE_CHECK_PATH)
    print_session_report(session, stats, free_space_info, dry_run, args)

    torrents = get_torrents(trans)

    if args.rmall:
        sys.exit(0 if rm_all_torrents(trans, torrents, dry_run) else 1)

    if START_TIMEOUT_MIN and check_pending(trans, torrents, dry_run):
        torrents = get_torrents(trans)

    if args.rm_stalled is not None and rm_stalled(trans, torrents, args.rm_stalled, dry_run):
        torrents = get_torrents(trans)

    if IDLE_REMOVE_HOURS and rm_idle(trans, torrents, IDLE_REMOVE_HOURS, dry_run):
        torrents = get_torrents(trans)

    if not args.skip_orphans:
        delete_orphans(list_disk_orphans(torrents), dry_run=dry_run)

    # espace : MIN_FREE_GB doit rester libre une fois tous les téléchargements terminés ;
    # sinon on supprime des torrents terminés (VICTIM_POLICY), même pendant les DL
    check_space(torrents)
    room = {'budget': space_budget(torrents), 'victims': victim_order(torrents),
            'removed': 0, 'simulated': False}

    if room['budget'] < 0:
        print(f">> Les téléchargements en cours passeraient sous {MIN_FREE_GB} Go libres : suppression")
        if not make_room(trans, room, 0, dry_run):
            print("\033[1;33m>> Pas assez de torrents terminés à supprimer : pas d'ajout\033[0m")
            note("disque plein, rien a supprimer")
            sys.exit(0)
    else:
        print("\033[1;33m>> Ok il reste de la place\033[0m")

    slots = free_slots(torrents)
    if not slots:
        sys.exit(0)

    print(f"\033[1;33m>> Conditions OK : jusqu'à {slots} ajout(s)\033[0m")
    sys.exit(add_torrents(trans, torrents, slots, room, dry_run))


if __name__ == "__main__":
    main()
