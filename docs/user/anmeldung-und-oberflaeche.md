# Anmeldung und Oberfläche

## Anmeldung

MatoLoop verwendet eine lokale Cookie-Anmeldung. Geschützte Seiten leiten zur Anmeldung weiter; **Abmelden** beendet sie. Externe SSO-Anmeldung ist nicht implementiert. Das erste Konto ist Administrator. Unter **Konto** änderst du dein Passwort; dadurch werden bestehende Anmeldungen dieses Kontos beendet.

Unter **Administration > Benutzer** können Administratoren Konten anlegen, Passwörter zurücksetzen, Konten aktivieren/deaktivieren und Rollen vergeben. Ein Konto muss vor dem Löschen deaktiviert sein. Das eigene Konto und der letzte aktive Administrator sind geschützt.

Administratorrechte erlauben gemeinsame Konfiguration und Kontoverwaltung, keinen Zugriff auf persönliche Inhalte anderer Benutzer. **Administration** und **Einstellungen** haben getrennte gemeinsame bzw. persönliche Listen. Beim Löschen eines Kontos entfernt MatoLoop dessen Datenbankdaten und Backend-Sessionbäume; Computerdateien und externe Daten bleiben beim jeweiligen Dienst.

## Navigation

Die Seitenleiste enthält **Home**, **Chat**, **Automationen**, **Projekte** und **Sessions** in dieser Reihenfolge sowie Suche, **Variablen & Secrets**, **Archiv**, direkt darunter **Papierkorb** und das Benutzermenü. **Chat** öffnet deinen dauerhaften persönlichen Gesprächsfaden; **Home** und das Anwendungslogo führen zur Startübersicht. Statussymbole zeigen aktive Arbeit, offene Anfragen und ungelesene Antworten. Projekt-Sessions werden beim Aufklappen geladen; **Mehr anzeigen** lädt weitere Einträge. Listenfehler können direkt erneut versucht werden.

Der persönliche Chat erscheint nicht zusätzlich unter **Sessions**. Home zeigt zuletzt aktive Unterhaltungen, offene Anfragen, kommende Automationen und Computerstatus. Manche Übersichtselemente sind auf kleinen Bildschirmen ausgeblendet; der Chat bleibt nutzbar. Eine ausdrückliche neue Unterhaltung öffnet einen leeren Entwurf.

Klicke auf eine Projektzeile, um ihre Sessions ein- oder auszuklappen. Die Ordnerkontur zeigt, ob das Projekt geöffnet ist. In der Session-Tab-Leiste erkennst du das Projekt am selben Kürzel und Farbton.

Unter **Projekt bearbeiten** kannst du ein **Projektkürzel** aus bis zu zwei Buchstaben oder Ziffern und eine **Projektfarbe** aus der Palette wählen. Lass das Kürzel leer, um es aus dem Projektnamen abzuleiten. **Automatisch** vergibt die Farben in der Erstellungsreihenfolge der Projekte und beginnt nach zehn Farben wieder von vorne. Beim Archivieren oder Löschen können automatische Farben nachrücken; manuell gewählte Farben bleiben gleich. Der Farbton bleibt beim Wechsel zwischen Hell- und Dunkelmodus wiedererkennbar.

Unter **Zuletzt aktiv** auf Home findest du die sechs zuletzt aktiven Sessions und zusätzlich alle laufenden Sessions, Sessions mit offenen Freigaben oder Rückfragen sowie Sessions mit ungelesenen Antworten. Beim Öffnen zählt die neueste Nutzernachricht, Rückfrage, Freigabeanfrage oder fertige Agent-Antwort für die Reihenfolge. Danach bleiben bestehende Positionen während des Home-Besuchs stabil; neu relevante Sessions kommen unten hinzu. Wie in der Seitenleiste zeigt ein Punkt eine ungelesene Antwort an, sofern kein laufender Agent oder eine offene Anfrage Vorrang hat. Der Status aktualisiert sich weiter. Gelesene Antworten und erledigte Anfragen lassen einen Eintrag nicht sofort verschwinden. Archivierte oder gelöschte Sessions werden entfernt. Stumme Antworten mit `silence` zählen nicht als neue Antwort; Fehler bleiben sichtbar.

