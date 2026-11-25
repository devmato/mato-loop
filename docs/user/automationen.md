# Automationen

Eine Automation ist ein gespeicherter Agentenauftrag in einer gewöhnlichen sichtbaren Session. Klapp **Automationen** in der Seitenleiste auf und wähle einen Eintrag, um die zugehörige Unterhaltung und die Automation im Control Center zu öffnen. Beim Wechsel der Session bleibt die Liste so, wie du sie eingestellt hast. Das Eintragsmenü bietet **Automation bearbeiten** und **Automation löschen**; Sessionaktionen findest du in der Projekt- oder Sessionliste.

## Erstellen und bearbeiten

Öffne **Control Center > App hinzufügen > Automationen** in einer Session. Für eine neue Unterhaltung verwende die Plusaktion bei Automationen. Das Öffnen der App legt bei Bedarf eine leere Session an.

Die App listet alle Aufgaben der Session. **Neu** legt eine Definition mit Name und Auftrag an. Füge einen Zeitplan oder einen Ereignisauslöser hinzu und speichere. Ohne beides läuft die Aufgabe nur auf Anforderung. Eine Session darf mehrere Aufgaben enthalten; das Schließen des App-Tabs stoppt oder löscht sie nicht.

Zeitplanzeilen zeigen lesbare Beschreibungen; öffne eine Zeile zum Bearbeiten. Lange Aufträge können über **Vollständigen Auftrag anzeigen** und **Bearbeiten** geöffnet werden. **Zeitregel bearbeiten** erhält ungewöhnliche bestehende Regeln.

Unter **Weitere Optionen > Ausführungsmodell** verwendet **Aktuelles Sessionmodell** bei jedem Start das dann wirksame Modell der Session. Das gilt auch für bestehende Automationen. Eine feste Modellauswahl gilt nur für die jeweilige Automation und ändert das Sessionmodell nicht. Ist das gewählte Modell nicht mehr verfügbar, schlägt der Lauf mit einer Fehlermeldung fehl; es wird kein anderes Modell als Ersatz verwendet. Die Vertrauens- und Datenschutzregeln richten sich nach dem tatsächlich verwendeten Modell.

Agent, Computer, Arbeitsordner und Rechte stammen bei der Ausführung aus den aktuellen Sessionvorgaben. Benötigte Skills nennst du im Auftrag ausdrücklich; eine zusätzliche Automations-Skillzuordnung gibt es nicht. Normale Chatnachrichten ändern die gespeicherte Definition nicht, können aber den Kontext späterer Läufe beeinflussen.

## Ausführen und interagieren

- **Jetzt ausführen** reiht den gespeicherten Auftrag ein; speichere oder verwirf Änderungen zuerst.
- **Deaktivieren** stoppt automatische Starts, nicht laufende Arbeit. Manuelle Starts bleiben möglich.
- Der Chat-Stoppknopf beendet den aktuellen Agentenlauf.
- Tools, Ergebnisse, Fehler, Freigaben und Fragen erscheinen im normalen Gespräch.
- Ist ein Agent gerade tätig oder eingereiht, wartet die Automation. Wartet er dagegen auf eine Freigabe oder Rückfrage, können weitere Automationsläufe andere Arbeit erledigen; die Anfrage bleibt erhalten. Eine Zustimmung setzt den ursprünglichen Aufruf erst fort, wenn die laufende Arbeit die Ausführung freigibt. Läufe mit aktiven Subagenten bleiben blockierend.
- Weitere Auslösungen einer bereits eingereihten oder tatsächlich arbeitenden Aufgabe werden als übersprungen protokolliert. Ein nur auf deine Entscheidung wartender Lauf verhindert keine neue Auslösung – das gilt für Zeitpläne, Ereignisse und **Jetzt ausführen** gleichermaßen.

**Letzter Lauf** zeigt die letzte tatsächliche Ausführung mit Beginn, Status und Verweis auf Ergebnis oder Fehler; übersprungene Auslösungen ersetzen ihn nicht. Während der Arbeit führt der Link gegebenenfalls zur Eingabenachricht. **Abgeschlossen** bedeutet normales Laufende, nicht den Nachweis einer erfüllten überwachten Bedingung. Der Chat ist das vollständige Ausführungsprotokoll; eine zweite Detailhistorie gibt es in der App nicht.

