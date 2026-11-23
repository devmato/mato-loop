# Memory und Verarbeitung

Memory ermöglicht MatoLoop, passende Informationen über einzelne Sessions hinaus zu verwenden. Die Funktion muss in den Memory-Einstellungen aktiviert sein und wird zusätzlich je Session gesteuert.

Unter **Einstellungen > Memory** findest du drei Tabs: **Einstellungen** für Memory-Zugriff und automatischen Kontext, **Erinnerungen** zur Verwaltung gespeicherter Memories und **Verarbeitung** für die Memory-Extraktion und Session-Zusammenfassungen. Änderungen speicherst du über **Speichern** oben auf der Seite; Schalter werden direkt gespeichert.

## Session-Modi

| Modus | Frühere Sessions und Erinnerungen nutzen | Erinnerungen schreiben | Verarbeitung |
| --- | --- | --- | --- |
| Voll | Ja | Ja | Ja |
| Nur lesen | Ja | Nein | Nein |
| Deaktiviert | Nein | Nein | Nein |

Provider- und Modelleinstellungen können den zulässigen Umgang mit öffentlichen und privaten Inhalten weiter einschränken.

## Über mich

Unter **Einstellungen > Personalisierung > Über mich** fügst du Angaben aus einem festen Katalog hinzu und pflegst dort vollständigen und bevorzugten Namen, Anrede, Pronomen, Hauptsprache und Hauptwohnsitz. Diese Angaben sind kein Memory: Sie gelten unabhängig vom Memory-Modus und werden weder durchsucht noch extrahiert. Der Agent kann sie nur auf deine ausdrückliche Bitte über ein eigenes Tool ändern oder entfernen; die Tool-Richtlinie bestimmt, ob du die Änderung einzeln freigeben musst.

Für diese Profilangaben kannst du zwischen privaten und öffentlichen Modellen umschalten. Mit **Auch für öffentliche Modelle verwenden** übernimmt das öffentliche Feld den privaten Wert; andernfalls pflegst du beide Werte getrennt. Eine aktuelle direkte Aussage im Chat hat weiterhin Vorrang vor dem gepflegten Profil; das Profil hat Vorrang vor älteren Erinnerungen und Sessions.

Über **Telefonnummer hinzufügen** kannst du eine oder mehrere Nummern mit einem Label wie **Mobil** oder **Arbeit** hinterlegen. Änderungen übernimmst du mit **Speichern**. Gespeicherte Telefonnummern werden nicht an Modelle weitergegeben.

## Sitzungsübergreifender Abruf

Geeignete Anfragen können begrenzte Ausschnitte aus anderen eigenen Sessions abrufen. In einem Projekt stammen automatische Gesprächsausschnitte ausschließlich aus demselben Projekt; ohne Projekt bleibt der Abruf projektübergreifend. Automatische Quellen benötigen vollen Memory-Modus; die aktuelle Session und Subagenten sind ausgeschlossen. Explizite Sessiontools und die eigene Oberfläche-Suche können auch Sessions anderer Memory-Modi lesen. Ein vorheriger Verarbeitungslauf ist für den Sessionabruf nicht erforderlich.

Der zusätzliche Projektindex enthält bis zu zehn neueste geeignete Sessions desselben Projekts, nur mit Titeln und Verweisen auf aktuelle Nachrichten. Relevante Ausschnitte danach können standardmäßig bis zu drei unterschiedliche eigene Sessions desselben Projekts berücksichtigen. Sie liefern den stärksten thematischen Anker, nie automatisch das vollständige Gespräch. Für einen konkreten projektübergreifenden Bedarf kann der Agent gezielt außerhalb des Projekts suchen, ohne jedes Mal nachzufragen. Leere Projekttreffer allein lösen keine globale Suche aus. Allgemeine Nutzerpräferenzen bleiben verfügbar; fremde Projekte bestimmen nicht das Thema der aktuellen Anfrage.

Beim ersten Beitrag oder bei einer ausdrücklichen Frage nach den letzten Sessions können zusätzlich kleine aktuelle Ausschnitte erscheinen. Ein ausdrücklicher Verzicht auf Memory oder vorherige Sessions und reine Textumformungen unterdrücken den automatischen Abruf.

Unter **Einstellungen > Memory > Einstellungen > Automatischer Kontext** begrenzt du die Anzahl der Memory-Treffer, relevanten und letzten Session-Auszüge sowie das gemeinsame Token-Budget. Über **Speichern** speicherst du nur diese vier Werte, unabhängig von ungespeicherten Einstellungen im Tab **Verarbeitung**.

Unter **Projekt bearbeiten > Projektanweisungen** kannst du optionale feste Regeln und Entscheidungen hinterlegen. Diese Anweisungen gelten autoritativ für alle Sessions des Projekts. Sie ergänzen die automatisch gefundene Projekthistorie, ersetzen sie aber nicht.

