# NPI Provider Lookup MCP server

An MCP server for US healthcare provider lookup and NPI verification. It is meant for healthtech developers,
credentialing and provider-data teams, revenue-cycle staff, and pharma/medtech sales ops. It reads the official
**CMS NPPES NPI Registry API v2.1** (`https://npiregistry.cms.hhs.gov/api/?version=2.1`), which is public and
needs no key or login.

## Tools

| Tool | What it does |
|---|---|
| `lookup_npi` | Full registry record for one NPI: individual or organization, name, credential, status, dates, all taxonomies with state licence numbers, mailing and practice addresses with phone/fax, extra practice locations, other identifiers, other names, endpoints, and for organizations the authorized official |
| `search_providers` | Search by first/last name, organization name, specialty (taxonomy description), city, state, ZIP, entity type. Up to 200 per page, paging with `skip` up to 1,000 |
| `validate_npi` | Format check, Luhn check digit with the `80840` prefix (as CMS specifies; returns the expected digit when wrong), and registry existence/status. Verdict is `active`, `not_in_registry`, `invalid_check_digit` or `invalid_format` |
| `specialty_codes` | Keyword → taxonomy codes and descriptions as they appear in the registry, with counts from a sample of up to 400 providers |

### Example calls

```jsonc
// lookup_npi
{"npi": "1003000126"}

// search_providers
{"last_name": "smith", "state": "CA", "taxonomy": "Cardiovascular Disease", "limit": 25}
{"organization_name": "mayo clinic", "state": "MN", "entity_type": "organization", "limit": 50}
{"taxonomy": "Dentist, Pediatric Dentistry", "city": "austin", "state": "TX"}
{"postal_code": "94143", "limit": 200, "skip": 200}

// validate_npi
{"npi": "1234567890"}   // -> invalid_check_digit, expectedCheckDigit 3
{"npi": "1234567893"}   // -> not_in_registry (valid check digit, never issued)

// specialty_codes
{"query": "cardio"}                                  // -> 207RC0000X Internal Medicine, Cardiovascular Disease, ...
{"query": "nurse practitioner", "state": "TX", "limit": 5}
```

### Notes on the registry's search behaviour

- At least one of first name, last name, organization name, taxonomy, city or ZIP is required. State or entity
  type alone is rejected, as NPPES requires.
- A trailing `*` wildcard needs at least 2 characters before it (`jo*`, `9410*`).
- `taxonomy` takes a **description, not a code**. NPPES matches the classification ("Dentist") or the
  specialization ("Pediatric Dentistry") separately. It never matches the joined "Dentist, Pediatric Dentistry"
  text. When given the joined form, the server queries the specialization and then keeps only providers whose
  taxonomy starts with the full text. If an exact description finds nothing, it retries once with `*` appended.
- Address filters match any address on file (mailing or practice location).
- NPPES returns at most 200 rows per request and `skip` up to 1,000, so at most 1,200 rows per query. Narrow
  the query for more.
- The API does **not** return deactivated NPIs. A valid-check-digit NPI that is not found was never issued or has
  been deactivated. The registry API cannot tell these apart; the CMS monthly deactivation file can, but it is not used here.

## Data source, terms and freshness

- **Source:** CMS National Plan and Provider Enumeration System (NPPES) NPI Registry API v2.1,
  https://npiregistry.cms.hhs.gov/api-page. It is a US federal registry, public and keyless, with no licence
  fee or registration. CMS makes NPPES data public by regulation (HIPAA administrative simplification). The
  exact legal citation and any licence wording were not re-verified for this README: the API page sets no
  explicit licence, and US federal works are generally public domain.
- **Freshness:** the registry API reflects NPPES updates daily. Every record carries `lastUpdated` (and
  `certificationDate` when present). This server makes a live API call per request and caches nothing.
- **Privacy / no enrichment.** The registry publishes provider practice and mailing addresses, phone and fax
  numbers, licence numbers and, for organizations, the authorized official's name, title and phone. This server
  returns **only what the registry publishes**. It does not add data from any other source, does not infer
  personal information, and does not store queries or results. Users are responsible for using the data
  lawfully (for example, telemarketing and anti-spam rules apply to any outreach).
- **Taxonomy codes:** `specialty_codes` derives codes only from registry records. The NUCC Health Care
  Provider Taxonomy CSV is not bundled, because the NUCC site says commercial use needs a licence from them.
  As a result the list is sample-based: rare specialties can be missing, so try a narrower query or a state.

## Limits

- 60 s timeout per tool call, 30 s per HTTP request, 2 retries on 429/5xx.
- `search_providers`: `limit` 1–200 (default 25), `skip` 0–1,000. `specialty_codes`: 2 registry requests of
  200 rows each, `limit` 1–100.
- The descriptive User-Agent identifies the server; no e-mail or account is sent.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/server.py                         # streamable-http on 0.0.0.0:8080, endpoint /mcp, GET /health
PORT=8095 .venv/bin/python src/server.py               # other port
MCP_TRANSPORT=stdio .venv/bin/python src/server.py     # stdio, for local MCP clients
.venv/bin/python tests/live_test.py                    # live test of every tool
```

Environment variables: `HOST` (default `0.0.0.0`), `PORT` (default `8080`), `MCP_TRANSPORT` (`streamable-http`, `stdio` or `sse`; default `streamable-http`). No API key is needed.

`mcpize.yaml` is included for MCPize (`runtime: python`, `startCommand.type: http`).

## Use with Claude Desktop or another MCP client

Local (stdio). Clone the repo, create the venv as above, then add this to `claude_desktop_config.json`
(or your client's MCP config), with the paths changed to where you cloned it:

```json
{
  "mcpServers": {
    "npi-provider": {
      "command": "/path/to/npi-provider-mcp/.venv/bin/python",
      "args": ["/path/to/npi-provider-mcp/src/server.py"],
      "env": {"MCP_TRANSPORT": "stdio"}
    }
  }
}
```

Remote (streamable HTTP). Run `.venv/bin/python src/server.py` on a server and point any client that supports
remote MCP servers at `http://<host>:8080/mcp`. `GET /health` returns `{"status": "healthy"}`.

## License

MIT, see [LICENSE](LICENSE). The data returned by the tools stays under the terms of its sources (see above).
