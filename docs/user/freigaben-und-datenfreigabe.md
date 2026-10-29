# Freigaben und Datenfreigabe

## Session-Freigaben

Der eigene Session-Tempordner ist standardmäßig lesbar. Die Profiloption
**Temporären Arbeitsordner automatisch freigeben** ergänzt den Schreibzugriff;
Plan und Build aktivieren sie standardmäßig. Löschen und fremde Sessionordner benötigen normale Dateirechte.
Unter **Session > Freigaben** sind diese abgeleiteten Rechte mit Herkunft und konkretem Pfad sichtbar,
ohne zusätzliche gespeicherte Freigaben oder Widerruf-Buttons. Den Schreibzugriff ändern Sie am Profil.
Auch dort gelten Computersperren, Werkzeugverbote und die Prüfung vertraulicher Daten.
Der eigene Pfad ist eine Zugriffsgrenze der Dateitools, keine Betriebssystem-Sandbox; legen Sie dort keine Secrets ab.

Unter **Session > Freigaben** sehen Sie erteilte und abgeleitete Sessionrechte mit Herkunft und Geltungsbereich. Computer und Arbeitsverzeichnis wählen Sie in den eigenen Feldern des Sessionbereichs. Eine Ordnerwahl erlaubt Navigation, keine Datei- oder Shell-Aktionen. Weitere automatische Regeln und konfigurierte Shell-Präfixe gelten auch bei leerer Freigabeliste. Eine Freigabe überstimmt keine deaktivierte Aktion oder harte Grenze.

## Freigabeanfragen

Benötigt eine zulässige Aktion Zustimmung, pausiert der ausführende Agent. Die kompakte Freigabekarte zeigt die Aktion, das Ziel, gegebenenfalls die Begründung des Agenten und wesentliche Folgen. Unter **Freigabe für** steht die tatsächlich freigegebene Ressource; sie kann weiter reichen als das einzelne angezeigte Objekt. Über das Stiftsymbol bearbeiten Sie unterstützte Argumente. Normale Datei- und Ordnerfreigaben zeigen Pfad und Rechte statt einer Inhaltsvorschau; an konkrete Änderungen gebundene Bestätigungen behalten die nötigen Inhalte. Statt pauschaler Risikostufen zeigt die Karte die vorhandenen Aktionskategorien und konkrete Hinweise wie **Mit Administratorrechten** oder **Analyse unvollständig**. **Deaktiviert** und harte Grenzen sind dagegen keine durch Zustimmung überstimmbaren Rückfragen.

**Control Center > Benachrichtigungen** verweist auf die Quell-Session; dort entscheiden Sie im Gesprächskontext. Dreaming-Hinweise öffnen **Memory > Dreaming > Offene Vorschläge**. Lesen oder Löschen eines Hinweises entscheidet nichts. Anfragen bleiben bei geschlossenem Browser offen, solange ihr Lauf aktiv ist. Ein Backend-Neustart beendet aktive Läufe und lässt deren Anfragen ablaufen; unabhängige Memory-Vorschläge bleiben erhalten.

## Entscheidungen

| Entscheidung | Wirkung |
| --- | --- |
| Ablehnen | Die angefragte Aktion wird nicht ausgeführt. |
| Für diesen Lauf erlauben | Passender angezeigter Zugriff gilt bis zum Ende des ausführenden Laufs. |
| Für Session erlauben | Passender Zugriff gilt auch für spätere Läufe derselben Session bis zum Widerruf. |

Der Haken **Für diese Session merken** ändert den Hauptbutton zur Session-Freigabe. Er ist bei neuen Anfragen ausgeschaltet. Wenn **Vertrauliche Pfade und mögliche Secret-Ausgabe** auf **Nachfragen** steht, bieten betroffene Aktionen keine Session-Freigabe; ihre Zustimmung bleibt an exakte Werte und geprüfte Revisionen gebunden. Dasselbe gilt für technisch nicht wiederverwendbare Aktionen.

Reine Rechteanfragen heißen **Zugriff freigeben** beziehungsweise **Shell-Zugriff freigeben**. Sie erteilen die angezeigten Rechte für spätere Aktionen und führen jetzt keine Aktion aus.

