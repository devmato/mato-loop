# Mit Git und Worktrees arbeiten

Git läuft auf dem ausgewählten Computer mit dessen Git-Installation und Anmeldung. Es gibt keine eigene Git-Integration oder Backend-Clonefunktion. Die **Git**-App im Control Center zeigt Änderungen und verwaltet den Arbeitsbereich; Agentenaufträge verwenden die normalen Shell-Rechte.

## Ein Repository verwenden

1. Wähle einen verbundenen Computer und einen zugänglichen Arbeitsordner mit Git-Repository.
2. Bei neuen Projekt-Sessions wird **Git** automatisch zum Control Center hinzugefügt, sobald im gewählten Projekt-Arbeitsordner ein Repository erkannt wird. Die App öffnet sich dabei nicht. Öffne **Control Center > Git > Repository**, um das Repository anzusehen. Unter **Einstellungen** bietet **Git-Repository anlegen** ohne vorhandenes Repository einen Agentenauftrag zur Initialisierung; er legt weder Commit noch Remote an.
3. Für Projektvorgaben wähle in **Projekt bearbeiten** die Repository-Wurzel und speichere **Standard-Zielbranch** und **Standard-Arbeitsbereich**. Ein später angelegtes Repository wird beim erneuten Öffnen oder Prüfen erkannt.

Git-App und Befehlspalette allein erzeugen keinen Chat. Erst Sessionstart oder Agentenauftrag speichert die Unterhaltung.

## Änderungen ansehen

**Git > Änderungen** vergleicht HEAD mit dem aktuellen Arbeitsstand. Die Liste enthält staged und unstaged Änderungen sowie neue, nicht ignorierte Dateien des Repositorys oder Worktrees. Auch eigene, bereits vorhandene Änderungen zählen mit. Die Zahlen sind kein Änderungsprotokoll einzelner KI-Antworten.

Der Tab zeigt hinzugefügte Zeilen als grünes **+** und gelöschte Zeilen als rotes **−**, jeweils nur bei einer bekannten Anzahl größer null. Ohne solche Zeilenänderungen steht dort nur **Änderungen**; betroffene Dateien bleiben in der Liste sichtbar.

Ein Klick auf eine Datei öffnet den schreibgeschützten Diff im normalen Dateitab. Das Diff-Symbol schaltet zwischen Vergleich und Quelltext um. Ungespeicherte Entwürfe gehören nicht zum Git-Diff und bleiben beim Umschalten erhalten. Auf kleinen Flächen stehen die Vergleichsseiten untereinander.

Die Liste aktualisiert sich beim Öffnen und nach abgeschlossenen Läufen, solange die App sichtbar ist. Für externe Änderungen wähle **Aktualisieren**. Binärdateien, Submodule und Links haben keine Textvorschau; Textvergleiche sind auf 256 KiB je Seite begrenzt. Begrenzte Listen und fehlende Zähler werden gekennzeichnet. Git-Filterprogramme laufen bei dieser Ansicht nicht, daher kann sie von einem gefilterten Git-Diff abweichen.

## Einen getrennten Worktree starten

Worktrees stehen für Projekt-Sessions mit konfiguriertem Computer, Repository und einer Vorlage mit **Worktrees verwenden** zur Verfügung. **Coding** aktiviert die Option standardmäßig.

1. Wähle vor dem Sessionstart unter **Arbeitsbereich** **Neuer Worktree**.
2. Prüfe den lokalen Zielbranch. Er muss einen Commit besitzen.
3. Starte die Session. Existiert der gleichnamige Zielbranch auf dem ausgewählten Remote, ruft MatoLoop ihn zuerst ab und aktualisiert den lokalen Zielbranch per Fast-forward. Danach entsteht eine getrennte Arbeitskopie, zunächst ohne eigenen Branch (**Losgelöster Stand**, technisch Detached HEAD).

Bei Abruffehlern, uncommitteten Änderungen im Ziel-Checkout, auseinanderlaufenden Branches oder einem Zielbranch in einem anderen Worktree bricht die Erstellung ab. Es gibt keinen automatischen Stash oder Reset. Lokale Commits, die den Remote-Stand bereits enthalten, bleiben erhalten. Ohne ausgewählten Remote oder ohne gleichnamigen Branch dort verwendet MatoLoop den lokalen Stand. Bereits erstellte Worktrees werden nicht erneut aktualisiert.

