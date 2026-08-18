# i-net HelpDesk MCP Server

Ein [MCP](https://modelcontextprotocol.io)-Server, der die **Ticket-Web-API des
i-net HelpDesk** als Werkzeuge für beliebige KI-Agenten bereitstellt: Tickets
suchen und lesen, Bearbeitungsschritte ansehen, neue Tickets anlegen und
Ticketaktionen (antworten, schließen, eskalieren …) ausführen — inklusive
Dateianhängen.

Der Server kann auf zwei Arten betrieben werden:

| Modus | Wofür | Authentifizierung |
| --- | --- | --- |
| **stdio** | lokaler Prozess je Agent (Claude Desktop/Code, Cursor, VS Code …) | Token bzw. Benutzer/Passwort aus Umgebungsvariablen |
| **HTTP** (streamable) | zentral gehostet, mehrere Nutzer teilen sich einen Serverprozess | jeder Client schickt seinen eigenen `Authorization`-Header mit, optional zusätzlich die HelpDesk-URL |

---

## Voraussetzungen

* Python 3.10 oder neuer
* Ein i-net HelpDesk mit aktivierter Web-API
* Ein Benutzer mit dem Recht **„Web API"** — ohne dieses Recht antwortet der
  Server mit HTTP 403. Welche Tickets sichtbar sind und welche Aktionen
  erlaubt sind, richtet sich nach den Rollen dieses Benutzers.

## Installation

```bash
# direkt aus dem Repository ausführen (empfohlen für den Einstieg)
uvx --from git+https://github.com/roddyst/i-net_mcp_server inet-helpdesk-mcp --help

# oder klassisch installieren
pip install git+https://github.com/roddyst/i-net_mcp_server
```

Für die Entwicklung:

```bash
git clone https://github.com/roddyst/i-net_mcp_server
cd i-net_mcp_server
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

---

## Schnellstart: stdio (lokaler Agent)

```bash
export INET_BASE_URL="https://helpdesk.example.com:9000"
export INET_TOKEN="VGhpcyBpcyBqdXN0IGEgZGVtbyBhY2Nlc3MgdG9rZW4u"
inet-helpdesk-mcp
```

Konfiguration für Claude Desktop / Claude Code (`claude_desktop_config.json`
bzw. `.mcp.json`) — weitere Beispiele liegen unter [`examples/`](examples/):

```json
{
  "mcpServers": {
    "i-net-helpdesk": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/roddyst/i-net_mcp_server", "inet-helpdesk-mcp"],
      "env": {
        "INET_BASE_URL": "https://helpdesk.example.com:9000",
        "INET_TOKEN": "dein-access-token"
      }
    }
  }
}
```

Statt eines Tokens gehen auch `INET_USERNAME` und `INET_PASSWORD` (Basic Auth).
Der Token wird als `Authorization: Bearer <token>` gesendet, genau wie in der
i-net-Dokumentation beschrieben.

## Schnellstart: HTTP (zentral gehostet)

```bash
inet-helpdesk-mcp --transport http --host 0.0.0.0 --port 8000 \
                  --base-url https://helpdesk.example.com:9000
```

Der Endpunkt liegt dann unter `http://<host>:8000/mcp`. Der Agent trägt diese
URL ein und schickt seinen HelpDesk-Token im `Authorization`-Header mit — genau
das ist der Ablauf „URL + Bearer-Token", der Server reicht den Header an den
HelpDesk weiter. Beispiel für einen MCP-Client, der Remote-Server unterstützt:

```json
{
  "mcpServers": {
    "i-net-helpdesk": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer dein-access-token" }
    }
  }
}
```

**Ohne** `--base-url` bestimmt der Client zusätzlich das Zielsystem über den
Header `X-Inet-Base-Url`. Das ist praktisch für Mandanten mit mehreren
HelpDesk-Instanzen, öffnet den Server aber als Proxy für beliebige Adressen —
in einem offenen Netz deshalb besser eine feste `--base-url` setzen (dann ist
der Header abgeschaltet, außer er wird mit `--allow-url-header` erlaubt).

