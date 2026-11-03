from scrapers.serial_check import check

r = check("Neumann U87 vintage", "Serial number 12345, works great")
assert r and "12345" in r and "vintage" in r, r
r = check("Neumann U87", "made in 2005, serial: 23456")
assert r and "2005" in r and "era" in r, r
assert check("Neumann U87 Ai", "serial 12345") is None
assert check("Neumann U87", "serial 234567") is None
assert check("Neumann U87", "no serial given") is None
assert check("Shure SM58", "serial 12345") is None
assert check("Neumann U87", "sn 12") is None
assert check(None, None) is None
assert check(123, object()) is None
print("ok")
