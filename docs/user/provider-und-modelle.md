# Provider und Modelle

## Provider verwalten

**Administration > Provider** öffnet zunächst **Einstellungen** mit dem Schalter für persönliche Provider. Unter **Verwaltung** legen Administratoren gemeinsame Verbindungen an und verwalten deren Modelle. **Einstellungen > Provider** verwaltet ausschließlich deine eigenen Provider, auch für Administratoren. Deaktivierte persönliche Konfiguration bleibt gespeichert; gemeinsame Modelle bleiben verfügbar.

Wähle pro Chatmodell die **Tool-Aufrufart**: **Direkt (Standard)** oder **Per Skript (experimentell)**. Im Skriptmodus ruft das Modell normale Tools ausschließlich über JavaScript auf und kann aufeinander aufbauende Schritte bündeln. Rückfragen, geschützte Eingaben und andere Steueraktionen bleiben direkt. Jeder Schritt nutzt die bestehenden Freigaben und bleibt im Verlauf sichtbar. Das Skript wartet auf jede nötige Freigabe; bei Ablehnung oder Abbruch bleiben bereits ausgeführte Änderungen bestehen.

### ChatGPT Codex

MatoLoop zeigt einen einmaligen Gerätecode und die ChatGPT-Anmeldeseite. Bestätige ihn mit einem geeigneten Konto. OAuth-Zugangsdaten werden verschlüsselt gespeichert; ein lokaler Callback-Port ist nicht nötig. Danach können Modelle synchronisiert werden.

**Fast** und **Ultrafast** erscheinen als eigene Modelle, zum Beispiel **GPT-6.1 Sol Fast**. Fast folgt dem Codex-Katalog; für GPT-6 Astra, GPT-6.1 Sol sowie GPT-6.2 Astra und Sol wird Ultrafast auch angeboten, wenn der Katalog diesen Modus nicht aufführt. Synchronisiere und aktiviere die gewünschten Varianten; die Reasoning-Stufe wählst du weiterhin unabhängig davon. Bestehende Modelle werden nicht umgestellt. Ob die Ausführung erlaubt ist und wie viel Kontingent oder Guthaben sie verbraucht, hängt von deinem ChatGPT-Tarif und den Workspace-Freigaben ab.

### GitHub Copilot (Abonnement)

Nutze dein bestehendes GitHub-Copilot-Abo ohne Copilot SDK, Copilot CLI oder GitHub Models. MatoLoop führt Tool-Aufrufe weiterhin selbst aus und verwaltet die Freigaben.

