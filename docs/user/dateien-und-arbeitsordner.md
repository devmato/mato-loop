# Dateien und Arbeitsordner verwenden

MatoLoop trennt gespeicherte Session-Dateien von den Dateien deines Computers:

| Bereich | Zweck |
| --- | --- |
| Ablage | Verwaltete Dateien und Versionen einer Session; auch ohne Computer verfügbar |
| Computer-Dateien | Gewöhnliche Pfade auf dem ausgewählten Computer |
| Arbeitsordner | Aktueller Ordner des Agents auf diesem Computer |

Dateien werden zwischen Ablage und Computer nur ausdrücklich kopiert, nicht automatisch synchronisiert. Computerdateien haben keine MatoLoop-Versionshistorie.

## Arbeitsordner auswählen

1. Wähle unter **Control Center > Session** den verbundenen Computer.
2. Öffne über das Ordnersymbol die Ordnerauswahl.
3. Wähle **Diesen Ordner auswählen**. Alternativ übernimm einen Ordner aus **Computer-Dateien**.

Das Suchsymbol rechts in der Ordnerauswahl öffnet ein Suchfeld in der Pfadleiste. Es filtert nur bereits geladene Ordner nach Namen, unabhängig von Groß- und Kleinschreibung; Unterordner werden nicht durchsucht. Über **Weitere Ordner laden** ergänzt du die Liste. Mit dem X oder `Esc` entfernst du den Filter und kehrst zur Pfadnavigation zurück.

