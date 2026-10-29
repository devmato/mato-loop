# Webfetch

Webfetch uses `curl_cffi` with Chrome transport impersonation for HTTP and HTTPS.
This is the built-in provider and remains the default. Administrators can instead
select one provider under **Administration > Tools > Einstellungen
> Webabrufe und Downloads**. Configure Jina's base URL (including self-hosted Reader
instances) and optional API key through the normal integration editor. Personal Jina
connections and personal provider selections are not supported. Keys are encrypted
in credentials, never stored in integration configuration or sent to target sites.

Jina receives the requested URL and performs its own fetching, JavaScript rendering,
redirects, and document/image extraction. MatoLoop authorizes the requested URL and
the selected connection; it does not apply its local public-address filter or DNS
pinning to Jina's target requests. The configured Reader endpoint is an ordinary
administrator-managed integration connection. `DNT: 1` requests no Reader caching;
the service still receives the URL. Limits bound the Reader response, not its remote
page load. Reader uses the existing `network.fetch` policy and profile integration
availability, not separate connection-specific approvals. Reader output uses the
existing content/source envelope and sanitization.
The selected Reader connection extends the `webfetch` schema with `engine`
(`auto`, `browser`, `curl`), `remove_overlay`, `no_cache`, `target_selector`,
`wait_for_selector`, `remove_selector`, `with_links_summary`, and
`with_images_summary`. Built-in Webfetch does not accept these arguments.
Link and image overviews are returned as `links` and `imageLinks` dictionaries;
they are untrusted discovery data, not fetched attachments or citation evidence.
Browser mode renders JavaScript but does not automatically solve consent, login,
or bot challenges. Reader-specific arguments cannot be combined with `pdf_pages`.
The Jina-specific tool description distinguishes extracted content from original
files and does not promise verbatim structured data or validated image attachments.
It directs the model to use `auto` unless a mode was requested and, for incomplete
JavaScript content, retry at most once with `engine=browser` and `no_cache=true`.
Selectors must identify known expected elements. HTTP 401/403, login, and CAPTCHA
blocks do not warrant an automatic retry. These are model instructions, not host
retries; downloads and explicit PDF-page rendering retain their direct paths.

Connections configure the default timeout and overlay removal. Explicit tool values
override those defaults. The user's `UserPreferences.preferred_language` supplies
Reader's browser locale; `null` leaves Reader's own default unchanged. This is not
translation and does not select the application's UI language.

Cookies are stored alongside the API key in encrypted credentials. Configuration
responses expose only their target hosts and presence, never cookie values. Cookie
replacement/removal is explicit; omitting cookies preserves them independently of
API-key updates. Only cookies whose exact target host matches the requested HTTPS
URL are sent. They are encoded as secure host-only browser cookies with path `/`,
without a Domain attribute. Matching cookies force browser mode and bypass cache;
explicit `engine=curl` is rejected because Reader's direct redirect handling does
not reliably preserve host-only cookie scope. The administrator-selected Reader is
trusted to enforce browser cookie scoping and receives the secrets. Cookies on a
global connection are shared by all users authorized to use it; do not store personal
login sessions. No cookie acquisition, consent automation, or raw header/script
arguments are provided.
Reader decodes cookie values and re-encodes them with JavaScript's
`encodeURIComponent`. New values must survive that operation unchanged; raw `+`,
`/`, or `=` values are rejected, not rewritten. Existing unsupported values also
block matching fetches, but remain replaceable or removable. Do not manually
re-encode a cookie to pass validation: that changes the value expected by its site.

Direct Webfetch defaults to a 30-second total deadline. Jina defaults to 90 seconds
total, reserving 10 seconds from that budget for transfer and conversion. An explicit
tool timeout overrides the total deadline.
Target HTTP statuses are read from `httpStatus` or, when absent, Reader's
`Target URL returned error <status>:` warning line. Target failures are returned as
controlled errors, not citation sources, even when Reader itself responds with HTTP
200. Raw warning text is never forwarded. Without either target status, the Reader
response status is used. A disabled, deleted, profile-excluded, or changed selected
connection fails without silently switching providers.

There is no automatic cross-provider retry. Legacy stored fallback values are ignored
without changing the selected primary provider. HTTP 200 block pages are not classified
by content heuristics.

Markdown, text, HTML, and documents supported by Reader use Jina normally. Original
downloads, transcripts, and integration file imports remain on the direct pipeline.
Explicit `pdf_pages` requests also retain direct PDF extraction and page-image
rendering, rather than replacing page images with Reader text. Reader image extraction
returns its textual representation; it does not import image links as attachments.

## Built-in fetching

It does not execute JavaScript or load page dependencies. There is no browser
process or additional service.
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
normal precedence; download writes are authorized separately. Tool availability
remains enforced. Direct requests also enforce the configured address restriction,
including when internal targets have been explicitly enabled.

For direct requests, every redirect is authorized separately. DNS is resolved and validated again after
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