Die Textabschnitte und die Volltextsuche werden gemeinsam mit der jeweiligen Nachricht gespeichert und stehen nach dem Speichern sofort bereit. Semantische Embeddings entstehen im Hintergrund. Falls der Embedding-Dienst noch nicht verfügbar ist, funktioniert die Volltextsuche weiter. Beim Start und regelmäßig im Betrieb repariert MatoLoop fehlende Indexdaten. Beim Start werden passende endgültig fehlgeschlagene Embeddings einmal erneut versucht; im laufenden Betrieb werden solche Fehler nicht endlos wiederholt.

## Erinnerungen verwalten

Unter **Einstellungen > Memory > Erinnerungen** kannst du Erinnerungen suchen, filtern, anlegen, bearbeiten und löschen. Verfügbare Kategorien sind Kontext, Fakt, Ziel, Identität und Präferenz.

**Immer im Kontext** ist die einzige besondere Abrufart. Solche Erinnerungen werden bei jeder zulässigen Anfrage vollständig berücksichtigt. Eine Erinnerung mit **Bei passender Anfrage** kann unabhängig von ihrer manuellen, Chat- oder Extraktionsquelle automatisch als kurzer relevanter Treffer erscheinen. Quelle, Quell-Session und Zeitangaben helfen bei der Einordnung.

Eine ausdrückliche Bitte im Chat, eine Information künftig immer zu berücksichtigen, kann diese Markierung bereits beim Speichern setzen. Dafür gilt dieselbe konfigurierbare Freigabe wie für andere Schreibzugriffe auf Memory. Eine gewöhnliche Bitte, etwas zu merken, setzt die Markierung nicht.

Beim Ändern von **privat** auf **öffentlich** warnt MatoLoop, weil diese Einstufung die zulässige Übertragung an öffentliche Modelle beeinflusst.

## Verarbeitung {#dreaming}

Unter **Memory > Verarbeitung** schaltest du **Memories extrahieren** und **Session-Zusammenfassungen erstellen** unabhängig voneinander ein. Du kannst eines der Verfahren oder beide verwenden. Die Memory-Extraktion ist zunächst eingeschaltet, Session-Zusammenfassungen sind ausgeschaltet.