Neue Sessions haben ohne ausdrückliche Auswahl oder Projektvorgabe keinen Arbeitsordner. Beim Computerwechsel wird dessen Home übernommen. Plan und Build benötigen einen zugänglichen Arbeitsordner. Unter Linux und macOS verwendest du absolute POSIX-Pfade, unter Windows etwa `C:/Users/Ada/workspace`. Beachte unter macOS zusätzlich die [Datenschutzrechte für geschützte Ordner](computer-und-terminal.md#unter-macos-mit-apple-silicon-installieren).

Eine Ordnerauswahl erlaubt Navigation im gewählten Baum, keine Datei- oder Shell-Aktionen. Diese unterliegen ihren eigenen [Rechten und Freigaben](freigaben-und-datenfreigabe.md). Computerwechsel des Modells benötigen ebenfalls passende Rechte. Bei Verbindungsverlust erfolgt kein automatischer Wechsel oder erneuter Versuch.

!!! warning "Keine Betriebssystem-Sandbox"
    Der Agent arbeitet mit den Rechten seines Betriebssystembenutzers. Deine direkten Aktionen in Computer-Dateien umgehen absichtlich Modellrichtlinien und können auf dessen erreichbare Pfade zugreifen. Eigentümer- und technische Linkprüfungen bleiben bestehen. Das Löschen deines MatoLoop-Kontos entfernt keine Computerdateien.

Für geschützte Computerdateien kann das Modell Lesen, Schreiben, Bearbeiten und Löschen mit Administratorrechten anfragen, wenn dein Agent dies unterstützt. Prüfe vor einer Löschfreigabe den Zielpfad und die Angabe **Rekursiv löschen**. Eine Passwortabfrage dient nur zur Anmeldung und ersetzt keine Aktionsfreigabe. Gelöschte Computerdateien haben keine MatoLoop-Wiederherstellung; eine rekursive Löschung kann bei einem Fehler oder Abbruch teilweise ausgeführt sein.

Native Linux-Dateioperationen folgen keinen symbolischen Links – auch nicht mit Administratorrechten. Das betrifft etwa viele Pfade unter `/sys/class`. Für solche Systemdiagnosen kann die Shell verwendet werden, sofern deren eigene Rechteprüfung den Zugriff erlaubt.

## Dateien hochladen und verwalten

Öffne **Ablage** für Session-Dateien oder **Computer-Dateien** für den Rechner. Dort kannst du Ordner öffnen, filtern und durchsuchen, Dateien hoch- und herunterladen, anlegen, umbenennen oder löschen. Home- und absolute Pfadauswahl gehören nur zu Computer-Dateien. **Ordner als ZIP herunterladen** lädt einen Ordner samt Inhalt; große oder unsichere Dateibäume können abgelehnt werden.

Bis zu vier Dateien lassen sich gemeinsam auswählen und nacheinander hochladen. Transfers und normale Chat-Anhänge unterstützen bis zu 1 GiB, soweit Instanzlimits und Speicherkontingent es erlauben. Vorschau-, Format-, Medien- und Providergrenzen bleiben getrennt. Große Dateien können bei der Verarbeitung mehr Arbeitsspeicher als ihre Dateigröße benötigen.

Wenn du den aktiven Dateitab schließt, kehrst du zur **Ablage** oder zu **Computer-Dateien** zurück, je nachdem, wo du die Datei geöffnet hast. Der Ausgangstab muss dafür noch offen sein.

Wechsle in der **Ablage** über **Zum übergeordneten Ordner** vom Stammordner deiner Projekt-Session in den Ordner **Projekt**. Erst dort werden die Session-Ordner des Projekts geladen. Öffne einen Ordner, um die Ablage dieser Session zu sehen. Das ändert weder die aktive Chat-Session noch die Anzahl der angezeigten Sessions in der Seitenleiste. Beim Verschieben einer Session bleibt ihr vollständiger Dateiverlauf erhalten. Verweisen andere Sessions darauf, kann die besitzende Session nicht verschoben oder gelöscht werden.

## Text und Markdown bearbeiten

Öffne eine unterstützte Textdatei im Editor und speichere ausdrücklich. Markdown bietet Quelltext und eine bereinigte Vorschau, keinen WYSIWYG-Editor; externe Vorschau-Bilder bleiben deaktiviert. Für gespeichertes Markdown öffnet **Als PDF speichern** die Druckansicht deines Browsers. Ungespeicherte Änderungen werden nicht exportiert.

Tabs behalten Entwurf, Auswahl und Undo-Verlauf im Browser-Arbeitsspeicher. Tabwechsel und Zuklappen erhalten sie, ein Reload nicht. Bei einem Speicherkonflikt vergleichst du den Serverstand und entscheidest, welchen Stand du behältst; keine Auswahl speichert oder mischt automatisch.

Wenn du bei geöffneter Datei eine Nachricht sendest, erhält der Agent nur einen Dateiverweis und den Status ungespeicherter Änderungen, nicht automatisch den Inhalt. Solange der übermittelte Stand als ungespeichert markiert ist, werden passende Modell-Schreibzugriffe blockiert.

!!! warning "Gleichzeitige Änderungen abstimmen"
    Agentenänderungen arbeiten gegen den Dateiinhalt bei der Tool-Ausführung, nicht gegen einen beim früheren Lesen festgehaltenen Gesamtstand. Vollständiges Schreiben kann vorhandene Inhalte ersetzen. Vermeide parallele Änderungen derselben Computerdatei und prüfe anschließend den Diff; interne Schreibsperren halten externe Programme nicht auf.

## Versionen ansehen und wiederherstellen

Nur Dateien der **Ablage** besitzen gespeicherte Vollversionen. Öffne deren Versionsansicht zum Anzeigen, Herunterladen oder Aktivieren einer älteren Version. Aktivieren ersetzt den aktuellen Inhalt, erhält aber alle gespeicherten Versionen. Umbenennen und Verschieben erhalten Dateiidentität und Verlauf; Kopieren erzeugt eine eigenständige Datei.

Während eines Agentenlaufs siehst du bereits geänderte aktuelle Dateien, kannst betroffene Dateien aber nicht gleichzeitig ändern oder wiederherstellen. Am Laufende wird je tatsächlich geänderter Datei eine endgültige Version veröffentlicht. Bestätigte Aktionen erscheinen als **Dateiänderungen**; sie sind kein vollständiges Dateisystem-Audit. Für Repositoryvergleiche verwendest du [Git](git-und-worktrees.md#anderungen-ansehen).

!!! danger "Löschen entfernt den Verlauf"
    Das Löschen einer Datei entfernt ihren gesamten Versionsverlauf. Löschen eines Ordners wirkt rekursiv. Verweise in alten Nachrichten sind danach nicht mehr verfügbar. Prüfe den Pfad vor der Bestätigung.

## Speicherlimit prüfen

**Administration > Instanzlimits** legt Upload-, Vorschau- und optionale **Benutzer-Speicherlimits (MiB)** fest. Der Dateibrowser zeigt deinen Gesamtverbrauch. Zum Benutzerlimit zählen Dateien, Vollversionen und reservierte endgültige Laufausgaben aller eigenen Sessions, auch archivierter oder automatisierter.

Ohne Limit ist der Speicher unbegrenzt. Wird es unter den vorhandenen Verbrauch gesenkt, bleiben Dateien lesbar, herunterladbar und löschbar; zusätzlicher Verbrauch wird blockiert. Neue Dateien überschreiben keine vorhandenen Pfade. Andere Dateiformate oder zu große Vorschauen bleiben, soweit zulässig, zum Download verfügbar.
