# Computer verbinden und Terminal verwenden

Ein verbundener MatoLoop Agent ermöglicht Datei- und Shell-Zugriffe auf deinem Computer. Chat und Session-Ablage funktionieren auch ohne ihn. Plan und Build benötigen einen verbundenen Computer und einen zugänglichen Arbeitsordner.

## Computer einrichten

1. Öffne **Einstellungen > Computer > Computer hinzufügen**.
2. Wähle Plattform, Installationsweg und Zugriffsrichtlinien.
3. Installiere und kopple den Agenten wie im passenden Abschnitt unten beschrieben. Du benötigst Backend-Adresse, Computer-ID und das einmalig sichtbare Token mit begrenzter Gültigkeit.
4. Wähle den verbundenen Computer und den [Arbeitsordner der Session](dateien-und-arbeitsordner.md#arbeitsordner-auswahlen).

Computer gehören deinem MatoLoop-Konto. Eine erneute Kopplung erfordert vorheriges Entkoppeln. Übersicht, Einstellungen und Wartungsaktionen öffnest du über den Computernamen oder das Zahnrad.

!!! warning "Der Agent hat Betriebssystemrechte"
    MatoLoop ist keine Betriebssystem-Sandbox. Begrenze Benutzerrechte, Mounts und Netzwerk passend zu deinem Einsatz. Direkte Aktionen in Computer-Dateien und im persönlichen Terminal verwenden deine Agent-Betriebssystemrechte, nicht die Tool-Rechte des Modells. Das Löschen eines MatoLoop-Kontos entfernt keine Computerdateien.

### Mitgelieferten Docker-Agenten koppeln

Die Produktionsvorlage startet den Agenten zunächst ungekoppelt. Führe im Verzeichnis deiner Produktionskonfiguration aus:

```bash
docker compose exec matoloop-agent matoloop-agent pair
```

Gib die im Einrichtungsdialog angezeigten Daten ein. `MATOLOOP_AGENT_DATA` wird als `/data`, `MATOLOOP_AGENT_HOME` als `/workspace` eingebunden. Konfiguration und Zugang bleiben unter `/data`; Arbeitsdateien unter `/workspace`. MatoLoop synchronisiert diese Dateien nicht mit der Session-Ablage.

Für nichtinteraktive Kopplung müssen Backend-WebSocket-URL, `MATOLOOP_AGENT_ID` und `MATOLOOP_AGENT_PAIRING_TOKEN` gemeinsam gesetzt sein. Compose liefert bereits die interne URL. Entferne das einmalige Token nach erfolgreicher Kopplung.

!!! warning "Rechte des vorbereiteten Containers"
    Der Agent-Container läuft derzeit als root und mit `seccomp:unconfined`. Docker-Seccomp ist deaktiviert; Chromiums eigene Renderer-Sandbox bleibt davon getrennt. Gewähre nur die benötigten Mounts und Netzwerkzugriffe.

### Unter Linux installieren

1. Kopiere den Linux-Installationsbefehl aus den Computereinstellungen.
2. Führe ihn als normalen Benutzer aus. Aus einem lokalen Checkout kannst du stattdessen `./scripts/install-matoloop-agent.sh` verwenden.
3. Bestätige nur die benötigten Systempakete und die Dienstverwaltung per sudo.
4. Schließe die Kopplung mit den Daten aus MatoLoop ab.

Die Installation verwaltet private Werkzeuge unter `~/.local/share/matoloop-agent`, ohne Shellprofile zu ändern. Der systemd-Dienst startet beim Booten mit den Rechten des installierenden Benutzers. Browser-Setup ist optional; Terminal und OpenSCAD benötigen ihre jeweiligen Hostvoraussetzungen.

`matoloop-agent status` prüft den Dienst. `matoloop-agent pair` wiederholt eine fehlgeschlagene Kopplung, ohne neu zu installieren. `uninstall` erhält die Zugangsdaten; `uninstall --purge` entfernt die verwaltete Installation einschließlich Zugang. Workspaces bleiben in beiden Fällen erhalten.

### Unter Windows installieren

1. Öffne eine normale Windows PowerShell unter dem Konto, das den Agenten besitzen soll.
2. Kopiere und starte den Windows-Installationsbefehl aus den Computereinstellungen.
3. Bestätige UAC für die Aufgabenregistrierung mit **demselben Windows-Konto**, nicht mit einem anderen Administratorkonto.
4. Gib das Kontopasswort, nicht die Windows-Hello-PIN, verdeckt im Terminal ein. Es wird dem Aufgabenplaner übergeben.
5. Kopple mit Computer-ID und Token. Starte anschließend das Terminal neu, um den Befehl `matoloop-agent` zu verwenden.

Die Installation liegt unter `%LOCALAPPDATA%\matoloop-agent`, der Standardworkspace unter `%USERPROFILE%\mato-loop-agent\workspace`. Nur das Starterverzeichnis wird dem Benutzer-`PATH` hinzugefügt. Der Starttask läuft unter dem Installationskonto ohne Anmeldung und ohne Administratorrechte.

PowerShell-Shellzugriff ist verfügbar. Die optionale [Browserfunktion](browser.md) richtest du anschließend über die Browser-Integration ein; OpenSCAD und interaktive Terminals werden unter Windows nicht angeboten. Eine fehlgeschlagene Kopplung lässt die Installation bestehen und kann mit `matoloop-agent pair` wiederholt werden. `matoloop-agent uninstall` beziehungsweise `uninstall -Purge` erhalten den Workspace. Zugangsdaten sind an den Windows-Benutzerkontext gebunden; beachte die [Datensicherung](betrieb-und-datensicherung.md#zu-sichernde-daten).

## Dateizugriff begrenzen

Jeder Computer besitzt getrennte Dateirichtlinien für Home und externe Pfade. Standardmäßig gilt im Home **Automatisch**, außerhalb **Nachfragen**. Sie steuern die ausdrückliche Ordnerwahl; **Nicht erlaubt** bleibt zusätzlich eine harte Grenze für Modellaktionen.

Dateiaktionen außerhalb des aktuellen Arbeitsordners benötigen außerdem die passende Profilberechtigung. Shell hat eigene Rechte ohne Workspace-Begrenzung. Zwei Schalter erlauben Shell-Aktionen in der Home- und externen Zone; beide sind anfangs aktiviert. Alle erkannten Zielzonen müssen erlaubt sein, bei unvollständiger Analyse auch die externe Zone. Einzelheiten stehen unter [Shell-Freigaben](freigaben-und-datenfreigabe.md#shell-aktionen).

### Temporäre Arbeitsdateien

Der eigene Session-Tempordner ist standardmäßig lesbar. **Agentenprofil > Temporären Arbeitsordner automatisch freigeben** ergänzt Schreibzugriff; Plan, Build und Sysadmin aktivieren ihn standardmäßig. Unter Linux liegt er unter `/tmp/mato-loop/<session-id>/`, unter Windows im entsprechenden Unterordner des Agent-Tempverzeichnisses.

Löschen und fremde Sessionordner benötigen normale Dateirechte. Computersperren, Toolverbote, sensible Zugriffe und Delegationsgrenzen gelten weiter. Ältere Agents ohne gemeldeten Tempbereich erhalten keine zusätzliche Freigabe. Plan verwendet diesen Bereich nur für Analyseprüfungen, nicht für Implementierung. Lege dort keine Secrets ab.

## Terminal öffnen

Öffne in einer gespeicherten Session mit verbundenem Computer **Control Center > Terminal**:

| Ansicht | Zweck |
| --- | --- |
| Agent-Ausgabe | Befehle des Agenten und gefilterte Liveausgabe sowie jüngste gespeicherte Ergebnisse |
| Terminal | Persönliche interaktive Shell im Home eines unterstützten Linux-Agents |

Klappe einen Shell-Aufruf im Chat auf, um dessen Befehl und gefilterte Liveausgabe direkt als schreibgeschützten Terminalblock zu sehen. Nach Abschluss bleibt das gespeicherte Ergebnis sichtbar. Neue Ausgabe scrollt mit, solange du nicht nach oben gescrollt hast. Beim erneuten Öffnen erscheinen die gespeicherten Standard- und Fehlerausgaben nacheinander, nicht zwingend in ihrer ursprünglichen zeitlichen Reihenfolge.

Enthält ein Abbruch- oder Fehlerergebnis keine gespeicherte Ausgabe, bleibt die bereits empfangene Liveausgabe vorerst sichtbar. Nach dem Neuladen oder Verlassen der Session lässt sie sich nicht wiederherstellen.

Deine eigenen Terminalbefehle laufen außerhalb der Modellrechte. **Strg+C** beendet den Vordergrundprozess deiner Shell; der Stoppknopf im Chat beendet den Agentenlauf. Das Schließen der Ansicht stoppt keinen Agentenbefehl. Abgeschlossene Wirkungen bleiben bestehen.

Neuladen, Zuklappen und Sessionwechsel erhalten die persönliche Shell. Der neueste berechtigte Viewer ersetzt den vorherigen. Tabentfernung, Shellende, Bindungsverlust oder Backend-/Agentneustart beenden sie. Persönliche Ausgabe ist nur ein begrenzter Arbeitsspeicherausschnitt; gespeicherte Agent-Ausgabe ist kein wiederhergestellter Prozess.

### Warum übernimmt der Agent mein `cd` nicht?

Jeder Agentenbefehl startet einen frischen Prozess im gewählten Sessionordner. Variablen, Aliase, Funktionen und `cd` werden weder zwischen Aufrufen noch aus deinem persönlichen Terminal übernommen. Wechsle den Session-Arbeitsordner ausdrücklich; zusammengehörige Shellschritte gehören in denselben Aufruf.

Unter Linux bleibt `HOME` das Benutzerverzeichnis des ausführenden Betriebssystemkontos, auch im Worktree und im persönlichen Terminal. `PWD` bezeichnet den Arbeitsordner. Programme können dadurch deine Benutzerkonfiguration verwenden, etwa die globale Git-Identität; im Container gilt das Benutzerverzeichnis des Containerkontos. Andere Host-Umgebungsvariablen werden nicht pauschal übernommen. Temporärdateien gehören weiterhin in den Session-Tempordner.

## Aktualisieren oder Probleme beheben

Updates und ältere Installationsstände behandelt [Aktualisierung und Upgrade-Hinweise](aktualisierung.md#computer-agents-aktualisieren). Bei fehlenden Funktionen prüfe Computerstatus, gewählten Sessioncomputer, Profilrechte und die gemeldeten Operationen. Ein Verbindungsverlust löst keinen automatischen Computerwechsel oder erneute Ausführung aus.
