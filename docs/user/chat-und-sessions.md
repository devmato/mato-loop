# Chat und Sessions

## Neue und vorhandene Sessions

Die Startseite und eine leere neue Unterhaltung sind zunächst Browserentwürfe. Eine erste Nachricht oder das Öffnen einer sessiongebundenen Control-Center-App legt die Session samt internem Speicher an. Die feste Session-Einstellungsansicht, die Git-Ansicht und globale Benachrichtigungen allein erzeugen keine Session.

Sessions können angepinnt, umbenannt, zwischen Projekten verschoben, archiviert und gelöscht werden. Projekte gruppieren Sessions, teilen aber nicht deren Dateiablage. Ein Projekt kann Sessionvorlage, Computer und Arbeitsordner als Vorgaben für neue Sessions speichern; spätere Projektänderungen schreiben bestehende Sessions nicht um. Beim Projektwechsel wird ein Entwurf nur in einen leeren Zielentwurf übernommen, nie über vorhandene Inhalte oder eine Warteschlange geschrieben.

## Session-Einstellungen

| Einstellung | Bedeutung |
| --- | --- |
| Sessionvorlage | Verfügbare Agenten, Startagent, Memory, Nachrichtengröße und optionale Modell-/Reasoning-Vorgaben |
| Agent | Arbeitsweise, Tool-Rechte und Integrationsauswahl |
| Modell und Reasoning | Effektive Modellwahl und unterstützte Reasoning-Stufe |
| Memory | Voll, nur lesen oder deaktiviert |
| Websuche | Erzwingen, verfügbar oder deaktiviert |
| Computer und Arbeitsordner | Optionaler lokaler Ausführungsort |

Unter **Control Center > Session** ändern Sie die Sessionwerte. Die Vorlagen **Allgemein** und **Coding** bieten Allgemein/Sysadmin bzw. Plan/Build. Eine Vorlagenauswahl ersetzt Agentenauswahl, Memory, Nachrichtengröße und Modell-/Reasoning-Vorgaben; Projekt, Computer, Arbeitsordner und Websuche bleiben unabhängig. Spätere Änderungen einer gespeicherten Vorlage wirken nicht rückwirkend.

Modell und Reasoning gehören nicht zum Agentenprofil. Ein agentenspezifischer Sessionwert hat Vorrang vor dem sessionweiten Wert, dann persönlichen und globalen **Standardmodellen**; beim Reasoning folgt zuletzt der Modellstandard. Nicht verfügbare Modellreferenzen werden bei der Auflösung übersprungen. Ohne ausdrückliches Agentmodell kann das erste verfügbare toolfähige Chatmodell dienen. Hilfsmodelle verwenden Standardmodelle und ihre jeweiligen Fallbacks.

Plan und Build brauchen einen verbundenen Computer und zugänglichen Arbeitsordner. Allgemeiner Chat und Backend-Tools funktionieren ohne Computer. Fehlende Laufvoraussetzungen erscheinen als gespeicherte Host-Antwort, die Sie nach einer Korrektur erneut versuchen können.

### Befehle

Geben Sie `/` ins Eingabefeld ein. Die Befehlspalette
erscheint oberhalb des Composers. Tippen filtert die Einträge; Pfeiltasten wechseln
die Auswahl. Ein Klick, Enter oder Tab setzt `/befehl ` ins Eingabefeld. Ergänzen Sie
Argumente und senden Sie anschließend ab, um den Auftrag zu starten. Escape schließt
die Palette. Vorhandene Argumente und Anhänge bleiben bei der Auswahl erhalten.
Die Palette ist allgemein; Git-Befehle werden passend zum gewählten Workspace angeboten.
Nicht verfügbare Aktionen erklären ihre Voraussetzungen.
Nach dem Absenden zeigt die Benutzernachricht den aufgelösten Befehls-Prompt samt
Argumenten. Dieser Text lässt sich wie gewöhnlicher Nachrichtentext markieren und
kopieren. Rückgängig stellt den ursprünglichen Befehlsaufruf wieder her. Ältere
Befehlsnachrichten ohne gespeicherten Prompt zeigen weiterhin ihr Kürzel.

