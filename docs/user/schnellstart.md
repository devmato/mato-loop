# Schnellstart

## 1. Benutzer einrichten

Beim ersten Aufruf öffnet MatoLoop die Ersteinrichtung. Wählen Sie einen Benutzernamen und ein Passwort mit mindestens zehn Zeichen. Das erste lokale Konto erhält Administratorrechte und wird nach dem Speichern angemeldet. Weitere Konten legen Administratoren unter **Administration > Benutzer** an.

Für die Serverinstallation beginnen Sie mit [Betrieb und Datensicherung](betrieb-und-datensicherung.md).

## 2. Provider verbinden

Öffnen Sie **Administration > Provider > Verwaltung**, um einen gemeinsamen Provider anzulegen. Für erlaubte persönliche Verbindungen verwenden Sie **Einstellungen > Provider**:

- **ChatGPT Codex** verbindet ein geeignetes ChatGPT-Konto über einen Gerätecode.
- **OpenAI-kompatibel** verbindet einen Server über Basis-URL, optionale Zugangsdaten und Chat Completions oder Responses.
- **Lemonade** verbindet einen Lemonade-Server und erkennt installierte Chat- und Medienmodelle.
- **Azure Speech** stellt Transkription bereit, kein Chatmodell.

Beim Anlegen werden verfügbare Modelle synchronisiert. Später können Sie die Synchronisierung erneut starten.

## 3. Modell und Sessionvorlage wählen

Aktivieren Sie beim Provider ein verfügbares Chatmodell mit Tool-Unterstützung und testen Sie die Verbindung. Legen Sie unter **Administration > Standardmodelle > Verwaltung** das Agentmodell fest; ohne ausdrückliche Vorgabe kann MatoLoop das erste verfügbare toolfähige Chatmodell verwenden. Erlaubte persönliche Vorgaben stehen unter **Einstellungen > Standardmodelle**.

Agentenprofile enthalten keine Modellfelder. **Sessionvorlagen** legen verfügbare Agenten, Startagent, Memory, Nachrichtengröße und optionale Chatmodell-/Reasoning-Vorgaben fest. **Allgemein** bietet Allgemein und Sysadmin; **Coding** bietet Plan und Build und startet mit Plan. Hilfsmodelle, etwa für Transkription, wählen Sie unter **Standardmodelle**.

## 4. Computer optional verbinden

Für normalen Chat und die **Ablage** ist kein Computer nötig. Plan und Build benötigen dagegen einen verbundenen Computer mit zugänglichem Arbeitsordner. Fügen Sie ihn unter **Einstellungen > Computer** hinzu und folgen Sie den Installations- und Kopplungshinweisen.

Der mitgelieferte Compose-Agent wartet zunächst ungekoppelt. Mit `matoloop-agent pair` geben Sie Backend-Adresse, Computer-ID und das einmalig sichtbare Token ein. Die vollständigen Befehle für Docker, Linux und Windows finden Sie unter [Computer einrichten](betrieb-und-datensicherung.md#optionalen-computer-einrichten).

## 5. Erste Session starten

Auf Home oder in einer neuen Unterhaltung wählen Sie Sessionvorlage, Projekt, Memory und Websuche. Geben Sie eine Nachricht ein und senden Sie sie. Das legt Session, Speicher und Nachricht an. Eine sessiongebundene Control-Center-App kann auch vorher eine leere Session anlegen; das bloße Öffnen der Startseite tut dies nicht.

In einer gespeicherten Session ändern Sie Vorgaben unter **Control Center > Session**. Den absoluten Arbeitsordner wählen Sie über **Computer-Dateien**. Modell und Reasoning können Sie auch am Nachrichteneingabefeld ändern. Mehr dazu unter [Chat und Sessions](chat-und-sessions.md).

## 6. Freigaben und Rückfragen beantworten

Eine erforderliche Freigabe erscheint beim Tool-Aufruf. Prüfen Sie Aktion und Ressourcenumfang vor **Für diesen Lauf erlauben**, **Für Session erlauben** oder **Ablehnen**. Lauf-Freigaben enden mit dem ausführenden Lauf; Session-Freigaben können spätere passende Aktionen abdecken und sind widerrufbar.

**Benachrichtigungen** verweist auf die betroffene Session; dort entscheiden Sie mit dem Gesprächskontext. Das Lesen eines Hinweises entscheidet keine Anfrage. Strukturierte Rückfragen beantworten Sie direkt im Chat; das Eingabefeld bleibt für Zwischenfragen und neue Anweisungen verfügbar. Mit **Fertig** können Sie auch unbeantwortete Fragen abschließen. Geheimnisse gehören in [geschützte Eingaben](variablen-und-secrets.md), nicht in normale Chatnachrichten.
