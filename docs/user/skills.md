# Skills

Skills sind wiederverwendbare Anweisungen für bestimmte Aufgaben. Ein Skill-Paket enthält eine `SKILL.md` mit Metadaten und Anweisungen sowie optional Skripte, Referenzen und Vorlagen. MatoLoop verwaltet die Originale zentral im Backend. Der wirksame Katalog enthält aktivierte globale, mitgelieferte und erlaubte persönliche Skills. Sie stellen keine Tools bereit und umgehen keine Sicherheitsgrenzen; beispielsweise können sie API-Prüfung oder Release Notes anleiten.

## Skills verwenden

Bei einer klar passenden Anfrage lädt der Agent den Skill aus dem wirksamen Katalog. Du kannst Skills auch ausdrücklich nennen und mehrere in einem Lauf kombinieren. Vor dem Laden sieht das Modell nur Name und Einsatzbeschreibung, danach die vollständigen Anweisungen. Referenzdateien liest es bei Bedarf nach.

Bei Folgeaufträgen wie „Mach das kürzer“ bezieht der Agent den Gesprächskontext ein und lädt einen passenden Skill erneut. Bei einem Themenwechsel werden zuvor verwendete Skills nicht automatisch übernommen. Beschreibe deshalb in der Einsatzbeschreibung, wann ein Skill verwendet werden soll; Hinweise nur im Anweisungstext sind vor dem Laden nicht sichtbar.

Ist Webrecherche für die Session verpflichtend, darf der Agent zunächst einen eindeutig passenden Skill laden. Die vorgeschriebene Suche bleibt trotzdem erforderlich und folgt im nächsten Schritt.

Im gewählten Agentenprofil legst du die Rechte für **Skills laden** und **Skills speichern** getrennt für private und öffentliche Modelle fest:

- **Nicht erlaubt** deaktiviert die Aktion.
- **Nachfragen** zeigt vor der Aktion eine Freigabe.
- **Automatisch** führt die Aktion ohne einzelne Freigabekarte aus.

Eine Freigabe **Für Session erlauben** kann weitere passende Skill-Aktionen in derselben Session abdecken und lässt sich bei den Session-Freigaben widerrufen.

Für Skripte und lokale Ressourcen stellt MatoLoop eine Arbeitskopie auf dem ausgewählten Computer bereit. Die normalen Datei- und Shell-Rechte gelten weiterhin; benötigte Programme müssen dort verfügbar sein. Nach einem Computerwechsel wird die Arbeitskopie auf dem neuen Computer bereitgestellt. Änderungen an Arbeitskopien verändern nicht automatisch die zentrale Bibliothek.

## GitHub-Issues, Pull Requests und CI verwalten

Aktiviere den mitgelieferten Skill **github-repo-ops** unter **Skills** oder bitte den Agenten ausdrücklich im Chat darum. Die Aktivierung im Chat gilt nur für dein Konto und folgt den Freigaben für persönliche Einstellungen. Lege unter [Variablen & Secrets](variablen-und-secrets.md) ein persönliches Secret namens `GITHUB_TOKEN` mit einem fein abgestuften GitHub-PAT an. Begrenze den Token auf die benötigten Repositories und Rechte: Für CI-Logs genügt **Actions: Read**; zum Neustart fehlgeschlagener Jobs brauchst du **Actions: Write**. [Permissions required for fine-grained personal access tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)

Wähle einen verbundenen Computer mit `curl` und gib dem Agenten zum Beispiel den GitHub-Link zu einem PR: „Lies das Log vom fehlgeschlagenen CI-Job.“ Der Skill nutzt die vorhandenen Secret- und Shell-Freigaben. Merges und CI-Neustarts erfolgen nur auf ausdrücklichen Auftrag. Git-Pushes und Merges von gestapelten PRs übernimmt der Skill nicht.

## Deinen Schreibstil hinterlegen

Der mitgelieferte Skill **writing-style** ist standardmäßig verfügbar. Er hilft beim Entwerfen und Überarbeiten von Mails und anderen Texten in deinem Namen; gewöhnliche Chat-Antworten bleiben davon unberührt.