1. Erstelle auf GitHub einen [Fine-grained Personal Access Token](https://github.com/settings/personal-access-tokens/new). Wähle dein persönliches Konto als Ressourceninhaber und füge unter den Kontoberechtigungen **Copilot Requests** hinzu. Der Token braucht für MatoLoop keinen Zugriff auf private Repositorys.
2. Wähle in den Provider-Einstellungen **GitHub Copilot**, füge den Token ein und speichere. Richte dein persönliches Abo möglichst als persönlichen Provider ein, damit es nicht von anderen Personen in der Instanz genutzt wird.
3. Synchronisiere die Modelle und aktiviere das gewünschte Modell. Welche Modelle verfügbar sind und wie viel Kontingent du hast, hängt von deinem Abo und den Richtlinien deiner Organisation ab.

Die direkte Copilot-Schnittstelle ist keine öffentliche, stabile Modell-API. MatoLoop unterstützt hier synchronisierte Modelle mit Tool-Aufrufen über Chat Completions oder Responses; Modelle, die nur das Anthropic-Messages-Protokoll anbieten, werden nicht angezeigt. MatoLoop sendet sichtbare Nachrichten und Tool-Ergebnisse erneut, übernimmt aber keine internen Fortsetzungsdaten von GitHub Copilot in die nächste Anfrage.

### OpenAI-kompatibel

Verwende Basis-URL, optionalen API-Schlüssel und entweder Chat Completions oder Responses. Modelle werden möglichst über `/models` erkannt; bei fehlender Erkennung ist eine manuelle Modell-ID möglich.

MatoLoop übernimmt Eingabemodalitäten aus `input_modalities` oder ersatzweise `architecture.input_modalities`. Synchronisiere bestehende Modelle erneut, um diese Angaben zu aktualisieren. Gufo meldet Bildunterstützung nur mit geladenem Vision-Projektor (`mmproj`); der Modellname allein schaltet sie nicht frei. Für den Alias `gufo-flash-next` stehen mit Chat Completions **Aus**, **Minimal**, **Niedrig**, **Mittel**, **Hoch**, **Sehr hoch** und **Maximum** mit **Sehr hoch** als Standard bereit. Gültige Reasoning-Angaben aus der Modellerkennung haben Vorrang. Video- und Audioeingabe werden nicht aus diesem Alias abgeleitet.

### Azure und OpenAI-kompatible Realtime-Dienste {#azure-openai-realtime-und-openai-realtime}

Der Provider **Realtime** ermöglicht [Echtzeit-Sprachgespräche](anhaenge-und-medien.md#echtzeit-sprachgesprache) über Azure oder OpenAI-Realtime-GA-kompatible Server.
Er ist von Azure Speech und normalen OpenAI-kompatiblen Chatmodellen getrennt.

1. Füge unter **Provider** den Typ **Realtime** hinzu und wähle unter **Dienst** entweder **Azure** oder **OpenAI-kompatibel**. Verwende bei Azure den HTTPS-Ressourcenendpunkt ohne Pfad oder mit `/openai/v1`; für kompatible Server den HTTPS-Endpunkt mit abschließendem `/v1`, beispielsweise `https://api.openai.com/v1` oder `https://voice.example.com/custom/v1`. Es gibt keine Beschränkung auf OpenAI als Anbieter. Der Server muss Realtime-GA mit Client-Secrets, WebRTC-Aufrufen, Controller-Verbindung und Auflegen unterstützen; Chat- oder Transkriptionskompatibilität allein genügt nicht. Bestehende Realtime-Verbindungen erscheinen in derselben Maske; ihr Dienst, ihre Zugangsdaten und Modelle bleiben erhalten. Der Dienst einer gespeicherten Verbindung ist festgelegt.
2. Hinterlege den API-Key im Passwortfeld. MatoLoop speichert ihn verschlüsselt, nicht im Chat. Ohne gültigen Schlüssel ist kein Gespräch möglich. Entra ID wird hier noch nicht unterstützt.
3. Füge das Modell manuell hinzu und aktiviere es. Bei Azure ist die **Modell-ID** der Name deines Realtime-Deployments. Verwende nur ein Deployment mit Realtime-GA-, WebRTC- und Tool-Unterstützung; Region, Quoten und Zugriff müssen dazu passen.
4. Trage unter **Realtime-Stimme** eine unterstützte Stimme ein. Unter **Transkriptionsmodell** gehört eine vom Realtime-Dienst unterstützte Modell-ID hinein, zum Beispiel `gpt-4o-mini-transcribe`, nicht der Name deines Realtime-Deployments. Prüfe, welche Stimmen und Transkriptionsmodelle dein Dienst unterstützt.
5. Wähle das Modell unter **Standardmodelle > Echtzeit-Sprachmodus**. Die Headset-Taste im Chat startet das Gespräch direkt mit diesem Modell und dessen Stimme. Ist kein Standard gesetzt, verwendet MatoLoop das erste verfügbare Realtime-Modell.

Das Mikrofon-Audio wird direkt an den Provider übertragen und kann nicht textlich pseudonymisiert werden. MatoLoop speichert den Textverlauf, keine Audioaufnahme; für die Aufbewahrung beim Provider gelten dessen Einstellungen. Gespräch und Transkription können Kosten verursachen. Die Auswahl des Realtime-Modells ändert nicht das normale Agentmodell für übergebene Aufgaben.

### Kimi Code (Abonnement)

Wähle **Kimi Code (Abonnement)** und trage einen API-Key aus der [Kimi-Code-Konsole](https://www.kimi.com/code/console) ein. Die Vorlage setzt `https://api.kimi.ai/coding/v1` und **Responses API** voraus; für China kann `https://api.kimi.com/coding/v1` verwendet werden. Gespeichert wird eine normale OpenAI-kompatible Verbindung mit verschlüsseltem Schlüssel. Moonshot-API-Keys und separat abgerechnetes API-Guthaben sind damit nicht austauschbar.

Synchronisiere und aktiviere die verfügbaren Modelle. Falls die Erkennung nicht angeboten wird, hinterlege die Modell-ID manuell, beispielsweise `kimi-for-coding` oder `k3.1-pro`. Prüfe das Kontextfenster gegen deinen Tarif; MatoLoop unterstellt keinen pauschalen 1-Mio.-Token-Zugang. Die unterstützten einstellbaren Modelle bieten **Low**, **High** und **Max** an. Thinking bleibt aktiviert, damit Kimi Anfragen nicht ablehnt oder auf ein anderes Modell umleitet; `kimi-for-coding-highspeed` bietet keine einstellbaren Reasoning-Stufen.

Das Abo ist laut [Kimi-Nutzungsrichtlinien](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html) für persönliche interaktive Nutzung vorgesehen, nicht für unbeaufsichtigte Automatisierung, Batch-Jobs oder geteilten API-Zugang. Verwende es daher nicht für Hintergrundfunktionen wie Dreaming oder geplante Aufgaben. Kontingente und Ratenlimits gelten auch in MatoLoop; eine Freigabe für deinen Client und Tarif muss bei Kimi bestehen. MatoLoop verwendet seine echte Clientkennung, keine nachgeahmte Kimi-CLI-Identität.

### OpenAI-kompatible Bildserver

Verwende die `/v1`-Basis-URL des Bildservers beim allgemeinen OpenAI-kompatiblen Provider. Aktiviere am Modell nur die tatsächlich unterstützten Fähigkeiten **Bildgenerierung** und **Bildbearbeitung** und wähle es unter **Standardmodelle > Bilder**. Die Fähigkeiten werden nicht zwingend automatisch erkannt und können manuell gesetzt werden.

MatoLoop verwendet `/images/generations` (JSON) und `/images/edits` (Multipart mit geordneten `image[]`-Dateien) und erwartet Base64-Bilddaten in `data[].b64_json`. Reine URL-Antworten werden nicht unterstützt. Eine Chat-Anbindung allein genügt nicht.

Bei Qwen-Image-Turbo über Gufo lässt du die Schrittzahl weg oder verwendest acht Schritte. Sende keine vom Server nicht unterstützten Zusatzparameter. Modellladen und gegenseitiges Entladen verwaltet der Modellserver beziehungsweise ein vorgeschalteter Dienst wie llama-swap, nicht MatoLoop. Die Standardwartezeit beträgt 300 Sekunden und muss auch einen möglichen Modellwechsel abdecken. Prüfe Modelllizenz und Vertrauenslevel; Quellbilder werden nicht textlich pseudonymisiert.

### Lemonade

MatoLoop erkennt installierte Chat- und Medienmodelle eines Lemonade-Servers. Laden, Entladen und Speicherbelegung verwaltest du in Lemonade; MatoLoop übernimmt dessen Modellresidenz nicht. Nicht jede Modellart ist ein Chatmodell.

### Azure Speech

Verwende einen HTTPS-Ressourcenendpunkt wie `https://my-speech.cognitiveservices.azure.com` oder einen regionalen Endpunkt wie `https://westeurope.api.cognitive.microsoft.com` und den API-Schlüssel. Synchronisiere die Modelle; dabei prüft MatoLoop die verfügbaren Transkriptionssprachen und Stimmen. Dienstregion, Tarif, Quoten und Sprachen müssen bei Azure passend verfügbar sein.

- Aktiviere `azure-speech-fast-transcription` und wähle es unter **Standardmodelle > Transkription**, um aufgezeichnetes Audio zu transkribieren. Das Originalaudio wird übertragen und kann nicht textlich pseudonymisiert werden.
- Aktiviere `azure-speech-tts` und wähle es unter **Standardmodelle > Spracherzeugung**, um Text als WAV-Datei ausgeben zu lassen. Erlaube im Agentenprofil **Sprache erzeugen** und bitte den Agenten beispielsweise: „Erzeuge eine Sprachausgabe dieses Textes: …“. Der bestehende Schutz für Kennungen und Secrets wird vor der Textübertragung angewendet, sofern er für das Modell aktiviert ist.

Unter **Standardmodelle** erscheint beim Azure-Modell direkt unter **Spracherzeugung** die **Standardstimme**. Durchsuche die verfügbaren Stimmen beispielsweise nach `de-DE`, wähle eine Stimme und klicke auf **Speichern**. Fehlen die Stimmen, synchronisiere den Azure-Speech-Provider erneut. Die Vorgabe gilt für dieses Modell; ein Wechsel des Modells übernimmt keine fremde Stimme. Persönliche Stimmenvorgaben überschreiben die globale Auswahl. **Automatische Auswahl** setzt deine Abweichung zurück und übernimmt die geerbte Stimme beziehungsweise den Providerstandard `en-US-AvaMultilingualNeural`.

Eine ausdrücklich im Chat gewünschte Stimme hat Vorrang, beispielsweise `de-DE-KatjaNeural`. Du kannst dort auch die Geschwindigkeit zwischen `0.5` und `2.0` wählen; DragonHD- und Dragon HD Omni-Stimmen unterstützen nur die Standardgeschwindigkeit `1.0`. Verschwindet eine gespeicherte Stimme aus dem Katalog, entfernt die nächste Synchronisierung ihre Vorgabe und stellt die Vererbung wieder her. MatoLoop akzeptiert höchstens 64 KiB einschließlich des erzeugten SSML-Markups pro Aufruf. Azure gibt höchstens zehn Minuten Audio pro Anfrage zurück; teile längere Texte vorher auf. Es gibt keine automatische Aufteilung oder automatische Vorlesefunktion für Chatantworten. Azure Speech bietet hier weder Chat noch Live-Transkription.

## Modelle synchronisieren

Neu erkannte Modelle beginnen normalerweise deaktiviert; bestimmte hostseitig ergänzte native Medienmodelle können aktiv sein. Aktiviere und teste benötigte Modelle. Synchronisierung erhält interne IDs und eigene Laufzeiteinstellungen, kennzeichnet verschwundene Modelle als **Nicht verfügbar** und löscht sie nicht automatisch.

Bei OpenAI-kompatiblen Providern erkennt MatoLoop explizite Fähigkeitslisten und Fähigkeitsobjekte. `chat_completion` und `function_calling` werden als **Chat** und `image_to_image` als **Bildbearbeitung** übernommen; `vision` allein bedeutet nicht Bildbearbeitung. Unbekannte Fähigkeiten werden ignoriert. Fehlende oder unvollständige Angaben kannst du am Modell unter **Modellfähigkeiten** korrigieren, auch bei automatisch entdeckten Modellen. Verbindliche Providerangaben, etwa von Azure Speech oder Lemonade, bleiben schreibgeschützt. Automatisch übernommene Fähigkeiten folgen späteren Providerangaben, solange du sie nicht geändert hast. Eigene Änderungen bleiben bei der Synchronisierung erhalten. Ältere Modelle ohne gespeicherten Vergleichswert behalten ihre vorhandenen Fähigkeiten; ergänze fehlende Fähigkeiten dort bei Bedarf einmal selbst. Erkannte Fähigkeiten ersetzen keine Tool-Berechtigungen und erteilen keine Aktionsrechte.

Löschen entfernt passende Standardmodellzuordnungen und explizite sessionweite Verweise. Nicht verfügbare agentenspezifische Sessionwerte werden bei der Auflösung übersprungen. Eine spätere Synchronisierung kann das externe Modell mit einer neuen internen ID wieder anlegen. Ein Fehler bei der Synchronisierung ersetzt nicht den zuletzt gültigen Katalog.

## Laufzeiteinstellungen

Kontextfenster, Auto-/manuelles Ausgabelimit, Streaming, Tool-, Paralleltool- und Bildunterstützung, Memory, Vertrauenslevel und Reasoning werden je Modell konfiguriert. **Auto** lässt ein unbekanntes Ausgabelimit beim Provider; MatoLoop prüft trotzdem seinen lokalen Kontextbudget-Schätzwert. Ein manuelles Limit ist eine Obergrenze, keine reservierte feste Ausgabe.

Reasoning-Stufen erscheinen nur für unterstützte Modelle. Verfügbare Stufen und Standards hängen vom Provider und dessen erkanntem Modellprofil ab; synchronisiere nach Provideränderungen. Die Auswahl eines öffentlichen Modells gibt bereits autorisierte Inhalte an diesen Provider frei. [Datenfreigabe](freigaben-und-datenfreigabe.md) und Memory-Zielgruppen bleiben getrennte Themen.

- **Administration > Provider > Einstellungen > Modelltrennung:** Pro Provider legst du fest, ob seine Modelle als privat oder öffentlich gelten. Bei aktiver Modelltrennung gelten dafür getrennte Tool-Rechte, Erinnerungen und persönliche Profilangaben. Änderungen am Schalter werden sofort gespeichert.
- **Ohne Trennung:** Für alle Modelle gelten dieselben Datenfreigaben. Gespeicherte Einstufungen bleiben erhalten.
- **Kennungen und Secrets pseudonymisieren:** bleibt unabhängig einstellbar. Modelle können den Providerwert übernehmen oder überschreiben.
- **Grundeinstellungen:** enthält weiterhin einen direkten Link zu diesem Bereich. Dort steht die öffentliche Adresse an erster Stelle.

## Standardmodelle und Sessionvorlagen

Gemeinsame Zuordnungen stehen unter **Administration > Standardmodelle > Verwaltung**, erlaubte persönliche Vorgaben unter **Einstellungen > Standardmodelle**. Die administrative Einstellungsseite steuert die persönliche Freigabe. Zwecke sind:

- Agent und Reasoning-Vorgabe.
- Kontextkomprimierung und Titelgenerierung.
- Memory / Dreaming mit eigener Reasoning-Vorgabe.
- Bild-, Video- und Audioanalyse.
- Bild-, Sprach- und Audiogenerierung sowie Transkription.

Das Dropdown **Agentenprofil** zeigt unter **Standard** alle bisherigen Modellvorgaben. Wähle ein Profil, um nur dessen **Agentmodell** und **Reasoning-Stufe** anzupassen. Alle Modell- und Reasoning-Dropdowns bieten **Automatische Auswahl** als erste Option. Sie übernimmt die geerbte Vorgabe und ist ohne eigenen Wert vorausgewählt. Wähle diese Option erneut, um einen eigenen Wert zurückzusetzen. Der Hinweis **Effektiv** zeigt die bekannte Vorgabe; **Auswahl zur Laufzeit** bedeutet, dass der bestehende Fallback erst bei der Verwendung entscheidet. Änderungen werden mit **Speichern** übernommen. Hilfs- und Medienmodelle bleiben von Profilvorgaben unberührt. Die weiteren Einstellungen eines Agentenprofils verlinken direkt auf dessen Modellauswahl.

Die **Reasoning-Stufe** neben **Memory / Dreaming** gilt für Memory-Extraktion, den Abgleich mit vorhandenen Erinnerungen und Session-Zusammenfassungen. Du kannst nur unterstützte Stufen auswählen; ein Modellwechsel setzt eine unpassende eigene Stufe zurück. Ohne unterstützte geerbte Vorgabe verwendet **Automatische Auswahl** wie beim Agentmodell den Modell-/Providerstandard, nicht eine fest vorgegebene Stufe. Höhere Stufen können Laufzeit und Tokenverbrauch erhöhen; bessere Ergebnisse sind nicht garantiert.

**Bilder** legt das gemeinsame Standardmodell für Bilderzeugung und Bildbearbeitung fest. Unter **Abweichendes Modell für Bildbearbeitung** kannst du ein ausführbares, bildbearbeitungsfähiges Modell wählen. **Automatische Auswahl** übernimmt zunächst eine globale Bearbeitungsvorgabe; ohne diese verwendet die Bildbearbeitung das gemeinsame Bildmodell, sofern es Bildbearbeitung unterstützt. Eine persönliche Vorgabe für **Bilder** ersetzt keine geerbte globale Bearbeitungsvorgabe. Die bestehenden aktiven Modell-Fallbacks bleiben erhalten.

**Sessionvorlagen** wählen verfügbare Agenten, Startagent, Memory, Nachrichtengröße und optionale Chatmodell-/Reasoning-Werte für die Session oder einzelne Agenten. **Allgemein** bietet Allgemein/Sysadmin; **Coding** bietet Plan/Build/Interactive und kleine Nachrichten, startet aber weiterhin mit Plan. Beide beginnen mit vollem Memory. Eine Session kopiert die Vorlage einmal; spätere Vorlagenänderungen wirken nicht auf sie zurück.

**Agenten für den Tastaturwechsel** legt fest, zwischen welchen Profilen du im Eingabefeld mit **Strg/⌘ + .** wechselst. In **Coding** sind das standardmäßig Plan und Build, sonst alle verfügbaren Agenten. Ist ein Profil außerhalb der Auswahl aktiv, ändert der Shortcut nichts. Alle verfügbaren Profile bleiben im Menü auswählbar.

Ein agentenspezifischer Sessionwert gewinnt vor dem sessionweiten Wert, dann folgen persönliche und globale Profilvorgaben und schließlich persönliche und globale Standardwerte. Modell und Reasoning werden unabhängig vererbt. Nicht verfügbare Modellwerte werden bei der Sessionauflösung übersprungen. Ohne Agentmodell-Vorgabe dient das erste verfügbare toolfähige Chatmodell in der effektiven Liste. Hilfs-/Medienmodelle verwenden Standardmodelle und ihre bestehenden aktiven Modell-Fallbacks. Plan, Build und Interactive verlangen außerdem einen verbundenen Computer mit zugänglichem Arbeitsordner.
