# gemini-media-mcp

[![CI](https://github.com/wimplunch/gemini-media-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/wimplunch/gemini-media-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**English:** [README.md](README.md)

Ein [MCP](https://modelcontextprotocol.io)-Server, mit dem Claude (oder jeder andere
MCP-Client) über die Gemini-API Bilder mit Googles **Nano-Banana**-Modellen erzeugt und
bearbeitet und Videos mit **Gemini Omni Flash** erstellt.

Der Server ist an den richtigen Stellen langweilig: Jeder kostenpflichtige Aufruf wird vorher
bepreist und gegen dein Budget geprüft. Dateien liest er nur aus freigegebenen Ordnern. Er
überschreibt nie etwas, und zu jeder Ausgabe legt er eine JSON-Datei ab, die festhält, wie sie
entstanden ist.

> **Hinweis zu Video (31.10.2026):** Google hat die Veo-3.1-Preview-Modelle in der Gemini-API
> am 22.10.2026 abgeschaltet und empfiehlt Gemini Omni Flash als Ersatz. Die Video-Tools
> nutzen standardmäßig `gemini-omni-1.1-flash`. Details: [docs/research.md](docs/research.md).

## Tools

| Tool | Was es tut | Kostet Geld |
|---|---|---|
| `generate_image` | Text → Bild. Optionen: `model`, `aspect_ratio`, `resolution`, `output_name` | ja |
| `edit_image` | Bild(er) + Prompt → Bild. Bis zu 14 Referenzbilder (Gesicht, Logo, Produkt …) als Dateipfade | ja |
| `start_video` | Startet einen Video-Job (aus Text oder mit einem Bild als Startframe) und gibt sofort eine `job_id` zurück. Ohne `confirm_cost=true` liefert es nur den Preis | ja |
| `get_video_status` | Fragt den Job ab und lädt das fertige MP4 herunter | nein (Kosten werden beim Start verbucht) |
| `list_models` | Konfigurierte Modelle, Preise und Optionen | nein |
| `cost_report` | Geschätzte Ausgaben heute, im Monat und gesamt, dazu das Restbudget | nein |

## Voraussetzungen

- Python 3.11+ und [uv](https://docs.astral.sh/uv/)
- Ein Gemini-API-Key aus [Google AI Studio](https://aistudio.google.com/apikey) **mit
  aktivierter Abrechnung**. Für keines der Bild- und Videomodelle gibt es eine kostenlose Stufe.

## Installation

Das Paket ist noch nicht auf PyPI. Du startest es direkt von GitHub mit `uvx`:

```bash
uvx --from git+https://github.com/wimplunch/gemini-media-mcp gemini-media-mcp
```

oder aus einem lokalen Klon:

```bash
git clone https://github.com/wimplunch/gemini-media-mcp
uvx --from ./gemini-media-mcp gemini-media-mcp
```

Nach der Veröffentlichung auf PyPI reicht `uvx gemini-media-mcp`.

### API-Key

Der Server liest den Key **nur** aus der Umgebungsvariable `GEMINI_API_KEY`. Er gehört weder
in Konfigurationsdateien noch in ein Chatfenster. Unter Linux machst du ihn so für
Desktop-Apps verfügbar (die Eingabe bleibt verdeckt, die Datei kann nur dein Benutzer lesen):

```bash
mkdir -p ~/.config/environment.d && (umask 077; read -rsp "Gemini-Key: " K && printf 'GEMINI_API_KEY=%s\n' "$K" > ~/.config/environment.d/gemini.conf; unset K)
```

Danach einmal ab- und wieder anmelden. Unter macOS und Windows setzt du die Variable im
Shell-Profil oder in den Systemeinstellungen.

### Claude Code

```bash
claude mcp add --scope user gemini-media \
  -e GEMINI_MEDIA_OUTPUT_DIR="$HOME/Bilder/gemini" \
  -- uvx --from git+https://github.com/wimplunch/gemini-media-mcp gemini-media-mcp
```

Claude Code gibt seine Umgebung an den Server weiter. `GEMINI_API_KEY` wird also geerbt und
landet nie in der Konfiguration. Prüfen kannst du das mit `claude mcp list`.

### Claude Desktop

Trag Folgendes in `claude_desktop_config.json` ein
(Linux `~/.config/Claude/`, macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "gemini-media": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/wimplunch/gemini-media-mcp", "gemini-media-mcp"],
      "env": {
        "GEMINI_MEDIA_OUTPUT_DIR": "/home/du/Bilder/gemini"
      }
    }
  }
}
```

Danach Claude Desktop neu starten. Der Server erbt `GEMINI_API_KEY` aus der Umgebung der App.
Trag den Key nicht in diese Datei ein, vor allem dann nicht, wenn du sie synchronisierst oder
weitergibst.

## Konfiguration

Alle Einstellungen sind Umgebungsvariablen.

| Variable | Standard | Bedeutung |
|---|---|---|
| `GEMINI_API_KEY` | – | Dein Gemini-API-Key (für die Generierung nötig) |
| `GEMINI_MEDIA_OUTPUT_DIR` | `~/gemini-media` | Zielordner für Bilder, Videos und Sidecars |
| `GEMINI_MEDIA_ALLOWED_DIRS` | Ausgabeordner | Ordner, aus denen Eingabebilder gelesen werden dürfen, getrennt mit `:` (unter Windows `;`). Muss den Ausgabeordner enthalten |
| `GEMINI_MEDIA_MAX_COST_PER_CALL` | `0.50` | Höchstens so viele geschätzte USD pro Aufruf |
| `GEMINI_MEDIA_DAILY_BUDGET` | `2.00` | Höchstens so viele geschätzte USD pro Tag (Ortszeit) |
| `GEMINI_MEDIA_MONTHLY_BUDGET` | `20.00` | Höchstens so viele geschätzte USD pro Kalendermonat |
| `GEMINI_MEDIA_MAX_INPUT_MB` | `7` | Maximale Größe pro Eingabebild |
| `GEMINI_MEDIA_DEFAULT_IMAGE_MODEL` | `gemini-nano-banana-2.1` | Standardmodell für Bild-Tools |
| `GEMINI_MEDIA_DEFAULT_VIDEO_MODEL` | `gemini-omni-1.1-flash` | Standardmodell für Video-Tools |
| `GEMINI_MEDIA_CONFIG` | – | Pfad zu einer TOML-Datei, die Modelle und Preise ergänzt oder ersetzt |
| `GEMINI_MEDIA_STATE_DIR` | `<Ausgabe>/.gemini-media` | Ablage für Kosten-Log und Video-Jobliste |
| `GEMINI_MEDIA_TIMEOUT_SECONDS` | `180` | Timeout pro API-Anfrage |
| `GEMINI_MEDIA_LOG_LEVEL` | `WARNING` | Log-Level (Logs gehen nach stderr) |

Mit dem Standardlimit von 0,50 $ pro Aufruf sind Videos gesperrt, denn ein 10-Sekunden-Video
kostet etwa 1,01 $. Wenn du Videos willst, erhöhst du `GEMINI_MEDIA_MAX_COST_PER_CALL`
bewusst.

### Modelle und Preise

Der mitgelieferte Katalog liegt in [`src/gemini_media_mcp/models.toml`](src/gemini_media_mcp/models.toml).
Die Preise stammen aus der [offiziellen Preisliste](https://ai.google.dev/gemini-api/docs/pricing),
Stand **31.10.2026**. Zum Ändern kopierst du die Datei, passt sie an und setzt
`GEMINI_MEDIA_CONFIG` auf deine Kopie. Jede `[models."<id>"]`-Tabelle in deiner Datei ersetzt
den mitgelieferten Eintrag mit derselben ID.

## Kostenbeispiele

Schätzungen nach Googles Listenpreisen vom 31.10.2026 (USD, Standard-Tarif):

| Was | Modell | Schätzung |
|---|---|---|
| 1 Bild, 1K | Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) | 0,034 $ |
| 1 Bild, 1K | Nano Banana 2.1 (`gemini-nano-banana-2.1`) | 0,034 $ |
| 1 Bild, 4K | Nano Banana 2.1 | 0,113 $ |
| 1 Bild, 2K, 3 Referenzbilder | Nano Banana Pro (`gemini-3-pro-image`) | 0,141 $ |
| 1 Bild, 4K | Nano Banana Pro | 0,240 $ |
| 1 Video, 720p, bis 10 s | Gemini Omni Flash | ≈ 1,01 $ |
| 50 Bilder im Monat, 1K | Nano Banana 2.1 | ≈ 1,70 $ |

Das Log speichert *Schätzungen*. Verbindlich ist deine Google-Rechnung, die du in
[AI Studio](https://aistudio.google.com/usage) siehst.

## Sicherheitskonzept

- **API-Key:** Er kommt nur aus `GEMINI_API_KEY` und geht nur als HTTP-Header an Google. Er
  wird nie geloggt, ist nie Teil des Settings-Objekts und wird aus jeder Fehlermeldung entfernt.
- **Dateizugriff:** Eingabebilder werden nur aus `GEMINI_MEDIA_ALLOWED_DIRS` gelesen. Pfade
  werden vor der Prüfung vollständig aufgelöst, Symlinks eingeschlossen. Tricks mit `../` und
  Symlinks nach außen werden abgewiesen.
- **Eingabeprüfung:** Der Dateityp wird am Inhalt erkannt (PNG, JPEG, WebP, HEIC/HEIF), nicht
  an der Endung. Die Dateigröße ist begrenzt.
- **Kein Überschreiben:** Ausgaben heißen `JJJJMMTT-HHMMSS_<slug>.<ext>` und werden exklusiv
  angelegt. Ist ein Name schon vergeben, bekommt die neue Datei die Endung `-2`, `-3` usw.
- **Kostenkontrolle:** Jeder Aufruf wird vor dem Senden bepreist und abgelehnt, wenn er das
  Limit pro Aufruf, pro Tag oder pro Monat überschreiten würde. Videos brauchen zusätzlich
  ausdrücklich `confirm_cost=true`.
- **Netzwerk:** Der Server spricht nur mit der Google-Gemini-API (`*.googleapis.com`). Keine
  Telemetrie, keine Analytics, keine Update-Checks.
- **Abhängigkeiten:** Wenige direkte Laufzeit-Abhängigkeiten, exakt gepinnt; das Lockfile
  pinnt den Rest. Im Repo laufen Dependabot und ein gitleaks-Secret-Scan.

Mehr dazu in [SECURITY.md](SECURITY.md).

## Transparenz: Sidecars und KI-Kennzeichnung

Neben jeder Ausgabe liegt eine `<name>.json` mit Prompt, Modell, Referenzbildern (Pfad und
SHA-256), Parametern, geschätzten Kosten und Zeitstempel.

Google bettet in jedes mit diesen Modellen erzeugte Bild und Video ein unsichtbares
**SynthID**-Wasserzeichen ein. Wenn du Ergebnisse veröffentlichst, kennzeichne sie als
KI-generiert: Ab dem 2. August 2026 verlangt **Art. 50 der EU-KI-Verordnung (AI Act)**, dass
Deepfakes und KI-generierte Texte, die die Öffentlichkeit informieren sollen, als künstlich
erzeugt offengelegt werden. Die Sidecar-Dateien helfen dir festzuhalten, was generiert wurde.
(Das ist ein Hinweis, keine Rechtsberatung.)

## Beispiel-Prompts

Sprich Claude ganz normal an, zum Beispiel:

- „Mach ein 16:9-Headerbild: ein Roboter, der am Schreibtisch zu Mittag isst, flacher Illustrationsstil, nenn es robot-lunch.“
- „Nimm `~/Bilder/gemini/ich.jpg` und `~/Bilder/gemini/logo.png`: Setz das Logo auf mein T-Shirt, mein Gesicht soll gleich bleiben.“
- „Was würde ein 9:16-Video von Wellen im Sonnenuntergang kosten?“
- „Ist das Video schon fertig?“
- „Wie viel habe ich diesen Monat für Bilder ausgegeben?“

## Entwicklung

```bash
uv sync
uv run pytest          # Tests nutzen eine Fake-API, kein Netzwerk
uv run ruff check . && uv run ruff format --check .
uv run mypy
uvx pre-commit install # gitleaks + ruff vor jedem Commit
```

Siehe [CONTRIBUTING.md](CONTRIBUTING.md).

## Lizenz

MIT, siehe [LICENSE](LICENSE).

---

Not affiliated with or endorsed by Google. Gemini, Nano Banana and Veo are trademarks of Google LLC.

Built by Wimplunch – Lunch is for wimps.