### Git-Projekte und Worktrees

**Projekt bearbeiten** prüft das ausgewählte Verzeichnis auf ein Git-Repository.
Auch ein nachträglich angelegtes Repository wird beim erneuten Öffnen oder über
die Schaltfläche zum erneuten Prüfen erkannt. Bei einem Repository speichern Sie
**Standard-Zielbranch** und **Standard-Arbeitsbereich**. Bereits gespeicherte Werte werden nicht durch erkannte
Vorschläge überschrieben. Verwenden Sie die Repository-Wurzel als Projektverzeichnis.

Vor dem Sessionstart steht über dem Eingabefeld **Arbeitsbereich** mit **Nicht verfügbar**,
**Lokal** oder **Worktree**. Ohne Computer oder Workspace ist der Arbeitsbereich
nicht verfügbar. Ein ausgewählter Offline-Computer ändert den angezeigten Modus nicht.
Bei neuen Projekt-Sessions wählen Sie vor dem Start **Lokal** oder **Worktree**;
die Projektvorgabe liefert die Vorauswahl. Worktrees benötigen den im Projekt
eingerichteten Computer und Repositorypfad. Nach dem Start ist der Modus fest; das Feld
verschwindet aus dem Chat. Die Informationen bleiben unter **Control Center > Git > Einstellungen** sichtbar.
Sessionvorlagen enthalten keinen Git-Schalter.
Lokal bleibt einschließlich eigener Änderungen alles im vorhandenen Verzeichnis.
Beim Sessionstart mit Worktree verwendet MatoLoop ohne Remote den letzten Commit des lokalen
Zielbranches. Der Branch muss bereits einen Commit enthalten; uncommittete Änderungen
bleiben im ursprünglichen Verzeichnis. Mit ausgewähltem Remote wird der Zielbranch
frisch vom Server abgerufen; ein fehlgeschlagener Abruf fällt nicht auf den lokalen
Stand zurück. Für einen anderen Arbeitsbereich starten Sie eine neue Session.

Die Git-Einstellungen zeigen Arbeitsbereich, kopierbaren Pfad, Branch, Zielbranch und Remote. **Änderungen** enthält
die Dateiliste und Agentenaufträge; ein Klick auf eine Datei öffnet ihren Diff im Editor.
Ein eindeutiger Remote oder `origin`
wird vorgeschlagen. Über **Einrichten** beziehungsweise **Bearbeiten** wählen Sie
einen vorhandenen Remote oder beauftragen den Agenten, seine Adresse hinzuzufügen
oder zu ändern. Verwenden Sie eine HTTPS-/SSH-Adresse ohne Zugangsdaten; die Anmeldung
wird auf dem Computer eingerichtet. Eine Adressänderung betrifft alle Worktrees des
Repositorys. Bestehende Sessions müssen das geänderte Ziel vor weiteren Git-Aufträgen
über **Ziel übernehmen** ausdrücklich bestätigen. Projektvorgaben werden dabei nur
für den zugehörigen Computer und Repositorypfad aktualisiert.

Ohne Repository bietet die Git-App **Git-Repository anlegen** (`/git-init`). Der
Agent initialisiert nur den gewählten Workspace und legt weder Commit noch Remote
an. Die Git-Ansicht lässt sich im Browserentwurf öffnen, ohne eine Session oder einen
Worktree anzulegen. Erst ein Agentenauftrag oder der Sessionstart erzeugt sie;
Repository- und Remote-Einrichtung starten zunächst lokal.

