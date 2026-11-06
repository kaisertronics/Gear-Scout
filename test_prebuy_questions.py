from scrapers.prebuy_questions import prebuy_questions as pq

# tube mic, shipped, no serial -> tube + accessories + tested + serial, capped 4
q = pq("Vintage tube condenser mic", "", None, has_shipping=True)
assert 2 <= len(q) <= 4 and "tubes" in q[0], q
assert any("serial" in x for x in q), q

# fully described local listing -> nothing
assert pq("Shure SM7B with shockmount and case, tested working", "", None, is_local=True) == [], "clean"

# local mic without notes: accessories + tested, no serial question
q = pq("Neumann TLM 103 mic", "", None, has_shipping=False, is_local=True)
assert len(q) == 2 and not any("serial" in x for x in q), q

# serial mentioned -> no serial question
q = pq("AKG C414 mic serial 12345", "", None, has_shipping=True)
assert not any("serial number" in x for x in q), q

# bad input never raises
assert pq(None) == [] or isinstance(pq(None), list)
print("ok")