Unter **Gemeinsame Einstellungen** steuert **Automatisch verarbeiten** die zeitgesteuerten Läufe. Dieser Schalter ist zunächst ausgeschaltet. Nachrichtenlimit, Intervall und Leerlauf gelten für die eingeschalteten Verfahren. **Jetzt verarbeiten** startet sie auch ohne Automatik. Sind beide Verfahren ausgeschaltet, ist kein Start möglich. Modell und Reasoning-Stufe wählst du unter [**Standardmodelle > Memory / Dreaming**](provider-und-modelle.md#standardmodelle-und-sessionvorlagen).

### Memory-Extraktion

**Eine klare persönliche Aussage kann reichen.** Dreaming kann sich zum Beispiel familiäre Beziehungen, Geburtstage, Beruf und Fähigkeiten, dauerhafte Lebensumstände, eigene Geräte, Setups, allgemeine Vorlieben und persönliche Langzeitziele merken. Eine beiläufige Aussage braucht zwei weitere passende Originalaussagen von dir aus zwei anderen geeigneten Sessions. Automationsaufträge, Assistentenantworten und Zusammenfassungen zählen nicht. Mit „Merk dir das“ kannst du weiterhin ausdrücklich speichern.

Ein erfolgreiches Experiment, ein Kaufvorgang oder eine Projektentscheidung bleibt im Gespräch, auch wenn du das Thema oft erwähnst. „Bei Reviews sehe ich lieber zuerst die Risiken“ kann eine dauerhafte Präferenz belegen. „Zeig mir diesmal zuerst die Risiken“ gilt nur für den aktuellen Auftrag.

Extraktion und Abgleich sollen zuerst prüfen, ob deine eigene Aussage den gesamten Inhalt trägt. Eine vorhandene Erinnerung bestätigt keine zusätzliche Vermutung; Assistentenantworten liefern keine neuen persönlichen Fakten. Projektregeln und Arbeitskoordination bleiben über den Gesprächsverlauf auffindbar, auch wenn daraus kein persönliches Memory entsteht. Die Einordnung trifft weiterhin das Modell, daher sind Fehlentscheidungen möglich. Prüfe gespeicherte Angaben unter **Erinnerungen** und korrigiere oder entferne unpassende Einträge.

Der Zeitpunkt deiner Aussage bleibt über die ursprüngliche Nachricht und ihre Belege erhalten; Erinnerungen brauchen deshalb keinen zusätzlichen Datumszusatz wie „Stand …“. MatoLoop verwendet dafür vorhandene Nachrichtenzeiten und Belegverknüpfungen, keine neuen Zeitspalten in der Datenbank. Dem Modell werden höchstens zehn neueste datierte Belegverweise mitgegeben; eine Kürzung wird kenntlich gemacht. Diese Zeiten gehören zu einzelnen Quellnachrichten: Ein neuer Beleg bestätigt nicht automatisch jeden Satz einer zusammengeführten Erinnerung oder deren fortdauernde Gültigkeit.

Die Suche prüft nur wenige Belege pro Kandidat. Findet sie keine hinreichende Bestätigung, bleibt die Aussage über den Verlauf auffindbar. Die Kategorien im manuellen Editor und bereits vorhandene Erinnerungen bleiben unverändert.

Mit **Zusätzliche Hinweise für die Memory-Extraktion** passt du Relevanz, Themen und Detailgrad an. Die Hinweise ändern keine zulässigen Quellen, Berechtigungen oder Datenschutzgrenzen. Änderungsrichtlinien und Kandidatenlimit findest du im selben Abschnitt. Beim Ausschalten bleiben vorhandene Erinnerungen nutzbar und der Extraktionsfortschritt erhalten. Offene Extraktionsvorschläge verfallen. Nach dem erneuten Einschalten setzt die Extraktion am bisherigen Stand fort.

Anschließend sucht der Host über Wort- und Bedeutungssuche nach vorhandenen Erinnerungen – auch aus anderen Sessions. Der Abgleich berücksichtigt andere Kandidaten aus demselben Lauf sowie die Originalaussage mit Zeitbezug und begrenzten früheren Gesprächskontext. Ein genauer bezeichnetes Gerät ersetzt keine allgemeinere Erinnerung, solange nicht eindeutig derselbe Gegenstand gemeint ist. Unklare Zuordnungen werden ausgelassen, statt daraus eine neue vage oder widersprüchliche Erinnerung zu erzeugen. Der Abgleich unterscheidet:

- neu anlegen
- unverändert lassen und neue Belege ergänzen
- eine Erinnerung aktualisieren
- mehrere Erinnerungen zusammenführen
- eine exakt passende Erinnerung entfernen
- einen ausdrücklich beendeten Zustand aus den aktiven Erinnerungen entfernen
- ohne Änderung auslassen

Eine klare Korrektur wie „Mein Rechner hat jetzt 48 statt 24 GB RAM“ braucht keine Wiederholung. Verkaufst du ein eindeutig zugeordnetes Gerät, entfernt Dreaming die passenden aktiven Erinnerungen nach der eingestellten Löschrichtlinie. Standardmäßig fragt es nach deiner Freigabe. Der Verkauf bleibt im Gespräch; eine ältere Aussage über dasselbe Gerät legt es nicht wieder als aktuellen Besitz an. Eine Verkaufsabsicht ändert nichts. Die ausdrückliche Bitte, etwas zu vergessen, bleibt ein eigener Vorgang für den exakten Eintrag. Dreaming räumt den Altbestand nicht pauschal auf.

Jede neu durch die Extraktion angelegte Erinnerung beginnt mit **Bei passender Anfrage**. Beim Ändern oder Zusammenführen bleibt eine vorhandene Einstellung **Immer im Kontext** erhalten; die Extraktion setzt sie jedoch nie selbst.

## Änderungsrichtlinien

Automatisch extrahierte Erinnerungen werden standardmäßig automatisch gepflegt. Sobald du eine Erinnerung selbst anlegst oder bearbeitest, benötigt die Extraktion für Änderungen, Zusammenführungen und Entfernungen immer deine Freigabe. Das gilt auch für ursprünglich automatisch extrahierte Erinnerungen; die angezeigte Quelle allein zeigt diesen Schutz nicht an. Solche Erinnerungen werden weiterhin beim Abgleich berücksichtigt und können Gegenstand eines Vorschlags sein. Einen Schalter zum Einschließen manueller Erinnerungen gibt es nicht mehr. Dein direktes Bearbeiten bleibt möglich.

Für das Ändern und Entfernen stehen jeweils diese Richtlinien zur Verfügung:

| Richtlinie | Wirkung |
| --- | --- |
| Deaktiviert | Die vorgeschlagene Änderung wird übersprungen. |
| Nachfragen | Die Änderung erscheint als offene Freigabe. |
| Automatisch | Die geprüfte Änderung wird ohne einzelne Freigabe angewendet, sofern keine manuell angelegte oder von dir bearbeitete Erinnerung betroffen ist. |

Standardmäßig werden Änderungen **Automatisch** angewendet; bei Entfernungen gilt **Nachfragen**. Deine gespeicherten Richtlinien bleiben erhalten. Eine Zusammenführung benötigt beide Rechte: Ist eine Richtlinie deaktiviert, wird sie übersprungen. Steht mindestens eine auf Nachfragen oder ist eine manuell angelegte oder von dir bearbeitete Erinnerung betroffen, entsteht eine Freigabe. Nur zweimal Automatisch und ausschließlich automatisch verwaltete Ziele erlauben eine sofortige Zusammenführung.

Neue offene Entscheidungen prüfst du im Chat **Persönlicher Assistent**; Benachrichtigungen und die Laufdetails verlinken zum zugehörigen Chat. Bereits vorhandene Vorschläge bleiben in ihrer bisherigen Quell-Session erreichbar. Ältere Links ohne Sessionbezug zeigen nach dem Laden einen Link **Im Chat prüfen**. Die Vorschläge überleben einen Backend-Neustart. Vor Genehmigung werden Ziele, Belege und Richtlinien erneut geprüft; veraltete Anfragen verfallen ohne Teiländerung. Bei Änderungen/Zusammenführungen kannst du den vorgeschlagenen Inhalt und die Zielgruppe/Markierung bewusst bearbeiten. Die geprüften Zielrevisionen müssen trotzdem unverändert sein.

Beim Laden offener Vorschläge oder Benachrichtigungen werden nicht mehr gültige Vorschläge automatisch als abgelaufen markiert. Sie verschwinden zusammen mit ihrer Benachrichtigung; du musst sie nicht erst genehmigen oder ablehnen. Bereits zugestellte Betriebssystem-Pushmeldungen lassen sich nicht zuverlässig zurückziehen.

Die Verarbeitung kann manuell für alle oder für eine einzelne Session gestartet werden. Die gemeinsame Übersicht zeigt die letzten Läufe beider Verfahren. Über **Alle Läufe ansehen** öffnest du den vollständigen, seitenweisen Verlauf. Öffne einen Lauf, um seine Quell-Session, angewendete Memory-Änderungen mit Vorher/Nachher, offene Vorschläge und die damals erzeugte Zusammenfassung zu sehen. Später genehmigte Änderungen werden dem ursprünglichen Lauf zugerechnet. Bei älteren Läufen sind nur die bereits gespeicherten Zähler verfügbar. Beim Löschen einer Erinnerung werden zugehörige gespeicherte Änderungsinhalte entfernt.

Die Tabs **Einstellungen / Erinnerungen / Verarbeitung** bleiben auch im Verlauf und in den Laufdetails sichtbar. Über **Verarbeitung › Durchläufe › Details** wechselst du zurück zur Übersicht oder zur Verarbeitung.

### Session-Zusammenfassungen einschalten

Aktiviere unter **Memory > Verarbeitung > Session-Zusammenfassungen** den Schalter **Session-Zusammenfassungen erstellen** unter der Erklärung. Das Verfahren verwendet dasselbe Modell, denselben Zeitplan und **Jetzt verarbeiten** wie die Memory-Extraktion, funktioniert aber auch bei ausgeschalteter Extraktion. Zusätzliche Modellaufrufe können Kosten verursachen.

Automatisch werden nur neue oder ab der Aktivierung wieder aktive Sessions nach der eingestellten Ruhezeit berücksichtigt, nicht dein gesamter Altbestand. Für eine ältere Session wählst du sie gezielt aus und startest **Jetzt verarbeiten**. Der erste Lauf verarbeitet ihren bisherigen Verlauf in begrenzten Abschnitten; spätere Läufe aktualisieren die Zusammenfassung mit neuen abgeschlossenen Nachrichten.

Passende Zusammenfassungen helfen dem Agenten beim Einstieg in frühere Gespräche. Innerhalb eines Projekts bleiben dessen Sessions der automatische Gesprächskontext. Fehlende Details kann der Agent weiterhin aus dem Original nachlesen; Sessions ohne Zusammenfassung bleiben auffindbar. Zusammenfassungen sind keine vollständigen Belege und können Informationen auslassen.

Beim Ausschalten werden Zusammenfassungen weder weiter erstellt noch automatisch verwendet. Gespeicherte Fassungen bleiben in den Laufdetails sichtbar. Wenn du Quellnachrichten zurücknimmst, werden betroffene Zusammenfassungen verworfen. Zusammenfassungen und Memories haben getrennten Fortschritt: Ein fehlgeschlagener Schritt setzt einen bereits erfolgreichen nicht zurück.

!!! info "Memory ist kein exaktes Archiv"
    Erinnerungen sind verdichtete Arbeitsinformationen. Verwende für unveränderte Originalinhalte weiterhin Sessions und Dateien.

!!! info "Nach Benutzer getrennt"
    Erinnerungen, Duplikaterkennung, Verarbeitung und der Abruf aus früheren Sessions verwenden ausschließlich Daten des angemeldeten Benutzers. Auch Administratoren können Erinnerungen anderer Benutzer nicht öffnen.
