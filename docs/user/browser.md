# Browser verwenden

Mit der Browser-Integration bedienst du gemeinsam mit dem Agenten Webseiten im selben Chat-Browser. Sie benötigt einen eigenen verbundenen Linux- oder Windows-Computer. Native Agents und Linux-Docker-Agents bieten dieselbe Bedienung.

## Browser einrichten

1. Lege unter **Einstellungen > Integrationen** eine persönliche Browserverbindung an und wähle den Computer.
2. Docker-Agents enthalten die Laufzeit bereits. Bei nativen Agents starte bei Bedarf **Browser einrichten**. Unter Linux bestätige benötigte Systempakete ausdrücklich. Unter Windows läuft die Einrichtung ohne weitere Eingaben und ohne Administratorrechte im Konto des Agents.
3. Wähle denselben Computer in der Session und erlaube die Verbindung für das gewünschte Profil. Plan, Build und Sysadmin schließen Integrationen standardmäßig aus.
4. Öffne **Control Center > Browser** oder bitte den Agenten, eine Webseite aufzurufen.

Browser-Tools starten die Laufzeit auch ohne geöffnete App. Abgelehnte Hostpakete können die Funktion unverfügbar lassen.

Bei nativen Agent-Updates bleibt die eingerichtete Browser-Unterstützung erhalten. Das Update prüft die passende Chromium-Version vor der Aktivierung. Fehlen zusätzliche Systempakete, bleibt die bisherige Agent-Version aktiv. Kläre die fehlenden Pakete vor einem erneuten Update und bestätige ihre Installation ausdrücklich.

## Webseiten bedienen

Adressfeld, Tabs, Zurück, Vorwärts und Neuladen funktionieren wie in einem Browser. Native Browsermenüs und DevTools sind nicht enthalten. Während einer Agent-Browseraktion ist deine Eingabe kurz gesperrt; **Agent aktiv** zeigt dies an. Danach kannst du sofort weiterarbeiten.

Markiere Text mit Maus oder Tastatur und verwende **Strg/Cmd+C** und **Strg/Cmd+V**. Einfügen verwendet das zuvor gewählte Websitefeld und bietet bei Bedarf einen Ausweichdialog. Kopieren übernimmt die Auswahl direkt; eigene Kopierfunktionen einer Website werden nicht emuliert. Nur Text unter 16 KiB wird übertragen.

**Tab/Umschalt+Tab** wechselt Websitefelder. **Strg/Cmd+L**, **Strg/Cmd+T/W** und **Strg/Cmd+R** öffnen das Adressfeld, öffnen/schließen Tabs und laden neu. Manche lokalen Browser behalten eigene Tastenkürzel, etwa zum Tabwechsel, für sich.

### Verbindung wiederherstellen

Bei kurzen Verbindungsabbrüchen verbindet sich die sichtbare Browser-App bis zu dreimal automatisch mit demselben laufenden Browser. Zugeklappte Ansichten pausieren die Versuche. Klicks und Eingaben werden nicht wiederholt.

Scheitert die Wiederverbindung, klicke auf **Browser erneut verbinden** links in der Browserleiste. Ein Neuladen der WebUI ist dafür nicht nötig. Wurde die Browserlaufzeit beendet, etwa durch einen Agent-Neustart oder **Browser schließen** in einer anderen Ansicht, öffnet erst dein ausdrücklicher Neuversuch einen neuen Browser; vorherige Tabs, Anmeldungen und ungespeicherte Eingaben sind verloren.

### Dateien hochladen und herunterladen

Klicke zuerst das Datei-Uploadfeld der Website an und wähle dann **Datei hochladen** aus Ablage oder Computer-Dateien. Falls das Websitefeld nicht mehr offen ist, erlaubt **Datei einsetzen** einen erneuten Versuch.

Downloads landen in der **Session-Ablage** und bleiben nach dem Schließen des Browsers erhalten. Computerdateien werden weder verschoben noch gelöscht; Arbeitsverzeichnisse werden nicht synchronisiert. Pro Transfer sind bis zu 1 GiB möglich, soweit niedrigere Instanzlimits und dein Speicherkontingent es erlauben. Vorschau und Verarbeitung haben eigene Grenzen.

Unter Windows wird die Downloadgröße erst nach Abschluss geprüft. Halte ausreichend freien Speicher für laufende Downloads bereit; die Transfergrenze begrenzt deren vorübergehenden Platzbedarf nicht.

## Web-Apps beim Coding prüfen

Starte den Entwicklungsserver im richtigen Arbeitsordner und gib dem Agenten seine URL. Er kann die App mit Playwright bedienen, Fehler nachstellen und Änderungen anhand von Seitenzustand und Screenshots prüfen. Über dieselbe Browseransicht kannst du selbst eingreifen. Dauerhafte Playwright-Tests im Projekt bleiben davon unabhängig.

Die URL muss vom ausgewählten Computer aus erreichbar sein. `localhost` bezeichnet diesen Computer, bei Docker-Agents den Agent-Container. Ein Server auf deinem Entwicklungsrechner ist dadurch nicht automatisch erreichbar.

## Rechte und Risiken

**Browser verwenden** umfasst Lesen, Navigation, Bedienung und JavaScript innerhalb der Webseite. Build und darauf basierende Profile erlauben dies standardmäßig automatisch, außer bei **Alles bestätigen**. Allgemein, Plan und Sysadmin fragen standardmäßig nach. Eigene Verbindungs- und Profilregeln gehen vor.

**Browser-Python ausführen** benötigt zusätzlich ein eigenes Recht und fragt standardmäßig in jedem Preset nach. Eine gewöhnliche Browserfreigabe erlaubt kein Python. Dateien und Secrets behalten ihre eigenen Rechte.

!!! warning "Website-Aktionen können Daten ändern"
    Ein automatisch erlaubter Klick oder JavaScript kann absenden, kaufen oder löschen. **Automatisch außer Löschen** verhindert das nicht. Freigegebenes Python kann außerdem Dateien, Prozesse, Netzwerk und andere Browserdaten mit den Rechten des Browserprozesses erreichen. Getrennte Chat-Browser sind keine Betriebssystem-Sandbox.

## Browser stoppen oder schließen

- **Laden stoppen** beendet das manuelle Seitenladen.
- **Agent-Anfrage stoppen** beendet den Agentenlauf, auch während eines Browserbefehls. Mobil findest du diese Aktion im Kopf des Control Centers.
- Das Schließen des App-Tabs beendet die Browserlaufzeit nicht.
- **Browser schließen** verwirft Tabs, Website-Anmeldungen und ungespeicherte Website-Eingaben; gespeicherte Downloads bleiben erhalten.

Jeder Chat besitzt einen eigenen Browser. Andere Chats erhalten dessen Cookies, Tabs und Anmeldungen nicht. Mehrere Ansichten desselben Chats teilen dagegen denselben Browser. Nach Agent-Neustart musst du dich erneut an Websites anmelden.

Abbruch macht bereits ausgeführte Website-Aktionen nicht rückgängig. Muss der Browserprozess für einen sicheren Abbruch beendet werden, gehen auch temporäre Tabs und Anmeldungen verloren. Prüfe vor dem Wiederholen einer unklaren Änderung zuerst den tatsächlichen Websitezustand.
