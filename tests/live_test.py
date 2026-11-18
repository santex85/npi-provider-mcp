"""Live smoke test: calls every tool against the NPPES NPI Registry API. Run: .venv/bin/python tests/live_test.py"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import server  # noqa: E402


def show(name, r, ok):
    s = json.dumps(r, ensure_ascii=False)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(s)} bytes | {s[:600]}\n")
    return bool(ok)


async def timed(name, coro):
    t = time.time()
    r = await coro
    return f"{name} ({time.time() - t:.1f}s)", r


async def main():
    res = []
    tools = await server.mcp.list_tools()
    res.append(show("list_tools", [t.name for t in tools], len(tools) == 4))
    res.append(show("luhn offline", [server.luhn_ok("1003000126"), server.luhn_ok("1234567893"), server.luhn_ok("1234567890")],
                    server.luhn_ok("1003000126") and server.luhn_ok("1234567893") and not server.luhn_ok("1234567890")))

    n, r = await timed("lookup_npi individual", server.lookup_npi("1003000126"))
    res.append(show(n, r, r.get("found") and r["provider"]["entityType"] == "individual" and r["provider"]["taxonomies"]))
    n, r = await timed("lookup_npi organization", server.lookup_npi("1881018208"))
    res.append(show(n, r, r.get("found") and r["provider"]["entityType"] == "organization"))
    n, r = await timed("lookup_npi not found", server.lookup_npi("1234567893"))
    res.append(show(n, r, r.get("found") is False))

    n, r = await timed("search_providers name+state+taxonomy", server.search_providers(last_name="smith", state="CA", taxonomy="Cardiovascular Disease", limit=5))
    res.append(show(n, r, r.get("returned", 0) > 0))
    n, r = await timed("search_providers org", server.search_providers(organization_name="mayo clinic", state="MN", entity_type="organization", limit=3))
    res.append(show(n, r, r.get("returned") == 3 and r["nextSkip"] == 3))
    n, r = await timed("search_providers full taxonomy text", server.search_providers(taxonomy="Dentist, Pediatric Dentistry", city="austin", state="TX", limit=5))
    res.append(show(n, r, r.get("returned", 0) > 0 and r["taxonomyFullDescriptionFilter"] == "dentist, pediatric dentistry"))
    n, r = await timed("search_providers zip paging", server.search_providers(postal_code="94143", limit=200, skip=200))
    res.append(show(n, {k: r.get(k) for k in ("query", "returned", "nextSkip")}, r.get("returned", 0) > 0))
    n, r = await timed("search_providers no criteria", server.search_providers(state="CA"))
    res.append(show(n, r, "error" in r))

    for npi, verdict in [("1003000126", "active"), ("1234567893", "not_in_registry"), ("1234567890", "invalid_check_digit"),
                         ("12345", "invalid_format"), ("3234567890", "invalid_format")]:
        n, r = await timed(f"validate_npi {npi}", server.validate_npi(npi))
        res.append(show(n, {k: v for k, v in r.items() if k != "source"}, r.get("verdict") == verdict))

    n, r = await timed("specialty_codes cardio", server.specialty_codes("cardio"))
    res.append(show(n, r, any(c["code"] == "207RC0000X" for c in r.get("codes", []))))
    n, r = await timed("specialty_codes nurse practitioner TX", server.specialty_codes("nurse practitioner", state="TX", limit=5))
    res.append(show(n, r, any(c["code"] == "363LF0000X" for c in r.get("codes", []))))
    n, r = await timed("specialty_codes no match", server.specialty_codes("zzqxv"))
    res.append(show(n, r, r.get("returned") == 0))
    print(f"{sum(res)}/{len(res)} passed")


asyncio.run(main())
