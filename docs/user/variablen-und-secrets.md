# Variablen und Secrets

**Variablen & Secrets** in der Seitenleiste verwaltet persönliche Werte benutzerweit. Namen und Beschreibungen helfen dem Agenten bei der Auswahl; Zugangsdaten gehören ausschließlich in das Wertfeld.

## Gespeicherte Werte verwalten

1. Wählen Sie **Variable hinzufügen**.
2. Geben Sie einen eindeutigen Namen, optional eine Beschreibung und den Wert ein.
3. Aktivieren Sie für Zugangsdaten **Als Secret schützen** und speichern Sie.

Normale Variablen zeigen ihren Wert an; Secrets sind nach dem Speichern ausgeblendet und werden auch dem Modell nicht als Klartext zurückgegeben. Namen sind ohne Beachtung der Groß-/Kleinschreibung eindeutig. Werte liegen verschlüsselt im Credential-Speicher und gehören nur Ihrem Konto.

Beim Bearbeiten eines Secrets können Sie **Neuer Wert (optional)** leer lassen, um den gespeicherten Wert zu erhalten. Für einen Austausch geben Sie einen neuen Wert ein. Löschen benötigt eine Bestätigung und kann spätere davon abhängige Tool-Aufrufe verhindern; bereits erfolgte externe Verwendung lässt sich dadurch nicht rückgängig machen.

## Geschützte Rückfragen

Benötigt ein Tool ein Passwort oder einen Token, kann MatoLoop eine geschützte Rückfrage anzeigen. Der Wert wird im Backend aufgelöst; der Agent erhält eine undurchsichtige Referenz für ausdrücklich dafür geeignete Tool-Felder, etwa eine Prozessumgebung oder Verbindungskonfiguration. Die Referenz selbst ersetzt keine Aktionsfreigabe.

- Eine einmalige Eingabe bleibt standardmäßig nur im aktuellen Lauf verfügbar und verschwindet mit dessen Ende.
- **Für diese Session merken** behält einen ausgefüllten Wert in Session-Arbeitsspeicher für spätere Läufe. Ein Backend-Neustart verliert diese temporären Werte.
- Dauerhafte Speicherung unter Variablen & Secrets erfolgt separat mit einer ausdrücklich bestätigten Speicherrückfrage oder Ihrer eigenen Oberflächeingabe.

Unter **Session > Freigaben** sehen Sie auch die Namen der aktuell für diese Session gemerkten Variablen und Secrets, niemals deren Werte. Diese Einträge sind nur zur Ansicht und erteilen keine Aktionsrechte; **Alle Session-Freigaben widerrufen** entfernt die gemerkten Werte nicht.

Mit **Fertig** können Sie auch nur einen Teil der Felder beantworten oder alle auslassen. Nur ausgefüllte Felder erzeugen Referenzen oder gespeicherte Werte. Eine leere Speicherrückfrage legt nichts an und verändert nichts. Ein ausgelassener benötigter Wert blockiert die davon abhängige Arbeit; er wird nicht geraten. Senden Sie eine geänderte Arbeitsanweisung als normale Nachricht, nicht in einem Credential-Feld.

## Administratorzugriff

Unter **Computer > Verwalten > Einstellungen > sudo-Passwort** können Sie ein eigenes Secret als Standard für diesen Computer zuordnen. Es wird für native Linux-Updates und Admin-Ausführungen verwendet. Im Chat gelten weiterhin die Secret-Zugriffsregeln und die Freigabe der konkreten Aktion. Explizite Passwortreferenzen und temporäre Lauf-/Session-Passwörter haben Vorrang. Das Entfernen des Computers oder der Zuordnung löscht das persönliche Secret nicht.

Fehlt bei einer Administratoraktion ein Passwort und ist kein passwortloses sudo verfügbar, öffnet MatoLoop die geschützte Eingabe direkt und setzt danach mit der Aktionsfreigabe fort. Überspringen oder Abbrechen stoppt diesen Aufruf. Der Agent kann Zugangsdaten weiterhin vorab oder gemeinsam abfragen.

Ein geschütztes Administratorpasswort ist an den ausgewählten eigenen Computer gebunden. Eingeben, Auswählen oder **Für diese Session merken** dient nur der Authentifizierung und erzeugt keine Zugriffsrechte. Administratoraktionen werden unabhängig davon nach den wirksamen Regeln und passenden Freigaben geprüft: Dateifreigaben gelten für exakte Pfade, Shell-Freigaben für exakte Befehle samt Computer, Arbeitsverzeichnis, Umgebung und Administrator-Einstellung.

Diese Rechte überstimmen weder deaktivierte Aktionen noch Computer-/Pfadgrenzen. Ein abgelehntes Passwort kann erneut geschützt angefragt werden. Das Betriebssystem bleibt für sudo-Rechte zuständig; der Agent wird dadurch nicht dauerhaft als Administratordienst betrieben.

!!! warning "Verwendung ist keine Sandbox"
    Ein autorisierter Prozess oder externer Dienst erhält den tatsächlich verwendeten Wert. Ausgabe wird gefiltert, aber ein Programm kann Werte umformen oder speichern. Prüfen Sie daher Tool-Ziel und Rechte; tragen Sie Secrets nicht in gewöhnliche Chat-, Persona-, Skill- oder Beschreibungsfelder ein.

Siehe [Freigaben](freigaben-und-datenfreigabe.md) und [Datensicherung](betrieb-und-datensicherung.md#zu-sichernde-daten) für Berechtigungen und den zugehörigen Verschlüsselungsschlüssel.
