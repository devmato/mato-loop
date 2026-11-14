# Integrationen einrichten

Verbinde hier Kalender, E-Mail und andere Dienste mit MatoLoop. Gemeinsame Verbindungen stehen unter **Administration > Integrationen**, persönliche unter **Einstellungen > Integrationen**. Computergebundene Integrationen, Microsoft 365, OpenCode und tbs.Desk sind persönlich. Generische MCP-Server haben einen eigenen Menüpunkt.

## Eine Verbindung anlegen und testen

1. Öffne im passenden Bereich **Integrationen > Verwaltung > Integration hinzufügen**.
2. Wähle Typ, Anbieter und Name, dann **Weiter**.
3. Ergänze die in der folgenden Übersicht genannten Angaben und wähle **Speichern**. Erst jetzt wird die Verbindung angelegt.
4. Wähle **Verbindung testen**. Der Test verwendet die gespeicherten Werte; speichere Änderungen daher vorher.
5. Prüfe **Berechtigungen** und die Verfügbarkeit für das gewünschte Agentenprofil.

Der Stift in der Liste öffnet dieselbe Seite zum Bearbeiten. Leere Credential-Felder behalten gespeicherte Zugangsdaten. **Speichern** übernimmt den aktiven Tab. Tabwechsel erhalten Entwürfe; beim Verlassen kannst du sie verwerfen.

Zugangsdaten werden verschlüsselt gespeichert und nicht wieder im Klartext angezeigt. Verwende HTTPS außerhalb eines ausdrücklich vertrauenswürdigen Netzes; einige Anbieter verlangen es grundsätzlich.

## Benötigte Angaben

