# Anhänge, Diktat und Medien

## Nachrichtenanhänge

Du kannst Dateien über Dateiauswahl oder Zwischenablage anhängen oder direkt auf die Nachrichteneingabe ziehen. Beim Ziehen von Dateien zeigt die Eingabe **Dateien hier ablegen** an; nach dem Ablegen erscheinen sie als Anhänge im Entwurf und werden erst beim Senden hochgeladen. Anzahl, Einzelgröße und Gesamtgröße legen Administratoren unter **Administration > Instanzlimits** fest. Browserlokale Anhangsentwürfe bleiben über alle Sessions zusammen auf 256 MiB begrenzt.

Uploads und ausdrücklich gespeicherte Dokumentableitungen sind verwaltete Dateien der besitzenden Session; Nachrichten behalten ihre exakten Versionsverweise. Es gibt keinen separaten Anhangsbaum. Office-/PDF-Textbegleiter können dauerhaft entstehen, Audio-WAV-Konvertierung erfolgt dagegen bei Bedarf nur vorübergehend. Das Original bleibt zum Download verfügbar.

## Bildverarbeitung

PDF-Anhänge erhalten neben dem Textauszug eine visuelle Vorschau: bei bis zu vier Seiten vollständig, sonst zunächst die erste Seite. Ein bildfähiges Chatmodell erhält die Seitenbilder direkt; andernfalls liest das konfigurierte Vision-Modell Text, Tabellen und Bildinhalte. Der Fallback verwendet kein weniger vertrauliches Modell als das Chatmodell. Weitere Seiten kann der Agent gezielt über den Dateizugriff nachladen. Die üblichen Bild- und Größenlimits gelten weiterhin; nicht übertragene oder nicht ausgewertete Seiten werden gekennzeichnet. Temporäre Seitenbilder werden nicht als zusätzliche Sessiondateien gespeichert.

PDFs lassen sich im gemeinsamen Dateiviewer öffnen. Die Anzeige verwendet den PDF-Viewer deines Browsers. Wenn dieser keine eingebettete Vorschau unterstützt, stehen **PDF separat öffnen** und der Download zur Verfügung.

Bildfähige Modelle können geeignete aktuelle oder ausdrücklich referenzierte Bilder erhalten. Große Bilder werden für die Modellanfrage normalisiert. Das gespeicherte Original bleibt unverändert. Bilder, Audio und andere Binärdaten werden vor der Übertragung nicht pseudonymisiert.

Bei einer Bildbearbeitung speichert MatoLoop sowohl die unmittelbaren Quellen als auch eine kontrollierte Ursprungslinie. Wenn du eine Bearbeitung derselben Bildlinie für die Session freigibst, können daraus erzeugte Bilder erneut bearbeitet werden, ohne bei jedem Zwischenergebnis nachzufragen. Andere Bilder, Sessions, Modelle oder Vertrauenslevel bleiben von dieser Freigabe getrennt.

Externe Markdown-Bilder werden nicht automatisch geladen. Du entscheidest je Ursprung und Session, ob der Browser die externe Ressource abrufen darf.

## QR-Codes

Der Agent kann QR-Codes lokal und ohne Bildgenerator als scannbare PNG-Datei erzeugen. Zum Lesen hänge ein Bild mit einem QR-Code an und formuliere einen kurzen Auftrag. Die Verarbeitung findet lokal statt; QR-Bilder und codierte Inhalte werden dafür nicht an einen Medienprovider gesendet.

## Sprachnachrichten

Die Mikrofontaste startet die Aufnahme mit Browserberechtigung. Stoppen hängt sie automatisch an den Entwurf; sie endet spätestens nach fünf Minuten. Dort kannst du sie anhören oder entfernen. Scheitert das Anhängen an den Grenzen, bieten **Anhängen**, **Herunterladen** und **Verwerfen** eine Wiederaufnahme bzw. Sicherung. Innerhalb der Grenzen sind mehrere Aufnahmen möglich.