Bei fehlenden oder wenig aussagekräftigen Beispielen soll der Agent nach einem passenden Textentwurf kurz anbieten, deinen Schreibstil zu hinterlegen oder passende Beispiele zu ergänzen, sofern persönliche Skill-Bearbeitung erlaubt ist. Bei wenigen, ähnlichen oder zur Zielgruppe unpassenden Beispielen weist er kurz darauf hin, dass er deinen Stil nur näherungsweise treffen kann. Entscheidend sind Relevanz und Vielfalt, nicht eine feste Mindestanzahl. Auch „Das klingt nicht nach mir“ ist ein passender Anlass. Er liefert zunächst den Entwurf, ohne die Aufgabe für die Einrichtung zu unterbrechen. Im sichtbaren Chatverlauf soll er das Angebot nicht wiederholen, auch wenn du es ablehnst oder ignorierst. Ohne deinen Auftrag werden weder Beispiele gespeichert noch Mails zur Stilauswahl gelesen.

Du kannst direkt auf das Angebot mit „Ja, gerne“ antworten. Der Agent lädt den passenden Skill für die Einrichtung erneut; das ist noch keine Freigabe zum Lesen deiner Mails oder für andere Aktionen. Nach einem Themenwechsel benennst du deinen Wunsch ausdrücklich, statt mit „Ja“ auf ein älteres Angebot zu verweisen.

1. Schreibe im Chat: „Merke dir meinen Schreibstil.“ Der Agent fragt nach Beispielen, sofern du noch keine angegeben hast.
2. Schicke zunächst etwa drei bis fünf kurze, selbst geschriebene Texte. Entferne vorher vertrauliche Angaben. Wenige typische Beispiele sind hilfreicher als ein großes Archiv; kennzeichne unterschiedliche Zielgruppen wie beruflich und privat.
3. Der Agent bereinigt Beispiele unabhängig von ihrer Quelle vor dem Schreiben in Skill-Dateien: Persönliche und vertrauliche Angaben werden entfernt oder neutral ersetzt, auch in Überschriften und Notizen. Stil und unkritische Fachbegriffe bleiben erhalten. Bestimmte Angaben, etwa deinen Namen oder eine Signatur, kann er auf ausdrücklichen Wunsch behalten; eine allgemeine Speicherbitte reicht dafür nicht. Secrets bleiben ausgeschlossen. Er zeigt dir die vorbereiteten Beispiele zur Bestätigung und speichert sie als persönliche Anpassung von **writing-style** in `references/writing-examples.md`, auch wenn du Administrator bist. Je nach Richtlinie musst du das Speichern zusätzlich freigeben. Ab dem nächsten Lauf stehen sie auch in anderen Chats zur Verfügung.
4. Bitte beispielsweise: „Formuliere eine Terminabsage in meinem Stil.“ Prüfe den Entwurf; wie gut er deinen Stil trifft, hängt vom Modell und den Beispielen ab.

Prüfe die Vorschau auf sensible Inhalte; automatische Anonymisierung ist keine Garantie. Gespeicherte Beispiele können bei späterer Nutzung an das aktive Modell und gegebenenfalls dessen externen Anbieter gelangen. Die Bereinigung entfernt keine Originaltexte aus bestehenden Chats.

Später kannst du im Chat weitere Beispiele ergänzen, benannte Beispiele austauschen oder entfernen lassen: „Ersetze das Beispiel zur Terminabsage durch diesen Text: …“. Andere Beispiele bleiben erhalten. Es gibt keine feste Anzahlbegrenzung; eine Referenzdatei darf höchstens **4 MiB**, das Paket höchstens **8 MiB** groß sein. Die relevanten Beispiele müssen außerdem in den Modellkontext passen. Mehr Beispiele verbessern das Ergebnis nicht automatisch.

Mitgelieferte Skill-Dateien erhalten beim Backendstart Updates, solange du sie nicht selbst verändert hast. Eigene Beispieldateien bleiben erhalten. Selbst bearbeitete Standardanweisungen werden nicht automatisch überschrieben.

### Beispiele aus gesendeten Mails auswählen