| Dienst | Verbindung | Zugang |
| --- | --- | --- |
| CalDAV | HTTPS-Serveradresse | Benutzername und Passwort/App-Passwort |
| IMAP/SMTP | Beide Server, TLS-Modus und fester Absender | Benutzername und Passwort/App-Passwort; kein unverschlüsselter Betrieb |
| Microsoft 365 | Mandanten-ID und Anwendungs-ID einer Entra-App | Gerätecode oder Browser-Anmeldung mit Client-Secret; [Postfach verbinden](#microsoft-365-postfach-lesen) |
| Mealie | Basis-URL ohne `/api` | Langlebiger API-Token aus dem Mealie-Benutzerprofil |
| Paperless-ngx | Basis-URL ohne `/api` | API-Token eines Kontos mit den benötigten Dokumentrechten |
| BookStack | HTTPS-Serveradresse | Token-ID und Token-Secret aus dem Benutzerprofil |
| Arcane | Manager-URL | API-Schlüssel mit passenden Umgebungsrechten |
| SonoBin | HTTPS-Basis-URL; `/mcp/` wird abgeleitet | Persönlicher Bearer-Token aus den Kontoeinstellungen |
| tbs.Desk | Basis-URL | Benutzername und Passwort |
| OpenCode | Basis-URL | Benutzername und optionales Passwort |
| OpenSCAD | Eigener verbundener Linux-Computer | Geprüfter Binary-Pfad über **Erkennen** |
| Browser | Eigener verbundener Linux- oder Windows-Computer | [Browser einrichten](browser.md#browser-einrichten) |
| SearXNG | Globale Serveradresse mit aktivierter JSON-Schnittstelle | Je nach Server |
| Jina Reader | Globale Reader-Basis-URL | Optionaler API-Schlüssel; [Webabrufe konfigurieren](webrecherche-und-quellen.md#webabrufe-konfigurieren) |

## Rechte und Profilverfügbarkeit

Im Tab **Berechtigungen** wählst du **Standard für diese Integration** oder ein Agentenprofil. **Eigene Regel hinzufügen** legt eine Abweichung für eine Aktion oder Ressource an. **Übernehmen** aktualisiert das Formular, **Speichern** die Verbindung. Entfernen einer Regel stellt die Vererbung wieder her.

Ohne eigene Regel gilt das [Freigabeverhalten](tools-und-integrationen.md#globale-tool-vorgaben-anwenden). Eigene Verbindungs-, Profil- und Ressourcenregeln können davon abweichen. Globale Aktionsregeln für eingebaute Tools sind keine zusätzliche Obergrenze für Integrationen. Die Rechte des Dienstkontos gelten immer zusätzlich.

Ressourcen wie Kalender oder Bücher lassen sich ein- und ausschalten, ohne ihre Regeln zu löschen. Profilabweichungen gehen dem gleichen Verbindungswert vor; Ressourcenrechte sind spezieller als allgemeine Aktionsrechte. Zum Anpassen einer nur geerbten Ressource wähle **Eigene Regel hinzufügen**, die Ressource und **Verfügbarkeit**.

Die allgemeine Auswahl **Integrationen standardmäßig verfügbar** steht im Agentenprofil unter **Weitere Einstellungen**. Ausnahmen für einzelne Verbindungen bearbeitest du ausschließlich an der Verbindung. Persönliche Verbindungen können eigene Regeln für zugängliche globale oder eigene Profile erhalten; globale Verbindungen verwalten Administratoren für globale Profile.

### Werkzeuge sofort oder bei Bedarf laden

**Agentenprofil > Weitere Einstellungen > Integrationen > Integrations-Tool-Verfügbarkeit** bietet:

- **Bei Bedarf:** Der Agent sucht passende Werkzeuge und lädt ihre Beschreibungen nach.
- **Native sofort:** Produktintegrationen werden sofort geladen, generische MCP-Tools bei Bedarf. Dies ist die Vorgabe für Allgemein.
- **Alle:** Alle erlaubten Werkzeuge werden sofort geladen; große Kataloge benötigen mehr Modellkontext.

Die anderen mitgelieferten Profile verwenden **Bei Bedarf**. **Tool-Ladeverhalten anpassen** an einer Verbindung überschreibt dies für die Verbindung oder ein Profil. Das Laden gewährt keine Ausführungsrechte. Nach Kontextverdichtung können Werkzeuge erneut gesucht werden müssen.

## Kalender und Aufgaben mit CalDAV

Nach dem Verbindungstest wähle unter **Standardressourcen dieses Kontos** einen Standardkalender und eine Standard-Aufgabenliste. Unspezifische Anfragen nutzen diese Auswahl; eine ausdrücklich benannte Ressource hat Vorrang.

Den Standardkalender bekommt das Modell als Hinweis mit, sofern die Berechtigungen ihn für die jeweilige Kalenderaktion zulassen. Deaktivierte Kalender werden nicht als Hinweis mitgegeben. Das Modell muss den Zielkalender bei jedem Terminaufruf ausdrücklich angeben; MatoLoop ergänzt ihn nicht automatisch. Prüfe bei einer Freigabe den angezeigten Kalender und das Konto, bevor du zustimmst.

Bitte beispielsweise: „Lege morgen um 10 Uhr einen Termin im Kalender an.“ Termine unterstützen bis zu zehn Erinnerungen. Wiederkehrende Termine und solche mit Einladungen oder nicht unterstützten Eigenschaften lassen sich lesen, aber nicht verändern.

Aufgaben unterstützen Titel, Notiz, Fälligkeit und offen/erledigt. Erledigte Aufgaben werden standardmäßig ausgeblendet. Bis zu 50 fehlende Aufgaben können gemeinsam erstellt werden; gleichnamige offene Aufgaben werden übersprungen. Wiederholungen, Unteraufgaben, Erinnerungen, Kategorien und Prioritäten lassen sich nicht bearbeiten.

## E-Mail lesen und senden

Bei einem aktiven Konto erscheint **E-Mail** im Control Center. Wähle einen Ordner und bestätige Suchbegriffe mit Enter oder dem Suchsymbol. **Weitere laden** zeigt ältere Nachrichten oder weitere Treffer. Du kannst Anhänge herunterladen, Entwürfe speichern und senden. Externe HTML-Inhalte werden nicht nachgeladen; eine lokale Mail-Synchronisationsdatenbank gibt es nicht.

Die Kontorechte unterscheiden Lesen, Entwürfe verwalten, Senden und Löschen. IMAP-Ordner besitzen keine eigenen Rechte.

Hinterlege bei Bedarf in den Kontoeinstellungen eine **Signatur (optional)**. Sie wird beim Erstellen einer neuen E-Mail einmal in den Mailtext eingefügt. Du kannst sie dort ändern oder entfernen; gespeicherte Entwürfe bekommen keine zusätzliche Signatur. Bei einer Versandfreigabe kannst du über **Argumente bearbeiten** den Betreff und den gesamten Mailtext einschließlich Signatur ändern. Empfänger und Anhänge bleiben dabei unverändert.

!!! warning "Versand nicht blind wiederholen"
    Die Annahme durch SMTP bestätigt keine Zustellung beim Empfänger. Fehlt die Bestätigung der Kopie unter **Gesendet**, zeigt MatoLoop eine Warnung und erhält den Entwurf. Die Nachricht wurde trotzdem bereits an den Mailserver übergeben. Sende den Entwurf nicht erneut; prüfe zuerst den Mailserver. Bei einem unklaren Versand erfolgt ebenfalls keine automatische Wiederholung.

## Microsoft-365-Postfach lesen

Verbinde dein eigenes Firmen-/Schulkonto: **E-Mails lesen** ist die Grundfunktion. Entwürfe, Versand, Löschen sowie Kalender- und Aufgabenzugriffe schaltest du unabhängig zu. Freigegebene Postfächer und private Outlook.com-Konten werden nicht unterstützt.

### Entra-App registrieren

1. Registriere im [Microsoft Entra Admin Center](https://entra.microsoft.com/) unter **App-Registrierungen > Neue Registrierung** eine App nur für dein Organisationsverzeichnis. Notiere **Verzeichnis-ID (Mandant)** und **Anwendungs-ID (Client)**.
2. Ergänze unter **API-Berechtigungen > Berechtigung hinzufügen > Microsoft Graph > Delegierte Berechtigungen** `Mail.Read`. MatoLoop fordert außerdem `offline_access` zur Token-Erneuerung an. Anwendungsberechtigungen sind nicht nötig; je nach Firmenrichtlinie muss ein Administrator zustimmen.
3. Richte das gewünschte Anmeldeverfahren ein:

| Verfahren | Entra-Konfiguration | Voraussetzung |
| --- | --- | --- |
| Gerätecode | [Öffentliche Clientflows aktivieren](#geratecode-offentliche-clientflows-aktivieren) | Kein Secret und keine Rückruf-URL; Conditional Access kann Gerätecode sperren |
| Browser-Anmeldung | Unter **Authentifizierung** eine Plattform **Web** mit `https://<dein-matoloop-host>/api/integrations/microsoft365/oauth/callback` anlegen; unter **Zertifikate & Geheimnisse** ein Client-Secret erstellen | Öffentliche MatoLoop-Basis-URL unter **Administration > Grundeinstellungen** korrekt setzen; HTTPS verwenden, HTTP nur für `localhost` |

Beide Verfahren können dieselbe App nutzen. Beim Client-Secret brauchst du den **Wert**, nicht die Secret-ID. Kopiere ihn nur in MatoLoops Passwortfeld, nicht in Chat oder Logs. Nach Ablauf ersetze ihn und melde dich erneut an.

### Gerätecode: Öffentliche Clientflows aktivieren

1. Öffne in **App-Registrierungen** deine App und wähle **Authentifizierung**.
2. Stelle unter den erweiterten Einstellungen **Öffentliche Clientflows zulassen** (**Allow public client flows**) auf **Ja**.
3. Wähle **Speichern** und starte einen bereits fehlgeschlagenen MatoLoop-Anmeldeversuch neu.

Das erlaubt Gerätecode-Anmeldungen ohne Secret, macht dein Postfach aber nicht öffentlich und erteilt keine zusätzlichen Rechte. Anmeldung, Einwilligung und MFA-/Firmenrichtlinien gelten weiterhin. Für die reine Browser-Anmeldung ist der Schalter nicht nötig.

!!! warning "Nur selbst gestartete Gerätecodes verwenden"
    Verwende nur Gerätecodes aus deiner MatoLoop-Instanz nach **Bei Microsoft anmelden**. Fremde Codes aus Mails oder Chats können einer fremden Anwendung Kontozugriff geben.

### Verbinden und verwenden

Die Info-Knöpfe neben **Microsoft 365** und **Signatur (optional)** zeigen Einrichtungshinweise und Bildlimits, ohne das Formular dauerhaft zu verlängern.

1. Wähle **Einstellungen > Integrationen > Verwaltung > Integration hinzufügen > Microsoft 365**. Trage Mandanten-ID, Anwendungs-ID und Anmeldeverfahren ein, bei Browser-Anmeldung auch das Secret. Wähle **Speichern**.
2. Wähle **Bei Microsoft anmelden > Microsoft öffnen**, melde dich an und bestätige die angeforderten Rechte. Gib beim Gerätecode den angezeigten Code ein. Lass MatoLoop geöffnet, bis **Microsoft 365 ist verbunden.** erscheint. Bei Browser-Anmeldung muss die angezeigte Rückruf-URL exakt zur Entra-Registrierung passen.
3. Wähle erst jetzt **Verbindung testen** und prüfe unter **Berechtigungen** die Aktion **E-Mails lesen** sowie die Profilverfügbarkeit.

Im **Control Center > E-Mail** wählst du dein Konto. Start ist der Posteingang; **Unterordner** und **Übergeordnete Ordner** navigieren durch die Ordnerstruktur. Weitere Ordner, Mails und Anhänge lädst du bei Bedarf nach. Je nach Freigabe kannst du neue Mails mit Cc/Bcc und Anhängen verfassen, Antwortentwürfe speichern, vorhandene Entwürfe bearbeiten, senden und Mails löschen. Lade vor dem Bearbeiten alle Anhänge nach. Formatierte Outlook-Entwürfe zeigen unter **Nachricht (HTML)** ihren Quelltext; ändere HTML nur, wenn du mit dem Format vertraut bist. Ohne Textänderung bleiben Formatierung und Signatur unverändert. Normale Anhänge kannst du hinzufügen oder entfernen; eingebettete Bilder bleiben erhalten.

### E-Mails lesen

Mit `Mail.Read` kannst du etwa bitten: „Fasse meine Rechnungen von letzter Woche zusammen.“ Standardmäßig wird nur der Posteingang durchsucht; nenne andere Ordner ausdrücklich. Übersichten im Chat enthalten nur Betreff, Beteiligte, Datum und Status mit einer kurzen Referenz. Den Nachrichtentext lädt der Agent erst bei Bedarf einzeln. Nachrichten werden nicht als gelesen markiert. Ergebnisse sind seitenweise lesbar; Graph-Suchen liefern höchstens 1.000 Treffer, lange Texte werden mit Hinweis gekürzt.

#### Mailverläufe und Anhänge lesen

„Lies auch den bisherigen Mailverlauf“ bezieht passende gesendete Nachrichten ein. Die Übersicht enthält zunächst nur Grundinformationen; benötigte Nachrichtentexte lädt der Agent einzeln nach. Die Seiten sind nicht zwingend zeitlich sortiert.

„Speichere den PDF-Anhang in der Session-Ablage und fasse ihn zusammen“ benötigt zusätzlich die Schreibfreigabe für die Zieldatei; vorhandene Dateien bleiben unverändert. Normale Dateianhänge bis 20 MiB sind möglich, angehängte Outlook-Nachrichten und Cloudverknüpfungen nicht. Unterstützte Dokumente liest der Agent anschließend über die Dateifunktionen.

!!! warning "Fremde Inhalte bleiben fremde Daten"
    Mailtexte, Anhänge und Kalenderinhalte sind keine vertrauenswürdigen Anweisungen und können beim Lesen an das ausgewählte Modell gelangen. Lass unbekannte Anhänge nicht ausführen.

### E-Mail-Entwürfe aktivieren und verwenden

!!! warning "Microsofts Schreibrecht ist umfassender"
    `Mail.ReadWrite` erlaubt bei Microsoft umfassende Mailänderungen. MatoLoop trennt Entwürfe und Löschen trotzdem in eigene Optionen und Berechtigungen.

1. Ergänze in Entra unter **API-Berechtigungen > Microsoft Graph > Delegierte Berechtigungen** `Mail.ReadWrite` statt `Mail.Read`. `Mail.Send` und Kalenderrechte sind nicht nötig.
2. Aktiviere in MatoLoop **E-Mail-Entwürfe in Outlook erstellen** und wähle **Speichern**. Das setzt Anmeldung und Zugriffsfreigaben zurück. Melde dich erneut an und bestätige die Rechte. Ist die Entwurfsoption bereits aktiviert, ist keine neue Anmeldung nötig.
3. Prüfe **Berechtigungen > E-Mail-Entwürfe verwalten** und die Profilverfügbarkeit.

Bitte etwa: „Erstelle einen Entwurf an name@example.com mit Betreff Test und Text Hallo.“ Für eine Antwort nenne die ursprüngliche Mail; sie richtet sich an deren Absender beziehungsweise Antwortadresse und verwendet das Betreffpräfix **Aw:**. Unter deinem Antworttext und deiner Signatur bleibt die ursprüngliche Mail wie in Outlook als Zitat erhalten. Bei unklarem Ergebnis prüfe vor einem neuen Versuch den Entwurfsordner.

### Versand, Löschen und Signatur einrichten

1. Ergänze in Entra die benötigten **delegierten** Rechte: `Mail.ReadWrite` für Versand oder Löschen und zusätzlich `Mail.Send` für Versand. Größere Versandanhänge benötigen einen temporären Entwurf. War bisher nur `Mail.Read` mit `Mail.Send` freigegeben, melde die Verbindung erneut an.
2. Aktiviere in MatoLoop **E-Mails senden** beziehungsweise **E-Mails und Entwürfe löschen**, speichere und melde dich erneut an.
3. Prüfe unter **Berechtigungen** die Aktionen **E-Mails senden** und **E-Mails löschen**. Der Agent benötigt zusätzlich Leserechte auf angehängte Session-Dateien.

Füge deine **Signatur** aus Outlook oder Word direkt in den Editor ein; Tabellen, Farben und Abstände bleiben erhalten. Nicht mitgelieferte oder extern verlinkte Bilder werden nicht nachgeladen; der Editor weist dich darauf hin. Ergänze sie mit **Signaturbild einfügen** an der gewünschten Stelle. Erlaubt sind PNG/JPEG/WebP mit je 200 KiB, zusammen 400 KiB, höchstens acht Bildern und 2048 Pixeln je Seite. Signaturänderungen benötigen keine neue Anmeldung. Neue Mails und Antwortentwürfe erhalten die Signatur automatisch; vorhandene Entwürfe bleiben unverändert. Die Kontenabfrage zeigt dem Modell den Signaturtext und weist es an, Grußformel, Name und Kontaktdaten nicht im Mailtext zu wiederholen.

Für ausgehende Anhänge gelten die Upload-Limits deiner Instanz für Dateigröße, Anzahl und Gesamtgröße sowie die Grenzen deines Microsoft-Postfachs. MatoLoop begrenzt Anhänge einschließlich Signaturbilder zusätzlich auf insgesamt **150 MiB**; Microsoft kann kleinere Nachrichtenlimits vorgeben. Im Control Center wählst du lokale Dateien, im Chat nennst du Dateien aus der Session-Ablage. Größere Dateien werden automatisch in Teilstücken hochgeladen. Bei einem Abbruch kann ein unvollständiger Entwurf zurückbleiben: Prüfe Outlook vor einem neuen Versuch. Speichere Antworten zunächst als Entwurf und sende sie anschließend nach Prüfung.

!!! warning "Versand und Löschen prüfen"
    Prüfe Empfänger, Cc/Bcc, Text und Anhänge vor dem Versand. Microsoft bestätigt zunächst nur die Annahme, nicht die Zustellung. Bei unklarem Status prüfe Outlook und starte keinen neuen Versand. Löschen im Papierkorb kann endgültig sein.

### Kalenderzugriff aktivieren und verwenden

1. Ergänze in Entra unter **API-Berechtigungen > Microsoft Graph > Delegierte Berechtigungen** `Calendars.Read` zum Lesen oder `Calendars.ReadWrite` zum Erstellen, Ändern und Löschen. Antwortentwürfe und `Mail.ReadWrite` sind dafür nicht nötig.
2. Aktiviere in MatoLoop nach Bedarf **Kalender lesen**, **Termine erstellen und ändern** und **Termine löschen**. Wähle **Speichern**, melde dich erneut an und bestätige die Rechte. Die Änderung setzt bisherige Zugriffsfreigaben zurück.
3. Prüfe unter **Berechtigungen** die Aktionen **Kalender lesen**, **Kalender ändern** und **Kalendereinträge löschen** sowie die Profilverfügbarkeit.

Bitte etwa: „Welche Termine habe ich morgen?“ oder „Erstelle morgen von 10 bis 11 Uhr einen Termin Projektplanung in meinem Hauptkalender.“ Nenne bei anderen Zeitzonen die gewünschte Zone. Für ganztägige Termine ist das Ende der erste nicht mehr enthaltene Tag. Standard ist dein Hauptkalender. Wiederkehrende Termine werden beim Lesen für höchstens 366 Tage aufgelöst. Du kannst einen einzelnen Termin oder ausdrücklich die gesamte Serie ändern und löschen. Nicht genannte Felder bleiben unverändert. Beschreibungen bestehender Online-Besprechungen lassen sich nicht ersetzen, damit ihre Beitrittsinformationen erhalten bleiben; andere Felder kannst du weiterhin ändern.

Terminübersichten enthalten nur Titel, Beginn, Ende, Ort und Status mit einer kurzen Referenz. Beschreibungen, Teilnehmer und Wiederholungsdetails lädt der Agent erst bei Bedarf für den einzelnen Termin.

!!! warning "Besprechungen und Serien prüfen"
    Termine mit Teilnehmern versenden automatisch Einladungen. Änderungen können Aktualisierungen, das Löschen als Organisator Absagen auslösen. Prüfe Teilnehmer, Zeiten und die Auswahl zwischen Einzeltermin und ganzer Serie vor der Freigabe. Bei unklarem Ergebnis prüfe Outlook, bevor du den Auftrag wiederholst.

### Microsoft To Do aktivieren und verwenden

1. Ergänze in Entra die delegierte Berechtigung `Tasks.Read` zum Lesen oder `Tasks.ReadWrite` zum Erstellen, Ändern und Löschen.
2. Aktiviere in MatoLoop nach Bedarf **Microsoft To Do lesen**, **Aufgaben, Listen und Anhänge erstellen und ändern** und **Aufgaben, Listen und Anhänge löschen**. Speichere und melde dich erneut bei Microsoft an. Vorhandene Zugriffsfreigaben werden zurückgesetzt.
3. Prüfe unter **Berechtigungen** die Aktionen **Aufgaben lesen**, **Aufgaben ändern** und **Aufgaben löschen** sowie die Profilverfügbarkeit.

Bitte etwa: „Zeige meine Aufgabenlisten“, „Erstelle in Arbeit die Aufgabe Angebot prüfen, fällig morgen, mit Erinnerung um 9 Uhr“ oder „Markiere Angebot prüfen als erledigt.“ Du kannst Listen und Aufgaben erstellen, umbenennen, bearbeiten und löschen sowie Aufgaben abschließen und wieder öffnen. Aufgaben unterstützen Notizen, Priorität, Beginn, Fälligkeit, Erinnerungen, Wiederholungen und bereits vorhandene Outlook-Kategorien. Checklistenpunkte lassen sich hinzufügen, ändern, abhaken und entfernen; verknüpfte Links lassen sich hinzufügen, ändern und löschen. Links werden nicht automatisch geöffnet.

Aufgabenabfragen zeigen standardmäßig nur offene Aufgaben. Erledigte Aufgaben werden bereits bei Microsoft ausgefiltert und nicht erst nach dem Laden verworfen. Wenn du sie brauchst, bitte ausdrücklich darum, etwa: „Zeige in Arbeit auch die erledigten Aufgaben.“

Die Übersicht enthält nur Titel, Status, Priorität, Fälligkeit und einen Anhanghinweis mit einer kurzen Referenz. Notizen, Erinnerungen und Wiederholungsdetails lädt der Agent erst bei Bedarf für die einzelne Aufgabe. Weitere Ergebnisseiten müssen vollständig gelesen sein, bevor eine Gesamtzahl feststeht.

Anhänge kommen aus der Session-Ablage oder werden dorthin heruntergeladen. Nenne zum Hochladen die Datei und die Zielaufgabe. Es gelten die Upload-Limits deiner Instanz, einschließlich Anzahl und Gesamtgröße vorhandener Aufgabenanhänge, sowie höchstens **25 MiB pro Datei**. Größere Dateien werden in Teilstücken hochgeladen; vorhandene lokale Dateien werden beim Download nicht überschrieben. Nach einem Upload-Abbruch prüfe zuerst die Anhänge in Microsoft To Do.

!!! warning "Aufgaben und Listen löschen"
    Das Löschen einer Liste entfernt auch ihre Aufgaben mit Checklisten, Links und Anhängen. Das Löschen einer Aufgabe entfernt ihre zugehörigen Inhalte. Beim Abschließen einer Serienaufgabe kann Microsoft To Do automatisch die nächste Aufgabe erzeugen. Prüfe die Zielauswahl und wiederhole unklare Änderungen nicht ungeprüft.

Kalender und Aufgaben bedienst du bisher über den Chat; eigene Control-Center-Ansichten gibt es dafür noch nicht. **Mein Tag**, die Verwaltung von Listenfreigaben und das Anlegen neuer Outlook-Kategorien werden nicht unterstützt.

### Anmeldung erneuern oder trennen

Token werden verschlüsselt gespeichert und automatisch erneuert. Bei abgelehnter Erneuerung melde dich erneut an. Nach Backend-Neustarts musst du nur laufende Anmeldeversuche neu starten. Bei Gerätecode-Konfigurationsfehlern prüfe zuerst **Öffentliche Clientflows zulassen**.

Änderungen an App-Daten, Anmeldeverfahren, Secret oder Zusatzoptionen erfordern eine neue Anmeldung. Diese setzt Zugriffsfreigaben zurück; bei reinen Optionsänderungen bleibt das Browser-Secret erhalten. **Verbindung trennen** entfernt MatoLoops Token, nicht die App-Konfiguration oder Microsofts Einwilligung. Widerrufe die Einwilligung bei Bedarf zusätzlich in Entra beziehungsweise deinen Microsoft-App-Berechtigungen.

## Rezepte und Einkaufsliste mit Mealie

Der Agent kann Rezepte suchen, lesen, erstellen, bearbeiten, per URL importieren und löschen sowie den Essensplan verwalten. Nenne bei Suchfiltern die exakten Tags, Kategorien oder Lebensmittel.

Wenn du ein neues Rezept entwickeln lässt, nutzt der Agent deine vorhandenen Angaben und fragt fehlende Portionenzahl und relevante Vorlieben zusammen ab. Bei der Übernahme aus einer URL, einem Text, PDF oder Foto fragt er nicht unnötig nach, wenn Quelle und Auftrag ausreichen. Unklare Quellenangaben erfindet er nicht; nur wenn sie den Auftrag blockieren, fragt er nach.

Bitte den Agenten, eine Bilddatei aus der Session-Ablage oder vom ausgewählten Computer als Rezeptbild zu setzen. Das ersetzt das bisherige Hauptbild. Dafür braucht er sowohl Leserechte auf die Bilddatei als auch Schreibrechte für Rezepte. Nach einem unklaren Upload-Ergebnis prüfe zuerst das Rezept in Mealie, bevor du den Upload wiederholst.

Nicht angegebene Rezeptfelder bleiben erhalten; übergebene Zutaten- oder Zubereitungslisten ersetzen die jeweilige Liste vollständig. Nach einem unklaren Schreibergebnis prüfe Mealie vor einer Wiederholung: Bei einer Erstellung kann bereits ein teilweise befülltes Rezept vorhanden sein.

Für eine Einkaufsliste richte eine CalDAV-Aufgabenliste ein und wähle sie als Standard. Bitte den Agenten, den Wochenplan und die Rezepte zu lesen und fehlende Einkaufsaufgaben anzulegen. Er fasst kompatible Mengen zusammen; unklare Einheiten bleiben getrennt. Bestehende offene Aufgaben bleiben erhalten. Es gibt weder automatische Synchronisierung noch native Mealie-Einkaufslisten.

## Paperless-ngx verbinden

Wähle beim Anlegen den Typ **Dokumente** und den Anbieter **Paperless-ngx**. Trage die Basis-URL deiner Instanz und den API-Token ein, speichere die Verbindung und teste sie. Beim Bearbeiten lässt du das Token-Feld leer, um den gespeicherten Token beizubehalten.

Verwende außerhalb eines vertrauenswürdigen lokalen Netzes HTTPS: Über HTTP werden Token und Dokumentinhalte unverschlüsselt übertragen.

Verwende ein Konto mit den benötigten Dokumentrechten statt eines Administratorkontos. Die Paperless-ngx-Kontorechte begrenzen den Zugriff zusätzlich zu den MatoLoop-Freigaben. Persönliche und globale Verbindungen verwenden dieselben Verfügbarkeits- und Berechtigungseinstellungen wie andere Integrationen.

Der Agent kann Dokumente suchen, OCR-Text lesen, Dateien übertragen, Metadaten und Notizen bearbeiten sowie Tags, Korrespondenten, Dokumenttypen, Ablagepfade und eigene Felder verwalten. Stapelaktionen erlauben Klassifizierung, Neuverarbeitung und PDF-Bearbeitung. Versionsfunktionen benötigen eine entsprechend ausgestattete Paperless-Instanz; Dokumentaktionen verwenden API-Version 9, paginierte Statusabfragen Version 10. Auf älteren Instanzen benötigt die Statusabfrage die konkrete Task-ID des Auftrags; die gesamte Historie wird nicht geladen. Dateiübertragungen sind auf 32 MiB begrenzt.

Klassifizierung läuft im normalen Chat oder in einer von dir angelegten Automation. Beschreibe dort, welche Dokumente bearbeitet werden sollen und ob neue Kategorien angelegt werden dürfen. Es gibt keinen zusätzlichen Klassifizierungsdienst und keine automatische Archivkopie. Vergib für unbeaufsichtigte Läufe vorher die benötigten Lese-, Schreib- und gegebenenfalls Löschrechte.

!!! warning "Bestehende Daten gezielt ändern"
    Vollständige Tag- und Feldlisten ersetzen die bisherigen Zuordnungen; additive Stapelaktionen erhalten nicht genannte Felder, überschreiben aber ausdrücklich gesetzte Werte. Leerungen, PDF-Änderungen am Original sowie Änderungen bestehender Feldtypen und Auswahloptionen benötigen zusätzlich Löschrechte. Entfernte Auswahloptionen können Werte aller zugeordneten Dokumente leeren. Endgültiges Löschen im Paperless-Papierkorb ist nicht rückgängig zu machen. Bei externer OCR kann Paperless Dokumente an seinen konfigurierten OCR-Dienst senden. Synchrone Metadatenänderungen werden direkt bestätigt; Uploads und Hintergrundaufträge sind erst nach erfolgreicher Verarbeitung abgeschlossen. Prüfe bei unklarem Ausgang den Status oder die betroffenen Dokumente vor einer Wiederholung. Gleichzeitige Änderungen in Paperless können durch vollständige Ersetzungen überschrieben werden.

## Wissen mit BookStack verwalten

Der Agent kann Seiten, Bücher, Kapitel und Regale verwalten; Seiteninhalte werden als Markdown ausgetauscht. Regalzuordnungen bearbeitest du über das Regal. Das Löschen eines Regals löscht seine Bücher nicht.

Rechte einzelner Bücher begrenzen auch Suche, Bücherlisten, Regalzuordnung und Anhänge. Nicht erlaubte Bücher bleiben bei einer vollständigen Regaländerung erhalten. Die Ressourcenverwaltung wird bei mehr als 500 sichtbaren Büchern abgelehnt, statt Regeln für unsichtbare Bücher zu übergehen.

Anhänge können aus einer URL, der Session-Ablage, einer Computerdatei oder einem reinen Link stammen. Uploads sind auf 32 MiB begrenzt; Quellzugriff und Webabrufe benötigen ihre eigenen Rechte. Bei einem fehlgeschlagenen Download kann der Agent stattdessen einen Link hinterlegen.

!!! warning "Eine Seitenänderung ersetzt den gesamten Inhalt"
    Der Agent liest die Seite vor dem Schreiben erneut und lehnt erkannte Versionskonflikte ab. BookStack unterstützt jedoch kein bedingtes Speichern: Eine zeitgleiche Änderung zwischen Prüfung und Speichern kann überschrieben werden. Sehr große Seiten werden deshalb nicht gekürzt zum Zurückschreiben geliefert.

## Docker mit Arcane verwalten

Wähle nach dem Test die erlaubten Umgebungen und Aktionen. Der Agent kann Container, Projekte, Images, Volumes und Netzwerke untersuchen und angebotene Verwaltungsaktionen ausführen. Compose- und Umgebungsinhalte können vertrauliche Daten enthalten; dafür gelten zusätzliche sensible Zugriffsrechte. Container-exec und Massenbereinigung gehören nicht dazu.

Das Speichern einer Projektkonfiguration startet kein Deployment. Nach einem unklaren Änderungsergebnis prüfe Arcane vor einer Wiederholung; MatoLoop wiederholt die Mutation nicht automatisch. Die Arcane-Rechte des API-Schlüssels bleiben bindend.

## Musik mit SonoBin verwenden

Suchen, Playlists, Likes und entfernte Imports folgen den SonoBin-Kontorechten. Playliständerungen laden keine Musik herunter. Bei einem Import verfolge den Auftrag bis zum bestätigten Bibliotheksimport; ein fertiger Download reicht dafür nicht.

SonoBin-Tools teilen derzeit die Kategorie **Schreiben**. Auch reine Leseaufrufe benötigen unter den ausgelieferten Grundstandards eine Freigabe, sofern keine eigene Regel sie erlaubt. Administration, Diagnostik und Assetdownloads brauchen zusätzlich passende SonoBin-Berechtigungen und Serverkonfiguration.

## Support mit tbs.Desk

Teste und aktiviere die persönliche Verbindung. Sie stellt den **Support-Assistent** und die tbs.Desk-App bereit. Der Agent sucht anhand eines Kundenhinweises oder aktiven Anrufs passende Kunden. Wähle den Kunden aus dem Ergebnis; Ticketzugriff wird auf ihn begrenzt.

## Projekte mit OpenCode verbinden

1. Lege die persönliche OpenCode-Verbindung an; ein MatoLoop-Computer ist dafür nicht nötig.
2. Öffne ein Projekt und wähle **Control Center > OpenCode > OpenCode verbinden**, dann Server, erkanntes Projekt und Arbeitsverzeichnis.
3. Wähle unter **OpenCode > Modelle** getrennte Analyse- und Ausführungsmodelle. **OpenCode-Standard** übernimmt die Modellvariante; andere angebotene Reasoning-Varianten gelten für das gewählte Modell.
4. Beauftrage im Projektchat Analyse und Planung. OpenCode untersucht das Repository schreibgeschützt; MatoLoop speichert daraus den Plan.
5. Wähle den gewünschten Plan und reihe ihn in die Ausführungswarteschlange ein.

Die Bindung kann bis zum ersten gespeicherten Plan geändert oder getrennt werden. Die Dateiansicht ist schreibgeschützt. Pro Projekt läuft höchstens eine Ausführung. Externe OpenCode-Aktivität oder ein unklarer Ausgang pausiert die Warteschlange; prüfe vor dem Fortsetzen den tatsächlichen Arbeitsbaum. Freigaben erscheinen mit **Einmal erlauben**, **Immer erlauben** oder **Ablehnen**.

## 3D-Modelle mit OpenSCAD erstellen

Der Docker-Computer enthält OpenSCAD bereits; native Linux-Computer benötigen eine Installation. Lege eine Verbindung an, wähle den Computer und übernimm den Binary-Pfad mit **Erkennen**. Der Test prüft auch ein begrenztes Rendering.

Beschreibe im Chat das Modell mit Abmessungen und Randbedingungen. Neue Dateien liegen standardmäßig in der Session-Ablage; vorhandene Computerprojekte können ausdrücklich gewählt werden. Der Agent rendert und prüft Diagnosen, Abmessungen, Topologie und Ansichten. Ohne bildfähiges Modell oder **Standardmodelle > Bildanalyse** bleibt das Ergebnis visuell ungeprüft.

Unter **Ablage** oder **Computer-Dateien** wechselst du bei `.scad` zwischen Quelle und 3D-Ansicht und exportierst ausdrücklich STL. Vorschaugeometrie und Prüfbilder sind temporär; STL-Exporte werden dauerhaft gespeichert. Rendering zählt als Schreiben und benötigt standardmäßig Zustimmung.

!!! warning "Keine Garantie für Druckbarkeit oder Festigkeit"
    Ein erfolgreicher Render und eine Sichtprüfung beweisen weder verdeckte Geometrie noch Passung, Wandstärke, Festigkeit oder Druckbarkeit. OpenSCAD arbeitet mit den Betriebssystemrechten des Agents, nicht in einer OS-Sandbox. Große Modelle können nur zum Download verfügbar sein.

## MCP-Server integrieren

Generische Server richtest du unter **Einstellungen > MCP-Server** oder **Administration > MCP-Server** ein. Persönliche Server benötigen administrative Freigabe. Unterstützt wird Streamable HTTP ohne Anmeldung, mit Bearer-Token oder OAuth. HTTP überträgt Zugangsdaten unverschlüsselt; verwende außerhalb eines vertrauenswürdigen Netzes HTTPS.

Prüfe nach dem Speichern den erkannten Tool-Katalog und dessen Rechte. Ein fehlgeschlagener Refresh erhält den vorherigen Katalog. Namen und Beschreibungen sind durchsuchbar, aber unbestätigte Serverangaben: Sie gewähren keine Rechte. MCP-Tools teilen derzeit ebenfalls die Kategorie **Schreiben**. Produktadapter wie SonoBin richtest du im eigenen Produktbereich ein.

### Mit OAuth anmelden

1. Prüfe die öffentliche Adresse unter **Administration > Grundeinstellungen**; sie muss den Ursprung ohne Pfad enthalten.
2. Wähle **Anmeldung öffnen** und bestätige beim Anbieter.
3. Beende die Anmeldung in MatoLoop.

Der Server muss MCP-Authentifizierungsmetadaten und Bearer-Zugriff unterstützen; Browser-SSO allein reicht nicht. Ein vorregistrierter Client benötigt `<ursprung>/api/mcp/oauth/callback`. Nach Ablauf oder Backend-Neustart starte den Anmeldeversuch erneut. Gespeicherte Verbindungen bleiben erhalten; nach abgelehnter Token-Erneuerung ist eine neue Anmeldung nötig. Tool-Aufrufe werden nicht automatisch wiederholt.