Unter **Session > Freigaben** sehen Sie Kategorie, Computer und Ressource. Explizit erteilte Sessionrechte sind einzeln oder gemeinsam widerrufbar; abgeleitete Rechte bleiben davon unberührt. Laufrechte erscheinen dort nicht und bleiben von **Alle Session-Freigaben widerrufen** unberührt. Die Einträge sind keine offenen Anfragen und zählen nicht zum Benachrichtigungszähler. Reload beendet eine Freigabe nicht. Profil, Vertrauenslevel, Computer/Ressource, Kategorie und gegebenenfalls Integrationsrevision grenzen sie weiterhin ein. **Inaktiv** kennzeichnet eine nicht mehr passende Bindung, Computersperre oder einen nicht mehr unterstützten alten Freigabetyp. Ein Subagenten-Laufrecht überträgt sich nicht auf andere Läufe.

Eine Freigabe besteht aus **Zugriffsrecht, Ressourcenbereich und Laufzeit**. **Lesen und Schreiben** erlaubt auch normales Lesen desselben Bereichs, etwa zur Kontrolle einer Änderung. **Lesen**, **Löschen** und **Vertrauliche Daten lesen** bleiben ansonsten unabhängig. Diese Regel gilt für Dateien, Shell und Integrationen. Die effektiven Profil- und Integrationsregeln bleiben maßgeblich: Eine Freigabe erfüllt eine Nachfrage, überstimmt aber kein Verbot. Schon automatisch erlaubte Aktionen benötigen keine Zustimmung.

Neue Freigaben für einzelne Dateien gelten exakt für den angezeigten Pfad. Erkannte Ordner und neu anzulegende Ordner schließen Unterpfade ein. Kann der Pfadtyp nicht ermittelt werden, wird vorsichtshalber ein Bereich einschließlich Unterpfaden angefragt; reine Dateiaktionen wie Schreiben und Bearbeiten bleiben exakt. Die Liste nennt eingeschlossene Unterpfade im Klartext, ohne `/**` an den Pfad anzuhängen. Exakte Freigaben erlauben keine rekursiven Aktionen, wenn an der Stelle später ein Ordner liegt. Bestehende Freigaben behalten ihren bisherigen Umfang. Standardmäßig wird nur der angefragte Pfad freigegeben. Unter **Freigabe für** können Sie bei normalen Dateianfragen stattdessen den übergeordneten Ordner einschließlich Unterordnern auswählen. Die Auswahl nennt den vollständigen Pfad und ob Unterpfade eingeschlossen sind; sensible und Administratorzugriffe behalten ihre engeren Grenzen. Für gezielte Ordnerfreigaben kann der Agent über **Session-Freigaben anfragen** Lese-, Schreib- oder Löschzugriffe vorbereiten, ohne die vorgeschlagene Aktion auszuführen. Eine solche Freigabe wechselt nicht das Arbeitsverzeichnis. Geänderte Dateiinhalte allein heben sie nicht auf. Dateifreigaben erlauben keine Shell-Ausführung.

Die Workspace-Begrenzung hängt vom Agentenprofil ab: **Zugriff außerhalb des Arbeitsverzeichnisses** kann automatisch erlaubt, nachgefragt oder verboten werden. Plan und Build fragen außerhalb nach; Sysadmin hat standardmäßig keine Workspace-Begrenzung. Die jeweiligen Lese-, Schreib- und Löschregeln gelten trotzdem. Innerhalb des Arbeitsordners erlaubt Plan normale Datei-Leseaktionen und fragt bei Schreiben und Löschen nach; Build erlaubt Lesen, Schreiben und Löschen automatisch. Plan bleibt unabhängig von Freigaben auf Analyse und Planung beschränkt. Vertrauliche Daten behalten ihre eigene Prüfung.

Bei Integrationen gilt die Freigabe für die vom Adapter bezeichnete Ressource, zum Beispiel einen Kalender, ein Buch oder eine Rezeptsammlung, innerhalb derselben Verbindung. Weitere Aktionen der erlaubten Zugriffsarten können sie wiederverwenden; ihre jeweiligen Richtlinien werden trotzdem geprüft. Es gibt keine automatische Freigabe übergeordneter oder anderer Ressourcen.

Beim Wechsel auf dieses Modell werden alle vorhandenen Freigaben einmalig zurückgesetzt; offene Agenten-Freigabeanfragen laufen ab. Bestätigen Sie benötigte Zugriffe erneut. Für eine Navigationsfreigabe können Sie das Arbeitsverzeichnis ausdrücklich neu auswählen. Ihre Richtlinien, Presets und offenen Memory-Vorschläge bleiben erhalten.

