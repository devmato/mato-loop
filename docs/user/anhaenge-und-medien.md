# Anhänge, Diktat und Medien

## Nachrichtenanhänge

Sie können Dateien über Dateiauswahl oder Zwischenablage anhängen oder direkt auf die Nachrichteneingabe ziehen. Beim Ziehen von Dateien zeigt die Eingabe **Dateien hier ablegen** an; nach dem Ablegen erscheinen sie als Anhänge im Entwurf und werden erst beim Senden hochgeladen. Anzahl, Einzelgröße und Gesamtgröße legen Administratoren unter **Administration > Instanzlimits** fest. Browserlokale Anhangsentwürfe bleiben über alle Sessions zusammen auf 256 MiB begrenzt.

Uploads und ausdrücklich gespeicherte Dokumentableitungen sind verwaltete Dateien der besitzenden Session; Nachrichten behalten ihre exakten Versionsverweise. Es gibt keinen separaten Anhangsbaum. Office-/PDF-Textbegleiter können dauerhaft entstehen, Audio-WAV-Konvertierung erfolgt dagegen bei Bedarf nur vorübergehend. Das Original bleibt zum Download verfügbar.

## Bildverarbeitung

PDF-Anhänge erhalten neben dem Textauszug eine visuelle Vorschau: bei bis zu vier Seiten vollständig, sonst zunächst die erste Seite. Ein bildfähiges Chatmodell erhält die Seitenbilder direkt; andernfalls liest das konfigurierte Vision-Modell Text, Tabellen und Bildinhalte. Der Fallback verwendet kein weniger vertrauliches Modell als das Chatmodell. Weitere Seiten kann der Agent gezielt über den Dateizugriff nachladen. Die üblichen Bild- und Größenlimits gelten weiterhin; nicht übertragene oder nicht ausgewertete Seiten werden gekennzeichnet. Temporäre Seitenbilder werden nicht als zusätzliche Sessiondateien gespeichert.

PDFs lassen sich im gemeinsamen Dateiviewer öffnen. Die Anzeige verwendet den PDF-Viewer Ihres Browsers. Wenn dieser keine eingebettete Vorschau unterstützt, stehen **PDF separat öffnen** und der Download zur Verfügung.

Bildfähige Modelle können geeignete aktuelle oder ausdrücklich referenzierte Bilder erhalten. Große Bilder werden für die Modellanfrage normalisiert. Das gespeicherte Original bleibt unverändert. Bilder, Audio und andere Binärdaten werden vor der Übertragung nicht pseudonymisiert.

Bei einer Bildbearbeitung speichert MatoLoop sowohl die unmittelbaren Quellen als auch eine kontrollierte Ursprungslinie. Wenn Sie eine Bearbeitung derselben Bildlinie für die Session freigeben, können daraus erzeugte Bilder erneut bearbeitet werden, ohne bei jedem Zwischenergebnis nachzufragen. Andere Bilder, Sessions, Modelle oder Vertrauenslevel bleiben von dieser Freigabe getrennt.

Externe Markdown-Bilder werden nicht automatisch geladen. Sie entscheiden je Ursprung und Session, ob der Browser die externe Ressource abrufen darf.

## QR-Codes

Der Agent kann QR-Codes lokal und ohne Bildgenerator als scannbare PNG-Datei erzeugen. Zum Lesen hängen Sie ein Bild mit einem QR-Code an und formulieren einen kurzen Auftrag. Die Verarbeitung findet lokal statt; QR-Bilder und codierte Inhalte werden dafür nicht an einen Medienprovider gesendet.

## Sprachnachrichten

Die Mikrofontaste startet die Aufnahme mit Browserberechtigung. Stoppen hängt sie automatisch an den Entwurf; sie endet spätestens nach fünf Minuten. Dort können Sie sie anhören oder entfernen. Scheitert das Anhängen an den Grenzen, bieten **Anhängen**, **Herunterladen** und **Verwerfen** eine Wiederaufnahme bzw. Sicherung. Innerhalb der Grenzen sind mehrere Aufnahmen möglich.

Beim Senden wird das Original gespeichert und bei verfügbarer Transkription der erkannte Text ergänzt. Das Transkriptionsmodell wählen Sie unter **Standardmodelle > Transkription**, nicht am Agentenprofil. Diese ausdrückliche Benutzeraktion erzeugt keine weitere Tool-Freigabe; technisch deaktivierte Transkription bleibt deaktiviert. Aufnahme und Versand funktionieren auch ohne Transkription, und Fehler lassen das Original erhalten. Azure Speech ist eine mögliche reine Transkriptionsquelle; dabei wird das Originalaudio an Azure übertragen.

Gesendete Aufnahmen besitzen einen Audioplayer und einen Download der Originaldatei. Nur das Originalaudio wird dauerhaft gespeichert. Für Transkription und Wiedergabe erzeugt MatoLoop bei Bedarf vorübergehend eine WAV-Datei aus dem geprüften Original, ohne der Session eine doppelte Datei hinzuzufügen. Ältere WAV-Begleitdateien bleiben wegen bestehender Dateiverweise gespeichert, werden für Wiedergabe und Transkription aber nicht mehr benötigt. Das Modell kann später erneut auf den Anhang zugreifen, auch für eine weitere Transkription. Wenn Sie eine Nachricht wieder in den Entwurf übernehmen, bleibt das Audio erhalten; ein bereits enthaltenes erfolgreiches Transkript wird beim erneuten Senden nicht nochmals angehängt.

Verhindert eine Anhangsgrenze das Hinzufügen, bleibt die Aufnahme für einen weiteren Versuch, einen Download oder das ausdrückliche Verwerfen verfügbar. Beim Wechsel der Session bleiben fertige Aufnahmen im ursprünglichen Entwurf erhalten. Ungesendete Aufnahmen sind Entwürfe im Browserspeicher und keine serverseitigen Sessiondateien; eingereihte Nachrichten verwenden den dauerhaften Browser-Postausgang. Durch das frühere reine Transkriptionsverhalten verworfene Aufnahmen werden nicht nachträglich wiederhergestellt.

## Mediengenerierung

Je nach Provider, Modellfähigkeiten und Rechten des gewählten Agentenprofils kann der Agent Bilder, Sprache oder Audio erzeugen und Bilder bearbeiten. Dauerhafte erzeugte oder bearbeitete Bilder, QR-Codes, Sprach- und Audioausgaben, abgerufene Bilder, gerenderte PDF-Seiten sowie OpenSCAD-Exporte werden als verwaltete Dateien mit gespeicherten Vollversionen abgelegt. Die jeweilige Nachricht verweist auf genau diese Version. Normales Bearbeiten, Verschieben und das Aktivieren einer anderen Version erhalten diesen Verweis; das Löschen der vollständigen verwalteten Datei macht ihn unbrauchbar.

Die Bearbeitung eines einzelnen, bereits erzeugten Bildes ergänzt dessen Versionsverlauf. Ein hochgeladenes oder heruntergeladenes Original sowie mehrere gemeinsam bearbeitete Quellen bleiben unverändert; das Ergebnis wird als neue Datei gespeichert.