Die Seitenleiste bleibt nach deiner letzten Nachricht sortiert, neueste zuerst; angepinnte Sessions stehen oben. Bei leeren Sessions zählt der Erstellungszeitpunkt. Status und Ungelesen-Markierungen bleiben überall aktuell.

Beim Zurückkehren zu Home wird die Auswahl neu geladen und einmal sortiert angezeigt. Bis die Session- und Anfragestatus geladen sind, erscheint eine Ladeanzeige statt einer alten Liste, die anschließend umspringt.

### Session in ein anderes Projekt verschieben

Wähle im Sessionmenü **Verschieben** und dann das Zielprojekt. Über **Neues Projekt anlegen** kannst du stattdessen einen Namen eingeben und mit **Anlegen und verschieben** beides in einem Schritt erledigen. **Ohne Projekt** löst die bisherige Zuordnung.

Für bestehende Zielprojekte ist **Projektvorgaben übernehmen** vorausgewählt. Damit kann die Session ihre Vorlage, ihren Computer und ihren Arbeitsbereich wechseln. Ein bisheriger Worktree bleibt unverändert; Änderungen werden nicht in den Ziel-Worktree übertragen. Entferne den Haken, um die bisherigen Session-Einstellungen zu behalten. Anweisungen des Zielprojekts gelten unabhängig davon.

### Session-Tabs

Aktiviere **Einstellungen > Grundeinstellungen > Session-Tab-Leiste anzeigen**, wenn du mehrere Unterhaltungen als Tabs offen halten möchtest. Unter **Position der Session-Tab-Leiste** wählst du **Oben** oder **Unten** am gemeinsamen Chat-/Control-Center-Bereich; die Auswahl wird für dein Konto gespeichert, Vorgabe ist **Unten**. Die Leiste erscheint ab Tabletbreite (600 Pixel); auf dem Handy bleibt sie ausgeblendet. Standardmäßig ist sie ausgeschaltet.

Ein einfacher Klick auf eine Session öffnet einen kursiven Vorschau-Tab und ersetzt die vorherige Vorschau. Mit Doppelklick auf die Session oder den Tab, über **In neuem Tab öffnen** im Sessionmenü oder ab der ersten Texteingabe bzw. einem Anhang bleibt der Tab erhalten. Bereits geöffnete Sessions werden nicht doppelt angelegt. Inhalt und Reihenfolge der Leiste einschließlich der Vorschau werden für dein Konto geräteübergreifend gespeichert und beim Öffnen bzw. Zurückkehren ins Fenster geladen. Bei gleichzeitigen Änderungen gilt die zuletzt gespeicherte Liste. Entwurfstexte und Anhänge werden dabei nicht übertragen.

Wähle in den Grundeinstellungen unter **Session-Tabs sortieren** zwischen **Nach Projekt** (Standard) und **Nach Hinzufügen**. Nach Projekt stehen Sessions ohne Projekt zuerst, danach folgen die Projekte in der Reihenfolge der Seitenleiste. Verschiebe Tabs innerhalb desselben Projekts oder unter den Tabs ohne Projekt per Ziehen, **Alt+Pfeil links/rechts** oder über das Kontextmenü (Rechtsklick oder langes Drücken). Eine verschobene Vorschau bleibt als eigener Tab erhalten. Nach Hinzufügen erscheinen neue Tabs rechts, und die Reihenfolge ändert sich beim Wechseln zwischen Tabs nicht. In diesem Modus kannst du Tabs nicht manuell verschieben. Mit × entfernte Tabs bleiben auch nach Neuladen geschlossen.

Das Schließen eines Tabs beendet keinen Lauf und löscht weder Session noch Entwurf. Textentwürfe werden pro Benutzer und Session browserlokal gespeichert; ungesendete Anhänge bleiben beim Wechsel oder Tab-Schließen im Arbeitsspeicher, nicht über ein Neuladen hinweg.