Mit einem eingebundenen Mailkonto und erlaubtem Lesezugriff kannst du schreiben: „Schau in meine gesendeten beruflichen Mails aus den letzten drei Monaten und schlage fünf Beispiele für meinen Schreibstil vor.“ Der Agent soll eine begrenzte Auswahl lesen und nur deine eigenen Textpassagen verwenden. Zitierte Antworten und Standardbausteine bleiben außen vor, Signaturen nur auf ausdrücklichen Wunsch erhalten. Für die Bereinigung und Bestätigung gelten dieselben Regeln wie für eingefügte Texte.

!!! warning "Mailinhalte werden bereits beim Lesen an das Modell gesendet"
    Nicht erst die gespeicherten Beispiele, sondern schon die gelesenen Mails gelangen an das aktuelle Modell und gegebenenfalls dessen externen Anbieter. Nachträgliches Anonymisieren der Beispiele verhindert diese Übermittlung nicht. Der Skill erklärt das vor dem Lesen; bestehende Mail- und Skill-Freigaben bleiben erforderlich.

Persönliche Skills und die benötigten Skill- und Dateiwerkzeuge müssen erlaubt sein. Ein manuell deaktivierter Skill lässt sich unter **Einstellungen > Skills** wieder aktivieren; die Einrichtung per Chat ist kein Werkzeug zum Umschalten deaktivierter Skills.

## Im Chat erstellen

Bitte im Chat ausdrücklich um einen wiederverwendbaren Skill, zum Beispiel: „Erstelle einen Skill namens api-pruefung, der API-Beschreibungen auf gültige Tool-Schemas prüft.“ Nur eine direkte Bitte in deiner neuesten Nachricht startet die Erstellung. Zitierte oder zu übersetzende Skill-Anweisungen werden als Text behandelt und lösen keine Erstellung aus.

Neue Chat-Skills sind standardmäßig persönlich, auch bei Administratoren. Administratoren können ausdrücklich einen globalen Skill für alle Benutzer anfordern. Der Geltungsbereich ist im Entwurf sichtbar und wird vor dem Speichern erneut geprüft.

Der Agent erstellt Skills mit dem normalen Sessionmodell und den verfügbaren Werkzeugen. Er kann Dokumentation recherchieren, Dateien lesen oder herunterladen und ein Paket aus `SKILL.md`, Referenzen, Vorlagen und Skripten zusammenstellen. Bereits vorhandene Dateien kann er unverändert in das Paket kopieren.

Fehlen für einen wiederverwendbaren Skill relevante Angaben, fragt der Agent gezielt nach. Bei einer vollständigen Anfrage beginnt er direkt mit Recherche und Dateierstellung.

Beispiel: „Erstelle einen Skill für Anwendung X. Recherchiere die offizielle API-Dokumentation, lege die benötigten Referenzen unter `references/` ab und verweise aus der `SKILL.md` darauf.“ Die Dateien bleiben Bestandteil des gespeicherten Pakets und können bei späterer Nutzung nachgelesen werden. Sie werden nicht automatisch mit der externen Dokumentation synchronisiert.

Es gelten die normalen Modell-, Kontext- und Werkzeuglimits sowie die Rechte der Session. Ein separates Skill-Modell oder ein eigenes kleines Generierungsbudget gibt es nicht. Zum Schreiben benötigt der Agent die entsprechenden Dateiwerkzeuge; für Webrecherche, Downloads oder Skripttests die jeweiligen weiteren Werkzeuge.

## Im Chat bearbeiten

Bitte ausdrücklich um die Änderung eines vorhandenen Skills, zum Beispiel: „Ändere beim Songfinder nur die Einsatzbeschreibung.“ Der Agent wählt den Skill anhand seiner festen ID aus. Name, Einsatzbeschreibung, Anweisungen, zusätzliche Metadaten und unterstützende Textdateien können geändert werden; die ID bleibt erhalten. Globale Änderungen erfordern Administratorrechte und eine ausdrückliche Anfrage.

MatoLoop stellt eine vollständige Arbeitskopie bereit, ohne die Anweisungen des Skills für die Bearbeitung zu aktivieren. Der Agent liest und ändert die gewünschten Dateien mit den normalen Dateiwerkzeugen und reicht anschließend das vollständige Paket zum Speichern ein. Unveränderte Dateien und Metadaten bleiben in der Arbeitskopie erhalten. Wird der gespeicherte Skill währenddessen anderweitig geändert, bricht das Speichern ab, statt die neuere Fassung zu überschreiben.