Du und der Agent können in diesem Worktree normal arbeiten und committen. Für eine Veröffentlichung wählt der Agent einen passenden Remote-Branch; ein lokaler Branch ist auch für Pull Requests und lokale Merges nicht nötig. Uncommittete Änderungen bleiben im ursprünglichen Verzeichnis. Laufende Sessions desselben Projekts verhindern das Anlegen nicht. Bestehende Worktrees behalten ihre Branches. **Lokal** und die Auswahl eines bestehenden Worktrees erstellen keinen Branch.

!!! warning "Worktrees isolieren nicht die ganze Umgebung"
    Mehrere Sessions können denselben Worktree und damit Dateien und Änderungen teilen. Worktrees teilen Git-Metadaten; das ursprüngliche Repository muss erreichbar bleiben. `.env`-Dateien, Abhängigkeiten, Prozesse, Ports und Datenbanken werden nicht automatisch kopiert oder getrennt.

## Arbeitsbereich oder Branch wechseln

Unter **Git > Repository** findest du unterhalb der **Aktionen** die **Einstellungen**. Dort öffnen die Bearbeiten-Symbole die Dialoge für Arbeitsbereich, Branches und Remote. Die Arbeitsbereich- und Branchlisten kannst du auch während eines Auftrags durchsuchen und schließen. Markiere einen Arbeitsbereich und wähle **⋮ > Aktivieren**; bei Branches übernimmt **Wechseln** die Auswahl. Ein Wechsel verschiebt keine Dateien und kopiert keine Freigaben. Beende laufende Aufträge vor dem Wechsel. Speichere vor einem Branchwechsel die Editoränderungen im betroffenen Arbeitsbereich; ein neuer Branch ohne Checkout benötigt das nicht. Vor dem Wechsel des Session-Arbeitsbereichs müssen weiterhin alle Editorentwürfe gesichert sein.

**Geändert** kennzeichnet uncommittete Änderungen einschließlich neuer, nicht ignorierter Dateien. Prüfe bei Bedarf mit **Aktualisieren**; nicht prüfbar bedeutet nicht sauber. Sessionlinks zeigen andere Unterhaltungen am selben Arbeitspfad, reservieren aber keinen Worktree.

Ein Branchwechsel betrifft alle Sessions dieses Arbeitsbereichs. Belegte Branches können nicht nochmals ausgecheckt werden. Bei Blockern wird nichts automatisch committet, gestasht oder verworfen. Den Zielbranch änderst du separat; er ist eine Sessionvorgabe, nicht der aktuell ausgecheckte Branch.

Beim Wechsel von einem losgelösten Stand warnt die Git-App vor dem möglichen Verlust von Commits, die durch keinen Branch gesichert sind. Du kannst trotzdem bestätigen und wechseln. Möchtest du die Commits behalten, integriere sie vorher oder sichere sie mit **Branch erstellen** am aktuellen Stand.

## Remote einrichten und veröffentlichen

Unter **Git > Repository > Einstellungen** wählst du einen vorhandenen Remote oder beauftragst über **Einrichten/Bearbeiten** den Agenten, seine Adresse zu ändern. Verwende HTTPS-/SSH-Adressen ohne Zugangsdaten; richte die Anmeldung auf dem Computer ein.

Eine Remoteänderung betrifft alle Worktrees. Bestehende Sessions müssen das geänderte Ziel über **Ziel übernehmen** bestätigen. Ein nachträglicher Remote setzt den Worktree nicht zurück. Veröffentlichung, Pull Requests und Remote-Synchronisierung benötigen ihn, lokale Arbeit und Review nicht.