Projekt-Sessions zeigen links ein farbiges Projektkürzel mit einem kleinen Symbol für den Status, falls vorhanden. Wenn du mit der Maus über den Tab fährst, siehst du den vollständigen Sessiontitel und Projektnamen. Die Leiste bleibt einzeilig: Bei wenig Platz werden die Tabs schmaler. Reicht der Platz nicht für alle, bleiben der aktive und die zuletzt genutzten Tabs sichtbar. Die übrigen bleiben offen, sind über die Seitenleiste erreichbar und erscheinen bei mehr Platz wieder in der Tab-Leiste.

Standardmäßig bleiben bis zu 10 Session-Tabs offen. Unter **Maximal offene Session-Tabs** in den Grundeinstellungen kannst du 1 bis 20 einstellen. Beim Öffnen eines neuen Tabs machen bei Bedarf die am längsten nicht genutzten Tabs Platz. Wenn du das Limit senkst, bleiben die Tabs bis zum nächsten Öffnen eines neuen Tabs offen. Ein kleineres Fenster schließt keine Tabs. Ist die Leiste beim Neuladen voll, öffnet sich die Session aus der URL ohne eigenen Tab, falls sie nicht schon in der Leiste steht.

### Suche

Die Suche findet eigene Projekte, Sessions und Nachrichten ab zwei Zeichen. **Strg+F** bzw. **Cmd+F** öffnet die Anwendungssuche; ist sie bereits offen, bleibt die Browsersuche verfügbar. Ohne Embedding-Dienst funktioniert die lexikalische Suche weiter. Suchergebnisse öffnen den passenden Gesprächskontext.

### Archiv

Über das Menü einer Session oder eines Projekts archivierst du Unterhaltungen. **Archiv** ermöglicht Lesen und Wiederherstellen. Ein archiviertes Projekt archiviert seine Sessions wirksam mit; einzeln archivierte Sessions bleiben nach der Projektwiederherstellung archiviert. Neue Aufträge benötigen die Wiederherstellung, bestehende Arbeit darf noch enden. Mehr zu Zeitplänen unter [Automationen](automationen.md).

### Papierkorb

Aktiviere unter **Einstellungen > Grundeinstellungen** bei Bedarf **Papierkorb verwenden** und lege die **Aufbewahrung in Tagen** fest (1–3650, Vorgabe 30). Standardmäßig ist der Papierkorb aus. Änderungen gelten nur für künftige Löschvorgänge; Deaktivieren leert ihn nicht und ändert keine bestehenden Fristen.