> **Hinweis zum Betrieb:** Der Server terminiert selbst kein TLS und
> authentifiziert Clients nicht eigenständig — die Anmeldung passiert am
> HelpDesk mit dem durchgereichten Token. Wenn er über das lokale Netz hinaus
> erreichbar sein soll, gehört ein Reverse Proxy mit HTTPS davor.

### Variante: fester Service-Account statt Token pro Nutzer

Läuft der Server auf derselben Maschine wie der Agent, ist oft ein einziger
technischer Benutzer gewollt. Dann liegt der Token auf dem Server und
`--ignore-client-auth` sorgt dafür, dass ein `Authorization`-Header des Agenten
ihn **nicht** überschreibt (Agenten schicken solche Header gelegentlich für
eigene Zwecke mit — ohne den Schalter landet der beim HelpDesk und jeder Aufruf
scheitert mit 401):

```bash
INET_BASE_URL=https://helpdesk.example.com:9000 INET_TOKEN=… \
  inet-helpdesk-mcp --transport http --host 127.0.0.1 --port 8765 --ignore-client-auth
```

Fertige systemd-Unit, Env-File und Schritt-für-Schritt-Anleitung dafür:
[`deploy/`](deploy/README.md).

---

## Werkzeuge

| Tool | Web-API | Beschreibung |
| --- | --- | --- |
| `server_info` | – | Zeigt die Konfiguration und prüft Verbindung + Zugangsdaten. Erste Anlaufstelle bei Fehlern. |
| `search_tickets` | `POST /api/ticket/search` | Tickets über eine Suchphrase finden (`query`, `limit`, `start`, `locale`). |
| `get_ticket` | `GET /api/ticket/<id>` | Felder und Attribute eines Tickets; `fields` grenzt die Antwort ein. |
| `list_ticket_actions` | `GET /api/ticket/<id>/actions` | Aktuell erlaubte Ticketaktionen als Map „Id → Anzeigename". |
| `list_ticket_steps` | `GET /api/ticket/<id>/steps` | Bearbeitungsschritte eines Tickets, optional ab Zeitstempel `since`. |
| `get_ticket_step` | `GET /api/ticket/<id>/steps/<step-id>` | Ein Bearbeitungsschritt inklusive Text. |
| `create_ticket` | `POST /api/ticket/create` | Neues Ticket anlegen, liefert die Ticket-Id. |
| `apply_ticket_action` | `POST /api/ticket/<id>/apply` | Ticketaktion ausführen, liefert die Id des neuen Bearbeitungsschritts. |

`create_ticket` und `apply_ticket_action` werden mit `--read-only` gar nicht
erst registriert — sinnvoll, wenn ein Agent nur lesen können soll.

Ticket-Ids werden sowohl als Zahl als auch in der kodierten Form akzeptiert,
die in den Betreffzeilen der HelpDesk-E-Mails steht.

### Typischer Ablauf

1. `search_tickets` mit einer Phrase wie `Drucker` oder `Resource:"First Level Support"`
2. `get_ticket` / `list_ticket_steps` / `get_ticket_step` zum Lesen
3. `list_ticket_actions`, um die gültige `action_id` zu ermitteln
4. `apply_ticket_action` mit dieser Id — die Ids unterscheiden sich je Ticket,
   Benutzer und Ticketstatus, sie dürfen also nicht geraten werden.

### Ticketfelder und Aktionsargumente

`ticket_fields`, `step_fields` und `action_arguments` sind optional und werden
im Normalfall nicht gebraucht. Wenn doch, gelten die Regeln der Web-API:
Schlüssel müssen echten Feldschlüsseln (oder deren lokalisiertem Anzeigenamen)
entsprechen, Werte sind Strings; JSON-Werte müssen als String kodiert werden.
Beispiele aus der i-net-Dokumentation:

```jsonc
{
  "ticketextension.dispatchNow": "ALWAYS",           // Ticket sofort disponieren
  "ticketextension.automail": "NO_MAILS_TO_ENDUSER", // keine Auto-Mails an Endanwender
  "processingtimeextension.appointment": "1733875200000", // Wiedervorlage/Termin
  "ticketactionextension.escalate": "{'targetResID':'<GUID>','changeTicketStatus':true}"
}
```

