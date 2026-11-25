# Kanäle

Kanäle verbinden persönliche Messenger mit MatoLoop. Sie sind keine Integrationen und stellen dem Agenten keine zusätzlichen Tools oder Zugriffsrechte bereit. Jeder Benutzer verwaltet ausschließlich die eigenen Kanalinstanzen und Zugangsdaten unter **Einstellungen > Kanäle**.

## Telegram verbinden

Erstelle mit BotFather einen persönlichen Telegram-Bot. Nach globaler Kanalfreigabe öffne **Einstellungen > Kanäle > Kanal hinzufügen > Telegram**. MatoLoop speichert den Bot-Token verschlüsselt und zeigt ihn nicht erneut. Folge dem Telegram-Link oder sende `/start PAIRING_CODE` im privaten Botchat. Gruppen und Telegram-Kanäle werden nicht verarbeitet.

Nach der Kopplung sendest du Text, Bilder, Dateien, Audio, Video oder Sprachnachrichten. Aufnahmen verwenden dieselbe Verarbeitung wie Webaufnahmen: Originalaudio bleibt gespeichert und abspielbar, ein verfügbares Standard-Transkriptionsmodell ergänzt Text. Ein Fehler entfernt die Aufnahme nicht. Gewöhnliche Audiodateien werden ohne automatische Transkription gespeichert.

Ohne ausdrücklich gewählte Session verwendet Telegram denselben persönlichen **Chat** wie die Weboberfläche. Bestehende Sessionbindungen bleiben erhalten. Antworten, zugehörige Rückfragen und Freigaben gehen an den Ursprung der jeweiligen Anfrage; Nachrichten aus der Weboberfläche oder Dreaming-Vorschläge werden nicht allein wegen der gemeinsamen Session an Telegram weitergeleitet.

Während der Agent arbeitet, zeigt Telegram parallel den Schreibstatus und die aktuelle Aktivität, Reasoning-Zusammenfassung oder entstehende, formatierte Antwort als temporären Entwurf. Die Aktivität verwendet dieselben Bezeichnungen wie die MatoLoop-Oberfläche und ein passendes Symbol, zum Beispiel **📁 Datei lesen**. Vor dem ersten Reasoning erscheint **⚙️ Verarbeite Anfrage**. Reasoning erscheint als **🧠 Nachdenken**, ergänzt um eine tatsächlich veröffentlichte Zusammenfassung. Die letzte Tool-Aktivität bleibt bis zur nächsten sichtbaren Aktivität oder Antwort stehen. Tool-Argumente und -Ergebnisse erscheinen dort nicht. Wartet der Agent auf eine Freigabe oder Antwort, pausiert der Schreibstatus.

Freigaben einer Telegram-Anfrage erscheinen mit verfügbaren Entscheidungsknöpfen im privaten Chat. MatoLoop prüft Eigentümer, Ausgangsnachricht, Session, Lauf und offene Anfrage erneut. Shell-Befehle verwenden normale Freigaben für isolierte Agentenprozesse. Skillentwürfe brauchen weiterhin genaue Dateirevisionsprüfung in MatoLoop; Telegram kann sie nur ablehnen. Geschützte Credential-Rückfragen beantwortest du in MatoLoop, nicht als gewöhnliche Messenger-Antwort.

Strukturierte Agent-Rückfragen erscheinen ebenfalls direkt im privaten Chat. Wähle Antworten
über die Schaltflächen aus und wechsle mit **Zurück** oder **Weiter** zwischen mehreren Fragen.
Eigene Antworten und Freitext gibst du in das dafür eingeblendete Antwortfeld ein. Alternativ
kannst du Telegrams Funktion **Antworten** an der Fragen-Nachricht verwenden. Das Absenden einer
Texteingabe wechselt direkt zur nächsten Frage. Mit
**Antworten senden** schließt du die Rückfrage ab; **Ablehnen** beendet sie ohne Antworten.