### Hinweise und stille Prüfungen

Antworten verwenden dieselben Ungelesen-Markierungen und Benachrichtigungen wie interaktive Chats. Der Agent kann mit `notify` einen gezielten Hinweis erzeugen oder mit `silence` den gewöhnlichen Abschluss-Hinweis unterdrücken, etwa wenn eine verlangte Prüfung nichts Berichtenswertes fand. Eine stumme Antwort bleibt im Chat und erzeugt keine Ungelesen-Markierung an der Session oder am Projekt. Ein erfolgreich abgeschlossener stummer Lauf ohne gezielten Hinweis rückt die Session unter **Zuletzt aktiv** nicht nach oben. Ältere ungelesene Antworten bleiben markiert. Fehler, Freigaben, Fragen und schon erzeugte Hinweise werden dadurch nicht unterdrückt.

Eine sichtbar fokussierte Session erzeugt keinen automatischen Antwort-Hinweis. Web Push wird nur bei fehlender aktiver Anwendungspräsenz gesendet; siehe [Benachrichtigungen](anmeldung-und-oberflaeche.md#benachrichtigungen).

Vor Modellschritten erhält der Agent aktuelle Definitionen seiner Session, einschließlich deaktivierter Aufgaben und nächstem wirksamem Termin, soweit das Kontextbudget reicht. Bei Platzmangel wird ein kompakter Katalog mit Auslassungszahl verwendet; Details können über das normale Tool nachgeladen werden. Definitionen sind Konfigurationsdaten, keine automatische Aufforderung, jede Aufgabe zu starten.

## Persönlichen Assistenten einrichten

Der persönliche Assistent nutzt eine gewöhnliche sichtbare Session und eine Automation.
Starte die Einrichtung mit `/assistant-init` oder in den persönlichen Einstellungen.
Dabei werden die feste Session, eine zunächst deaktivierte Automation und der persönliche
Skill `assistant` vorbereitet. Führst du den Befehl in einer anderen Unterhaltung aus,
folge dem Link zur zugewiesenen Session und starte `/assistant-init` dort erneut.

1. Beschreibe im Einrichtungschat die gewünschten Quellen, den Umfang und deine Anweisungen.
   Der Agent fragt nur fehlende notwendige Angaben ab und verwendet normale Tools zur Auswahl.
2. Lass die Auswahl im persönlichen Skill veröffentlichen. Sie liegt in
   `references/preferences.json`; separate technische Quellen- oder Filtereinstellungen gibt es nicht.
3. Bereite die benötigten Freigaben in dieser Session vor. Quelleninhalte können an das
   verwendete Modell gelangen; wähle Modell und Datenschutzvorgaben entsprechend.
   Eine Quellenauswahl erteilt keine Rechte, und Rechte anderer Chats werden nicht übernommen.
4. Aktiviere die Automation erst nach abgeschlossener Einrichtung und Rechtevorbereitung.
   Ohne gültige veröffentlichte Auswahl bleiben Aktivierung und manuelle Starts blockiert.

Der Standardzeitplan prüft alle 15 Minuten. Aktivierung, Zeitpläne und Ausführungsmodell
änderst du wie bei anderen Aufgaben in der Automations-App. Änderungen an Quellen oder
Anweisungen erfolgen im Chat über den persönlichen Skill und ersetzen keinen Zeitplan.
In einem reinen Planungsprofil erklärt der Agent Änderungen, ohne sie auszuführen.

Beschreibe auch, was die Assistenz selbst erledigen darf, was sie dir vorher vorlegen
soll und was sie nur auf deinen ausdrücklichen Auftrag tut. Das bleibt eine freie
Anweisung; technische Freigaben gelten zusätzlich. Offene Freigaben und Rückfragen
bleiben im Chat gebündelt sichtbar. Für weitere Prüfungen während offener Anfragen
gelten die [normalen Automationsregeln](#ausfuhren-und-interagieren), keine Assistenz-Sonderregeln.

Notizen werden über die normalen Sessionkontext-Tools gepflegt. Verarbeitete Quell-IDs
und Versionen stehen als Nachrichten-Metadaten
mit Verweisen auf Tool-Ergebnisse im Gespräch. Sie helfen gegen Wiederholungen, beweisen
aber keine externe Wirkung. Unklare Ergebnisse müssen geprüft werden; Aktionen werden
nicht blind wiederholt. Für Stoppen, Archivieren und Löschen gelten dieselben Regeln
wie für andere Automationen.

## Zeitpläne

Vorlagen bieten täglich, werktags, wöchentlich und monatlich. **Eigener Rhythmus > Zeitregel bearbeiten** erlaubt fünfteilige Cron-Ausdrücke, etwa `0 8 * * 1-5` für werktags 08:00 in der gespeicherten Zeitzone. Neue Zeitpläne übernehmen die Browserzeitzone; ein Gerätewechsel ändert gespeicherte Regeln nicht.

Einmalige Termine und optionale Endzeitpunkte werden in der lokalen Gerätezeitzone eingegeben. Das Ende eines wiederkehrenden Plans ist einschließlich. Während Serverstillstand verpasste Termine werden nicht nachgeholt; gleichzeitig fällige Pläne werden gebündelt. Fehlgeschlagene oder unterbrochene Arbeit wird nicht automatisch wiederholt.

## Archivieren und entfernen

Archivieren betrifft die Session oder ihr Projekt, nicht die Automationsdefinition. Archivierte Sessions bleiben lesbar, neue Starts benötigen Wiederherstellung. Eingereihte Arbeit wird abgebrochen, aktive Arbeit darf enden. Die Aktivierungseinstellung bleibt gespeichert; verpasste Auslösungen werden nicht nachgeholt. Details stehen unter [Archiv](anmeldung-und-oberflaeche.md#archiv).

**Automation entfernen** in der App oder **Automation löschen** im Seitenleistenmenü löscht nach Bestätigung Definition, Zeitpläne und Auslösungsverlauf; Session, Dateien, Rechte und bereits laufender Agent bleiben erhalten. Das Löschen der Session oder ihres Projekts entfernt nach Bestätigung auch die abhängigen Automationen.

Hinweise zu früheren Ausführungssessions und Ausgabekopien stehen im [Upgrade-Guide](aktualisierung.md#automationen-und-aufnahmen).

## Ereignisauslöser und Rechte

**Agent hat geantwortet** und **Agent-Auftrag fehlgeschlagen** reagieren auf abgeschlossene normale Läufe deiner eigenen Sessions. Streaming und fremde Konten lösen nichts aus. Automationsbedingte Läufe, ihre Retries und Subagenten erzeugen keine weiteren Automationsauslösungen.

Optionale Bedingungen gehören in den Auftrag und werden vom ausgelösten Agenten beurteilt. Ereigniskontext enthält Session-/Nachrichtenverweise; Lesen folgt weiterhin den Sessionrechten. Für beschäftigte und auf Entscheidungen wartende Aufgaben gelten dieselben [Ausführungsregeln](#ausfuhren-und-interagieren) wie für Zeitpläne.

Vor der Aktivierung einer Automation bereitet der Agent die absehbar benötigten Session-Freigaben vor. `request_session_permissions` prüft unterstützte Datei-, Shell-, Session- und Integrationsaktionen sowie Websuchen und Abrufe bekannter URLs, ohne sie auszuführen. Erst im Lauf ermittelte URLs prüft der Agent beim Abruf. Außerhalb der Automationseinrichtung bereitet der Agent Freigaben nur vor, wenn du ausdrücklich darum bittest. Deaktivierte Aktionen bleiben blockiert, einmalige oder nicht vorab prüfbare Rechte werden bei der tatsächlichen Ausführung angefragt. Das Speichern einer Automation erlaubt deren spätere Aktionen nicht automatisch.
