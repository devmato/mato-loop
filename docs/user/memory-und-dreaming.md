# Memory und Dreaming

Memory ermöglicht MatoLoop, passende Informationen über einzelne Sessions hinaus zu verwenden. Die Funktion muss in den Memory-Einstellungen aktiviert sein und wird zusätzlich je Session gesteuert.

## Session-Modi

| Modus | Frühere Sessions und Erinnerungen nutzen | Erinnerungen schreiben | Dreaming |
| --- | --- | --- | --- |
| Voll | Ja | Ja | Ja |
| Nur lesen | Ja | Nein | Nein |
| Deaktiviert | Nein | Nein | Nein |

Provider- und Modelleinstellungen können den zulässigen Umgang mit öffentlichen und privaten Inhalten weiter einschränken.

## Über mich

Unter **Einstellungen > Personalisierung > Über mich** fügen Sie Angaben aus einem festen Katalog hinzu und pflegen dort vollständigen und bevorzugten Namen, Anrede, Pronomen, Hauptsprache und Hauptwohnsitz. Diese Angaben sind kein Memory: Sie gelten unabhängig vom Memory-Modus, sind weder durchsuchbar noch Teil von Dreaming. Der Agent kann sie nur auf Ihre ausdrückliche Bitte über ein eigenes Tool ändern oder entfernen; die Tool-Richtlinie bestimmt, ob Sie die Änderung einzeln freigeben müssen.

Für jede Angabe können Sie zwischen privaten und öffentlichen Modellen umschalten. Mit **Auch für öffentliche Modelle verwenden** übernimmt das öffentliche Feld den privaten Wert; andernfalls pflegen Sie beide Werte getrennt. Eine aktuelle direkte Aussage im Chat hat weiterhin Vorrang vor dem gepflegten Profil; das Profil hat Vorrang vor älteren Erinnerungen und Sessions.

## Sitzungsübergreifender Abruf

Geeignete Anfragen können begrenzte Ausschnitte aus anderen eigenen Sessions abrufen. In einem Projekt stammen automatische Gesprächsausschnitte ausschließlich aus demselben Projekt; ohne Projekt bleibt der Abruf projektübergreifend. Automatische Quellen benötigen vollen Memory-Modus; die aktuelle Session und Subagenten sind ausgeschlossen. Explizite Sessiontools und die eigene Oberfläche-Suche können auch Sessions anderer Memory-Modi lesen. Ein Dreaming-Durchlauf ist für den Sessionabruf nicht erforderlich.

Der zusätzliche Projektindex enthält bis zu zehn neueste geeignete Sessions desselben Projekts, nur mit Titeln und Verweisen auf aktuelle Nachrichten. Relevante Ausschnitte danach können standardmäßig bis zu drei unterschiedliche eigene Sessions desselben Projekts berücksichtigen. Sie liefern den stärksten thematischen Anker, nie automatisch das vollständige Gespräch. Für einen konkreten projektübergreifenden Bedarf kann der Agent gezielt außerhalb des Projekts suchen, ohne jedes Mal nachzufragen. Leere Projekttreffer allein lösen keine globale Suche aus. Allgemeine Nutzerpräferenzen bleiben verfügbar; fremde Projekte bestimmen nicht das Thema der aktuellen Anfrage.

Beim ersten Beitrag oder bei einer ausdrücklichen Frage nach den letzten Sessions können zusätzlich kleine aktuelle Ausschnitte erscheinen. Ein ausdrücklicher Verzicht auf Memory oder vorherige Sessions und reine Textumformungen unterdrücken den automatischen Abruf.

Unter **Projekt bearbeiten > Projektanweisungen** können Sie optionale feste Regeln und Entscheidungen hinterlegen. Diese Anweisungen gelten autoritativ für alle Sessions des Projekts. Sie ergänzen die automatisch gefundene Projekthistorie, ersetzen sie aber nicht.

Die Textabschnitte und die Volltextsuche werden gemeinsam mit der jeweiligen Nachricht gespeichert und stehen nach dem Speichern sofort bereit. Semantische Embeddings entstehen im Hintergrund. Falls der Embedding-Dienst noch nicht verfügbar ist, funktioniert die Volltextsuche weiter. Beim Start und regelmäßig im Betrieb repariert MatoLoop fehlende Indexdaten. Beim Start werden passende endgültig fehlgeschlagene Embeddings einmal erneut versucht; im laufenden Betrieb werden solche Fehler nicht endlos wiederholt.

## Erinnerungen verwalten

Unter **Einstellungen > Memory** können Sie Erinnerungen suchen, filtern, anlegen, bearbeiten und löschen. Verfügbare Kategorien sind Kontext, Fakt, Ziel, Identität und Präferenz.

**Immer im Kontext** ist die einzige besondere Abrufart. Solche Erinnerungen werden bei jeder zulässigen Anfrage vollständig berücksichtigt. Eine Erinnerung mit **Bei passender Anfrage** kann unabhängig von ihrer manuellen, Chat- oder Dreaming-Quelle automatisch als kurzer relevanter Treffer erscheinen. Quelle, Quell-Session und Zeitangaben helfen bei der Einordnung.