Unbekannte **Ticketfelder** führen zu einem Fehler, unbekannte
**Aktionsargumente** werden vom HelpDesk stillschweigend verworfen und nur ins
Debug-Log geschrieben.

### Anhänge

Anhänge werden als Liste übergeben, jeweils mit Inhalt **entweder** inline als
Base64 **oder** als Pfad auf dem Dateisystem des Servers:

```jsonc
{
  "text": "Anfrage mit Anhang",
  "attachments": [
    { "name": "screenshot.png", "content_base64": "iVBORw0KGgo…" },
    { "path": "/tmp/protokoll.pdf", "attachment_type": "Attachment" }
  ]
}
```

`path` funktioniert nur im stdio-Modus, in dem Agent und Server dieselbe
Maschine teilen; in den HTTP-Modi ist es automatisch abgeschaltet (und lässt
sich mit `--no-local-files` auch für stdio deaktivieren). Erlaubte Werte für
`attachment_type`: `Attachment`, `EmbeddedImage`, `Signature`, `Unknown`.
Obergrenze pro Datei: 25 MB.

---

## Konfiguration

Jede Option gibt es als Umgebungsvariable und als Kommandozeilenschalter; die
Kommandozeile gewinnt.

| Umgebungsvariable | Schalter | Standard | Bedeutung |
| --- | --- | --- | --- |
| `INET_BASE_URL` | `--base-url` | – | Basis-URL des HelpDesk, z. B. `https://helpdesk.example.com:9000` |
| `INET_TOKEN` | `--token` | – | Access-Token für `Authorization: Bearer …` |
| `INET_USERNAME` / `INET_PASSWORD` | `--username` / `--password` | – | Basic Auth als Alternative zum Token |
| `INET_TRANSPORT` | `--transport` | `stdio` | `stdio`, `http` oder `sse` |
| `INET_HOST` | `--host` | `127.0.0.1` | Bind-Adresse der HTTP-Transporte |
| `INET_PORT` | `--port` | `8000` | Port der HTTP-Transporte |
| `INET_HTTP_PATH` | `--http-path` | `/mcp` | Pfad des Streamable-HTTP-Endpunkts |
| `INET_TIMEOUT` | `--timeout` | `30` | HTTP-Timeout in Sekunden |
| `INET_VERIFY_TLS` | `--no-verify-tls` | `true` | TLS-Zertifikat des HelpDesk prüfen |
| `INET_CA_BUNDLE` | `--ca-bundle` | – | PEM-Datei mit den CA-Zertifikaten, denen vertraut wird (interne Firmen-CA) |
| `INET_READ_ONLY` | `--read-only` | `false` | Schreibende Tools ausblenden |
| `INET_ALLOW_URL_HEADER` | `--allow-url-header` | nur ohne `INET_BASE_URL` | `X-Inet-Base-Url`-Header erlauben |
| `INET_IGNORE_CLIENT_AUTH` | `--ignore-client-auth` | `false` | `Authorization`-Header der Clients ignorieren und immer die konfigurierten Zugangsdaten verwenden |
| `INET_ALLOW_LOCAL_FILES` | `--no-local-files` | `true` bei stdio, sonst `false` | Anhänge per Dateipfad erlauben |
| `INET_LOCALE` | `--locale` | `en` | Standardsprache der Suchphrase |

### HelpDesk hinter einer internen CA

