# Deployment auf Benniclaw

Stand: 2026-09-16. Gilt für jede Person und jeden Agenten, der Novas laufenden Server
aktualisiert.

**Die Regel dahinter** steht in [`FORK.md`](../FORK.md): *Der laufende Host bekommt Commits,
keine kopierten Dateien.* Ein Server, auf den Dateien per `scp` gelegt werden, entspricht keiner
Revision mehr. Genau so war es bis zum 16.09.2026: 42 geänderte und kopierte Dateien auf einem
Upstream-Commit vom 03.09., dazu verirrte `.bak`-Kopien. Nachweisen, was dort eigentlich lief,
ging nur noch per Hash-Vergleich jeder einzelnen Datei. **Kein `scp` von Code auf Benniclaw.**

## Das Wichtigste auf einen Blick

| | |
|---|---|
| Server | Benniclaw, `benni@192.168.0.189`, Ubuntu |
| Installation | `/home/benni/YuriOS` (Git-Checkout, editierbar in `.venv` installiert) |
| Dienst | `yurios.service` (systemd, `User=benni`, `python -m yurios.world`, Port 8768) |
| Code-Quelle | Bare-Repo `/home/benni/yurios-fork.git` |
| Remote auf dem Gaming-PC | `benniclaw` → `benni@192.168.0.189:/home/benni/yurios-fork.git` |
| Remote im Server-Checkout | `fork` → `/home/benni/yurios-fork.git`; `main` folgt `fork/main` |
| `origin` (beide Seiten) | Upstream `yuri-os/YuriOS`, nur zum Holen, kein Push-Recht |
| Charaktere | `nova` (Telegram `@YourNovaBot`), `yuri` |

```
Gaming-PC  D:\AI Projekte\YuriOS
   │  git push benniclaw main          (SSH, im LAN)
   ▼
Benniclaw  /home/benni/yurios-fork.git   (bare, nur Ablage)
   │  git pull --ff-only               (Remote "fork")
   ▼
Benniclaw  /home/benni/YuriOS            → yurios.service
```

## Ein Update ausrollen

**Vorher auf dem Gaming-PC:** Die Änderung ist committet und das Gate ist grün
(`./scripts/check.sh`, siehe [`AGENTS.md`](../AGENTS.md)). Was nicht committet ist, kommt auf
dem Server nicht an, und das ist Absicht.

```bash
git push benniclaw main
```

**Auf Benniclaw:**

```bash
cd ~/YuriOS
TS=$(date +%Y%m%d-%H%M%S)
echo "vorher: $(git rev-parse --short HEAD)" | tee ~/yurios-deploy-$TS.txt   # Rollback-Ziel
git status --porcelain              # MUSS leer sein, sonst erst klären (siehe unten)
git pull --ff-only
```

Nur wenn nötig:

| Wenn sich geändert hat … | dann |
|---|---|
| `web/` | `(cd web && npm ci && npm run build)`, denn `web/dist` ist nicht im Repo |
| `pyproject.toml` / `constraints.txt` | `.venv/bin/pip install -e . -c constraints.txt` |
| eine neue Einstellung | in `.env` eintragen, vorher `cp .env .env.bak-$TS` |

```bash
sudo systemctl restart yurios.service
```

Nach ca. 15 s ist sie wieder da. **Danach prüfen:**

```bash
journalctl -u yurios.service -n 80 --no-pager | grep -E "boot:|channel up|ERROR|Traceback"
curl -s -H "Authorization: Bearer $(cat secrets/owner_token)" http://127.0.0.1:8768/api/health
git status --porcelain              # weiterhin leer
```

Erwartet werden: `tool server ready … 36 tools · 2 servers`, `channel up: telegram (@YourNovaBot)`,
`autonomy engine ready`, im Health-Check `"ok": true` und `"degraded": []`. Die Werkzeugzahl
ändert sich, wenn Hände dazukommen oder wegfallen. Dann ist die neue Zahl die erwartete.

## Rollback

```bash
cd ~/YuriOS
git checkout -f <vorher-sha>        # aus ~/yurios-deploy-<TS>.txt
sudo systemctl restart yurios.service
```

`main` bleibt dabei auf dem neuen Stand. Sobald der Fehler auf dem Gaming-PC behoben und neu
gepusht ist, kehrt `git checkout main && git pull --ff-only` zurück. Wurde `.env` geändert, die
`.env.bak-<TS>` zurückkopieren.

## Was nicht aus Git kommt

Git ignoriert diese Dateien, und ein Checkout lässt sie in Ruhe. Sie gehören dem Server:

| Pfad | Inhalt |
|---|---|
| `.env` | Hauskonfiguration: Modelle, Telegram, `STORY_*`, `MCP_SERVERS`, … |
| `secrets/` | `owner_token`, `elecrow_mcp_token` und weitere |
| `data/`, `vault/` | Novas und Yuris Zustand, Gedächtnis, Vault-Git |
| `traces/`, `tool-logs/`, `corpus/`, `selfies/` | Aufzeichnungen über sie |
| `.venv/`, `web/dist/`, `.yurios/` | Laufzeit und Build-Ausgabe |

⚠️ **`MCP_SERVERS` muss auf `tools/mcp-servers.benniclaw.json` zeigen**, nicht auf
`tools/mcp-servers.json`. Die zweite Datei ist die **Windows**-Konfiguration des Gaming-PCs.
Nova startet damit ohne Fehlermeldung, hat aber keinen Elecrow-Proxy, also 17 Werkzeuge weniger.

## Wenn `git status` auf dem Server nicht leer ist

Dann hat jemand direkt auf dem Server geändert oder kopiert. **Nicht mit `-f` darüberbügeln.**

1. `git status --porcelain` und `git diff` ansehen.
2. Gehört die Änderung in den Fork: auf den Gaming-PC übertragen, dort committen, testen, pushen.
3. Ist sie Müll (`.bak`, verirrte Kopie): nach `~/yurios-stray-<TS>/` verschieben, nicht löschen.
4. Erst mit leerem Status `git pull --ff-only`.

## Upstream übernehmen

Upstream wird **auf dem Gaming-PC** zusammengeführt, nie auf dem Server:

```bash
git fetch origin
git merge --no-ff origin/main        # Konflikte lösen, spec-map neu erzeugen, Gate grün
git push benniclaw main              # dann wie oben ausrollen
```

## Offen

- **Kein Offsite-Backup des Forks.** Die Commits liegen nur auf dem Gaming-PC und auf Benniclaw,
  im selben Haus. Ein privates GitHub-Repo als zusätzliches Remote wäre der nächste Schritt.
- Das Gate auf Windows ist nicht vollständig grün (Git im Temp-Ordner, Symlink-Rechte,
  PID-Prüfung, `web/dist`). Diese 71 Fälle sind umgebungsbedingt und bekannt. Maßgeblich ist,
  dass **keine neuen** dazukommen.

## Protokoll

| Datum | Von → nach | Anmerkung |
|---|---|---|
| 2026-09-16 00:22 | `9f56d55` + 42 kopierte Dateien → `b086bc8` | Umstellung auf Git-Deployment. Jede Datei per Hash gegen die Fork-Historie geprüft, nichts ging verloren. Sicherung `~/yurios-predeploy-20260916-002136.tgz`, verirrte Dateien in `~/yurios-stray-20260916-002136/`. `MCP_SERVERS` auf `mcp-servers.benniclaw.json` umgestellt. Web gebaut, Neustart: 36 Werkzeuge, Telegram oben, keine Fehler, `elecrow_status` ok. |
