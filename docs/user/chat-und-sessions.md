# Chats und Sessions organisieren

Eine Session ist eine gespeicherte Unterhaltung mit eigenen Einstellungen, Dateien und Verlauf. Projekte gruppieren Sessions, teilen aber nicht deren Dateiablage.

## Eine Session starten und organisieren

**Chat** öffnet deinen festen persönlichen Gesprächsfaden. Er ist eine normale Session mit denselben Modellvorgaben, Freigaben, Kontextlimits und derselben Kompaktierung wie andere Sessions. Das erste Öffnen legt ihn an, startet aber weder einen Modelllauf noch die proaktive Assistenz. Dreaming-Vorschläge erscheinen hier als Freigabekarten; ihre Belegquellen bleiben getrennt erhalten.

Für ein eigenes Thema öffne eine **neue Session**, wähle die Startvorgaben und sende eine Nachricht. Erst das Senden oder Öffnen einer sessiongebundenen Control-Center-App speichert diese neue Session. Explizit ausgewählte Sessions und Projektentwürfe werden nicht durch den persönlichen Chat ersetzt.

Deine Nachricht erscheint direkt nach dem Absenden im Chat. **Wird gesendet …** bleibt sichtbar, während der Browser sie lokal sichert, Anhänge hochlädt und auf die Serverbestätigung wartet. Der Platz für den Antwortstatus wird dabei bereits freigehalten; **Verarbeite Anfrage** erscheint erst nach der Serverbestätigung. Schlägt die lokale Sicherung fehl, bleiben Text und Anhänge im Entwurf erhalten.

Über das Sessionmenü kannst du anpinnen, umbenennen, einem Projekt zuordnen, archivieren oder löschen. Projektvorgaben für Vorlage, Computer und Arbeitsordner gelten für neue Sessions, nicht rückwirkend. Ein Projektwechsel übernimmt einen Entwurf nur in ein leeres Ziel, niemals über dortige Eingaben oder Warteschlangen.

Ziehe Projekte in der Seitenleiste mit gedrückter Maustaste an die gewünschte Position. Auf Touch hältst du den Projektnamen kurz gedrückt und verschiebst ihn dann. Eine Linie zeigt die Einfügeposition. Die Reihenfolge bleibt für dein Konto auch nach dem Neuladen und auf anderen Geräten gespeichert; normales Wischen scrollt weiterhin die Liste.

## Session-Einstellungen ändern

Unter **Control Center > Session** bearbeitest du die Vorgaben; Agent, Modell und Reasoning sind auch am Eingabefeld auswählbar.

| Einstellung | Zweck |
| --- | --- |
| Sessionvorlage | Verfügbare Agenten, Startagent, Memory, Nachrichtengröße, Worktree-Unterstützung und optionale Modellvorgaben |
| Agent | Arbeitsweise, Tool-Rechte und Integrationsauswahl |
| Modell und Reasoning | Modell und unterstützte Reasoning-Stufe |
| Memory | Voll, nur lesen oder deaktiviert |
| Websuche | Erzwingen, verfügbar oder deaktiviert |
| Computer und Arbeitsordner | Optionaler lokaler Ausführungsort |

**Allgemein** bietet Allgemein und Sysadmin, **Coding** Plan und Build. Plan und Build benötigen einen verbundenen Computer mit zugänglichem Arbeitsordner. Eine Vorlagenauswahl ersetzt deren Sessionwerte, aber nicht Projekt, Computer, Arbeitsordner oder Websuche. Spätere Vorlagenänderungen wirken nicht rückwirkend.