Die endgültige Antwort wird als normale Nachricht zugestellt und zusammen mit der Anfrage vollständig in der zugehörigen MatoLoop-Session gespeichert. Telegram stellt Überschriften, Hervorhebungen, Listen, Links, Zitate und Code passend dar; komplexere Inhalte wie Tabellen, Mathematik oder Mermaid-Diagramme erscheinen in einer lesbaren Text- oder Codeform. Mit `/new` beginnst du eine neue Session, `/sessions` zeigt zuletzt verwendete Sessions und `/use <ID>` wechselt die Session. `/current`, `/stop`, `/unlink` und `/help` stehen ebenfalls zur Verfügung.

## Teams verbinden

Teams nutzt eine zentral verwaltete App für genau einen Microsoft-365-Mandanten.
Du koppelst dein Teams-Konto unter **Einstellungen > Kanäle**; ein eigener Bot
pro Benutzer ist nicht nötig. Unterstützt wird ausschließlich der persönliche
1:1-Chat, nicht Gruppen, Besprechungen oder Teams-Kanäle.

### Relay und Microsoft-App einrichten

Als Administrator brauchst du einen separaten öffentlich erreichbaren
HTTPS-Server für den Relay und die Berechtigung, eine benutzerdefinierte
Teams-App im Mandanten zu registrieren und zu installieren. MatoLoop bleibt
privat und benötigt nur ausgehende HTTPS-Verbindungen zu diesem Server.
Dateiübertragungen setzen verfügbares OneDrive/SharePoint und passende
Mandantenrichtlinien voraus. Nationale Microsoft-Clouds werden nicht unterstützt.

**Microsoft und der Relay können Nachrichten und Dateien lesen.** Betreibe den
Relay vertrauenswürdig und beschränke seinen administrativen Zugang. Ein
kompromittierter Relay kann auch Eingaben als Teams-Benutzer vortäuschen.
MatoLoops Tool-Rechte gelten weiter, beweisen dann aber nicht die menschliche
Herkunft einer Nachricht. Übertrage keine Geheimnisse als Chattext.

