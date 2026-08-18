# Deployment: MCP-Server als lokaler Dienst

Anleitung für den Fall, dass der MCP-Server dauerhaft auf demselben Server
läuft wie der Agent (z. B. Hermes) und über `http://127.0.0.1:8765/mcp`
erreichbar ist. Die Zugangsdaten für den i-net HelpDesk liegen dabei auf dem
Server: jeder Aufruf wird als ein technischer Benutzer ausgeführt.

Alle Befehle als `root` auf dem Zielserver.

## 1. Benutzer und Verzeichnis anlegen

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin inet-mcp
install -d -o inet-mcp -g inet-mcp /opt/inet-helpdesk-mcp
```

## 2. Installieren

```bash
apt-get install -y python3-venv git          # Debian/Ubuntu
python3 -m venv /opt/inet-helpdesk-mcp/venv
/opt/inet-helpdesk-mcp/venv/bin/pip install --upgrade pip
/opt/inet-helpdesk-mcp/venv/bin/pip install \
    git+https://github.com/roddyst/i-net_mcp_server
chown -R inet-mcp:inet-mcp /opt/inet-helpdesk-mcp

/opt/inet-helpdesk-mcp/venv/bin/inet-helpdesk-mcp --version
```

## 3. Zugangsdaten hinterlegen

```bash
install -d -m 750 -o root -g inet-mcp /etc/inet-helpdesk-mcp
install -m 640 -o root -g inet-mcp \
    inet-helpdesk-mcp.env.example /etc/inet-helpdesk-mcp/env
editor /etc/inet-helpdesk-mcp/env      # INET_BASE_URL und INET_TOKEN eintragen
```

Die Datei ist die einzige Stelle mit Zugangsdaten. `chmod 640` mit Gruppe
`inet-mcp` heißt: nur `root` schreibt, nur der Dienst liest.

## 4. Dienst einrichten

```bash
install -m 644 inet-helpdesk-mcp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now inet-helpdesk-mcp
systemctl status inet-helpdesk-mcp
```

Port oder Bind-Adresse ändern: im `ExecStart` der Unit anpassen, danach
`systemctl daemon-reload && systemctl restart inet-helpdesk-mcp`.

## 5. Prüfen

```bash
# Läuft der Listener - und nur auf localhost?
ss -tlnp | grep 8765

# Logs
journalctl -u inet-helpdesk-mcp -f
```

Ein direkter `curl` auf `/mcp` beantwortet **keine** sinnvolle Frage: MCP ist
JSON-RPC über Streamable HTTP, ein nacktes GET liefert erwartungsgemäß einen
Fehler. Der richtige Test läuft über den Agenten selbst: `server_info`
aufrufen. Kommt dort `"connection": "ok"`, stimmen URL, Token und Berechtigung.

## 6. Agent anbinden

Der Agent verbindet sich zu `http://127.0.0.1:8765/mcp`. Beispiel für eine
MCP-Client-Konfiguration:

```json
{
  "mcpServers": {
    "i-net-helpdesk": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Ein `Authorization`-Header ist nicht nötig — die Unit startet den Server mit
`--ignore-client-auth`, er nutzt also immer den konfigurierten Service-Account
und ignoriert Header, die der Agent für eigene Zwecke mitschickt.

## Aktualisieren

```bash
/opt/inet-helpdesk-mcp/venv/bin/pip install --upgrade \
    git+https://github.com/roddyst/i-net_mcp_server
systemctl restart inet-helpdesk-mcp
```

## Sicherheitsüberlegungen

* **Der Listener gehört auf `127.0.0.1`.** Der Server prüft nicht, *wer* sich
  verbindet — wer den Port erreicht, handelt mit den Rechten des hinterlegten
  Service-Accounts. Auf `0.0.0.0` gehört er nur hinter einen Reverse Proxy mit
  TLS und eigener Authentifizierung.
* **Rechte des Service-Accounts klein halten.** Der Benutzer braucht das Recht
  „Web API"; alles darüber hinaus bestimmt, was ein fehlgeleiteter Agent
  anrichten kann. Für den Anfang ist ein Account mit Leserechten plus
  `INET_READ_ONLY=true` die risikoärmste Variante.
* **Nachvollziehbarkeit:** Im HelpDesk erscheinen alle Aktionen unter diesem
  einen Benutzer, nicht unter dem jeweiligen Endanwender. Wenn das stört,
  betreibe den Server ohne `--ignore-client-auth` und lass den Agenten den
  Token des jeweiligen Nutzers als `Authorization`-Header mitschicken.
* **Auto-Mails:** `apply_ticket_action` kann je nach Konfiguration E-Mails an
  Endanwender auslösen. Zum Testen entweder ein Testsystem verwenden oder das
  Aktionsargument `"ticketextension.automail": "NEVER"` setzen.
