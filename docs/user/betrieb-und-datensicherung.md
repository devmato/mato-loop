# Betrieb und Datensicherung

MatoLoop wird selbst gehostet. Die Produktionsvorlagen verwenden veröffentlichte Images; Entwicklung und Prüfungen verwenden dagegen `./scripts/dev.sh`. Die folgenden Betriebsbefehle gehören in das Verzeichnis Ihrer Produktionskonfiguration.

## Erstinstallation

Benötigt werden Docker Engine, Compose v2 und für veröffentlichte Images ein `linux/amd64`-Host. Verwenden Sie `docker-compose.yaml` und `.env.example` aus dem zu installierenden Release des [GitHub-Repositorys](https://github.com/devmato/mato-loop). Kopieren Sie `.env.example` nach `.env` und bearbeiten Sie die Werte vor dem Start:

- Zufälliges URL-sicheres `POSTGRES_PASSWORD`, beispielsweise aus Hex-Zeichen.
- `MATO_LOOP_IMAGE` und `MATOLOOP_AGENT_IMAGE` mit demselben tatsächlich veröffentlichten festen `YYYY-MM-DD-<SHA8>`-Tag aus einem erfolgreichen Veröffentlichungsdurchlauf.
- `MATO_LOOP_PUBLIC_BASE_URL` als Ihren öffentlichen HTTP(S)-Ursprung ohne Pfad, nicht den Beispielhost. **Administration > Grundeinstellungen** überschreibt später diesen Fallback; ist beides leer, gilt der eingehende Serverursprung.
- Hostpfade für Datenbank, Backend und Agent sowie HTTP-Adresse/Port. Die Vorlage verwendet `/opt/mato-loop` und `/opt/matoloop-agent` und veröffentlicht HTTP-Port `10020` auf `0.0.0.0`.
- HTTPS am Reverse Proxy und `MATO_LOOP_SECURE_COOKIES=true`. Die Vorlage liefert TLS nicht selbst; ein entferntes reines HTTP-Login mit Secure-Cookie ist keine passende Produktionskonfiguration.

Bei einem Proxy auf demselben Host setzen Sie `MATO_LOOP_HOST_ADDRESS=127.0.0.1`; bei einem anderen Proxy eine geeignete private Adresse. Für ausdrücklich vertrauenswürdigen HTTP-Netzbetrieb kann Secure-Cookie deaktiviert werden. Speichern Sie `.env` mit beschränkten Dateirechten, nicht im Repository.

```bash
cp .env.example .env
# Hier .env bearbeiten und HTTPS einrichten.
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 mato-loop
```

Öffnen Sie anschließend den konfigurierten HTTPS-Ursprung, legen Sie das erste Administratorkonto an und folgen Sie dem [Schnellstart](schnellstart.md). Infinity lädt beim ersten Start sein gepinntes Modell; das kann Minuten dauern. Lexikalische Suche bleibt ohne Embeddings verfügbar.

Für die Variante ohne Agent verwenden Sie bei allen Befehlen `docker compose -f docker-compose.no-agent.yaml`. Beim Wechsel eines bestehenden Stacks ergänzt `up -d --remove-orphans` die Entfernung des früheren Agent-Containers. Bestehende Installationen übernehmen ihre bisherigen Datenbankwerte und Hostpfade ausdrücklich; ein Umzug persistenter Daten ist eine gesonderte Migration.

### Reverse Proxy und Monitoring

Veröffentlichen Sie MatoLoop am Ursprung ohne URL-Unterpfad. Der Proxy muss WebSocket-Upgrades und lang laufende Verbindungen unterstützen sowie SSE ohne Antwortpufferung weiterleiten. Stimmen Sie Timeouts und Uploadlimits mit Ihrem Einsatz ab. PostgreSQL und Infinity bleiben intern; die Produktionsvorlagen veröffentlichen deren Ports nicht.

- `/api/health/live` zeigt Prozess-Liveness.
- `/api/health` ist Readiness; Datenbank-/pgvector-Fehler ergeben HTTP 503.
- `embedding: "degraded"` bei sonst `status: "ok"` bedeutet fehlende Embeddings, nicht ausgefallenen Chat.

Überwachen Sie Containerstatus, Fehlerlogs, freien Speicher, Backup-Erfolg und Embedding-Degradation getrennt. Der Produktionsdienst heißt `mato-loop`, der Entwicklungsdienst `backend`. API-Zugangsdaten und OAuth-Callback-Querywerte gehören nicht in Proxylogs.

## App und Push-Benachrichtigungen

Der Produktionsbuild ist als Web-App installierbar. Verwenden Sie vertrauenswürdiges HTTPS. Ein Administrator richtet Web Push unter **Administration > Benachrichtigungen** mit einer überwachten Kontaktadresse ein. MatoLoop erzeugt `/data/secrets/web-push-vapid.pem` und unterstützt Apple-, Google- und Mozilla-Browserpushdienste.

Benutzer aktivieren Push pro Browser/PWA im Benachrichtigungsbereich. Auf iOS/iPadOS ist mindestens 16.4 und eine vorherige Installation über **Zum Home-Bildschirm** nötig. Berechtigung wird nur nach dieser direkten Aktion angefragt. Bei fokussiertem MatoLoop wird Push für diesen Benutzer auf allen Geräten unterdrückt; gespeicherte Hinweise bleiben erhalten. Zustellung ist bestmöglich, kein Ersatz für den Verlauf.

Sichern Sie den VAPID-Schlüssel gemeinsam mit Datenbank und Backenddaten. Verlust macht bestehende Geräteabonnements unbrauchbar. Der Service Worker speichert die Anwendungshülle, keine API-, Authentifizierungs-, SSE- oder Dateidaten offline. Ein bewusst bestätigtes App-Update lädt offene Appfenster neu und kann ungespeicherten Arbeitsspeicherzustand verlieren.

Der Entwicklungsmodus verwendet `./scripts/dev.sh up --pwa --build -d`; localhost ist für Desktoptests vertrauenswürdig, mobile Geräte benötigen auch dort HTTPS.

## Optionalen Computer einrichten

Öffnen Sie **Einstellungen > Computer > Computer hinzufügen**. Wählen Sie Plattform und Installationsweg, optionalen Alias und Zugriffsrichtlinien. Kopplung verwendet Computer-ID, Backend-WebSocket-Adresse und ein nur einmal sichtbares Token mit begrenzter Gültigkeit. Ein gekoppelter Computer erzeugt einen dauerhaften eigenen Zugang; erneute Kopplung erfordert vorheriges Entkoppeln. **Verwalten > Aktionen** enthält Kopplung, Updates und unterstützte Deinstallation.

Die Computerübersicht zeigt eine Tabelle mit Name, Betriebssystem, Agent-Version, Status und Aktionen. Ein Klick auf den Namen oder das Zahnrad öffnet den Verwaltungsdialog mit **Übersicht**, **Einstellungen** und **Aktionen**. Unter **Übersicht** stehen auch Computer-ID, Home-Verzeichnis, letzte Verbindung und der Vergleich mit der MatoLoop-Version. Auf kleinen Bildschirmen zeigt die Tabelle nur Name, Status und Aktionen; die übrigen Angaben bleiben im Dialog erreichbar. Das Entfernen über den Papierkorb erfordert eine Bestätigung.

### Mitgelieferter Docker-Agent

`MATOLOOP_AGENT_DATA` wird als `/data`, `MATOLOOP_AGENT_HOME` als `/workspace` eingebunden. Konfiguration ist `/data/config.json`, Zugang und Zustand bleiben unter `/data`. Der Backenddienst verwaltet oder löscht den Agent-Arbeitsbereich nicht. Der Agent startet zunächst ungekoppelt:

```bash
docker compose exec matoloop-agent matoloop-agent pair
```

In der Entwicklung verwenden Sie stattdessen:

```bash
./scripts/dev.sh compose exec matoloop-agent uv run --no-sync matoloop-agent pair
```

Für nichtinteraktive Kopplung müssen Backend-WebSocket-URL, `MATOLOOP_AGENT_ID` und `MATOLOOP_AGENT_PAIRING_TOKEN` gemeinsam vorliegen. Compose setzt die interne URL bereits; Betreiber ergänzen ID und Token. Entfernen Sie das einmalige Token nach erfolgreicher Kopplung. Alte Umgebungsvariablen-Aliase werden nicht unterstützt.

Der Agent-Container läuft derzeit als root und setzt `seccomp:unconfined` sowie `no-new-privileges:true`. Docker-Seccomp ist damit deaktiviert; das ist weder eine MatoLoop-OS-Sandbox noch eine Abschaltung von Chromiums eigener Renderer-Sandbox. Die Vorlagen referenzieren keine zusätzliche `browser-seccomp.json`. Begrenzen Sie Mounts, Netzwerk und Betriebssystemrechte gemäß Ihrem Einsatzzweck.

### Nativen Agent unter Linux installieren

Kopieren Sie den Linux-Befehl aus den Computereinstellungen oder starten Sie aus einem Checkout `./scripts/install-matoloop-agent.sh` als normalen Benutzer. Privates uv/Python liegt unter `~/.local/share/matoloop-agent`, ohne Shellprofiländerung. Der systemweite systemd-Dienst startet beim Booten mit den Rechten des installierenden Benutzers. sudo wird nur für Dienstverwaltung und bestätigte Systempakete benötigt.

Standalone-Setup und Updates installieren den exakten Commit des laufenden Backends. Lokales `install` übernimmt ausdrücklich den Checkout. `matoloop-agent` bietet `pair`, `install`, `update`, `status`, `uninstall` und `uninstall --purge`. Ohne Purge bleiben Zugangsdaten erhalten; Workspaces bleiben auch mit Purge erhalten. Browser-Setup ist optional und separat; Container- und native Linux-Agents verwenden denselben headless Browser mit optionaler Webansicht. Interaktives Terminal und OpenSCAD erfordern ihre Hostvoraussetzungen.

### Nativen Agent unter Windows installieren

Öffnen Sie eine normale Windows PowerShell unter dem Konto, das den Agenten besitzen soll, und kopieren Sie den Windows-Befehl aus den Computereinstellungen. Für die Aufgabenregistrierung fordert der Installer über UAC Administratorrechte an; bestätigen Sie die Erhöhung mit demselben Windows-Konto, nicht mit einem anderen Administratorkonto. Die Installation liegt unter `%LOCALAPPDATA%\matoloop-agent`, der Standardworkspace unter `%USERPROFILE%\mato-loop-agent\workspace`. Der Installer verwendet vorhandenes Git oder privates geprüftes MinGit. Nur das Starterverzeichnis `%LOCALAPPDATA%\matoloop-agent\bin` wird zum Benutzer-`PATH` hinzugefügt; Git und Python bleiben privat.

Der Starttask läuft unter dem Installationskonto ohne Anmeldung und ohne Administratorrechte. Geben Sie dessen Kontopasswort, nicht die Windows-Hello-PIN, verdeckt im Terminal der Aufgabenregistrierung ein; es wird nur dem Aufgabenplaner übergeben. Bei interaktiver Erstinstallation startet anschließend die Kopplung mit bereits gesetzter Backend-Adresse: Geben Sie Computer-ID und Token aus **Einstellungen > Computer** ein. Starten Sie das Terminal nach der Installation neu; danach stehen `matoloop-agent pair`, `matoloop-agent update`, `matoloop-agent status`, `matoloop-agent uninstall` und `matoloop-agent uninstall -Purge` bereit. Ein fehlgeschlagenes Pairing lässt die Installation bestehen und kann mit `matoloop-agent pair` wiederholt werden. Die Deinstallation entfernt auch den Starter und dessen Benutzer-`PATH`-Eintrag. Ein lokaler Checkout kann mit `-Source` installiert werden. PowerShell-Shellzugriff ist verfügbar; Browser, OpenSCAD und interaktive Terminals sind nicht angeboten. Native Windows-Akzeptanz ist getrennt von Linux-/Containerprüfungen zu beurteilen.

**Bestehende Windows-Installationen:** Führen Sie den aktuellen Windows-Installationsbefehl aus **Einstellungen > Computer** einmal erneut aus, um den kurzen CLI-Befehl einzurichten. Deinstallieren oder bereinigen Sie vorher nichts; Kopplung und Workspace bleiben erhalten. `PATH`-Variablen wie `%JAVA_HOME%` bleiben unverändert. Die CLI-Einrichtung erfolgt vor dem Stoppen des bisherigen Agents; ein kompatibler Starter kann nach einem fehlgeschlagenen Versionswechsel bestehen bleiben.

## Zu sichernde Daten

Eine vollständige Wiederherstellung braucht einen gemeinsamen Stand von:

- PostgreSQL-Datenbank und Backend-`/data`, einschließlich Dateien/Versionen und `/data/secrets/credential.key`.
- `/data/secrets/web-push-vapid.pem`, falls Push eingerichtet ist.
- Agent-Konfiguration und vollständigem dauerhaften Datenverzeichnis sowie benötigten Home-/Workspaces.
- `.env`, gewählter Compose-Konfiguration, Proxykonfiguration und tatsächlich eingesetzten Image-Tags.

Provider-, OAuth- und persönliche Variable-Zugangsdaten liegen verschlüsselt in der Datenbank. Ohne passenden Fernet-Schlüssel sind sie nicht entschlüsselbar. Aktuelle Chat-Browserprofile sind flüchtig und werden nach Laufzeitende nicht wiederhergestellt. Ältere persistente Browserprofile können weiterhin sensible unverschlüsselte Websitecookies im Agent-Datenverzeichnis enthalten; sie werden weder geladen noch automatisch gelöscht. Behandeln Sie solche Altbestände und ihre Sicherungen entsprechend vertraulich.

Infinity-Modellcache und wegwerfbarer Agent-Sessioncache sind reproduzierbar. Agent-Zugangsdaten sind dagegen für eine Wiederherstellung ohne neue Kopplung erforderlich. Linux verwendet `<config-parent>/agent-state/credential.json` mit Dateimodus `0600` und eigentümergebundenem Verzeichnis `0700`. Windows verwendet DPAPI-gebundenes `agent-state/credential.dat`: eine Kopie ist ohne denselben entschlüsselbaren Windows-Benutzerkontext nicht portabel. Fehlt dieser Kontext, installieren Sie die Tasks neu und koppeln Sie erneut.

### Konsistente Sicherung mit PostgreSQL-Dump

Dieses Beispiel verwendet **die Standardhostpfade der Vorlage**. Passen Sie Archivquellen an Ihre tatsächlichen `.env`-Pfade an; der Backupordner muss außerhalb aller gesicherten Daten liegen. Stoppen Sie außerdem andere native Agents und Prozesse, die in gesicherte Workspaces schreiben. Verwenden Sie einen neuen privaten Backupordner:

```bash
umask 077
BACKUP_DIR="$HOME/matoloop-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p -m 700 "$BACKUP_DIR"
docker compose config --images > "$BACKUP_DIR/images.txt"
cp .env docker-compose.yaml "$BACKUP_DIR/"
# Laufende Aufträge beenden, Browser schließen und externe Workspace-Schreiber stoppen.
docker compose stop mato-loop matoloop-agent
docker compose exec -T db sh -c 'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' > "$BACKUP_DIR/database.dump"
sudo tar --acls --xattrs --numeric-owner -C /opt/mato-loop -cpf "$BACKUP_DIR/backend-data.tar" data
sudo tar --acls --xattrs --numeric-owner -C /opt/matoloop-agent -cpf "$BACKUP_DIR/agent-data.tar" data home
```

Prüfen Sie **jeden Befehl auf Erfolg**, bevor Sie den Backupstand als vollständig markieren. Sichern Sie Proxy-/native Agentkonfigurationen zusätzlich. Die Datenbank bleibt für den logischen Dump aktiv, Backend- und Agent-Schreiber dagegen gestoppt. Ein laufendes PostgreSQL-Datenverzeichnis darf nicht als gewöhnliche Dateikopie dieses Dumps behandelt werden; eine physische Kopie erfordert einen vollständig gestoppten Datenbankdienst und kompatible PostgreSQL-Version.

Listen Sie Dump/Archive zur Prüfung auf und starten Sie danach den vorherigen Betrieb:

```bash
docker compose exec -T db pg_restore --list < "$BACKUP_DIR/database.dump"
sudo tar -tf "$BACKUP_DIR/backend-data.tar"
sudo tar -tf "$BACKUP_DIR/agent-data.tar"
docker compose up -d
```

Archive und Konfiguration sind vertraulich und benötigen geschützte, möglichst getrennt gelagerte Kopien. Das erfolgreiche Auflisten ist noch kein Wiederherstellungstest.

### Wiederherstellung und Probe

Verwenden Sie einen frischen isolierten Zielhost bzw. leere geprüfte Zielverzeichnisse, mit gesperrten externen Integrations-/Agentverbindungen. Übernehmen Sie die gesicherten festen Image-Tags und die passende Compose-/`.env`-Konfiguration; veröffentlichen Sie den Dienst zunächst nicht. Passen Sie Hostpfade bewusst an, ohne Browser-/Agent-Bindungsidentitäten stillschweigend umzuschreiben. Stellen Sie mit gestopptem Backend/Agent die Dateien samt numerischen Eigentümern und Rechten wieder her, für die Standardpfade etwa:

```bash
sudo mkdir -p /opt/mato-loop /opt/matoloop-agent
sudo tar --acls --xattrs --numeric-owner -C /opt/mato-loop -xpf "$BACKUP_DIR/backend-data.tar"
sudo tar --acls --xattrs --numeric-owner -C /opt/matoloop-agent -xpf "$BACKUP_DIR/agent-data.tar"
docker compose pull
docker compose up -d db
# Auf eine gesunde, leere Ziel-Datenbank warten: docker compose ps
docker compose exec -T db sh -c 'pg_restore --exit-on-error --no-owner --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' < "$BACKUP_DIR/database.dump"
docker compose up -d embedding mato-loop
```

Prüfen Sie vor externer Freigabe: Readiness, Anmeldung, Entschlüsselung eines bestehenden Providers, Sessiondateien und historische Versionen, Memory, persönliche Variablen, Automationsdefinitionen und deren Zeitpläne. Begrenzen Sie ausgehendes Netz auch beim Backendstart, damit wiederhergestellte Aufgaben keine echten externen Änderungen auslösen. Starten und prüfen Sie den Agenten anschließend kontrolliert; prüfen Sie Zugang, Betriebssystemrechte, Originalbindung, Browserzustand und Workspace. Nicht entschlüsselbare Windows-Zugänge erfordern neue Kopplung.

Erst nach der Probe stellen Sie Netzwerk und öffentliche URL wieder her und prüfen Integrationen und Push gezielt. Verpasste Automationstermine werden nicht rückwirkend ausgeführt. Dokumentieren Sie Datum, Image-Tags und Prüfergebnis jeder Wiederherstellungsprobe. Dieser Ablauf ist eine Betriebsanleitung, kein Nachweis einer für Ihre Installation bereits ausgeführten Wiederherstellung.

## Entwicklungsdaten

Entwicklungsdaten liegen in benannten Compose-Volumes; Agentdaten in separaten Bindpfaden. `./scripts/dev.sh reset-storage` löscht alle benannten Entwicklungsvolumes einschließlich Datenbank, Konten, Secrets und Backenddateien. Es ist keine normale Aufräumaktion. Parallelcheckouts müssen eigene Compose-Projektnamen, Ports und Agentpfade verwenden; Standardcheckouts teilen sonst dasselbe Projekt.

## Aktualisierung

Sichern und proben Sie den gemeinsamen Datenstand vor Updates. Ändern Sie beide Images auf denselben erfolgreich veröffentlichten festen Tag, ziehen Sie sie und erstellen Sie Container neu. Beim Backendstart laufen Alembic-Migrationen. Prüfen Sie danach Anmeldung, Entschlüsselung, Dateien, Memory, Agents und Integrationen.

```bash
docker compose pull
docker compose up -d
docker compose ps
```

`develop` und `latest` sind veränderlich, keine reproduzierbaren Update-/Rollbackziele. Die Datums-/SHA-Tags gelten für beide Branchkanäle. CI veröffentlicht, installiert aber nicht auf Ihrem Server.

Verwaltete Agents aktualisieren sich über **Einstellungen > Computer > Verwalten > Aktionen > Agent aktualisieren** auf den exakten Backendcommit. Docker unterstützt auch `docker compose exec matoloop-agent matoloop-agent update`. Neustart erhält dieses Containerupdate; Neuerstellung stellt die Imageversion wieder her. Native Updates bereiten einen eigenen Checkout vor und ändern keinen Entwicklerarbeitsbaum. Dirty-/Unknown-Backends liefern kein reproduzierbares Installationsziel.

Links neben **Computer hinzufügen** erscheint **Agents aktualisieren (N)**, sobald verbundene, updatefähige Computer vom Backendstand abweichen und keine laufende Wartung haben. Eine gemeinsame Bestätigung nennt die betroffenen Computer. Anschließend werden deren Updates über denselben manuellen Updatepfad gestartet; erforderliche sudo-Eingaben erfolgen einzeln. Ein fehlgeschlagener Start verhindert die übrigen Updates nicht. Die Zusammenfassung zählt gestartete Updates; deren Abschluss sehen Sie am Wartungsstatus des jeweiligen Computers.

Ein manuelles **Agent aktualisieren** fragt vorab nach Bestätigung: Laufende MatoLoop-Aktionen auf diesem Computer werden abgebrochen und Terminals, Browser-Sitzungen und Browser-Setup geschlossen, auch aus anderen Chats. Nicht gespeicherte Eingaben können verloren gehen. Voraussetzungen und sudo werden vor dem Unterbrechen geprüft. Bereits laufende geschützte Dateischreibvorgänge dürfen sicher abschließen; bei zu langer Wartezeit startet das Update nicht. Ältere Agents benötigen beim ersten Update noch abgeschlossene Vorgänge und geschlossene Terminals/Browser. Für Deinstallation müssen Vorgänge weiterhin vorher beendet sein. Native Linux-Wartung kann eine einmalige geschützte sudo-Eingabe benötigen; Windows verwendet den installierten Wartungstask. Eine Verbindungstrennung allein beweist kein erfolgreiches Update; prüfen Sie Ergebnis und gemeldete Version.

Wartungsergebnisse werden vor der Übermittlung lokal gespeichert. Ist MatoLoop vorübergehend nicht erreichbar, meldet der Agent das Ergebnis später erneut; ein Reconnect ist dafür nicht zwingend nötig. Ohne bestätigten Abschluss bleibt das Ergebnis unbekannt. Neue Installationsstände speichern nur die benötigten Agent-Quellen statt des vollständigen Projekts; Kopplung und Arbeitsdateien bleiben beim Update erhalten.

**Agents automatisch aktualisieren** unter **Administration > Grundeinstellungen** legt den Instanzstandard fest und ist anfangs ausgeschaltet. Unter **Computer > Verwalten > Einstellungen > Automatische Agent-Updates** wählen Sie **Instanzstandard (aktuell an/aus)**, **An** oder **Aus**. Änderungen des Instanzstandards gelten für alle Computer ohne eigene Übersteuerung.

Unter **sudo-Passwort** ordnen Sie optional ein vorhandenes eigenes Secret aus **Variablen & Secrets** zu. Es dient als Standard für native Linux-Updates und für Admin-Ausführungen auf diesem Computer, sofern Secret-Zugriff und konkrete Aktion erlaubt sind. Ein ausdrücklich übergebenes oder temporäres Passwort hat Vorrang. **Kein Secret zugeordnet**, Entkoppeln oder Entfernen des Computers lösen nur die Zuordnung; das Secret bleibt bestehen. Änderungen seines Werts gelten für die nächste Verwendung. Docker, Windows und passwortloses sudo benötigen kein zugeordnetes Passwort. Die erforderlichen sudo-Rechte muss das Benutzerkonto bereits besitzen.

Die automatische Prüfung erfolgt beim Verbinden sowie nach Änderungen der Update-Einstellung oder der Secret-Zuordnung. Nach einem Backendneustart verbinden sich die Agents erneut. Automatische Updates unterbrechen keine laufende Arbeit. Beschäftigte Agents und fehlgeschlagene Updates werden erst beim nächsten solchen Ereignis oder manuell erneut versucht; es gibt keine zeitgesteuerten Wiederholungen. Das Ziel ist immer der exakte Backendstand, auch wenn dieser älter ist. Fehler erscheinen beim vorhandenen Wartungsstatus des Computers.

### Migration vom früheren Compose-Agenten

Ermitteln Sie das frühere Volume `<compose-projekt>_execution_agent_data` mit `docker volume ls`. Stoppen Sie den alten Dienst und übernehmen Sie den vollständigen Inhalt samt Eigentümern/Rechten in den neuen Agent-Datenpfad. Prüfen Sie insbesondere `agent-state/credential.json` (`0600`) und sein privates Elternverzeichnis (`0700`). Aktueller Pairingzustand liegt im selben Credentialobjekt, nicht in einer separaten `credential.pending.json`. Bei verlorenem Zugang entkoppeln und koppeln Sie neu.

<a id="removed-git-integration"></a>

### Entfernte Git-Integration

Vor dem ersten Neustart eines betroffenen alten Agents:

1. Stoppen Sie ihn und entfernen Sie aus seiner Konfigurations-JSON ausschließlich die früheren Felder `gitExecutable` und `allowNetwork`, falls vorhanden. Auch `null`/`false` sind keine gültigen Ersatzwerte.
2. Entfernen Sie ausschließlich `<config-parent>/git-home`, falls vorhanden, beispielsweise `MATOLOOP_AGENT_DATA/git-home`. Erhalten Sie Elternverzeichnis, Zugang, Repositories, Workspaces und Verlauf.
3. Starten Sie den neuen Agent. Das Backend bereinigt alte Integrationssettings per Migration; löschen Sie keine historischen Nachrichten, Tool-Ergebnisse oder Freigaben. Erhaltener Zugang braucht keine neue Kopplung.

Git bleibt als gewöhnlicher Shellbefehl auf einem verbundenen Computer mit installiertem Git verfügbar, nicht als Backend-Clonefunktion.

### Rollback

Setzen Sie beide Imagevariablen auf den vorherigen festen Tag, ziehen Sie ihn und erstellen Sie Container neu. Ein älteres Image kann mit dem bereits migrierten Schema unvereinbar sein. Stellen Sie dann die passende gemeinsam gesicherte Datenbank, Backenddateien und Schlüssel nach dem isolierten Wiederherstellungsverfahren wieder her; ein bloßer Imagewechsel ist kein Schema-Rollback.

Beim Löschen eines Benutzers entfernt MatoLoop dessen Datenbankdaten und Backend-Sessionbäume. Dateien, dauerhafter Agentzustand und bereits geschriebene externe Daten bleiben außerhalb dieser Löschung. Gemeinsame Instanzressourcen bleiben bestehen.