Ein Worktree benötigt keinen Remote. Erst Veröffentlichung, Pull Requests und
Remote-Synchronisierung benötigen ihn. Einen später hinzugefügten Remote übernehmen
Sie in der Git-App ausdrücklich als Sessionziel; der vorhandene Worktree bleibt erhalten.
Die Vorbereitung mit Remote verwendet Netzwerkadressen. Lokale Pfad- und
`file://`-Remotes benötigen die normalen Shell-Tools mit deren Berechtigungen.
Git-Hooks und Checkout-Filter werden bei der Vorbereitung nicht ausgeführt.
Unvollständige oder nachträglich vertauschte Arbeitskopien werden nicht automatisch
übernommen oder zurückgesetzt; prüfen Sie solche Fälle vor einem erneuten Start.

Die Arbeitskopie liegt dauerhaft unter
`<Agent-Datenverzeichnis>/worktrees/<Projekt-ID>/<Session-ID>`.
Computer, Verzeichnis und Branch dieser Arbeitskopie bleiben der Session zugeordnet.
Ein Vorlagenwechsel verschiebt sie nicht. Das ursprüngliche Repository muss erreichbar
bleiben; Worktrees teilen dessen Git-Metadaten. Lokale `.env`-Dateien, Abhängigkeiten,
laufende Dienste und Datenbanken werden nicht automatisch kopiert oder getrennt.
Archivieren entfernt den Worktree nicht. Beim Löschen einer Session können Sie im
Bestätigungsdialog **Worktree ebenfalls entfernen** auswählen. Die Checkbox ist
zunächst nicht angehakt. Ohne Auswahl bleibt der Worktree bestehen und kann später
manuell mit Git entfernt werden. Branch und Commits bleiben immer erhalten.
Beim Entfernen werden nicht committete Änderungen sowie zusätzliche und ignorierte
Dateien wie `.env` unwiderruflich gelöscht. Der Bestätigungsdialog warnt ausdrücklich
davor. Bei einem Fehler bleibt die Session erhalten. Andere Sessions und
laufende Aufträge dürfen den Worktree nicht mehr verwenden. Es gibt keine separate
Worktree-Verwaltung und keine automatische Löschung.
Ohne Remote muss der lokale Zielbranch mindestens einen Commit enthalten; fehlt er,
ist die Worktree-Option vor dem Sessionstart deaktiviert.