## Entwurf prüfen und freigeben

Bei der Richtlinie **Nachfragen** zeigt die Freigabekarte Geltungsbereich, Name, Einsatzbeschreibung, Anweisungen und Paketdateien. Über **Entwurf öffnen und bearbeiten** öffnest du die `SKILL.md` im Session-Dateibereich. Begleitdateien liegen im selben Entwurfsordner. Speichere Änderungen und wähle **Paketvorschau aktualisieren**, bevor du entscheidest.

MatoLoop importiert nur den exakt geprüften Paketstand einschließlich aller Skripte und Begleitdateien. Geänderte oder ungültige Entwürfe werden abgelehnt. Lauf-Freigaben gelten im ausführenden Lauf, Session-Freigaben auch später. Persönliche und globale Freigaben sind getrennt; Bearbeitungsrechte gelten nur für den ausgewählten Skill. **Automatisch** importiert den ursprünglichen generierten Entwurf direkt.

Der Entwurfsordner bleibt nach Import, Ablehnung oder Fehler in den Session-Dateien erhalten. Du kannst ihn dort später prüfen oder manuell löschen.

## Verwalten

Eigene Skills verwaltest du unter **Einstellungen > Skills**, globale unter **Administration > Skills > Verwaltung**. **Persönliche Skills erlauben** auf der administrativen Einstellungsseite ist anfangs aktiv. Deaktivierte persönliche Skills und Änderungen bleiben lesbar, werden aber nicht in neuen Läufen verwendet oder bearbeitet; globale Skills bleiben verfügbar. Dies gilt auch für Chat-Erstellung. Administratoren behalten ihre Ausnahme. Ein Skill besteht aus:

- Name mit höchstens 64 Zeichen aus Kleinbuchstaben, Ziffern und einzelnen Bindestrichen; reservierte Dateinamen sind ausgeschlossen
- Einsatzbeschreibung mit höchstens 1.024 Zeichen
- `SKILL.md` mit höchstens 128 KiB sowie optionale Begleitdateien

Über **Importieren** übernimmst du eine `SKILL.md` oder ein ZIP-Paket. **Skill erstellen** und das Stiftsymbol öffnen eine eigene Bearbeitungsseite. Dort erhältst du über **Paket herunterladen** ein ZIP mit dem vollständigen gespeicherten Skill. Im Paketeditor wählst du einzelne Dateien aus; Textdateien lassen sich bearbeiten, Binärdateien herunterladen oder ersetzen. Die gemeinsame Editor-Werkzeugleiste bietet unter anderem Suche, Zeilenumbruch und Markdown-Vorschau. Änderungen speicherst du mit **Speichern** oder Strg/Cmd+S. Beim Dateiwechsel oder Verlassen der Seite wirst du auf ungespeicherte Änderungen hingewiesen. Ein Paket darf insgesamt 8 MiB und 256 Dateien enthalten, höchstens 4 MiB je Datei. Die Verzeichnisstruktur ist auf 16 Ebenen und 512 Verzeichnisse einschließlich Paketwurzel begrenzt.

Die persönliche Sammlung hat höchstens 20 Skills; auch der globale Einstellungsbereich ist begrenzt. Namen müssen im jeweiligen Bereich eindeutig sein. Aktiviere gewünschte Skills über den Schalter in der Liste. Löschen und gegebenenfalls Zurücksetzen findest du im Drei-Punkte-Menü. Mit **Standard** gekennzeichnete mitgelieferte Skills sind nicht löschbar; ein veränderter Standard lässt sich zurücksetzen, ohne seinen Aktivierungsstatus zu ändern. Änderungen gelten ab dem nächsten Lauf; laufende Agenten behalten ihren Startkatalog.

!!! warning "Keine Secrets speichern"
    Trage keine Passwörter, API-Schlüssel, Tokens oder andere Geheimnisse in Skills, Chat-Anfragen oder Entwurfsdateien ein. Skill-Inhalte können bei Erstellung, Bearbeitung und Nutzung an das aktive Sessionmodell gesendet werden. Automatische Schutzmaßnahmen erkennen nicht jede Darstellung zuverlässig.