Eine ausdrückliche Bitte im Chat, eine Information künftig immer zu berücksichtigen, kann diese Markierung bereits beim Speichern setzen. Dafür gilt dieselbe konfigurierbare Freigabe wie für andere Schreibzugriffe auf Memory. Eine gewöhnliche Bitte, etwas zu merken, setzt die Markierung nicht.

Beim Ändern von **privat** auf **öffentlich** warnt MatoLoop, weil diese Einstufung die zulässige Übertragung an öffentliche Modelle beeinflusst.

## Dreaming

Dreaming verarbeitet optional geeignete abgeschlossene Chatnachrichten mit vollem Memory. Sessions mit Automationen folgen denselben Einstellungen; Subagenten und Fehlerausgaben sind keine Quellen. Extrahiert werden dauerhafte benutzerspezifische Angaben, etwa eigene Geräte, stabile Umgebungen, Beziehungen, Vorlieben oder langfristige Ziele. Aktueller Aufgabenfortschritt, vorübergehende Geräte und Implementierungsdetails bleiben im Verlauf. **Über mich** wird nicht kopiert. Die Extraktion sieht nur ihren Quellenabschnitt, keine vorhandenen Memories oder anderen Sessions.

Anschließend sucht der Host für jeden Kandidaten getrennt nach möglichen vorhandenen Erinnerungen. Exakte Duplikate werden ohne weitere Modellentscheidung als unverändert behandelt, und normale Kandidaten ohne Treffer werden neu angelegt. Bei Unsicherheit bleiben ähnliche Angaben getrennt: Ein genauer bezeichnetes Gerät oder eine weitere Präferenz ersetzt keine allgemeinere Erinnerung, solange die Aussage nicht eindeutig denselben Gegenstand oder eine Korrektur bezeichnet. Nur bei möglichen inhaltlichen Überschneidungen entscheidet ein getrennter Abgleich zwischen:

- neu anlegen
- unverändert lassen und neue Belege ergänzen
- eine Erinnerung aktualisieren
- mehrere Erinnerungen zusammenführen
- eine exakt passende Erinnerung entfernen

Eine Entfernung benötigt eine direkte Aussage des Benutzers, dass genau diese Information vergessen werden soll, beendet, ungültig oder nicht mehr wahr ist. Nur ein normalisiert exakter Datensatz kann entfernt werden; ähnliche Treffer reichen nicht aus. Alter, Nichtnennung oder geringe Relevanz sind niemals Löschgründe.

Jede neu durch Dreaming angelegte Erinnerung beginnt mit **Bei passender Anfrage**. Beim Ändern oder Zusammenführen bleibt eine vorhandene Einstellung **Immer im Kontext** erhalten; Dreaming setzt sie jedoch nie selbst.

## Änderungsrichtlinien

Für das Ändern und Entfernen stehen jeweils diese Richtlinien zur Verfügung:

| Richtlinie | Wirkung |
| --- | --- |
| Deaktiviert | Die vorgeschlagene Änderung wird übersprungen. |
| Nachfragen | Die Änderung erscheint als offene Freigabe. |
| Automatisch | Die geprüfte Änderung wird ohne einzelne Freigabe angewendet. |

Der Standard ist **Automatisch** für Änderungen und **Nachfragen** für Entfernungen. Eine Zusammenführung benötigt beide Rechte: Ist eine Richtlinie deaktiviert, wird sie übersprungen. Steht mindestens eine auf Nachfragen, entsteht eine Freigabe. Nur zweimal Automatisch führt sofort zusammen.

Offene Entscheidungen finden Sie unter **Einstellungen > Memory > Dreaming > Offene Vorschläge**; Benachrichtigungen verlinken dorthin. Sie überleben einen Backend-Neustart. Vor Genehmigung werden Ziele, Belege und Richtlinien erneut geprüft; veraltete Anfragen verfallen ohne Teiländerung. Bei Änderungen/Zusammenführungen können Sie den vorgeschlagenen Inhalt und die Zielgruppe/Markierung bewusst bearbeiten. Die geprüften Zielrevisionen müssen trotzdem unverändert sein.

Dreaming kann manuell für alle oder für eine einzelne Session gestartet werden. Die Oberfläche zeigt Laufstatus und die Anzahl neu angelegter, aktualisierter und entfernter Erinnerungen. Später genehmigte Änderungen werden dem ursprünglichen Lauf zugerechnet.

!!! info "Memory ist kein exaktes Archiv"
    Erinnerungen sind verdichtete Arbeitsinformationen. Verwenden Sie für unveränderte Originalinhalte weiterhin Sessions und Dateien.

!!! info "Nach Benutzer getrennt"
    Erinnerungen, Duplikaterkennung, Dreaming und der Abruf aus früheren Sessions verwenden ausschließlich Daten des angemeldeten Benutzers. Auch Administratoren können Erinnerungen anderer Benutzer nicht öffnen.
