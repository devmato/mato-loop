# MatoLoop aktualisieren

Diese Anleitung gilt für die Produktionsinstallation mit veröffentlichten Images. Arbeite im Verzeichnis deiner Produktionskonfiguration. Verwende für die Variante ohne Agent bei allen Compose-Befehlen zusätzlich `-f docker-compose.no-agent.yaml`.

## Update durchführen

Öffne im Benutzermenü unten links **Changelog**:

- **Seit dem letzten Update** zeigt die Release Notes zwischen der vorher installierten und der jetzt installierten Version. MatoLoop merkt sich diese Stände ab dem ersten Start mit dieser Funktion. Bis zum nächsten Update gibt es deshalb noch keinen Vergleich.
- **Noch nicht installiert** zeigt, was seit der jetzt installierten Version hinzugekommen ist. Vergleichsziel ist standardmäßig `develop`; du kannst auch die neueste offizielle Veröffentlichung wählen, sobald es eine gibt.

Die Ansicht lädt Release Notes von GitHub und installiert kein Update. Bei unbekannten oder lokal veränderten Builds ist kein genauer Vergleich möglich.

1. Prüfe den [Changelog](changelog.md) und die unten aufgeführten Upgrade-Hinweise für deinen Ausgangsstand.
2. Erstelle und prüfe eine [gemeinsame Sicherung](betrieb-und-datensicherung.md#zu-sichernde-daten) von Datenbank, Backenddateien, Schlüsseln, Agentdaten und Konfiguration. Probe die Wiederherstellung isoliert.
3. Setze in `.env` beide Imagevariablen auf denselben veröffentlichten festen `YYYY-MM-DD-<SHA8>`-Tag.
4. Lade und starte die Images:

    ```bash
    docker compose pull
    docker compose up -d
    docker compose ps
    docker compose logs --tail=100 mato-loop
    ```

5. Prüfe Anmeldung, bestehende Provider-Zugangsdaten, Sessiondateien und Versionen, Memory, Computer und Integrationen.

Beim Backendstart laufen Datenmigrationen automatisch. `develop` und `latest` sind veränderliche Tags, keine reproduzierbaren Update- oder Rollbackziele. CI veröffentlicht Images, aktualisiert aber deinen Server nicht.

### Weboberfläche neu laden

Nach einem Server-Update kann die installierte Web-App oben mittig melden: **Eine neue Version der Weboberfläche ist bereit.** Mit **Jetzt neu laden** übernimmst du die bereitstehende Weboberfläche; dadurch wird kein Update auf dem Server installiert. **Später** verschiebt die Meldung um eine Stunde.

**Neuerungen nach dem Neuladen anzeigen** ist standardmäßig aktiviert. Nach dem bewussten Neuladen öffnet sich der Changelog einmalig für die bisher und jetzt geladene Version, nicht für einen neueren Remote-Stand. Entferne das Häkchen, wenn du nur neu laden möchtest. Dafür müssen beide Web-Builds einen eindeutigen Commit-Stempel haben und der Tab-Speicher verfügbar sein. Normales Neuladen oder ein unveränderter Stand öffnet den Dialog nicht automatisch.

## Computer-Agents aktualisieren

Öffne **Einstellungen > Computer > Verwalten > Aktionen > Agent aktualisieren**. **Agents aktualisieren (N)** neben **Computer hinzufügen** kann mehrere verbundene, updatefähige Computer nach einer gemeinsamen Bestätigung starten. Der Wartungsstatus zeigt den tatsächlichen Abschluss; eine gestartete Aktualisierung ist noch kein Erfolg.

Die Aktualisierung verwendet den exakten Commit des laufenden Backends, auch wenn dieser älter ist. Dirty- oder unbekannte Backendstände liefern kein verwaltetes Installationsziel. Lokale Checkouts bleiben ein ausdrücklicher Entwicklungsweg und werden nicht durch verwaltete Updates verändert.

Alternativ verwendest du bei Docker `docker compose exec matoloop-agent matoloop-agent update`, bei nativen Agents `matoloop-agent update`. Ein Containerupdate überlebt Neustarts, aber die Neuerstellung des Containers stellt dessen Imageversion wieder her. Zugangsdaten und Workspaces bleiben in ihren Mounts.

!!! warning "Manuelle Updates unterbrechen Arbeit"
    Laufende MatoLoop-Aktionen auf dem Computer werden abgebrochen; Terminals, Browser und Browser-Setup werden geschlossen, auch aus anderen Chats. Ungespeicherte Eingaben können verloren gehen. Voraussetzungen und nötige sudo-Eingaben werden vorher geprüft. Bereits gestartete geschützte Dateischreibvorgänge dürfen abschließen; bei zu langer Wartezeit startet das Update nicht. Bei älteren Agents musst du laufende Vorgänge vorher selbst beenden.

Native Linux-Wartung kann eine geschützte sudo-Eingabe benötigen. Optional hinterlegst du unter **Computer > Verwalten > Einstellungen > sudo-Passwort** ein eigenes Secret aus **Variablen & Secrets**. Die Zuordnung gewährt weder sudo-Rechte noch Aktionsfreigaben; das Konto benötigt diese Rechte bereits. Entfernen der Zuordnung oder Entkoppeln löscht das Secret nicht. Windows verwendet den installierten Wartungstask; Docker und passwortloses sudo benötigen kein hinterlegtes Passwort.

macOS verwendet die verwaltete Benutzerinstallation ohne sudo-Passwort. Der Agent ist nur während der Benutzeranmeldung verfügbar; beachte die [macOS-Einrichtung](computer-und-terminal.md#unter-macos-mit-apple-silicon-installieren).

Wartungsergebnisse werden lokal gespeichert und bei vorübergehender Nichterreichbarkeit später gemeldet. Ohne bestätigten Abschluss bleibt das Ergebnis unbekannt. Prüfe Wartungsstatus und gemeldete Version, statt aus einer Verbindungstrennung auf Erfolg zu schließen.

### Automatische Agent-Updates

**Administration > Grundeinstellungen > Agents automatisch aktualisieren** setzt den Instanzstandard; er ist anfangs ausgeschaltet. Pro Computer wählst du unter **Automatische Agent-Updates** **Instanzstandard**, **An** oder **Aus**.

Die Prüfung erfolgt beim Verbinden und nach Änderungen der Einstellung oder Secret-Zuordnung. Automatische Updates unterbrechen keine laufende Arbeit. Beschäftigte Agents und fehlgeschlagene Updates werden erst beim nächsten solchen Ereignis oder manuell erneut versucht; es gibt keine zeitgesteuerten Wiederholungen.

## Hinweise für ältere Installationen

Diese Hinweise betreffen nur den Wechsel von den jeweils beschriebenen früheren Funktionen. Es sind keine zusätzlichen Schritte bei jedem Update. Prüfe nach dem Upgrade die wirksamen Rechte; ein Downgrade stellt entfernte Daten nicht automatisch wieder her.

### Schreibstil-Beispiele und mitgelieferte Skills

Bekannte alte Standardanweisungen von **writing-style** werden beim Backendstart aktualisiert, auch in persönlichen Fassungen mit eigenen Beispielen. Die Beispiele wandern unverändert aus `SKILL.md` nach `references/writing-examples.md`; ID, Eigentümer und Aktivierungsstatus bleiben erhalten. Die Übernahme bereinigt bestehende Beispiele nicht nachträglich.

Hast du die Anweisungen selbst geändert oder existiert bereits eine gleichnamige Beispieldatei, bleibt die Fassung unverändert. Prüfe sie unter **Einstellungen > Skills** beziehungsweise in der globalen Skill-Verwaltung und übernimm neue Regeln gezielt. Setze persönliche Pakete mit Beispielen nicht pauschal zurück: Zurücksetzen ersetzt das vollständige Paket und entfernt eigene Dateien. Sichere es vorher über **Paket herunterladen**.

### Freigaben und Profilzuordnungen

- Beim Wechsel zum gemeinsamen kategoriebasierten Freigabemodell werden vorhandene Freigaben zurückgesetzt; offene Agenten-Freigabeanfragen laufen ab. Bestätige benötigte Zugriffe erneut. Richtlinien, Presets und offene Memory-Vorschläge bleiben bei diesem Reset erhalten.
- Die Umstellung der Ordnernavigation setzt Navigationsfreigaben zurück. Wähle das Arbeitsverzeichnis ausdrücklich neu aus. Dieser separate Reset ändert keine Datei- oder Shell-Freigaben.
- Frühere Profilzuordnungen werden an Verbindungen übertragen, soweit sie denselben Eigentümerbereich betreffen. Bereits gespeicherte Verbindungsabweichungen gehen vor. Persönliche Zuordnungen zu globalen Verbindungen und veraltete Verweise werden entfernt; dadurch kann eine Verbindung verfügbar werden oder wegfallen. Downgrade stellt diese Zuordnungen nicht wieder her.
- Die Preset-Umstellung setzt gespeicherte Grundstandards auf privat **Komfortabel** und öffentlich **Ausgewogen**. Explizite Aktions- und Profilregeln bleiben erhalten. Vorhandene Dateisystem-Löschregeln werden für Session-Dateien übernommen, sofern dort keine eigene Regel besteht.
- Die getrennten Shell-Kategorien benötigen einen aktualisierten Agenten. Explizite Shell-Schreibregeln werden auch als Löschregeln übernommen, wenn keine eigene Löschregel besteht. Historische Freigaben werden nicht erweitert.

### MCP-Werkzeugnamen und Sperren

MCP-Werkzeuge erhalten lesbare Namen. Die Migration `c0d8c740723e` überträgt zuordenbare Deaktivierungen unter den alten technischen Namen auf die bestehenden Ressourcenregeln der jeweiligen Verbindung. Globale Sperren bleiben auch bei abweichenden Profilregeln gesperrt; Profilsperren werden einschließlich ihrer wirksamen Vererbung übernommen. Bei widersprüchlichen Ressourcenwerten bleibt die übertragene Sperre bestehen. Prüfe danach die **Berechtigungen** der betroffenen Verbindung und Profile.

Verbindungs-ID und ursprünglicher Werkzeugname bestimmen die Ressource; eine Umbenennung der Verbindung hebt ihre Sperren nicht auf. Historische Chat-Aufrufe bleiben unverändert. Nicht mehr zuordenbare alte Einstellungseinträge bleiben erhalten, sind aber keine Freigabe. Ein Downgrade behält die Ressourcenregeln und stellt die alten namensgebundenen Einstellungen nicht wieder her.

### Browserrechte und alte Websiteprofile

Die Umstellung auf **Browser verwenden** und das zusätzliche Python-Recht verwirft bisherige Browserregeln und Browserfreigaben; offene Browseranfragen laufen ab. Danach gelten die neuen Profilstandards. Integrationsaktivierung und andere Rechte bleiben erhalten. Prüfe insbesondere automatische Browserrechte für Build.

Frühere dauerhafte Browserprofile werden weder geladen noch automatisch gelöscht. Sie und ihre Backups können unverschlüsselte Websitecookies enthalten. Behandle sie vertraulich und bereinige nur ausdrücklich identifizierte Altbestände. Aktuelle Chat-Browser sind flüchtig.

### Modell-Ausgabelimits

Die einmalige Auto-Umstellung setzt bestehende Modell-Ausgabelimits auf **Auto**, auch ausdrücklich gewählte manuelle Werte. Es gibt keine interne Sicherung der alten Limits. Dokumentiere benötigte Obergrenzen vorher und setze sie nach dem Upgrade erneut. Ein Downgrade entfernt auch später eingetragene Limits, statt alte Werte wiederherzustellen. Kontext-, Reasoning- und andere Modellwerte bleiben bei dieser Migration erhalten.

### Websuche und Webabrufe

Persönliche SearXNG-Verbindungen und persönliche Suchanbieterwahl werden nicht mehr verwendet oder automatisch global übernommen. Jina-Verbindungen und die Anbieterwahl sind ebenfalls instanzweit. Alte Fallback-Werte werden ignoriert; der bisherige Primäranbieter bleibt erhalten. Richte benötigte Dienste als Administrator ein und prüfe die Auswahl unter **Tools > Einstellungen**.

Beim Wechsel zur alleinigen Tool-Konfiguration entfernt die Datenmigration alte Jina-/SearXNG-Verfügbarkeits- und Ladeabweichungen. Ausdrückliche Profilsperren werden als deaktiviertes `webfetch` beziehungsweise `web_search` in die Tool-Verfügbarkeit des Profils übernommen. Geerbte Sperren werden auch in abgeleiteten Profilen eingetragen, damit eigene Tool-Freigaben sie nicht aufheben. Das gilt konservativ auch für nicht ausgewählte Anbieterverbindungen und kann abgeleitete Profile stärker einschränken. Prüfe diese Tool-Einstellungen nach dem Update; die allgemeine Vorgabe „Integrationen nicht verfügbar“ wird nicht als Tool-Sperre übernommen. Ein Downgrade behält die übertragenen Tool-Sperren und stellt entfernte Anbieterabweichungen nicht wieder her.

### Windows-CLI nachrüsten

Führe bei alten Windows-Installationen den aktuellen Installationsbefehl aus **Einstellungen > Computer** einmal erneut aus. Deinstalliere oder bereinige vorher nichts: Kopplung und Workspace bleiben erhalten. Starte danach ein neues Terminal. Die CLI-Einrichtung erfolgt vor dem Versionswechsel und kann nach einem fehlgeschlagenen Update bestehen bleiben.

### Automationen und Aufnahmen

Früher verborgene Automationssessions bleiben mit IDs, Verlauf und Dateien als normale Chats erhalten. Alte Ausgabekopien bleiben historisch; neue Ergebnisse erscheinen nur in der Ausführungssession. Alte Konfigurations-/Lauflinks öffnen diese Session oder Nachricht. Ehemals archivierte Aufgaben werden deaktiviert; frühere Skillbindungen werden ausdrückliche Auftragsanweisungen.

Durch das frühere reine Transkriptionsverhalten verworfene Mikrofonaufnahmen werden nicht nachträglich wiederhergestellt. Alte Audio-Begleitdateien können noch von historischen Verweisen verwendet werden; lösche sie nicht pauschal. Aktuelle Audiokonvertierung arbeitet mit temporären Daten.

### Migration vom früheren Compose-Agenten

Ermittle das frühere Volume `<compose-projekt>_execution_agent_data` mit `docker volume ls`. Stoppe den alten Dienst und kopiere dessen vollständigen Inhalt mit Eigentümern und Rechten in den neuen Agent-Datenpfad. Prüfe insbesondere `agent-state/credential.json` (`0600`) und das private Elternverzeichnis (`0700`). Pairingzustand liegt im Credentialobjekt, nicht in `credential.pending.json`. Bei verlorenem Zugang entkopple und kopple erneut.

### Git-Aufträge mit vorherigem Commit

**Merge in Zielbranch** und **Pull Request erstellen** erlauben jetzt einen Aufgabencommit bei Bedarf ohne zusätzliches Häkchen. Prüfe die [aktuellen Git-Abläufe](git-und-worktrees.md#remote-einrichten-und-veroffentlichen), bevor du die Aktionen erneut startest. Der frühere führende Zusatz `--commit` bleibt für `/git-merge` und `/git-pr` lesbar, ist aber nicht mehr nötig. Bereits gesendete Auftrags-Prompts werden nicht nachträglich umgeschrieben.

### Entfernte Git-Integration {#removed-git-integration}

Vor dem ersten Neustart eines betroffenen alten Agents:

1. Stoppe ihn und entferne aus seiner Konfigurations-JSON ausschließlich die früheren Felder `gitExecutable` und `allowNetwork`, falls vorhanden. Auch `null` oder `false` sind keine gültigen Ersatzwerte.
2. Entferne ausschließlich `<config-parent>/git-home`, falls vorhanden, beispielsweise `MATOLOOP_AGENT_DATA/git-home`. Erhalte Elternverzeichnis, Zugang, Repositories, Workspaces und Verlauf.
3. Starte den neuen Agenten. Das Backend bereinigt alte Integrationssettings per Migration; lösche keine historischen Nachrichten, Tool-Ergebnisse oder Freigaben. Erhaltener Zugang braucht keine neue Kopplung.

Git bleibt als gewöhnlicher Shellbefehl auf dem verbundenen Computer verfügbar.

## Rollback

Setze beide Imagevariablen auf den vorherigen festen Tag, lade die Images und erstelle die Container neu.

!!! danger "Ein Imagewechsel ist kein Schema-Rollback"
    Ältere Images können mit dem bereits migrierten Schema unvereinbar sein. Stelle dann die passende gemeinsame Sicherung von Datenbank, Backenddateien und Schlüsseln nach dem [isolierten Wiederherstellungsverfahren](betrieb-und-datensicherung.md#wiederherstellung-und-probe) wieder her. Ohne Sicherung sind verworfene Einstellungen und Freigaben nicht rekonstruierbar.