Beim Senden wird das Original gespeichert und bei verfügbarer Transkription der erkannte Text ergänzt. Das Transkriptionsmodell wählst du unter **Standardmodelle > Transkription**, nicht am Agentenprofil. Diese ausdrückliche Benutzeraktion erzeugt keine weitere Tool-Freigabe; technisch deaktivierte Transkription bleibt deaktiviert. Aufnahme und Versand funktionieren auch ohne Transkription, und Fehler lassen das Original erhalten. [Azure Speech](provider-und-modelle.md#azure-speech) ist eine mögliche Transkriptionsquelle; dabei wird das Originalaudio an Azure übertragen.

Gesendete Aufnahmen besitzen einen Audioplayer und einen Download der Originaldatei. Nur das Originalaudio wird dauerhaft gespeichert. Für Transkription und Wiedergabe erzeugt MatoLoop bei Bedarf vorübergehend eine WAV-Datei aus dem geprüften Original, ohne der Session eine doppelte Datei hinzuzufügen. Ältere WAV-Begleitdateien bleiben wegen bestehender Dateiverweise gespeichert, werden für Wiedergabe und Transkription aber nicht mehr benötigt. Das Modell kann später erneut auf den Anhang zugreifen, auch für eine weitere Transkription. Wenn du eine Nachricht wieder in den Entwurf übernimmst, bleibt das Audio erhalten; ein bereits enthaltenes erfolgreiches Transkript wird beim erneuten Senden nicht nochmals angehängt.

Verhindert eine Anhangsgrenze das Hinzufügen, bleibt die Aufnahme für einen weiteren Versuch, einen Download oder das ausdrückliche Verwerfen verfügbar. Beim Wechsel der Session bleiben fertige Aufnahmen im ursprünglichen Entwurf erhalten. Ungesendete Aufnahmen sind Entwürfe im Browserspeicher und keine serverseitigen Sessiondateien; eingereihte Nachrichten verwenden den dauerhaften Browser-Postausgang.

## Echtzeit-Sprachgespräche

Richte zuerst einen [Realtime-Provider mit Modell und Transkriptionsmodell](provider-und-modelle.md#azure-openai-realtime-und-openai-realtime) ein. Dein Browser benötigt Mikrofonzugriff über HTTPS oder `localhost` und eine WebRTC-Verbindung zum Provider.

Dein Mikrofon-Audio wird direkt an den Provider übertragen und nicht textlich pseudonymisiert. MatoLoop speichert den Textverlauf, keine Audioaufnahme; für die Aufbewahrung beim Provider gelten dessen Einstellungen. Gespräch und Transkription können Kosten verursachen.

1. Öffne eine normale Session und wähle die Headset-Taste **Sprachmodus starten** neben der Aufnahmetaste. Der Sprachmodus startet direkt mit dem unter **Standardmodelle > Echtzeit-Sprachmodus** konfigurierten Modell und dessen Stimme, ohne Auswahldialog. Ist kein Standard gesetzt, verwendet MatoLoop das erste verfügbare Realtime-Modell. Ein anderer laufender Agentenauftrag muss vorher beendet werden; bei einem Sprachauftrag kannst du das Gespräch erneut starten. Ohne bestehende Session legt MatoLoop eine normale Session an.
2. Erteile gegebenenfalls die Browserberechtigung für das Mikrofon. Das Mikrofon bleibt bis zum vollständigen Verbindungsaufbau ausgeschaltet.
3. Sprich nach **Ich höre zu**. Du kannst die Antwort durch Sprechen unterbrechen oder **Sprache stoppen** wählen. Das stoppt die Sprachausgabe, nicht einen bereits angenommenen Auftrag.
4. Mit **Stummschalten** schaltest du nur dein Mikrofon aus; **Mikrofon an** aktiviert es wieder. Die Sprachausgabe und bereits angenommene Aufträge laufen weiter. **Zum Textchat** oder **Gespräch beenden** beendet die Sprachverbindung. Normale Textnachrichten sind erst nach Gesprächsende wieder möglich.

Während des Gesprächs hält MatoLoop den Bildschirm nach Möglichkeit eingeschaltet und gibt ihn beim Gesprächsende wieder frei. Das funktioniert auch in einer zum iPhone-Home-Bildschirm hinzugefügten Webapp ab iOS 18.4 über HTTPS. Lehnt das Gerät es etwa wegen des Stromsparmodus ab oder unterstützt der Browser es nicht, bleibt der Sprachchat nutzbar. Prüfe bei Bedarf die automatische Bildschirmsperre in den Geräteeinstellungen. Manuelles Sperren oder ein Appwechsel beendet weiterhin die Sprachverbindung; das Wachhalten ermöglicht kein Hintergrundgespräch.

Der Sprachmodus erhält die Anweisung, immer Deutsch mit neutraler hochdeutscher Aussprache ohne fremdsprachigen Akzent zu sprechen. Die tatsächliche Aussprache hängt vom Modell und der Stimme ab. Er erhält einen begrenzten Textausschnitt des bisherigen Chats und geeigneten Sessionkontext, nicht automatisch Anhänge oder vollständige Tool-Ergebnisse. Private Inhalte werden nicht in einen öffentlichen Sprachkontext übernommen. Erkennung und Transkription können ungenau sein. Im Chat steht bei abgebrochenen Antworten, dass erzeugter Text möglicherweise nicht vollständig gehört wurde.

Bitte für Arbeiten ausdrücklich den Agenten, etwa im Internet zu suchen. Der Auftrag läuft hier im selben Chat; eine andere Session ist nicht nötig. Höchstens ein Sprachauftrag läuft gleichzeitig. Profilbeschränkungen bleiben erhalten: **Plan** darf nicht durch einen Sprachauftrag zu **Build** werden. Beantworte Freigaben oder strukturierte Rückfragen direkt hier im Chat. Ein gesprochenes „Ja“ erteilt keine Tool-Freigabe. **Stoppen** auf der Auftragskarte beendet den Auftrag bestmöglich; bereits ausgeführte Aktionen werden nicht rückgängig gemacht.

MatoLoop entscheidet zunächst ohne Sprachausgabe, ob ein Auftrag nötig ist, und nimmt ihn vor der gesprochenen Bestätigung an. Während der Bestätigung kann die Arbeit bereits laufen. Auch direkte Gesprächsantworten durchlaufen diese zusätzliche Entscheidungsphase. Erfolgreich abgeschlossene Sprachaufträge verschwinden nach fünf Sekunden aus der Liste; ihre Ergebnisse bleiben im Chat. Fehlgeschlagene Aufträge und offene Freigaben oder Rückfragen bleiben sichtbar.

Angenommene Aufträge laufen nach Gesprächsende oder Verbindungsverlust weiter. Ergebnisse erscheinen direkt im Chat, auch wenn der Sprachmodus bereits beendet ist. Das Löschen dieses Chats oder seines Projekts beendet die laufenden Aufträge nach den normalen Sessionregeln. Ein Backend-Neustart unterbricht aktive Arbeit nach den normalen Sessionregeln.

Der Sprachagent erhält verfügbare Ergebnisse automatisch oder bei einer Statusabfrage und soll deine Frage anhand dieser Ergebnisse beantworten, einschließlich wichtiger Einschränkungen, Quellen und Informationsstand. Private Ergebnisse bleiben im Chat, wenn sie nicht an den gewählten Sprachprovider übermittelt werden dürfen; der Sprachmodus weist dann darauf hin. Fehlende Antworttexte oder gekürzte Auszüge werden ausdrücklich gekennzeichnet.

Beim Sessionwechsel, Ausblenden des Tabs, Verlassen der Seite oder Abmelden endet die lokale Sprachverbindung; das Mikrofon verbindet sich nicht automatisch neu. Bei einem Fehler prüfst du den Hinweis im Chat und startest das Gespräch bewusst erneut. Damit ist dieser Modus kein Hintergrundmikrofon und keine Audioaufzeichnung.

## Mediengenerierung

Für das OpenAI-kompatible Bildmodell mit der Modell-ID `Qwen-Image-2.1-Turbo`
erhält der Agent getrennte Formulierungshinweise zum Erzeugen und Bearbeiten.
Bei mehreren Referenzen benennt er deren Rollen in Upload-Reihenfolge als
`<image1>`, `<image2>` usw. Wortgetreu vorgegebene Prompts bleiben unverändert.
Andere Modell-Aliase erhalten diese Hinweise nicht automatisch.

Je nach Provider, Modellfähigkeiten und Rechten des gewählten Agentenprofils kann der Agent Bilder, Sprache oder Audio erzeugen und Bilder bearbeiten. Dauerhafte erzeugte oder bearbeitete Bilder, QR-Codes, Sprach- und Audioausgaben, abgerufene Bilder, gerenderte PDF-Seiten sowie OpenSCAD-Exporte werden als verwaltete Dateien mit gespeicherten Vollversionen abgelegt. Die jeweilige Nachricht verweist auf genau diese Version. Normales Bearbeiten, Verschieben und das Aktivieren einer anderen Version erhalten diesen Verweis; das Löschen der vollständigen verwalteten Datei macht ihn unbrauchbar.

Die Bearbeitung eines einzelnen, bereits erzeugten Bildes ergänzt dessen Versionsverlauf. Ein hochgeladenes oder heruntergeladenes Original sowie mehrere gemeinsam bearbeitete Quellen bleiben unverändert; das Ergebnis wird als neue Datei gespeichert.