1. Öffne im [Microsoft Entra Admin Center](https://entra.microsoft.com)
   **Entra ID > App-Registrierungen > Neue Registrierung**. Verwende eine
   eigene Registrierung für den Teams-Bot, nicht die einer bestehenden
   Microsoft-365-Integration. Wähle **Nur Konten in diesem Organisationsverzeichnis**
   (Single Tenant) und lass die Umleitungs-URI leer. Kopiere aus der Übersicht die
   **Anwendungs-ID (Client)** und die **Verzeichnis-ID (Mandant)**. Erstelle unter
   **Zertifikate & Geheimnisse > Geheime Clientschlüssel** ein Clientgeheimnis.
   Sichere sofort dessen **Wert** im Secret-Speicher; die Geheimnis-ID ist kein
   Ersatz. Für den persönlichen Botchat brauchst du weder eine
   Benutzer-OAuth-Anmeldung noch zusätzliche Microsoft-Graph-Berechtigungen.
2. Lege auf dem Relay-Host außerhalb des Checkouts ein geschütztes Verzeichnis
   mit den Dateien `client-secret` und `relay-token` an. Die erste Datei enthält
   das Microsoft-Clientgeheimnis. Erzeuge für die zweite mindestens 32
   kryptografisch zufällige Bytes, beispielsweise mit `openssl rand -base64 48`.
   Beide Dateien müssen für Container-UID 10001 lesbar sein, nicht für andere
   Hostbenutzer. Gib die Werte weder in Git noch in Supportprotokollen weiter.
3. Setze auf dem Relay-Host `MATO_TEAMS_APP_ID`, `MATO_TEAMS_TENANT_ID` und
   `MATO_TEAMS_SECRET_DIRECTORY` auf die IDs und den absoluten Verzeichnispfad.
   Setze `MATO_TEAMS_MATOLOOP_URL` auf die HTTPS-Adresse, unter der deine
   Nutzer MatoLoop im Browser öffnen (ohne Pfad, nicht die Relay-Adresse).
   Beide Kopplungskarten verwenden diese Einstellung. Ohne sie zeigen die
   Karten die manuelle Anleitung; **Gespeicherte Verbindung testen** weist
   in MatoLoop auf den fehlenden Button hin.
   Starte die separate Vorlage aus dem Repository:

   ```bash
   docker compose -f apps/matoloop-teams-relay/compose.yaml up -d --build
   ```

   Die Vorlage veröffentlicht nur `127.0.0.1:3978`. Leite deinen öffentlichen
   HTTPS-Namen über einen Reverse Proxy dorthin. Erlaube bis zu 20 MiB große
   Requests und Zeitüberschreitungen über 40 Sekunden. Protokolliere weder
   Requestinhalte noch Authorization-Header. Der Relay benötigt sein eigenes
   persistentes Volume; starte nur eine Replik.
4. Erstelle im [Azure-Portal](https://portal.azure.com) eine
   [**Azure Bot**-Ressource](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-quickstart-registration).
   Wähle beim Microsoft-App-Typ **Single Tenant** und **Vorhandene App-Registrierung
   verwenden**. Gib die Client- und Mandanten-ID aus Schritt 1 an. Setze unter
   **Konfiguration** den Messaging-Endpunkt auf `https://DEIN-RELAY/api/messages`
   und aktiviere unter **Kanäle** den
   [Microsoft-Teams-Kanal](https://learn.microsoft.com/en-us/azure/bot-service/channel-connect-teams)
   für die kommerzielle Cloud. Prüfe, dass `https://DEIN-RELAY/health`
   erreichbar ist. Veröffentliche keinen MatoLoop-Endpunkt und deaktiviere
   nicht die Microsoft-Authentifizierung.
5. Öffne in MatoLoop **Administration > Kanäle > Verwaltung >
   Teams-Relay konfigurieren**. Speichere Relay-URL, dieselben Microsoft-IDs und
   den separaten Relay-Token. MatoLoop speichert ihn verschlüsselt.
   **Gespeicherte Verbindung testen** prüft Relay und IDs. Aktiviere anschließend
   die globale Kanalfreigabe und den Anbieter **Microsoft Teams**.
   Nach kurzer Zeit soll der Verbindungstest auch einen verbundenen
   MatoLoop-Empfänger anzeigen.
6. Exportiere das Teams-Paket wie unten beschrieben. Für einen Einzeltest
   kannst du es gemäß [Microsofts Installationsanleitung](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-upload)
   als benutzerdefinierte App hochladen.

### Teams-App für die Organisation veröffentlichen

Für einen internen Rollout brauchst du keinen öffentlichen Store-Eintrag.

1. Öffne im gespeicherten Teams-Relay-Dialog **Teams-App-Paket exportieren**.
   Gib die Paketversion, den Namen deiner Organisation als Herausgeber und
   erreichbare HTTPS-Links zu Website, Datenschutz und Nutzungsbedingungen
   an. Wähle **ZIP herunterladen**. Die ZIP übernimmt die gespeicherte
   Microsoft-App-ID, den **globalen Agentennamen** und das Logo aus
   **Darstellung**. Für das weiße Symbol in der Teams-App-Leiste eignet sich
   ein Logo auf transparentem Grund. Ohne eigenes Logo verwendet der Export
   die MatoLoop-Standardsymbole. Das Paket enthält **keine Zugangsdaten**.
   Passe für den Namen und das Symbol am Bot separat das
   [Botprofil in Azure](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-manage-settings#bot-profile)
   an. Branding gilt für diese Teams-App, nicht pro Benutzer oder Session.
2. Öffne als Teams-Administrator im [Teams Admin Center](https://admin.teams.microsoft.com/policies/manage-apps)
   **Teams-Apps > Apps verwalten > Neue App hochladen** und lade das Paket hoch.
   Öffne danach die App und beschränke ihre
   [Verfügbarkeit](https://learn.microsoft.com/en-us/microsoftteams/app-centric-management)
   für den Pilot auf die vorgesehenen Benutzer oder Gruppen. Die Freigabe
   ersetzt nicht die persönliche Kopplung in MatoLoop.
3. Berechtigte Benutzer finden die App in Teams unter **Apps** im Bereich
   **Für deine Organisation erstellt**. Die App kann erst nach einigen
   Stunden erscheinen. Wenn sich Name oder Logo ändern, exportiere bei
   unveränderter Microsoft-App-ID ein Paket mit höherer Version und lade es
   auf der Detailseite der vorhandenen App hoch. Der Export allein ändert
   keine bereits installierte Teams-App.

### Relay mit Portainer und Nginx Proxy Manager betreiben

Für Docker Standalone mit beiden Diensten auf demselben Host verwende statt
des Compose-Aufrufs oben die Vorlage
[`compose.portainer.yaml`](https://github.com/devmato/mato-loop/blob/develop/apps/matoloop-teams-relay/compose.portainer.yaml).
Sie setzt das bestehende externe Docker-Netz `npm_access` voraus und gibt keinen
Hostport frei. Verbinde nur vertrauenswürdige Container mit diesem Netz.

1. Importiere ein vorher gebautes Relay-Image unter **Images > Import** oder
   verwende ein ausdrücklich veröffentlichtes Image. TAR-, TAR.GZ- und TAR.XZ-Dateien
   lassen sich direkt importieren. Ein Image für `linux/amd64` benötigt einen
   passenden x86-64-Docker-Host.
2. Lege die beiden Secretdateien wie oben beschrieben an. Standardmäßig erwartet
   die Vorlage sie unter `/opt/matoloop-teams-relay/secrets`; Verzeichnis und
   Dateien müssen für UID 10001 zugänglich sein. Verwende Verzeichnismodus `0700`
   und Dateimodus `0600` mit passendem Eigentümer. Fehlt das Verzeichnis, bricht
   die Bereitstellung ab, statt es unbemerkt anzulegen.
3. Erstelle einen Stack mit dem Inhalt der Vorlage. Setze in Portainer
   `MATO_TEAMS_RELAY_IMAGE` auf `ghcr.io/devmato/mato-loop/matoloop-teams-relay`
   mit demselben festen Datum/SHA-Tag wie MatoLoop. Wenn du ein Image-Archiv
   importiert hast, verwende stattdessen dessen genauen Image-Namen samt Tag.
   Setze außerdem `MATO_TEAMS_APP_ID` und `MATO_TEAMS_TENANT_ID`. Nur bei abweichendem
   Secretpfad brauchst du zusätzlich `MATO_TEAMS_SECRET_DIRECTORY`.
   Microsoft-Clientgeheimnis und Relay-Token gehören ausschließlich in die
   Secretdateien, nicht in Stack-Variablen oder YAML. Starte den Stack mit einer
   Replik und warte auf einen gesunden Container.
4. Erstelle in Nginx Proxy Manager einen **Proxy Host** für deine Relay-Domain:
   Scheme `http`, Forward Hostname `matoloop-teams-relay`, Forward Port `3978`.
   Aktiviere ein gültiges SSL-Zertifikat und **Force SSL**. Eine zusätzliche
   NPM-Zugriffsliste oder vorgeschaltete Anmeldung würde die Bot-Anmeldung
   blockieren; der Relay prüft Microsoft-Tokens und seinen privaten Bearer-Token
   selbst. Ergänze unter **Advanced**, ohne Inhalte oder Authorization-Header
   zu protokollieren:

   ```nginx
   client_max_body_size 20m;
   proxy_read_timeout 75s;
   proxy_send_timeout 75s;
   ```

5. Prüfe `https://DEIN-RELAY/health`. Der Endpunkt soll
   `{"status":"ok"}` liefern. Fahre dann mit Microsoft-Messaging-Endpunkt,
   App-Paket und MatoLoop-Konfiguration aus den obigen Schritten fort.
   Erhalte das vom Stack verwaltete Spool-Volume bei Updates.

### Dein Teams-Konto koppeln und verwenden

1. Wähle **Kopplung starten** in der Willkommenskarte im persönlichen Teams-Chat.
   Fehlt der Button, öffne **Einstellungen > Kanäle > Kanal hinzufügen** und
   wähle **Microsoft Teams**. Aktiviere den Kanal und starte die Kopplung in MatoLoop.
2. Kopiere den angezeigten **Kopplungsbefehl** und sende ihn im persönlichen
   Teams-Chat. Der Code im Befehl gilt 15 Minuten.
3. Kehre zu MatoLoop zurück. Prüfe den vorgeschlagenen Anzeigenamen sowie die
   Entra-Benutzer- und Mandanten-ID. Bestätige nur dein eigenes Konto mit
   **Dieses Teams-Konto bestätigen**. Erst danach nimmt MatoLoop Chatnachrichten
   vom neuen Konto an. Bei einer Neukopplung bleibt deine bisherige Verbindung
   bis zur Bestätigung aktiv.
4. Der Bot bestätigt die Kopplung im selben Teams-Chat und zeigt Beispiele
   für eine erste Aufgabe. **Neuer Chat** legt einmalig einen leeren MatoLoop-Chat
   an, ohne eine Nachricht zu senden; **Letzte Chats** öffnet die Chat-Auswahl.
   Der Teams-Chat bleibt derselbe. Falls die Karte nicht ankommt, kannst du
   nach erfolgreicher Bestätigung trotzdem eine Nachricht senden.

Mit `/new`, `/sessions`, `/use <ID>`, `/current`, `/stop`, `/unlink` und `/help`
steuerst du deine Sessions. `/sessions` zeigt die letzten acht verwendeten Chats
untereinander als Karte. Wähle einen Chat, um direkt zu wechseln. Die nächste
Nachricht geht an diesen Chat. `/use <ID>` funktioniert weiterhin.
Über das Befehlsmenü am Eingabefeld kannst du `/sessions`, `/new`, `/current`
und `/help` auswählen. Teams trägt den Befehl ins Eingabefeld ein; sende ihn
danach wie eine Nachricht ab.
`/help` zeigt eine Übersicht mit je einer
Zeile pro Befehl, getrennt nach **Chat und Sessions** und **Coding**.
Coding-Befehle behalten in Teams ihre normale Schreibweise, beispielsweise
`/git-review`; wähle vorher eine Session
mit Computer und Workspace. Mit einem Relay, der Antwort-Streaming unterstützt,
erscheint der generierte Antworttext schrittweise in einer Teams-Nachricht.
Vor dem ersten Antworttext können unter deiner Nachricht laufende Aktivitäten
wie **Datei lesen** erscheinen. Kurze Aufrufe und Tool-Aufrufe nach Beginn des
Antworttextes werden unter Umständen nicht angezeigt. Tool-Argumente und
-Ergebnisse bleiben in MatoLoop. Während eines offenen Streams sendet MatoLoop
keinen zusätzlichen Schreibstatus. Lehnt Teams den Stream-Abschluss ab, kann
die fertige Antwort als separate Nachricht erscheinen. Schalte in
**Administration > Kanäle > Verwaltung > Teams-Relay konfigurieren**
**Live-Antworten in Teams** aus, um nur den Schreibstatus und die fertige
Antwort zu senden. Bestehende Kopplungen bleiben dabei erhalten. Bei aktiver
Pseudonymisierung wird Antworttext erst nach Fertigstellung gesendet. Lange
Antworten werden bei Bedarf aufgeteilt.

Kann MatoLoop eine fertige Antwort nicht bestätigt zustellen, erscheint eine
Benachrichtigung in MatoLoop. Öffne dort den zugehörigen Chat und prüfe Teams,
bevor du den Auftrag erneut sendest.

Gewöhnliche Rückfragen und Freigaben erscheinen als Karten mit **Deine Antwort
ist nötig** beziehungsweise **Deine Freigabe ist nötig**. Solange MatoLoop auf
deine Eingabe wartet, erneuert es den Schreibstatus nicht. Ein Klick auf eine
Einzelauswahl übernimmt die Antwort direkt. Bei Mehrfachauswahl bleiben die
Häkchen bis zum Absenden in derselben Karte. Bei Mehrfachauswahl und Freitext
nutze **Weiter** oder **Antwort senden**. **Wird in MatoLoop geprüft.** bestätigt nur den
Eingang eines Klicks; erst die nachfolgende MatoLoop-Antwort bestätigt die
Entscheidung. Nach Änderungen kann eine neue Karte erscheinen; alte Karten
werden nicht für eine neue Kopplung gültig. Ein MatoLoop-Neustart beendet auch
Aufträge, die noch auf deine Antwort oder Freigabe warten. Die dazugehörigen
offenen Karten sind danach nicht mehr gültig. Geschützte Credential-Rückfragen und
die genaue Prüfung von Skillveröffentlichungen bleiben in MatoLoop.

Kleine unterstützte Bilder erscheinen direkt im Chat. Andere Dateien werden
als Dateiangebot gesendet: Erst wenn du in Teams zustimmst und MatoLoop die
aktuelle Kopplung erneut bestätigt, lädt der Relay die Datei in dein
OneDrive/SharePoint. Ein Angebot verfällt mit den temporären Dateibytes nach
30 Minuten. Pro Datei und für alle eingehenden Anhänge zusammen gelten höchstens
20 MiB sowie gegebenenfalls kleinere MatoLoop-Grenzen. Eine fehlgeschlagene oder
abgelehnte Dateiübertragung wird nicht als erfolgreiche Gesamtzustellung behandelt.

### Verbindungs- und Zustellprobleme beheben

Ist MatoLoop offline, weist der Relay neue Nachrichten zurück, statt sie später
automatisch auszuführen. Bereits von MatoLoop angenommene Anfragen bleiben dort
gespeichert. Prüfe bei fehlender Antwort zuerst die Session in MatoLoop.

Bei unklarem Zustellstatus kann eine Nachricht bereits in Teams angekommen sein.
Prüfe den Chat, bevor du sie erneut sendest. Ein Relay-Neustart, verlorene
Zustellbelege oder das Löschen seines Volumes dürfen keine erneute
Agentenausführung auslösen. Temporäre Anhänge bleiben höchstens 30 Minuten,
minimale Zustellbelege 24 Stunden und Gesprächsreferenzen 30 Tage im Relay.

Nach Änderungen an Relay-URL, Microsoft-IDs oder Relay-Token müssen alle Benutzer
erneut koppeln. Offene alte Zustellungen und Karten werden dadurch ungültig.
Zum Tokenwechsel ersetze zuerst die Relay-Secretdatei und starte den Relay neu;
speichere anschließend den neuen Token in MatoLoop. Beim Wechsel der URL ist
ein neuer Token ausdrücklich erforderlich. Erhalte das Relay-Volume bei normalen
Containerupdates; sichere es vertraulich und kopiere es nicht zwischen Instanzen.

## Coding-Befehle

Das Telegram-Befehlsmenü und `/help` verwenden denselben allgemeinen Befehlskatalog
wie die Palette am MatoLoop-Eingabefeld. [`/plan`, `/research` und `/compare`](chat-und-sessions.md#planen-recherchieren-und-vergleichen)
funktionieren auch in Telegram und Teams ohne ausgewählte Coding-Session.
Die **Git**-App zeigt nur die Git-Aktionen.

In MatoLoop heißen die Befehle beispielsweise `/git-review`. Telegram benötigt
Unterstriche und zeigt deshalb `/git_review` im Menü und in `/help`; MatoLoop
ordnet diese Schreibweise automatisch demselben Befehl zu. In Telegram stehen
folgende Befehle zur Verfügung:

- `/git_init`: Git im gewählten Workspace anlegen, ohne Commit oder Veröffentlichung.
- `/git_review` (**Prüfen**): Änderungen einschließlich noch nicht übernommener Commits prüfen, ohne Projektdateien zu ändern.
- `/git_finish` (**Abschließen**): Fehlende oder veraltete Reviews und betroffene Checks ergänzen, sichere Korrekturen innerhalb der Aufgabe und nötige Aufgabencommits erledigen. Auf dem konfigurierten Zielbranch endet der Auftrag lokal; sonst folgt die getrennte Worktree-Integration per Rebase nur bei Bedarf und Fast-forward. Kein Push.
- `/git_commit`: Ausgewählte Arbeitsänderungen ausdrücklich committen, ohne Push.
- `/git_merge`: Aufgabenänderungen bei Bedarf committen, eigene unveröffentlichte Commits auf den lokalen Zielbranch
  rebasen, reviewen, prüfen und das ursprüngliche Checkout per Fast-forward
  aktualisieren. Kein Fetch oder Push; ein Remote ist nicht erforderlich.
- `/git_push` (**Pushen**): Vorhandene Commits veröffentlichen, ohne Commit oder Merge; bei bereits integriertem Worktree den aktuellen Zielbranch.
- `/git_pr` (**Pull Request**): Aufgabenänderungen bei Bedarf committen, den Remote-Zielstand im Quell-Worktree einarbeiten, prüfen, normal auf den gewählten Veröffentlichungsbranch pushen und einen Pull Request erstellen beziehungsweise den vorhandenen aktualisieren. Kein lokales Zielupdate und kein automatischer PR-Merge.
- `/git_sync`: Den ursprünglichen Projekt-Checkout aktualisieren, wenn er sauber,
  auf dem Zielbranch und ohne zusätzliche Zusammenführung aktualisierbar ist.
- `/git_remote [Name] [Adresse]`: Einen Remote über den Agenten einrichten oder
  seine Adresse ändern. Fehlende Angaben fragt der Agent nach. Zugangsdaten gehören
  in die Anmeldung auf dem Computer, nicht in die Adresse. Ein geändertes Ziel muss
  in bestehenden Sessions über die Git-App ausdrücklich übernommen werden.

Wähle vorher mit `/sessions` und `/use <ID>` die gewünschte Coding-Session.
Die Befehle starten normale Agentenläufe auf dem Computer dieser Session; die
vorhandenen Tool-Rechte, Rückfragen und Freigaben gelten weiter. In der Git-App sind
**Prüfen**, **Abschließen**, **Pushen** und **Pull Request** die Hauptaktionen;
Commit, lokale Integration und Pull stehen unter dem eingeklappten **Weitere Git-Aktionen**.
Init und Remote bleiben unter **Einstellungen**. Dort kannst du **Abschluss einplanen**
lokal oder im Worktree verwenden; das plant die normale Nachricht `/git-finish` ein.

Merge und PR benötigen einen separaten Projekt-Worktree; ein vorhandener Quellbranch
muss sich vom Zielbranch unterscheiden, ein losgelöster Stand ist erlaubt. Abschluss
braucht diesen Worktree nur, wenn das aktuelle Checkout nicht bereits auf dem konfigurierten
Zielbranch ist. Der Agent wechselt keine Branches automatisch und verschiebt keine Benutzerarbeit.
Finish, Merge und PR dürfen eindeutig zugehörige Aufgabenänderungen vorher committen und
beachten Repository-Konventionen sowie ausdrückliche Einschränkungen wie
„nur vorhandene Commits übernehmen“. Bei unklarer Auswahl fragt der Agent nach.
Gültige Review- und Check-Ergebnisse werden weiterverwendet; nur fehlende, veraltete oder
betroffene Bereiche werden erneut geprüft. Merge kann klare Integrationskonflikte und
-fehler beheben; PR stoppt bei Konflikten, notwendigen Review-Befunden oder fehlgeschlagenen
Checks, statt sie zu reparieren. Nur eigene unveröffentlichte Aufgabencommits werden bei
Bedarf rebasiert. Veröffentlichte, geteilte, fremde oder Merge-Commits sowie ein unklarer
Veröffentlichungsstand erfordern vor dem Umschreiben eine Rückfrage. Es gibt weder neue
Merge-Commits noch Force-Push. Für die spätere PR-Übernahme beim Hoster verwende Squash
oder Rebase statt eines Merge-Commits. Für PRs müssen ein
geeignetes Werkzeug und die Anmeldung beim Git-Hoster auf dem Computer vorhanden
sein. Ein laufender Auftrag wird durch diese Befehle nicht unterbrochen.

## Administrative Freigabe

Kommunikationskanäle sind global zunächst deaktiviert, auch für Administratoren. Aktiviere **Kommunikationskanäle aktivieren** unter **Administration > Kanäle > Einstellungen**. **Verwaltung** enthält die Anbieterfreigaben; Telegram ist standardmäßig erlaubt, braucht aber weiterhin die globale Aktivierung.

Deaktivierung erhält Verbindungen, verschlüsselte Zugangsdaten und Anbieterfreigaben. Empfang, Zustellung, Kopplung und persönliche Kanaltests sind dann gesperrt; der administrative Relay-Verbindungstest bleibt zur Fehlersuche verfügbar. Offene externe Zustellungen werden beendet und beim Reaktivieren nicht nachgeholt. Automationsziele fallen auf das gespeicherte App-Ergebnis zurück; Aufträge und Ergebnisse bleiben in ihrer normalen Session verfügbar.