Unter **Git > Repository > Aktionen** findest du vier Hauptaktionen: **Prüfen** (`/git-review`), **Abschließen** (`/git-finish`), **Pushen** (`/git-push`) und **Pull Request** (`/git-pr`). Unter dem zunächst eingeklappten **Weitere Git-Aktionen** stehen **Commit**, die lokale Integration mit `/git-merge` und **Pull (Fast-forward)**. Repository und Remote richtest du weiterhin unter **Einstellungen** ein. Die [Git-Befehle](kanaele.md#coding-befehle) starten normale Agentenaufträge; die Befehlspalette setzt den Auftrag zunächst ein. Toolrechte gelten unabhängig davon. **Prüfen** berichtet nur und ändert keine Projektdateien. Ein positives Review ist kein Nachweis der Fehlerfreiheit; es prüft auch auf Overengineering.

Vor der Übernahme setzt der Agent eigene, unveröffentlichte Aufgabencommits per Rebase auf den Zielstand und prüft das kombinierte Ergebnis. Ist der Zielstand bereits enthalten, entfällt der Rebase. Veröffentlichte, geteilte, fremde oder Merge-Commits sowie ein unklarer Veröffentlichungsstand führen vor dem Umschreiben zu einer Rückfrage. Es gibt keinen automatischen Ersatz durch einen Merge oder Force-Push. Die Übernahme ins ursprüngliche Checkout bleibt ein Fast-forward; bestehende Zielcommits werden nicht umgeschrieben.

Die Aufträge bauen auf nachvollziehbaren Ergebnissen dieser Unterhaltung auf. Soweit die gewählte Aktion Reviews und Checks benötigt, verwendet der Agent gültige Ergebnisse weiter, solange sie den aktuellen Code, den Aufgabenumfang und die relevanten Prüfbedingungen abdecken. Ein Commit allein macht ein Review unveränderter Inhalte nicht ungültig. Nach Änderungen, Korrekturen oder dem Einarbeiten eines neueren Zielstands prüft er die betroffenen Bereiche erneut. Fehlen belastbare Ergebnisse, führt er die nötigen Prüfungen aus. Bereits erledigte Git-Schritte werden nicht pauschal wiederholt; Berechtigungen, aktuelle Zielprüfungen und offene Fehler bleiben verbindlich.

Die Git-Befehle bleiben auch ohne Remote, passenden Worktree oder verbundenen Computer
auswählbar. Der Agent prüft die Git-Voraussetzungen bei der Bearbeitung und erklärt fehlende
Voraussetzungen oder fragt nach. **Abschließen** funktioniert direkt im aktuellen Checkout,
wenn dort bereits der konfigurierte Zielbranch ausgecheckt ist: Nach nötigem Review, sicheren
Korrekturen innerhalb der Aufgabe, betroffenen Checks und Aufgabencommit ist Schluss, ohne
Integration, separaten Worktree oder Push. Andernfalls brauchst du dafür wie für Merge und Pull Request einen
separaten Worktree. Er darf auf einem losgelösten Stand stehen; ein vorhandener Arbeitsbranch muss sich vom Zielbranch unterscheiden. Fehlt ein passender Worktree, fordert dich
der Agent zur Auswahl in der Git-App auf; er wechselt den Arbeitsbereich nicht eigenmächtig.
Ist ein ausgewählter Computer nicht erreichbar, kann die normale Nachrichtenqueue den Versand
anhalten. Stelle die Verbindung wieder her und setze den Versand fort. Schreibgeschützte
Sessions und die Prüfungen für Zugriff, Arbeitsbereich und geänderte Publikationsziele bleiben geschützt.

**Pushen** prüft den aktuellen Stand: Ist der Worktree-Commit bereits vollständig im lokalen
Zielbranch enthalten, veröffentlicht der Agent den aktuellen Zielbranch. Dabei werden auch
zwischenzeitlich integrierte Commits anderer Aufgaben veröffentlicht. Andernfalls veröffentlicht
er den Worktree-Stand unter dem von dir genannten Branch, einer passenden bestehenden
Branch-/Upstream-Zuordnung oder dem eigenen, nicht technischen Branchnamen.
Bei einem losgelösten Stand oder technischen Worktree-Namen wie `worktree-77946357` fragt
er nach dem Veröffentlichungsbranch, sofern du ihn nicht bereits genannt hast. Du kannst
ausdrücklich auch die Veröffentlichung unter einem technischen Namen beauftragen.
Die Projektregeln gelten weiterhin. Für einen Pull Request veröffentlicht der Agent den geprüften
Commit unter dem gewählten Remote-Branch und verwendet diesen ausdrücklich als PR-Quelle.
Der lokale Branch oder losgelöste Stand bleibt erhalten, damit ein erneuter Versuch nicht am eigenen Branchwechsel scheitert.
Ein anderer Remote-Branchname erfordert keine Umbenennung eines bestehenden lokalen Branches.
Du darfst Worktree-Commits bereits vor einem Abschluss oder lokalen Merge veröffentlichen;
auch ein vorhandener Pull Request ist dafür nicht nötig. **Pushen** lässt uncommittete Änderungen
unberührt und führt keinen Commit, Fetch, Merge, Rebase, Reset oder Force-Push aus.

**Abschließen**, die lokale Integration mit `/git-merge` und **Pull Request** erlauben nötige Commits eindeutig zugehöriger Aufgabenänderungen im Session-Checkout. Dafür ist kein Häkchen nötig. Sind die Änderungen bereits committet, fährt der Agent ohne weiteren Aufgabencommit fort. Bei unklarer Auswahl fragt er nach; fremde Änderungen, bestehende fremde Staging-Auswahlen und Secrets dürfen nicht mitcommittet werden. Die aktuellen Repository-Anweisungen und referenzierten Richtlinien gelten für Branches, Commit-Nachrichten einschließlich Sprache und Trailern, Integration, Veröffentlichung und nötige Prüfungen. Bei widersprüchlichen Vorgaben fragt der Agent nach.

**Pull Request** arbeitet im getrennten Quell-Worktree: Der Agent übernimmt bei Bedarf den abgefragten Remote-Zielstand, prüft die Änderungen, pusht den geprüften Commit normal auf den gewählten Veröffentlichungsbranch und erstellt oder aktualisiert den vorhandenen PR. Bei Konflikten, notwendigen Review-Befunden oder fehlgeschlagenen Checks stoppt er ohne Reparatur. Der lokale Zielbranch bleibt unverändert; der PR wird nicht automatisch gemergt. Verwende für die spätere Übernahme beim Hoster Squash oder Rebase statt eines Merge-Commits.

Du kannst im Auftrag ausdrücklich nur vorhandene Commits freigeben, beispielsweise `/git-merge nur vorhandene Commits übernehmen`. Diese Einschränkung geht vor. Verhindern verbleibende Änderungen einen sauberen Worktree, stoppt der Agent, statt sie zu committen oder zu verwerfen. **Prüfen** bleibt schreibgeschützt; **Pushen**, **Pull (Fast-forward)** und die Einrichtung von Repository oder Remote erstellen keine Commits.

Die Rückmeldung beginnt mit Aktion und Ergebnis. Darunter nennt der Agent nur relevante
Details wie Commit, Zielbranch, PR-Link, ausgeführte Prüfungen oder offene Punkte.
Zu jedem als Ergebnis genannten Commit zeigt er neben dem Hash die vollständige gespeicherte
Commit-Nachricht aus Git: Titel, gegebenenfalls Beschreibung und Trailer wie `Release-Note`.
Der Text bleibt unverändert, auch wenn eine spätere Integration blockiert ist.
Reviews trennen notwendige Korrekturen von optionalen Verbesserungen und nummerieren
Befunde, damit du gezielt darauf verweisen kannst. Die Antwort folgt deiner
[Sprachvorgabe](webrecherche-und-quellen.md#webseiten-abrufen); Git-Bezeichner und
zitierte Commit-Titel bleiben unverändert. Bei Fehlern muss die Antwort zwischen bereits
abgeschlossenen Schritten und dem noch offenen Teil unterscheiden.

**Prüfen**, **Abschließen** und die Prüfungen bei Merge oder Pull Request folgen denselben
Kriterien: Fehler, Sicherheit und Datenerhalt, parallele Nutzung und erwartete Last,
fehlende Tests für riskante Logik, relevante Geschwindigkeitsprobleme und unnötige Komplexität.
Ein Befund braucht einen konkreten Nachteil und die kleinste vollständige Korrektur.
Vorhandene Lösungen wiederverwenden und unnötigen Code entfernen geht vor neuen Abstraktionen;
notwendige Schutzprüfungen bleiben erhalten. Optionale Vereinfachungen blockieren keinen Merge
und erlauben keine zusätzlichen Umbauten.

### Nach einer Umsetzung automatisch abschließen

1. Sende deinen Umsetzungsauftrag in **Build**.
2. Aktiviere unter **Git > Repository > Einstellungen** die Option **Abschluss einplanen** neben **Projektvorgaben** (bei wenig Platz darüber). Sie ist lokal und im Worktree verfügbar. Der Haken plant sofort die normale Nachricht `/git-finish` ein, genau wie eine manuelle Eingabe. Ohne laufende Antwort kann der Auftrag sofort starten.
3. Entferne den Haken, wenn du die noch nicht gesendete Nachricht wieder entfernen möchtest. Dafür muss der Computer nicht verbunden sein. Auch das normale Entfernen der Nachricht im Chat entfernt den Haken. Nach dem Senden zeigt der Chat den tatsächlich übermittelten Prompt statt des Kurzbefehls.

Der Auftrag ergänzt nur fehlende oder veraltete Review- und Check-Ergebnisse, behebt notwendige Befunde sicher innerhalb der Aufgabe und prüft betroffene Bereiche erneut. Gültige Vorarbeiten verwendet er wie die Einzelaktionen weiter. Erst ohne notwendige Korrekturen, fehlgeschlagene Checks oder fehlende Entscheidungen darf er Aufgabenänderungen committen. Auf dem konfigurierten Zielbranch endet der Abschluss damit lokal. Andernfalls folgt die getrennte Worktree-Integration per Rebase nur bei Bedarf und Fast-forward ins ursprüngliche Checkout. Neue Merge-Commits entstehen nicht. Fragen und reine Analysen berechtigen nicht zu diesen Git-Schritten.

Du kannst `/git-finish` auch selbst über die Befehlsauswahl oder **Abschließen** starten. Die Git-Aktionsbuttons reihen normale Befehlsnachrichten ein, auch während einer laufenden Antwort. Dein Entwurf und seine Anhänge bleiben dabei erhalten. Fehlende Voraussetzungen werden bei der jeweiligen Aktion erklärt; Berechtigungen und Prüfungen des Git-Zustands gelten weiterhin.

Der Haken verwendet die [gespeicherte Nachrichtenqueue](chat-und-sessions.md#wahrend-einer-antwort-eingreifen), keinen besonderen Git-Schrittautomaten und keine Vormerkung für eine spätere Build-Nachricht. Auch nach einem abgebrochenen oder fehlgeschlagenen Auftrag kann die normale Queue den Abschluss starten. Entferne die vorgemerkte Nachricht, wenn du das nicht möchtest. Änderungen an Computer, Arbeitsbereich, Branch oder Ziel werden vor dem Start erneut geprüft.

Klare Codekonflikte und Integrationsfehler behebt der Agent im eigenen Worktree und prüft erneut. Sind die beabsichtigten Änderungen nicht eindeutig vereinbar, erklärt er kurz das Problem und fragt mit Optionen und Empfehlung nach. Relevante Sessions desselben Projekts darf er dafür gezielt lesen; gespeicherte Memories und projektfremde Unterhaltungen bleiben an einen ausdrücklichen Abrufauftrag gebunden. Ungespeicherte Editoränderungen musst du vorher sichern. Es gibt keinen automatischen Push, Pull Request oder Worktree-Abbau. Die Prüfungen sind Agentenarbeit, kein unabhängiger Nachweis der Fehlerfreiheit.

Bei der getrennten lokalen Integration kann der Agent den committeten Zielbranch in seinen sauberen Worktree einarbeiten und dort prüfen,
während im ursprünglichen Verzeichnis noch uncommittete Änderungen liegen oder ein anderer Branch
ausgecheckt ist. Dieses Verzeichnis bleibt dabei unverändert. Erst direkt vor der Rückintegration
müssen beide Arbeitsbäume sauber und das ursprüngliche Verzeichnis auf dem Zielbranch sein.
Ist der Zielbranch inzwischen weitergelaufen, übernimmt er die neuen Commits und prüft die betroffenen Integrationsbereiche erneut. Er wechselt keine Branches automatisch und verschiebt keine Benutzerarbeit.

Vor einer Rückfrage wegen des ursprünglichen Verzeichnisses prüft der Agent aktuelle Tool-Hinweise
auf laufende Arbeit oder vorübergehende Git-Sperren. Sind diese bestätigt, soll er warten, später
erneut prüfen und selbstständig fortfahren; unabhängige erlaubte Schritte erledigt er inzwischen.
Geänderte Dateien allein belegen keine laufende Arbeit. Fremde Änderungen werden nicht automatisch
committet, gestasht, zurückgesetzt oder verworfen. Nur notwendige Entscheidungen oder Eingriffe
führen zu einer Rückfrage; mit dem normalen Stoppknopf brichst du ab. Es gibt keine zusätzliche
Merge-Warteschlange, automatische Erkennung externer Agenten oder garantierte Sperre gegen externe
Schreibzugriffe. Nach einem Backend-Neustart werden unterbrochene Läufe als fehlgeschlagen gemeldet;
prüfe vor einem neuen Versuch den tatsächlichen Git-Zustand.

**Pull (Fast-forward)** holt den Remote-Zielbranch ins ursprüngliche Projektverzeichnis. Es übernimmt keine Änderungen aus dem Session-Worktree und stoppt bei lokalen Änderungen oder wenn kein Fast-forward möglich ist.

Sessions reservieren weder Arbeitsbereiche noch gemeinsame Git-Metadaten. Eine andere laufende oder wartende Session blockiert deshalb keine neuen Chats oder Git-Aufträge. Der Agent muss konkrete Konflikte anhand des aktuellen Zustands erkennen und behandeln. Parallele Änderungen im selben Arbeitsbereich können sich trotzdem beeinflussen; nutze für unabhängige Aufgaben getrennte Worktrees.

## Worktree entfernen

Entferne einen Worktree im Arbeitsbereich-Dialog der Git-App oder aktiviere beim Löschen einer Projekt-Session **Worktree ebenfalls entfernen**. Die Option ist zunächst aus und erscheint nur, wenn der Worktree auf dem verbundenen Computer innerhalb von drei Sekunden geprüft werden kann. Ohne diese Auswahl und beim Archivieren bleibt er erhalten. Scheitert die Entfernung, wird die Session nicht gelöscht.

Fehlen Löschrechte in einem Verzeichnis, ist **Worktree ebenfalls entfernen** im Löschdialog deaktiviert; der Grund nennt den betroffenen Pfad. Die Session kannst du ohne Worktree entfernen. Vor der tatsächlichen Entfernung werden die Rechte erneut geprüft. Prüfe Zugriffsrechte und Eigentümer; MatoLoop ändert keine Rechte automatisch. Gleichzeitige Dateiänderungen können trotz Vorprüfung zu einem späteren Fehler führen.

Vorhandene Branches bleiben erhalten; andere betroffene Sessions wechseln nach Prüfung ins lokale Projektverzeichnis. Der Löschdialog zeigt diese Sessions an. Hat sich die Zuordnung vor dem Löschen geändert, bricht die Entfernung ab; öffne den Dialog dann erneut. Aktive Aufträge mit überlappendem Arbeitspfad und gesperrte Worktrees verhindern die Entfernung; Aufträge in anderen Worktrees desselben Repositorys nicht. Auch eingereihte oder auf Freigabe beziehungsweise Antwort wartende Aufträge gelten als aktiv. Ein losgelöster Stand verhindert die bestätigte Entfernung nicht; ein vorheriger Merge oder Sicherungsbranch ist nicht vorgeschrieben.

!!! danger "Lokale Dateien werden unwiderruflich gelöscht"
    Beim Entfernen gehen uncommittete Änderungen sowie zusätzliche und ignorierte Dateien wie `.env` verloren. Commits, die durch keinen Branch gesichert sind, können danach verloren gehen. Prüfe und sichere alles, was du behalten möchtest, vor der Bestätigung: integriere die Commits oder lege am aktuellen Stand einen Branch an. Es gibt keinen automatischen Commit, Stash oder Sicherungsbranch. Auch ein späterer Fehler beim Löschen der Session stellt den entfernten Worktree nicht wieder her. Es gibt keine automatische Worktree-Bereinigung.