Die allgemeine Befehlspalette und **Git** bieten die
[Git-Befehle](kanaele.md#coding-befehle) für Review,
Übernahme, PR, Synchronisierung und Einrichtung mit den vorhandenen Tools und Rechten.
Die Palette setzt den Befehl zunächst ein; die Aktionsliste in der Git-App startet
den gewählten Auftrag direkt und erhält den vorhandenen Nachrichtenentwurf samt Anhängen. Merge-Review und
Tests sind Teil des Agentenauftrags; ein positives Review garantiert keine Fehlerfreiheit.

## Laufende Antworten

MatoLoop zeigt Antworttext, aktuelle Aktivität, Tool-Ergebnisse, Laufzeit und gegebenenfalls Reasoning-Zusammenfassungen. Diese Zusammenfassungen sind keine vollständigen internen Gedankengänge. Bei Verbindungsunterbrechung lädt die Oberfläche den gespeicherten Zustand nach; ein Browserdisconnect stoppt den Agenten nicht. Der Stoppknopf beendet den Lauf, ohne abgeschlossene Wirkungen rückgängig zu machen.

### Während eines Laufs senden

**Jetzt senden** ergänzt den aktuellen Lauf um Ihre neue Nachricht. Eine laufende Modellanfrage wird neu aufgebaut; aktive Tool-Arbeit darf zuerst enden. Eine überholte Freigabe oder Rückfrage des Laufs wird geschlossen und der Agent plant mit Ihrer neuen Anweisung weiter. Das erlaubt keine zuvor gesperrte Aktion.

**Nach dem Lauf senden** reiht die Nachricht für einen anschließenden Lauf ein. Der ownergebundene Browser-Postausgang bewahrt eingereihte Nachrichten und Anhänge in Reihenfolge. Fehlgeschlagene Sendungen bleiben für einen erneuten Versuch oder die Übernahme in den Entwurf erhalten. Abmelden oder ein Kontowechsel entfernt den bisherigen Postausgang dieses Browsers.

Bei **Jetzt senden** werden auch Subagenten beendet, die auf eine Freigabe oder Rückfrage warten. Bereits laufende Arbeit darf zuerst enden. Danach setzt der Hauptagent mit Ihrer neuen Nachricht fort; eine spätere Rückfrage aus der alten delegierten Arbeit blockiert ihn nicht erneut.

Der laufende Auftrag behält sein Startprofil und seinen Modell-/Vertrauenskontext. Neue Profilauswahlen gelten für spätere Läufe. Für Details zu wirksamen Rechten siehe [Freigaben](freigaben-und-datenfreigabe.md).

### Aufgaben und Subagenten

**Agent-Coding** im Control Center bündelt Einstiege zu Git, Ablage, Computer-Dateien, Terminal, Browser und OpenCode, soweit verfügbar. Die App öffnet bestehende Werkzeuge; sie gewährt keine zusätzlichen Rechte und erzeugt keine automatische Workspace-Isolation.

Geeignete Profile können eine Aufgabenliste oberhalb des Eingabefelds anzeigen. Sie lässt sich zuklappen; der Status dokumentiert gemeldeten Fortschritt und ist kein unabhängiger Erfolgsnachweis.

Delegierte Aufgaben erscheinen mit anklickbarem Titel und Status in der Aktivität. Der Subagenteninspektor zeigt deren eigenen Verlauf schreibgeschützt. Fragen und Freigaben beantworten Sie in der Hauptsession. Kinder teilen den Computer-Arbeitsbereich; unterschiedliche Sessions bieten dort keine Dateisolation. Ein Stopp des Elternlaufs stoppt auch offene delegierte Arbeit.

## Rückfragen

Eine strukturierte Rückfrage erscheint direkt im Chat beim zugehörigen Tool-Aufruf. Das Eingabefeld bleibt verfügbar: Mit **Jetzt senden** können Sie nachfragen oder die Richtung ändern, ohne vorher **Fertig** oder **Ablehnen** zu wählen. Die offene Rückfrage des Laufs wird dabei ohne Übernahme unfertiger Antworten geschlossen; falls nötig, fragt der Agent erneut. **Weiter** navigiert; **Fertig** sendet den bisherigen Antwortstand auch vor der letzten Frage. Fragen dürfen unbeantwortet bleiben. **Ablehnen** verwirft den Entwurf. Geschützte Felder behandeln ausgelassene Werte ausdrücklich als fehlend; Geheimnisse gehören weiterhin nur in diese Felder, nicht in Chatnachrichten. Siehe [Variablen und Secrets](variablen-und-secrets.md).

## Nachrichtenaktionen

- **Erneut versuchen** startet für die letzte fehlgeschlagene Host-Antwort einen neuen Versuch. Vor-Ausführungsfehler werden mit aktuellen Sessionvorgaben erneut geprüft; bereits gestartete Läufe behalten ihren Retry-Kontext.
- **Rückgängig** entfernt eine Benutzernachricht und den folgenden Verlauf, nachdem Text und Anhänge wieder in den Entwurf übernommen wurden.
- **Kopieren** übernimmt Nachrichteninhalte in die Zwischenablage.

!!! warning "Externe Wirkungen bleiben bestehen"
    Rückgängig, Retry und Stopp machen bereits ausgeführte Datei-, Integrations-, Freigabe- oder Memory-Änderungen nicht rückgängig.

## Darstellung

Antworten unterstützen Markdown, Tabellen, Code, Mathematik und Mermaid. Codeblöcke lassen sich kopieren oder herunterladen; fertige Mermaid-Diagramme als SVG herunterladen, als Quelle kopieren oder vergrößern. Die Quelle bleibt in der Nachricht, ohne zusätzliche Datei. Externe Bilder benötigen eine Zustimmung je Ursprung und Session. **Kleine Nachrichten** aus der Sessionvorlage bzw. Sessionansicht verdichtet die Darstellung, ohne den Inhalt zu kürzen.
