"""NPI Provider Lookup MCP server.

US healthcare provider lookup and NPI verification from the CMS NPPES NPI Registry API v2.1
(https://npiregistry.cms.hhs.gov/api/?version=2.1, public, keyless). Read-only. Returns only what the registry
publishes (no enrichment, no other data sources).
"""
import asyncio
import logging
import os
import re
from collections import Counter

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("npi-provider", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))

API = "https://npiregistry.cms.hhs.gov/api/"
UA = "npi-provider-mcp/1.0 (MCP server; reads the public CMS NPPES NPI Registry API; python-httpx)"
CALL_TIMEOUT = 60      # seconds per tool call
HTTP_TIMEOUT = 30      # seconds per HTTP request
MAX_LIMIT = 200        # NPPES maximum per request
MAX_SKIP = 1000        # NPPES maximum skip
SOURCE = {"name": "CMS NPPES NPI Registry API v2.1", "url": "https://npiregistry.cms.hhs.gov/api-page",
          "note": "Public registry data as published by CMS (NPPES is updated daily). Returned as-is, no enrichment. "
                  "Deactivated NPIs are not returned by the API."}

STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
             "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI AA AE AP".split())


class RegistryError(Exception):
    pass


async def _get(params: dict) -> dict:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    params["version"] = "2.1"
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=HTTP_TIMEOUT,
                                 follow_redirects=True) as http:
        for attempt in range(3):
            try:
                r = await http.get(API, params=params)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise RegistryError(f"NPPES network error: {type(e).__name__}") from e
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RegistryError(f"NPPES HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if data.get("Errors"):
                raise RegistryError("; ".join(e.get("description", str(e)) for e in data["Errors"]))
            return data
    return {}


async def _run(coro):
    try:
        return await asyncio.wait_for(coro, timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"timed out after {CALL_TIMEOUT}s (NPPES slow); try again"}
    except (RegistryError, ValueError) as e:
        return {"error": str(e)}


# ---------- NPI check digit ----------

def luhn_ok(npi: str) -> bool:
    """NPI check digit: Luhn (ISO/IEC 7812) over '80840' + the 10-digit NPI."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + npi)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def expected_check_digit(npi: str) -> int | None:
    if not re.fullmatch(r"\d{9,10}", npi):
        return None
    base = npi[:9]
    for c in range(10):
        if luhn_ok(base + str(c)):
            return c
    return None


# ---------- normalisation ----------

def _phone(p):
    return p or None


def _zip(z):
    z = z or ""
    return f"{z[:5]}-{z[5:]}" if len(z) == 9 and z.isdigit() else (z or None)


def _address(a: dict) -> dict:
    return {
        "purpose": (a.get("address_purpose") or "").lower() or None,
        "line1": a.get("address_1") or None, "line2": a.get("address_2") or None,
        "city": a.get("city"), "state": a.get("state"), "postalCode": _zip(a.get("postal_code")),
        "country": a.get("country_code"), "phone": _phone(a.get("telephone_number")),
        "fax": _phone(a.get("fax_number")),
    }


def _name(b: dict, etype: str) -> str | None:
    if etype == "NPI-2":
        return b.get("organization_name")
    parts = [b.get("name_prefix") if b.get("name_prefix") not in (None, "--") else None, b.get("first_name"),
             b.get("middle_name"), b.get("last_name"),
             b.get("name_suffix") if b.get("name_suffix") not in (None, "--") else None]
    return " ".join(p for p in parts if p) or None


def _taxonomies(r: dict) -> list[dict]:
    return [{"code": t.get("code"), "description": t.get("desc"), "primary": bool(t.get("primary")),
             "state": t.get("state") or None, "license": t.get("license") or None,
             "group": t.get("taxonomy_group") or None} for t in r.get("taxonomies") or []]


def _primary_tax(r: dict) -> dict | None:
    tx = _taxonomies(r)
    return next((t for t in tx if t["primary"]), tx[0] if tx else None)


def _full(r: dict) -> dict:
    b = r.get("basic") or {}
    etype = r.get("enumeration_type")
    out = {
        "npi": r.get("number"),
        "entityType": "individual" if etype == "NPI-1" else "organization" if etype == "NPI-2" else etype,
        "name": _name(b, etype),
        "status": {"A": "active"}.get(b.get("status"), b.get("status")),
        "enumerationDate": b.get("enumeration_date"),
        "lastUpdated": b.get("last_updated"),
        "certificationDate": b.get("certification_date"),
        "primaryTaxonomy": _primary_tax(r),
        "taxonomies": _taxonomies(r),
        "addresses": [_address(a) for a in r.get("addresses") or []],
        "practiceLocations": [_address(a) for a in r.get("practiceLocations") or []],
        "otherIdentifiers": [{"identifier": i.get("identifier"), "type": i.get("desc"), "state": i.get("state") or None,
                              "issuer": i.get("issuer") or None} for i in r.get("identifiers") or []],
        "otherNames": [{k: v for k, v in o.items() if v not in (None, "", "--")} for o in r.get("other_names") or []],
        "endpoints": [{k: e.get(k) for k in ("endpointType", "endpointTypeDescription", "endpoint", "affiliation",
                                               "affiliationName", "use", "contentTypeDescription")}
                      for e in r.get("endpoints") or []],
    }
    if etype == "NPI-1":
        out.update({"firstName": b.get("first_name"), "middleName": b.get("middle_name"),
                    "lastName": b.get("last_name"), "credential": b.get("credential"), "sex": b.get("sex"),
                    "soleProprietor": b.get("sole_proprietor")})
    else:
        out.update({"organizationName": b.get("organization_name"),
                    "organizationalSubpart": b.get("organizational_subpart"),
                    "parentOrganizationLbn": b.get("parent_organization_legal_business_name"),
                    "authorizedOfficial": {
                        "name": " ".join(x for x in (b.get("authorized_official_first_name"),
                                                     b.get("authorized_official_middle_name"),
                                                     b.get("authorized_official_last_name")) if x) or None,
                        "title": b.get("authorized_official_title_or_position"),
                        "credential": b.get("authorized_official_credential"),
                        "phone": b.get("authorized_official_telephone_number")}})
    out["registryUrl"] = f"https://npiregistry.cms.hhs.gov/provider-view/{r.get('number')}"
    return out


def _compact(r: dict) -> dict:
    b = r.get("basic") or {}
    etype = r.get("enumeration_type")
    loc = next((a for a in r.get("addresses") or [] if a.get("address_purpose") == "LOCATION"), None)
    pt = _primary_tax(r)
    return {
        "npi": r.get("number"),
        "entityType": "individual" if etype == "NPI-1" else "organization",
        "name": _name(b, etype), "credential": b.get("credential"),
        "status": {"A": "active"}.get(b.get("status"), b.get("status")),
        "primaryTaxonomy": f"{pt['code']} {pt['description']}" if pt and pt.get("code") else (pt or {}).get("description"),
        "practiceAddress": _address(loc) if loc else None,
        "enumerationDate": b.get("enumeration_date"), "lastUpdated": b.get("last_updated"),
    }


def _npi_arg(npi) -> str:
    s = re.sub(r"\D", "", str(npi or ""))
    if len(s) != 10:
        raise ValueError(f"NPI must be exactly 10 digits, got {npi!r}")
    return s


def _wild(v: str | None, field: str) -> str | None:
    """NPPES wildcard rule: a trailing '*' needs at least 2 characters before it."""
    if not v:
        return None
    v = re.sub(r"\s+", " ", v.strip())
    if v.endswith("*") and len(v.rstrip("*")) < 2:
        raise ValueError(f"{field}: a trailing '*' wildcard needs at least 2 characters before it")
    return v


# ---------- tools ----------

@mcp.tool()
async def lookup_npi(npi: str) -> dict:
    """Get the full NPPES registry record for one 10-digit NPI (National Provider Identifier).

    npi: e.g. "1003000126". Returns the record as CMS publishes it: entity type (individual / organization), name,
    credential, status, enumeration / last-updated / certification dates, primary taxonomy and all taxonomies
    (code, description, state licence number), mailing + primary practice address with phone/fax, additional
    practice locations, other identifiers (e.g. state Medicaid ids), other names, Direct/FHIR endpoints, and for
    organizations the authorized official and parent organization. found=false if the registry has no active
    record (never issued or deactivated). No data from other sources is added.
    """
    async def go():
        n = _npi_arg(npi)
        data = await _get({"number": n})
        res = data.get("results") or []
        if not res:
            return {"found": False, "npi": n, "checkDigitValid": luhn_ok(n),
                    "message": "No record in the NPPES registry (never issued, or deactivated).", "source": SOURCE}
        return {"found": True, "provider": _full(res[0]), "source": SOURCE}
    return await _run(go())


@mcp.tool()
async def search_providers(first_name: str | None = None, last_name: str | None = None,
                           organization_name: str | None = None, taxonomy: str | None = None,
                           city: str | None = None, state: str | None = None, postal_code: str | None = None,
                           entity_type: str | None = None, limit: int = 25, skip: int = 0) -> dict:
    """Search US healthcare providers in the NPPES NPI Registry.

    Filters (combine freely; NPPES needs at least one besides state/entity_type):
      first_name, last_name: individual provider name; a trailing '*' wildcard is allowed after 2+ chars ("jo*");
      organization_name: organization legal or other name, e.g. "mayo clinic", "kaiser*";
      taxonomy: specialty DESCRIPTION, not code, e.g. "Cardiovascular Disease", "Nurse Practitioner", "Pediatric*",
                or a full "Classification, Specialization" text such as "Dentist, Pediatric Dentistry"
                (use specialty_codes to find valid descriptions). NPPES matches a taxonomy word-prefix only with a
                trailing '*'; if the exact text finds nothing the server retries once with '*' appended;
      city, state (2-letter, e.g. "CA"), postal_code (5 or 9 digits, wildcard "9410*" allowed);
      entity_type: "individual" (NPI-1) or "organization" (NPI-2).
    limit: 1-200 (default 25), skip: 0-1000 for paging (NPPES returns at most 1,200 rows per query).
    Address filters match any address the registry has for the provider (mailing or practice location).
    Returns compact rows: npi, entityType, name, credential, status, primaryTaxonomy, practiceAddress (with phone),
    enumerationDate, lastUpdated. Use lookup_npi for the full record.
    """
    async def go():
        et = {"individual": "NPI-1", "npi-1": "NPI-1", "organization": "NPI-2", "npi-2": "NPI-2"}
        etype = None
        if entity_type:
            etype = et.get(entity_type.strip().lower())
            if not etype:
                raise ValueError("entity_type must be 'individual' or 'organization'")
        st = state.strip().upper() if state else None
        if st and st not in STATES:
            raise ValueError(f"state must be a 2-letter US state/territory code, got {state!r}")
        criteria = [first_name, last_name, organization_name, taxonomy, city, postal_code]
        if not any(c and c.strip() for c in criteria):
            raise ValueError("give at least one of first_name, last_name, organization_name, taxonomy, city, postal_code")
        lim = max(1, min(int(limit), MAX_LIMIT))
        sk = max(0, min(int(skip), MAX_SKIP))
        params = {
            "first_name": _wild(first_name, "first_name"), "last_name": _wild(last_name, "last_name"),
            "organization_name": _wild(organization_name, "organization_name"),
            "taxonomy_description": _wild(taxonomy, "taxonomy"), "city": _wild(city, "city"), "state": st,
            "postal_code": _wild(postal_code.replace("-", "") if postal_code else None, "postal_code"),
            "enumeration_type": etype, "limit": lim, "skip": sk or None,
        }
        if first_name or last_name:
            params["use_first_name_alias"] = "True" if first_name else None
        tax = params["taxonomy_description"]
        if tax and re.fullmatch(r"[0-9]{3}[0-9A-Z]{6}X", tax.upper()):
            raise ValueError("taxonomy takes a specialty description, not a code; use specialty_codes to map "
                             f"{tax!r} to its description")
        full_desc = None
        if tax and "," in tax:
            # NPPES matches the classification or the specialization part separately, never the joined
            # "Classification, Specialization" string; query the specialization and filter on the full text.
            full_desc = tax.rstrip("*").lower()
            tax = tax.rsplit(",", 1)[1].strip() or tax.split(",", 1)[0].strip()
            params["taxonomy_description"] = tax
        widened = False
        try:
            data = await _get(params)
        except RegistryError as e:
            if not (tax and "No taxonomy codes found" in str(e)):
                raise
            data = {}
        res = data.get("results") or []
        if not res and tax and not tax.endswith("*"):
            # NPPES matches taxonomy descriptions by word prefix only with a wildcard ("cardiology*" also
            # matches "Interventional Cardiology"); retry once with one.
            params["taxonomy_description"] = tax + "*"
            widened = True
            try:
                res = (await _get(params)).get("results") or []
            except RegistryError as e:
                if "No taxonomy codes found" not in str(e):
                    raise
        raw_n = len(res)
        if full_desc:
            res = [r for r in res if any((t.get("desc") or "").lower().startswith(full_desc)
                                         for t in r.get("taxonomies") or [])]
        return {"query": {k: v for k, v in params.items() if v not in (None, "")}, "taxonomyWildcardAdded": widened,
                "taxonomyFullDescriptionFilter": full_desc,
                "returned": len(res), "mayHaveMore": raw_n == lim and sk + lim <= MAX_SKIP,
                "nextSkip": sk + lim if raw_n == lim and sk + lim <= MAX_SKIP else None,
                "providers": [_compact(r) for r in res], "source": SOURCE}
    return await _run(go())


@mcp.tool()
async def validate_npi(npi: str) -> dict:
    """Validate an NPI: format, check digit and registry status.

    npi: 10 digits, e.g. "1003000126" (spaces/dashes are ignored).
    Checks: (1) exactly 10 digits starting with 1 or 2; (2) check digit, Luhn over the "80840" prefix + NPI
    (as CMS specifies), with the expected digit if wrong; (3) registry existence and status in NPPES
    (an NPI with a valid check digit that is not in the registry was never issued or has been deactivated).
    Returns {valid, formatValid, checkDigitValid, expectedCheckDigit, inRegistry, status, entityType, name,
    primaryTaxonomy, lastUpdated, verdict}. verdict is one of: active, not_in_registry, invalid_check_digit,
    invalid_format.
    """
    async def go():
        s = re.sub(r"[\s\-]", "", str(npi or ""))
        fmt = bool(re.fullmatch(r"[12]\d{9}", s))
        out = {"npi": s, "formatValid": fmt, "checkDigitValid": None, "expectedCheckDigit": None,
               "inRegistry": None, "status": None}
        if not re.fullmatch(r"\d{10}", s):
            return out | {"valid": False, "verdict": "invalid_format",
                          "message": "an NPI is exactly 10 digits", "source": SOURCE}
        ok = luhn_ok(s)
        out["checkDigitValid"] = ok
        out["expectedCheckDigit"] = expected_check_digit(s)
        if not fmt:
            return out | {"valid": False, "verdict": "invalid_format",
                          "message": "NPIs start with 1 or 2", "source": SOURCE}
        if not ok:
            return out | {"valid": False, "verdict": "invalid_check_digit",
                          "message": f"check digit should be {out['expectedCheckDigit']}", "source": SOURCE}
        data = await _get({"number": s})
        res = data.get("results") or []
        if not res:
            return out | {"valid": False, "inRegistry": False, "verdict": "not_in_registry",
                          "message": "check digit is valid but NPPES has no active record (never issued or deactivated)",
                          "source": SOURCE}
        c = _compact(res[0])
        return out | {"valid": c["status"] == "active", "inRegistry": True, "status": c["status"],
                      "entityType": c["entityType"], "name": c["name"], "primaryTaxonomy": c["primaryTaxonomy"],
                      "enumerationDate": c["enumerationDate"], "lastUpdated": c["lastUpdated"],
                      "verdict": "active" if c["status"] == "active" else f"status_{c['status']}", "source": SOURCE}
    return await _run(go())


@mcp.tool()
async def specialty_codes(query: str, state: str | None = None, limit: int = 25) -> dict:
    """Find healthcare provider taxonomy (specialty) codes and descriptions by keyword, as used in the NPPES registry.

    query: specialty words, e.g. "cardio", "nurse practitioner", "pediatric dent", "physical therap".
    state: optional 2-letter state to sample providers from (default: nationwide).
    limit: max codes returned (1-100, default 25).
    How it works: the registry has no taxonomy-list endpoint, so this samples up to 400 providers whose taxonomy
    matches the query (taxonomy_description search, individuals and organizations) and returns the distinct
    taxonomy codes whose description contains every query word, with how many sampled providers carry each.
    Pass `description` (or the shorter `searchTerm`) as the `taxonomy` filter of search_providers.
    Codes come only from registry records; this is not the full NUCC code set.
    """
    async def go():
        words = [w for w in re.split(r"\s+", (query or "").strip().lower()) if w]
        if not words or len("".join(words)) < 3:
            raise ValueError("query must have at least 3 characters")
        st = state.strip().upper() if state else None
        if st and st not in STATES:
            raise ValueError(f"state must be a 2-letter US state/territory code, got {state!r}")
        lim = max(1, min(int(limit), 100))
        q = " ".join(words).rstrip("*")
        term = q + "*" if len(q) >= 2 else q

        async def sample(etype):
            try:
                return (await _get({"taxonomy_description": term, "enumeration_type": etype, "state": st,
                                    "limit": 200})).get("results") or []
            except RegistryError as e:
                if "No taxonomy codes found" in str(e):
                    return []
                raise
        a, b = await asyncio.gather(sample("NPI-1"), sample("NPI-2"))
        counts: Counter = Counter()
        primary: Counter = Counter()
        for r in a + b:
            seen = set()
            for t in r.get("taxonomies") or []:
                code, desc = t.get("code"), t.get("desc")
                if not code or not desc or (code, desc) in seen:
                    continue
                d = desc.lower()
                if all(w.rstrip("*") in d for w in words):
                    seen.add((code, desc))
                    counts[(code, desc)] += 1
                    if t.get("primary"):
                        primary[(code, desc)] += 1
        codes = [{"code": c, "description": d, "searchTerm": d.rsplit(", ", 1)[-1],
                  "providersInSample": n, "asPrimary": primary[(c, d)]}
                 for (c, d), n in counts.most_common(lim)]
        return {"query": q, "state": st, "sampledProviders": len(a) + len(b), "returned": len(codes),
                "codes": codes, "source": SOURCE,
                "note": "Sample-based: rare specialties may be missing; repeat with a narrower query or a state."}
    return await _run(go())


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