Läuft der HelpDesk mit einem Zertifikat der eigenen Unternehmens-CA (etwa den
Active-Directory-Zertifikatsdiensten), kennt der Server dessen Aussteller
zunächst nicht: geprüft wird gegen das mitgelieferte
[certifi](https://pypi.org/project/certifi/)-Bundle mit den öffentlichen CAs,
**nicht** gegen den System-Truststore. Der Aufruf scheitert dann mit einem
`CERTIFICATE_VERIFY_FAILED` im Verbindungsfehler.

Der direkte Weg ist `--ca-bundle` mit der PEM-Datei der ausstellenden CA:

```bash
inet-helpdesk-mcp --base-url https://helpdesk.intern.example.com:9000 \
                  --ca-bundle /usr/local/share/ca-certificates/firmen-ca.crt
```

Genauso als Umgebungsvariable, z. B. im Env-File der systemd-Unit:

```bash
INET_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

Ohne die Option werden zusätzlich die Standardvariablen `SSL_CERT_FILE` und
`REQUESTS_CA_BUNDLE` berücksichtigt (in dieser Reihenfolge); erst wenn auch die
fehlen, bleibt es beim certifi-Bundle. Ein gesetztes `--ca-bundle` gewinnt
immer, und die angegebene Datei ist dann der **einzige** vertraute Truststore —
öffentliche CAs werden nicht zusätzlich akzeptiert. Zeigt der Pfad ins Leere,
bricht der Server sofort beim Start mit einer Konfigurationsmeldung ab.

**Alternative:** die CA einmal im System hinterlegen, dann genügt der Verweis
auf den System-Truststore (oder es reicht `SSL_CERT_FILE`):

```bash
sudo cp firmen-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates            # Debian/Ubuntu
# RHEL/Fedora: /etc/pki/ca-trust/source/anchors/ + update-ca-trust
inet-helpdesk-mcp --ca-bundle /etc/ssl/certs/ca-certificates.crt …
```

`--ca-bundle` und `--no-verify-tls` schließen einander aus — der Server beendet
sich mit einem Konfigurationsfehler, statt stillschweigend die Prüfung
abzuschalten. Welcher Truststore am Ende verwendet wird, zeigen der Startlog und
das Feld `tls` von `server_info`.

---

## Fehlersuche

* **`server_info` zuerst aufrufen** — es zeigt Basis-URL, Auth-Verfahren und ob
  eine Testabfrage gegen den HelpDesk funktioniert.
* **HTTP 401/403**: Token abgelaufen oder dem Benutzer fehlt das Recht „Web API".
* **HTTP 404 bei einem Ticket**: Ticket existiert nicht oder ist für diesen
  Benutzer nicht sichtbar; noch nicht autorisierte Tickets brauchen die
  Dispatcher-Rolle.
* **Verbindungsfehler**: Basis-URL inklusive Port prüfen (Standard des HelpDesk
  ist `9000`). Bei selbstsignierten Testsystemen hilft `--no-verify-tls`.
* **`CERTIFICATE_VERIFY_FAILED` / „unable to get local issuer certificate"**:
  Das HelpDesk-Zertifikat stammt aus einer internen CA — siehe
  [HelpDesk hinter einer internen CA](#helpdesk-hinter-einer-internen-ca).
* Mehr Details liefert `--log-level DEBUG` (Logs gehen auf stderr).

---

## Sicherheitshinweise

* Zugangsdaten stehen in Umgebungsvariablen bzw. im `Authorization`-Header und
  werden nie geloggt.
* Der Server macht genau das, was der angemeldete Benutzer darf — die
  Rechteprüfung bleibt beim HelpDesk.
* `apply_ticket_action` und `create_ticket` verändern Daten und können je nach
  Konfiguration E-Mails an Endanwender auslösen. Für Tests empfiehlt sich das
  Aktionsargument `"ticketextension.automail": "NEVER"` oder ein Testsystem.
* `get_ticket` liefert standardmäßig alle Felder eines Tickets, inklusive
  personenbezogener Daten — mit `fields` gezielt einschränken.

---

## English summary

MCP server exposing the i-net HelpDesk Ticket Web-API: search, read, create and
act on tickets, with attachment support. Run it over **stdio** (credentials from
`INET_BASE_URL` + `INET_TOKEN`) or over **streamable HTTP**, where each client
authenticates by sending its own `Authorization: Bearer <token>` header — and,
when no base URL is configured, selects the HelpDesk instance with an
`X-Inet-Base-Url` header. Tools: `server_info`, `search_tickets`, `get_ticket`,
`list_ticket_actions`, `list_ticket_steps`, `get_ticket_step`, `create_ticket`,
`apply_ticket_action`. Start with `--read-only` to expose the reading tools only.

## Lizenz

[MIT](LICENSE). Kein offizielles Produkt der i-net software GmbH.
Web-API-Dokumentation:
<https://docs.inetsoftware.de/helpdesk/help/webapi.ticket/p/ticket-web-api>