Administrator-Dateizugriffe bleiben gesondert markiert und an den exakten Pfad gebunden. Frühere Freigabeformate werden nach dem einmaligen Zurücksetzen nicht mehr zur Autorisierung verwendet.

### Shell-Aktionen

Agentenbefehle laufen in frischen Prozessen im Sessionordner. Eine Shell-Freigabe prüfen Sie an der Anfrage; die persönliche Terminalzeile ist kein Freigabemechanismus. **Strg+C** im persönlichen Terminal betrifft dessen Prozess, der Stoppknopf im Chat den Agentenlauf. Als Lesen eingestufte Befehle können unbekannte Nebenwirkungen haben; siehe [Tools und Integrationen](tools-und-integrationen.md#lokale-funktionen).

Normale Shell-Anfragen verwenden unter **Freigabe für** standardmäßig **Diesen Befehl**: Die Freigabe bindet den exakten Befehl an Computer, Arbeitsverzeichnis und Umgebung. Wiederholungen im gleichen Kontext sind innerhalb der gewählten Laufzeit abgedeckt; andere Befehle können erneut fragen. Für längere Aufgaben können Sie ausdrücklich **Shell auf diesem Computer** auswählen. Dann deckt die Freigabe weitere passende Befehle unabhängig vom Arbeitsordner ab. **Lesen und Schreiben** umfasst normale Lese- und Schreibbefehle; Löschen bleibt separat. Der Workspace ist der Startordner und keine Shell-Zugriffsgrenze. Die Profilregeln für Shell-Lesen, Shell-Schreiben und erkannte Shell-Löschaktionen bestimmen, ob automatisch ausgeführt, nachgefragt oder blockiert wird. Plan und Sysadmin fragen bei erkannten Schreib- und Löschaktionen nach; Build erlaubt alle drei Kategorien. Computerweite Sperren und die Prüfung vertraulicher Daten gelten weiterhin.

Gemischte Befehle benötigen alle betroffenen Kategorien: `touch neu.txt; rm alt.txt` benötigt Schreiben und Löschen. Erkannte Lösch- und Leerungsbefehle werden separat eingestuft; normales Schreiben oder Überschreiben bleibt Schreiben. Administratorbefehle und Befehle mit expliziten Umgebungswerten behalten ihre exakte Befehls-/Kontextbindung. Vorhandene optionale Präfixregeln ersetzen keine Löschfreigabe.

Die Shell-Analyse erkennt bekannte Wirkungen, untersucht aber nicht vollständig, was beliebige Programme oder Skripte intern tun. Shell-Rechte sind keine Betriebssystem-Sandbox. Das Modell soll angebotene Datei-Tools für unterstütztes Lesen, Suchen und Ändern verwenden; Shell dient unter anderem Builds, Tests, Git und Prozesssteuerung. Ein Toolwechsel darf keine verweigerte Aktion umgehen.

Unterstützt ein Computer-Agent eine benötigte Protokollfunktion noch nicht, erscheint ein allgemeiner Aktualisierungshinweis. Aktualisieren Sie ihn über den vorhandenen Computer-Updateweg. Bis dahin bleibt die betroffene Funktion gesperrt, während kompatible Aktionen weiter nutzbar sind. Die getrennten Shell-Kategorien benötigen einen entsprechend aktualisierten Agenten.

## Vertrauenslevel und Datenfreigabe

- **Privat** bezeichnet einen von Ihnen als vertrauenswürdig eingestuften Provider.
- **Öffentlich** behandelt ihn als externen Empfänger und verwendet die dafür konfigurierten Berechtigungen und Memory-Zielgruppen.

Unter **Administration > Provider > Einstellungen > Modelltrennung** aktivieren Sie die Unterscheidung privater und öffentlicher Modelle. Die Einstufung legen Sie pro Provider fest. Bei aktiver Modelltrennung gelten dafür getrennte Tool-Rechte, Erinnerungen und persönliche Profilangaben. **Grundeinstellungen** verlinkt unter demselben Namen direkt dorthin.

- Die Änderung wird sofort gespeichert.
- Ohne Trennung gelten für alle Modelle dieselben Datenfreigaben. Auch Cloud-Modelle können dann privat freigegebene Erinnerungen und Profilangaben erhalten.
- Die Auswahlfelder für die Trennung werden ausgeblendet; Berechtigungen verwenden den privaten Zweig unter **Alle Modelle**.
- Gespeicherte Einstufungen und öffentliche Regeln bleiben zum Wiedereinschalten erhalten.
- Laufende Läufe behalten ihre eingefrorene Einstufung.

Die Einstellung prüft weder Lokalität noch TLS oder Betreiber. Mit der Modellwahl entscheiden Sie über die Übertragung bereits autorisierter Nachrichten, Dateien, Tool-Ergebnisse und Integrationsdaten an diesen Provider. Es gibt dafür keine zweite pauschale Session-Datenfreigabe. Eigentümergrenzen, Memory-Zielgruppen und Aktionsrechte gelten trotzdem. Ein Widerruf kann bereits übertragene Daten nicht zurückholen.

Unter **Session > Session-Einstellungen > Vertraulichkeit** stufen Sie den gesamten Verlauf als **Privat** oder **Öffentlich** ein. Beim ersten Senden übernimmt die Session die wirksame Einstufung des gewählten Modells, sofern Sie sie nicht vorher ausdrücklich gesetzt haben. Spätere Modellwechsel ändern sie nicht. Die Einstufung erscheint nicht mehr an einzelnen Nachrichten und teilt die Session nicht mit anderen Nutzern.

Private Modelle können eigene Sessions beider Einstufungen mit normalen Leserechten nutzen. Öffentliche Modelle erhalten automatisch nur öffentliche Sessions, einschließlich Titel und Vorschauen. Das gezielte Lesen einer anderen privaten Session oder eine Suche/Auflistung einschließlich privater Sessions benötigt **Vertrauliche Daten lesen**. Öffentliche Modelle sollen private Sessions nur auf ausdrückliche Nutzeranfrage einbeziehen. Die eigene aktuelle Session bleibt durch Ihre Modellwahl freigegeben.

Dreaming verarbeitet beide Einstufungen mit dem von Ihnen konfigurierten Modell ohne zusätzliche Rückfrage. Neue Erinnerungen aus privaten Sessions sind nur für private Modelle sichtbar; öffentliche Sessions erzeugen für beide Modellbereiche sichtbare Erinnerungen. Bereits erzeugte Erinnerungen werden beim Umstellen nicht verändert. **Privat → Öffentlich** betrifft auch den bisherigen Verlauf und verlangt eine Bestätigung. **Öffentlich → Privat** holt bereits übertragene Daten nicht zurück.

Ohne Modelltrennung ist das Dropdown ausgeblendet. Alle Modelle und Sessions gelten wirksam als privat; Session-Zugriffe nutzen normale Leserechte und neue Erinnerungen sind privat eingestuft. Gespeicherte Einstufungen bleiben für das erneute Aktivieren erhalten.

Pseudonymisierung ist unabhängig vom Vertrauenslevel am Provider und optional am Modell einstellbar. Die ausdrückliche Auswahl **Öffentlich** schaltet sie im Formular zunächst ein; Sie können sie danach ausschalten. Die Auswahl **Privat** oder das Abschalten der Trennung ändert den gespeicherten Pseudonymisierungswert nicht. Sie erkennt bestimmte Kennungen und strukturierte Secrets, aber nicht beliebige vertrauliche Aussagen, Personen oder Orte. Bilder, Audio und andere Binärdaten werden nicht pseudonymisiert. Die Erkennung ersetzt keine bewusste Provider- und Datenwahl.

## Rückfragen und geschützte Eingaben

Normale Rückfragen erlauben Auswahl/Freitext und **Fertig** auch bei unbeantworteten Fragen. Sie erzeugen keine Tool-Rechte. Geschützte Credential-Felder verbergen Werte vor dem Modell und liefern nur Referenzen. Auch ein eingegebenes oder gemerktes Administratorpasswort dient ausschließlich der Authentifizierung; Aktionen benötigen weiterhin passende Regeln oder Freigaben. Die Einzelheiten stehen unter [Variablen und Secrets](variablen-und-secrets.md).

## Memory und Personalisierung

Änderungen an Memory, Persona und persönlichem Profil können eine Freigabe benötigen. Prüfen Sie Inhalt und privaten/öffentlichen Modellbereich, weil die Wirkung über diese Antwort hinausgeht. Memory-Vorschläge haben zusätzlich veraltete-Ziel-Prüfungen.

!!! warning "Keine Secrets in Prompts"
    Verwenden Sie geschützte Eingaben für Passwörter und Tokens. Normale Chat-, Persona- und Skilltexte können an Modelle übertragen werden; automatische Erkennung deckt nicht jede Darstellung ab.
