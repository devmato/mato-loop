# Webfetch

Webfetch uses `curl_cffi` with Chrome transport impersonation for HTTP and HTTPS.
It does not execute JavaScript or load page dependencies. There is no browser
process, additional service, engine selection, or automatic transport fallback.
Impersonation improves compatibility with some sites but does not guarantee access
through bot challenges. For research, prefer another suitable source when needed;
browser control is appropriate for page interaction or browser-authenticated access.

The shared `ToolSettings.web_access` group owns `public_addresses_only`,
`user_agent`, and `max_response_mib`, exposed as `webAccess` in the configuration
API. These settings are under **Administration > Tools > Einstellungen > Webabrufe
und Downloads** and apply to Webfetch, downloads, transcripts, and integration URL
imports through that pipeline. They are not a network-wide filter for shell,
computer browser, search providers, or configured integration connections.
Tool availability remains under **Tool-Einstellungen**. Old JavaScript settings
are ignored and are no longer returned or saved; there is no `javascript` argument.

HTTP requests use the configured User-Agent (`MatoLoop/1.0` by default) and prefer
the requested Markdown, text, or HTML format using weighted Accept headers. The
actual response content type determines processing. HTML is sanitized and converted;
PDFs, images, structured data, plain text, and Markdown retain their existing processing.

## Model result size

The complete result remains persisted. Before token estimation, the model projection
limits large Webfetch content to 15,000 original characters: approximately 75% from
the beginning and 25% from the end, cut at line boundaries when possible. An explicit
omission marker and `toolResultReference` point to `session_tool_result_read` for the
full stored result. Its offsets count characters in the complete serialized result,
not just the page body. Source metadata and attachments remain available. This applies
to current results and retained results from the preceding turn; older results still
follow historical-reference rules. Further context-budget shortening preserves the
original reference. Inner JSON or other structured text may be incomplete in the
projection; the outer result envelope remains valid.

Reading omitted content uses the stored result, not another fetch. A new `webfetch`
still makes a fresh network request. There is no separate result cache or LLM summary.

## Authorization and networking

`network.fetch` and `network.search` are automatic in every preset except
**Alles bestätigen**, which asks for both. This covers Webfetch, published transcripts,
and the network part of downloads. Explicit action/profile rules retain their
normal precedence; download writes are authorized separately. Tool availability and
the configured address restriction remain enforced, including when internal targets
have been explicitly enabled.

Every redirect is authorized separately. DNS is resolved and validated again after
approval; libcurl connects only to those pinned addresses while retaining the
original hostname for TLS verification, SNI, and Host. Environment proxies are not
used. Requests have no shared cookies or authenticated browser state.
**Nur öffentliche Zieladressen zulassen** blocks private and special-use addresses;
disabling it deliberately permits targets reachable from the backend.

The configured response limit applies to declared and received sizes, including
decompressed bytes. Redirect and error bodies are not downloaded. Network requests
and conversion share the remaining timeout; approval waits do not consume it.
Cancellation closes the request session. Transport failures use controlled error
codes rather than external error text.

Fragments remain in result and citation identity, but are excluded from network
requests and permission matching. Webfetch exposes no POST or browser-interaction
argument; internal callers can use the shared bounded transport for binary POSTs.