Wähle im Löschdialog **In den Papierkorb**, um den Eintrag wiederherstellbar zu entfernen. Mit **Sofort löschen** überspringst du den Papierkorb. Beim Verschieben bleiben Session-Dateien und Automationen erhalten; Unterhaltungen sind nicht lesbar und neue Aufträge gesperrt. Ein [ausdrücklich zur Entfernung ausgewählter Worktree](git-und-worktrees.md#worktree-entfernen) wird trotzdem sofort entfernt. Unter **Papierkorb** siehst du Projekte und einzeln gelöschte Sessions mit ihrer Wiederherstellungsfrist. Klappe ein Projekt auf, um die enthaltenen Sessions zu sehen. Zuvor einzeln gelöschte Sessions behalten ihre Frist und können dort einzeln endgültig gelöscht werden. Der Archivstatus bleibt unverändert. Im Archiv öffnest du Sessions direkt per Klick auf die Zeile und kannst sie über das Aktionsmenü löschen.

!!! warning "Endgültiges Löschen ist unwiderruflich"
    Ohne aktivierten Papierkorb oder mit **Sofort löschen** werden Inhalte dauerhaft gelöscht. Beachte dabei die zusätzlichen Hinweise zu abhängigen Automationen und einer optionalen Worktree-Entfernung. Nach Ablauf der angezeigten Frist ist keine Wiederherstellung mehr möglich; endgültiges Löschen bleibt verfügbar. Auch bei ausgeschaltetem Papierkorb erreichst du vorhandene Einträge weiterhin über die Seitenleiste.

## Verwaltungslisten

In den Listen für Skills, Agenten, Provider, Modelle, Integrationen und MCP-Server steht der Ein-/Ausschalter rechts, getrennt von den Aktionen. Eine Hauptaktion ist direkt erreichbar; weitere Aktionen wie Testen, Aktualisieren oder Löschen findest du im Drei-Punkte-Menü. Auf schmalen Bildschirmen steht diese Bediengruppe unter dem Text. Beschriftete Schalter behalten ihre Bedeutung: **Erlaubt** steuert eine Anbieterfreigabe, **Verfügbar** die Tool-Verfügbarkeit und **Admin** die Benutzerrolle. Benutzerkonten aktivierst oder deaktivierst du im zugehörigen Drei-Punkte-Menü.

## Control Center

Die schwebende Schaltfläche öffnet das Control Center. **Session** ist der feste Einstellungsbereich. Neue Sessions haben außerdem die App **Ablage**; **Computer-Dateien**, **Automationen**, **Terminal**, **Browser** und andere verfügbare Apps fügst du über den Launcher hinzu. Sessiongebundene Apps können einen Entwurf als leere Session speichern.

Apps lassen sich umordnen und schließen. Das Schließen eines App-Tabs löscht seine Daten oder Konfiguration nicht; eine Automation läuft beispielsweise weiter. Das Zuklappen des Panels erhält Tabs und Dateientwürfe. Nur beim Schließen einer Datei mit ungespeicherten Änderungen ist eine Entscheidung erforderlich. Der feste Sessionbereich kann nicht entfernt werden.

Reihenfolge, Dateiziele und Ansichten werden je Session gespeichert, Editorentwürfe bleiben im Browser-Arbeitsspeicher. Der Öffnungszustand gilt dagegen gemeinsam für alle Sessions und wird für dein Konto geräteübergreifend gespeichert. Ab 1024 Pixeln stellt MatoLoop diese Sichtbarkeit im geteilten Layout wieder her. Der Vollbilddialog auf kleineren Bildschirmen startet geschlossen und muss ausdrücklich geöffnet werden; sein Öffnen und Schließen verändert die gespeicherte Sichtbarkeit nicht.

### Benachrichtigungen

Kurze App-Meldungen (Toasts) erscheinen auf schmalen Bildschirmen oben mittig unter der Kopfzeile, auf breiten Bildschirmen unten rechts. Das gilt auch bei geöffnetem Control Center. Verbindungs- und Update-Hinweise bleiben unten rechts. Mit **Meldung schließen** (`×`) blendest du Meldungen aus. Beim Update-Hinweis entspricht das **Später**; die automatische Verbindungsprüfung läuft auch nach dem Schließen weiter.

Der globale Bereich **Benachrichtigungen** zeigt eine chronologische Liste. Sein Zähler zählt ausschließlich ungelesene Hinweise. Die schwebende Control-Center-Schaltfläche öffnet bei vorhandenem Zähler direkt diese Liste; sonst die zuletzt verwendete App.

Schalte den **Benachrichtigungston** über das Lautsprechersymbol oben im Benachrichtigungscenter ein, um einen kurzen Ton für neue Hinweise zu hören. Die Einstellung bleibt in diesem Browser bzw. in der installierten Web-App gespeichert. Nach dem Neuladen kann der Browser den Ton bis zur ersten Interaktion blockieren. Ist MatoLoop geschlossen oder im Hintergrund, steuern Browser und Betriebssystem den Ton für Web Push.

Hinweise auf Freigaben und Rückfragen öffnen den zugehörigen Chat. Neue [Memory-Vorschläge](memory-und-dreaming.md#anderungsrichtlinien) prüfst du im Chat **Persönlicher Assistent**; ältere bleiben in ihrer bisherigen Quell-Session erreichbar. Normale Antworten und Laufzeitfehler erzeugen ebenfalls Hinweise, sofern ihre Session nicht sichtbar und fokussiert ist. Antwort- und Sessionanfrage-Hinweise verschwinden nach dem Lesen; andere Hinweise können einen gelesenen Verlauf behalten. Lesen oder Löschen entscheidet niemals die zugrunde liegende Anfrage.

Wenn du eine Session löschst, werden auch alle mit ihr verknüpften Benachrichtigungen gelöscht. Hinweise ohne Sessionbezug bleiben erhalten.

Mit `notify` erstellt der Agent höchstens einen gezielten Hinweis pro Lauf. Weitere Aufrufe korrigieren dessen Titel, Inhalt und Priorität; einen von dir gelöschten Hinweis erzeugen sie nicht erneut. Normale Hinweise bleiben unverändert; Warnungen tragen ⚠️, dringende Hinweise 🚨 vor dem Titel. Die Priorität ändert keine Ruhezeiten oder Zustellregeln. Korrekturen behalten den Gelesen-Status und aktualisieren vorhandene Push-Hinweise still, soweit Browser und Betriebssystem das unterstützen. Bereits gesehene Inhalte lassen sich nicht zurücknehmen.

Session-Freigaben verwaltest du unter **Session > Session-Freigaben**. Computer und Arbeitsverzeichnis haben eigene Auswahlfelder im Sessionbereich.

## App und Push verwenden

Die Produktionsanwendung lässt sich über deinen Browser als Web-App installieren. Verwende vertrauenswürdiges HTTPS. Für Push muss ein Administrator zuerst [die Instanz einrichten](betrieb-und-datensicherung.md#app-und-push-benachrichtigungen). Aktiviere anschließend Push pro Browser oder installierter App im Benachrichtigungsbereich. Auf iOS/iPadOS benötigst du mindestens Version 16.4 und die Installation über **Zum Home-Bildschirm**.

Antworten und Agentenhinweise erscheinen sofort in der App. Für Web Push bündelt MatoLoop sie pro Konto 30 Sekunden lang ab dem ersten Hinweis. Neue Hinweise verlängern das Fenster nicht. Solange MatoLoop auf einem deiner Geräte im Fokus ist, wartet der Push weiter. Zugestellt werden nur noch ungelesene Hinweise. Freigaben und Rückfragen haben keine zusätzliche Wartezeit.

Die Zustellung per Web Push ist nicht garantiert; die Hinweise in der App bleiben maßgeblich. Offline steht nur die Anwendungshülle bereit, keine Chats, Dateien oder Anmeldedaten.

!!! warning "Vor einem App-Update speichern"
    Ein von dir bestätigtes App-Update lädt offene Appfenster neu. Ungespeicherte Editorzustände und andere Eingaben im Arbeitsspeicher können verloren gehen.

## Desktop und Mobilgeräte

Auf größeren Bildschirmen teilen sich Chat und Control Center einen verstellbaren Bereich. Auf schmalen Geräten öffnet ein Vollbilddialog. Bei geschlossener Seitenleiste erscheint eine schwebende Navigationstaste.

## Heller und dunkler Modus

Das Benutzermenü schaltet den Farbmodus um. Die Dokumentation hat einen eigenen Schalter und berücksichtigt zunächst die Systemeinstellung.

### Darstellung der Instanz

Unter **Administration > Darstellung** ändern Administratoren Name und Beschreibung der Anwendung sowie Logo und Theme. **Anpassung** erlaubt einen Namen bis 80 Zeichen, Beschreibung bis 500 Zeichen und ein PNG-, JPEG- oder WebP-Logo bis 1 MiB; das Logo wird als quadratisches 512-Pixel-PNG normalisiert. Speichern und Zurücksetzen wirken auf die Instanz, einschließlich der Anmeldung.

Unter **Themes** wählst du ein aktiviertes Standardtheme. Der aktuelle Standard kann erst deaktiviert werden, wenn ein anderer gewählt wird. Derzeit ist nur MatoLoop mitgeliefert; eigene Theme-Auswahl je Benutzer oder Uploads gibt es nicht. Theme, Hell/Dunkel-Modus und Name/Persona des Assistenten sind getrennte Einstellungen. Andere geöffnete Browser laden die neue Darstellung beim nächsten Seitenaufruf.

## Ungespeicherte Änderungen

Ungespeicherte Dateiänderungen lösen vor Verlassen oder Verwerfen eine Rückfrage aus. Panelzuklappen und Tabwechsel erhalten sie. Ein Neuladen kann browserlokale Editorzustände verlieren; speichere wichtige Änderungen. Die Dokumentation öffnet in einem eigenen Tab.