Modellvorgaben gehören zu [Standardmodellen und Sessionvorlagen](provider-und-modelle.md#standardmodelle-und-sessionvorlagen), nicht ins Agentenprofil. Ein laufender Auftrag behält sein Startprofil und seinen Modellkontext; Änderungen gelten für spätere Läufe. Instanzweite Verfügbarkeit und Rechte bleiben wirksam.

## Befehle verwenden

Gib `/` ins Eingabefeld ein. Tippen filtert die Befehlspalette; Pfeiltasten, Klick, Enter oder Tab wählen einen Eintrag. Ergänze Argumente und sende anschließend den Befehl. Auswahl allein startet nichts; Escape schließt die Palette. Vorhandene Argumente und Anhänge bleiben erhalten.

Die Benutzernachricht zeigt den aufgelösten Auftrag. Rückgängig stellt den ursprünglichen Befehlsaufruf wieder her. Nicht verfügbare Aktionen nennen ihre Voraussetzungen.

### Eine Unterhaltung kopieren

Mit `/fork` kopierst du eine gespeicherte Session und schreibst unabhängig weiter. Das funktioniert auch während eines laufenden Auftrags: Die Kopie endet dann vor dessen Benutzernachricht; der Auftrag läuft im Original weiter. Vorgemerkte Nachrichten und noch nicht abgeschlossene Dateiänderungen werden nicht übernommen. Der Befehl nimmt keine Argumente oder neuen Anhänge an und startet keinen Modellaufruf.

Verlauf, Einstellungen, interne Dateien samt Versionen und Anhänge werden kopiert. Änderungen an den kopierten Session-Dateien wirken nicht aufs Original. Freigaben, offene Fragen, Automationen und Subagenten werden nicht übernommen. Kopien brauchen zusätzlichen Speicher und unterliegen deinem Speicherlimit.

!!! warning "Computerdateien werden nicht isoliert"
    Computer und Arbeitsordner bleiben gleich. Die Kopie erzeugt weder Branch noch Worktree und übernimmt keine Worktree-Verwaltung. Bereits ausgeführte Tool-Aktionen werden auch durch Rückgängig nicht zurückgenommen.

### Git-Projekte und Worktrees

Einrichtung, Diffs, Branches und getrennte Arbeitskopien stehen unter [Git und Worktrees](git-und-worktrees.md).

## Antworten zitieren und kopieren

Markiere eine Textstelle in einer fertig angezeigten Agentenantwort und wähle **Zitieren**. Der Button erscheint nach dem Loslassen mittig über oder unter der Auswahl, je nach verfügbarem Platz und Auswahlrichtung. Auf dem Desktop ersetzt ihn das Kommentarfeld am selben Auswahlanker; die Stelle bleibt hervorgehoben. Längere Kommentare lassen sich im Popup scrollen. Auf kleinen Bildschirmen oder bei zu wenig Platz neben einer großen Auswahl öffnet sich ein Dialog mit Zitat und Kommentar. Ergänze optional einen Kommentar und bestätige mit dem Häkchen oder Enter im Kommentarfeld. Umschalt+Enter fügt einen Zeilenumbruch ein; das Kreuz oder Escape verwirft die Eingabe.

Zitate erscheinen als kompakte Karten zuerst im Anhangsbereich, in der Reihenfolge, in der du sie hinzugefügt hast. Danach folgen Dateien und Sprachnachrichten. Dein Nachrichtentext bleibt in einem normalen Textfeld. Die Karte zeigt ein Zitat-Icon mit einer einzeiligen Textvorschau und darunter den Kommentar; lange Inhalte werden mit Auslassungspunkten gekürzt. Über den Stift öffnest du den vollständigen Text und bearbeitest Zitat und Kommentar auf dem Desktop in einem Popup an der Karte, auf kleinen Bildschirmen im selben Dialog wie beim Erstellen. Die Karte bleibt dabei kompakt. Über ihr Kreuz entfernst du die Karte. Die Schrift folgt der Session-Einstellung **Nachrichtengröße**; mobile Eingabefelder behalten ihre gut bedienbare Schriftgröße.

Text, Zitate, Kommentare und Anhänge bleiben mit dem Entwurf erhalten. Beim Senden stehen die Zitate mit ihren Kommentaren als Markdown unter deinem Nachrichtentext; sie werden nicht als Dateien hochgeladen. Offene Markdown-Codeblöcke im Nachrichtentext werden vor angehängten Zitaten automatisch geschlossen, damit die Zitate nicht Teil des Codes werden. Ältere Entwürfe mit Text zwischen Zitaten führen ihre Textabschnitte im gemeinsamen Textfeld zusammen und behalten alle Zitate und Kommentare. Bereits gesendete Nachrichten bleiben unverändert.

Der Kopierbutton in der Metazeile übernimmt den vollständigen Markdown-Antworttext inklusive Codeblöcken, aber ohne Aktivitäten und Metadaten. Er erscheint, wenn du die Antwort mit der Maus berührst oder den Button per Tastatur fokussierst; auf Touch-Geräten bleibt er sichtbar.

## Während einer Antwort eingreifen

**Jetzt senden** ergänzt den laufenden Auftrag um deine Nachricht. Eine Modellanfrage wird neu aufgebaut; aktive Tool-Arbeit darf zuerst enden. Überholte Freigaben und Rückfragen werden ohne unfertige Antworten geschlossen, auch bei zugehörigen Subagenten. Diese warten dann im selben Lauf auf eine neue Anweisung des Hauptagenten oder werden von ihm gezielt gestoppt. Andere Subagenten können weiterarbeiten. Neue Anweisungen erlauben keine gesperrten Aktionen.

Die bisherige Antwort mit ihren Aktivitäten bleibt oberhalb deiner Direktnachricht stehen. Die Fortsetzung erscheint darunter als eigener Antwortabschnitt, auch nach dem Neuladen. Bereits laufende Tool-Aktionen bleiben ihrem ursprünglichen Abschnitt zugeordnet; der reservierte Platz unter deiner Nachricht bleibt bis zum neuen Antwortabschnitt erhalten. Technisch bleibt es derselbe Lauf.

Bei **Jetzt senden** gilt dein beim Absenden gewähltes Agentenprofil ab der nächsten Modellanfrage, sobald aktive Tool-Arbeit beendet ist. Dabei wechseln auch die Profilanweisungen, verfügbaren Tools, Berechtigungen und die profilabhängige Modellwahl. Frühere Antworten behalten ihr ursprüngliches Profil; bereits ausgeführte Aktionen werden nicht rückgängig gemacht.

**Nach dem Lauf senden** reiht eine Nachricht für einen anschließenden Auftrag ein. Bis zur Serverbestätigung sichert der Browser Text und Anhänge lokal. Danach übernimmt MatoLoop die gespeicherte Queue: Angenommene Nachrichten warten in Reihenfolge auf das Ende des laufenden Auftrags, auch bei offenen Freigaben oder Rückfragen. Sie bleiben nach Schließen des Browsers und einem Backend-Neustart erhalten. Abmelden oder Kontowechsel entfernt nur den lokalen Postausgang, nicht bereits angenommene Aufträge.

Jede vorgemerkte Nachricht behält das beim Absenden gewählte Agentenprofil und verwendet es beim späteren Lauf. Eine spätere Auswahl im Profilwähler ändert bereits vorgemerkte Nachrichten nicht.

Deine zuletzt gewählte Profilauswahl bleibt im Eingabebereich erhalten – nach normalen Nachrichten, **Jetzt senden**, **Nach dem Lauf senden** und dem Laufende. Auch ein älterer Queue-Auftrag mit anderem Profil stellt sie nicht um. Ändere sie selbst, wenn deine nächste Nachricht ein anderes Profil verwenden soll.

Halte **Cmd** oder **Strg** gedrückt, um vorübergehend vorzumerken: Das Sende-Icon wechselt
zum Vormerken-Symbol. Sende mit **Cmd/Strg+Enter** oder klicke bei gedrückter Taste auf Senden.
Die Nachricht wartet hinter dem laufenden Auftrag, statt ihn zu unterbrechen. Nach dem Loslassen
gilt wieder die im Versandmenü gewählte Versandart. **Umschalt+Enter** bleibt ein Zeilenumbruch.
Ohne laufenden Auftrag kann eine vorgemerkte Nachricht sofort starten.

Fehlerhafte Sendungen bleiben für einen erneuten Versuch oder die Übernahme in den Entwurf erhalten. Entferne einen vorgemerkten Auftrag vor seinem Start, wenn er nicht mehr ausgeführt werden soll. Hat er bereits begonnen, verwende den Stoppknopf. Änderst du Computer oder Arbeitsordner, hält die Queue betroffene Nachrichten an; übernimm sie in den Entwurf, prüfe den neuen Kontext und sende erneut.

Der Stoppknopf beendet den Agentenlauf. Ein Browserdisconnect beendet ihn nicht; beim Wiederverbinden lädt MatoLoop den gespeicherten Zustand nach.

Eine verfügbare Reasoning-Zusammenfassung ersetzt die Bezeichnung **Nachdenken**. Die Zeile lässt sich nur aufklappen, wenn zusätzlicher Reasoning-Text vorhanden ist. Zusammenfassungen zeigen Modellangaben, keine garantiert vollständigen internen Gedankengänge.

Gleichartige Tool-Aufrufe erscheinen als aufklappbare Gruppe mit ihrer Anzahl. Ein Fehlersymbol an der Gruppe weist auf mindestens einen fehlgeschlagenen Aufruf hin. Klappe die Gruppe auf, um die einzelnen Ressourcen, ihren Status und verfügbare Details zu sehen. Freigaben, Rückfragen und Ausgaben bleiben auch bei geschlossener Gruppe sichtbar.

Während einer internen Reparaturrunde bleibt die Aktivitätsanzeige neutral. Gelingt die Korrektur, erscheint der Fehlversuch nicht im Arbeitsverlauf; ungelöste Fehler werden am Laufende sichtbar. Freigaben, Rückfragen und Hinweise auf mögliche Teiländerungen bleiben sofort sichtbar.

## Rückfragen und delegierte Aufgaben

Strukturierte Rückfragen erscheinen direkt im Chat. **Weiter** navigiert, **Fertig** sendet auch unvollständige Antworten. Bei mehreren Fragen leert **Überspringen** nur die aktuelle Antwort und wechselt zur nächsten Frage. Übersprungene Fragen bleiben unbeantwortet; auch eine vorgegebene Standardantwort wird nicht übernommen. Bei der letzten Frage sendet **Überspringen und senden** die übrigen Antworten; vorherige Antworten bleiben erhalten. Bei einer einzelnen Frage verwirft **Ablehnen** den Entwurf. Mit **Jetzt senden** kannst du stattdessen nachfragen oder die Richtung ändern. Geheimnisse gehören nur in [geschützte Eingabefelder](variablen-und-secrets.md).

Aufgabenlisten zeigen gemeldeten Fortschritt. Delegierte Aufgaben verwenden dieselbe Zeile wie Tool-Aufrufe: Der Auftrag steht anstelle des Toolnamens, der Status anstelle der Ressource. Die Zeilen sind gleich breit; lange Aufträge werden mit Auslassungspunkten gekürzt, während der Status sichtbar bleibt. Ein kleiner, quadratischer Statusrahmen mit leicht abgerundeten Ecken pulsiert während der Arbeit langsam im MatoLoop-Blau der Seitenleiste, ohne seine Größe zu verändern. Beim Warten bleibt er im gedämpften Blau des Pulsendes still; nach dem Ende ist er grau und unbewegt. Bei reduzierter Bewegung bleibt er auch während der Arbeit statisch. Der Statustext bleibt immer grau, auch bei **Abgebrochen**. **Wartet** umfasst offene Freigaben und Rückfragen. Der Pfeil erscheint bei Maus- oder Tastaturfokus, auf Touch-Geräten bleibt er sichtbar. Ein Klick öffnet den schreibgeschützten Verlauf mit dem vollständigen Auftrag.

Beim Start erscheint **Subagent starten**. Sobald die zugehörigen Subagenten vorhanden sind, ersetzen ihre Zeilen die Startanzeige. Scheitert der Start ohne angelegten Subagenten, bleibt der Tool-Fehler im Verlauf sichtbar. Aktive Subagenten stehen auch bei eingeklapptem Arbeitsverlauf in der neuesten Antwort des Hauptagenten. Nach einer Direktnachricht bleiben sie dort sichtbar und wechseln mit der nächsten Antwort in deren Abschnitt. Ihre Startinformationen und beendete Aufgaben findest du im aufgeklappten Verlauf der ursprünglichen Antwort. Fragen und Freigaben bleiben im Hauptchat bedienbar.

Sind mehrere Freigaben oder Rückfragen offen, zeigt der Chat jeweils eine als **Anfrage 1 von …**. Wechsle über die Balken zwischen den Anfragen; deine Eingaben bleiben beim Umschalten erhalten. Bei Anfragen von Subagenten steht oben rechts der Name oder Auftrag mit dem Subagent-Symbol. Ein Klick auf den Namen öffnet dessen schreibgeschützten Verlauf. Anfragen des Hauptagenten brauchen keinen Herkunftshinweis. Nach dem Erledigen erscheint die nächste offene Anfrage. Innerhalb einer Rückfrage wechselst du über nummerierte Schritte neben der Frage zwischen den Fragen; auf kleinen Bildschirmen stehen sie darunter. Beantwortete Fragen tragen ein Häkchen. Die aktuelle Frage und ihre Antwortfelder bleiben eingeblendet. Die Zähler in der Seitenleiste und auf Home zählen Freigaben und Rückfragen gemeinsam. Eine Rückfrage mit mehreren Fragen zählt dabei als eine Anfrage.

Kinder teilen den Computer-Arbeitsbereich, sind also nicht dateiseitig isoliert. Ein Stopp des Elternlaufs stoppt auch offene delegierte Arbeit.

Pro Hauptsession können höchstens vier Subagenten gleichzeitig aktiv sein; wartende Freigaben und Rückfragen zählen mit. Subagenten starten selbst keine weiteren Subagenten. Der Hauptagent kann einem früheren Subagenten Folgeaufträge mit erhaltenem Verlauf geben oder einen laufenden Auftrag korrigieren. Bitte ihn beispielsweise: „Lass denselben Subagenten die überarbeitete Version prüfen.“

Eine Zwischenantwort des Hauptagenten beendet laufende Subagenten nicht. Er übernimmt deren Ergebnisse anschließend. Eine bestätigte Weitergabe ist noch kein abgeschlossenes Ergebnis: Korrekturen können einen laufenden Toolaufruf abwarten und bereits ausgeführte Änderungen nicht rückgängig machen. Die Vierergrenze begrenzt die Parallelität, nicht die Gesamtkosten aufeinanderfolgender Aufträge.

## Nachrichten erneut versuchen oder zurücknehmen

- **Erneut versuchen** startet einen neuen Versuch der letzten fehlgeschlagenen Host-Antwort. Voraussetzungen werden erneut geprüft; bereits gestartete Läufe behalten ihren Retry-Kontext.
- **Rückgängig** übernimmt Text und Anhänge in den Entwurf und entfernt die Benutzernachricht mit dem folgenden Verlauf.

!!! warning "Externe Wirkungen bleiben bestehen"
    Rückgängig, Retry und Stopp machen ausgeführte Datei-, Integrations-, Freigabe- oder Memory-Änderungen nicht rückgängig. Prüfe unklare Ergebnisse vor einem erneuten Auftrag.

Antworten unterstützen Markdown, Tabellen, Code, Mathematik und Mermaid. Code und Diagrammquellen lassen sich kopieren oder herunterladen. Externe Bilder benötigen eine Zustimmung je Ursprung und Session. **Kleine Nachrichten** verdichtet die Darstellung, ohne Inhalt zu kürzen.
