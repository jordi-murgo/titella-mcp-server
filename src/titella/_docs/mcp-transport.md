# Shared MCP transport

## Problem solved

OMP can start dozens of agents and each agent expects an MCP server over stdio. Running the full core in every process would open the same UART several times and split the state of jobs, terminals, and transfers. The architecture uses two layers:

1. `titella-http` is the only process that creates `SerialLink`, `ShellClient`, `FilesClient`, and the HID lease. It exposes FastMCP via Streamable HTTP at `http://127.0.0.1:<port>/mcp`.
2. Each `titella-mcp` is a wrapper with no hardware access. It translates its MCP stdio bidirectionally to a Streamable HTTP session of the shared process.

SSE is not used. Streamable HTTP is the current MCP transport and allows keeping an independent session per wrapper without duplicating the physical state.

## Concurrent startup

The wrapper first queries `GET /health`. If there is no server:

1. it opens `TITELLA_STATE_DIR/http-<port>.lock` with mode `0600`;
2. it takes an exclusive cross-process lock;
3. it repeats the health check, because another wrapper may have finished starting the server;
4. it starts `titella-http` detached, with stdin closed and stdout/stderr sent to `TITELLA_STATE_DIR/http-<port>.log`;
5. it waits up to 15 seconds for `/health` to confirm service, transport, and version;
6. it releases the lock and connects to `/mcp`.

The other wrappers wait on the same lock and reuse the PID that is already listening. The lock and the log remain private. Closing a wrapper terminates only its HTTP session; it does not shut down the server or the UART.

## States and recovery

| State | Action |
|---|---|
| `ready` | Connect stdio to `/mcp`. |
| `absent` | Take the lock and start the server. |
| Port held by another service or version | Fail without killing or replacing the process. |
| Server dies with wrappers connected | Connections end; OMP can restart its wrappers. |
| New connection after a crash | The first wrapper starts a single server again. |

In-flight MCP requests are not automatically re-sent. Repeating a HID operation, a shell command, or a transfer block could duplicate effects. Recovery is done by creating a new MCP session and querying the remote state where appropriate.

## Configuration

Precedence remains `--config` > environment > TOML > defaults.

```toml
http_port = 8765
```

The equivalent variable is `TITELLA_HTTP_PORT`. Only ports between 1024 and 65535 are accepted. The host is fixed to `127.0.0.1`; there is no option to publish the server on the network.

All wrappers that need to share state must use the same port and the same device configuration. The first process started determines the daemon's effective configuration until it terminates.

## Operation

Normal startup from OMP:

```text
OMP agent → titella-mcp → http://127.0.0.1:8765/mcp → FastMCP → SerialLink
```

Manual foreground startup for diagnosis:

```bash
titella-http --config ~/.config/titella/config.toml
```

Check:

```bash
curl --fail --silent http://127.0.0.1:8765/health
```

`/health` publishes `service`, `transport`, `version`, and `pid`. To apply an upgrade, `SIGTERM` is sent to that PID and the next wrapper is allowed to start the new wheel. A wrapper of a different version rejects the existing daemon instead of mixing contracts.

## Documentation tools and resources

The documentation surface is identical over both transports: an stdio
session obtained through `titella-mcp` and a Streamable HTTP session opened
directly against `/mcp` see the same tools (`docs_index`, `docs_read`) and the
same fixed resources (`titella://docs/<id>`, MIME type `text/markdown`).

- `initialize` carries short instructions pointing at `docs_index` and
  `docs_read`, and states that documentation does not report live device
  state.
- `docs_index` lists the seven catalog entries with `source` and `version`, so
  a client can tell a server-bundled document (`titella`) from a
  slave-bundled one (`titella-slave-assets`) without reading it.
- `docs_read` returns the full document with provenance; a resource read
  returns exactly the same Markdown bytes.
- Neither the tools nor the resources require the HID lease, a ready agent or
  a reachable ESP32, and neither executes an operational tool. They read
  packaged assets only; there is no network access and no checkout lookup.
  A missing or oversized packaged document is reported as `docs_unavailable`,
  never as empty text.

Clients that only support tools can use `docs_index`/`docs_read`; clients that
support resources can use `resources/list` and `resources/read` to discover
and read the same documents.

## Security

FastAPI listens exclusively on loopback and FastMCP keeps DNS rebinding protection active. There is no HTTP authentication. Any local process that reaches the port can invoke the same tools and permissions as an authorized MCP client; this capability is part of the existing local and physical trust model.

The wrapper does not log MCP frames to stdout. Only JSON-RPC messages cross stdio; startup diagnostics go to the private log or to stderr if the connection cannot be established.

## Rollback

Rollback requires no data migration. Stop the daemon, restore the `titella-mcp` console script so it points to the direct stdio server, and reinstall the previous wheel. OMP's MCP entry does not change because the executable name is preserved.
