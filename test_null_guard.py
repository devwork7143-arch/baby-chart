"""Test that null-null events are properly guarded."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from parser import ingest_feeding
import json

# Create a test feedings dict
d = {"events": [], "unparsed": []}

# Test 1: Message that produces only null-null events
print("Test 1: Message producing only null-null events")
msg_dt = datetime.now()
res = ingest_feeding(d, "L foo bar", msg_dt, "parent_a")
print(f"  parsed={res['parsed']}, added={res['added']}, raw_events={len(res['raw_events'])}")
print(f"  Events stored: {len(d['events'])}")
assert res['parsed'] == True, "Should parse the message"
assert res['added'] == 0, "Should have added 0 events (all finalized away)"
assert len(d['events']) == 0, "No events should be stored"
print("  ✓ Test 1 passed: parsed=True, added=0")

# Test 2: Normal message that produces a good event
print("\nTest 2: Normal feeding message")
d = {"events": [], "unparsed": []}
res = ingest_feeding(d, "L 10:00 - 10:15", msg_dt, "parent_a")
print(f"  parsed={res['parsed']}, added={res['added']}, raw_events={len(res['raw_events'])}")
print(f"  Events stored: {len(d['events'])}")
assert res['parsed'] == True, "Should parse"
assert res['added'] == 1, "Should have added 1 event"
assert len(d['events']) == 1, "One event should be stored"
assert d['events'][0]['side'] == 'L', "Event should have side L"
print("  ✓ Test 2 passed: parsed=True, added=1")

# Test 3: Sideless complete event is rejected (not stored as "?" session)
print("\nTest 3: Sideless complete event rejected")
d = {"events": [], "unparsed": []}
res = ingest_feeding(d, "pump 10:00 - 10:15", msg_dt, "parent_a")
print(f"  parsed={res['parsed']}, added={res['added']}, raw_events={len(res['raw_events'])}")
print(f"  Events stored: {len(d['events'])}")
assert res['parsed'] == False, "Sideless start should be rejected"
assert res['added'] == 0, "No event should be stored"
assert len(d['events']) == 0, "No events in state"
print("  ✓ Test 3 passed: sideless complete event rejected (parsed=False, added=0)")

# Test 4: Side+time toggle — R <time> closes an open R session
print("\nTest 4: Side+time toggle closes open same-side session")
d = {"events": [], "unparsed": []}
t_open = datetime(2026, 6, 17, 17, 39)
ingest_feeding(d, "R 539", t_open, "parent_a")
assert len(d['events']) == 1 and d['events'][0]['end'] is None, "R 539 should open a session"
t_close = datetime(2026, 6, 17, 18, 0)
res = ingest_feeding(d, "R 555", t_close, "parent_a")
print(f"  parsed={res['parsed']}, added={res['added']}, closed_session set={res['closed_session'] is not None}")
print(f"  Events stored: {len(d['events'])}")
assert res['closed_session'] is not None, "Should have detected an open session to close"
assert len(d['events']) == 1, "Should still be exactly one event (not two)"
assert d['events'][0]['end'] == '2026-06-17T17:55', f"End should be 17:55, got {d['events'][0]['end']}"
assert d['events'][0]['side'] == 'R', "Side should be R"
print("  ✓ Test 4 passed: R 555 closed open R 539, single event stored")

# Test 5: Side+time — no open session → starts a new one (no spurious close)
print("\nTest 5: Side+time with no open session starts normally")
d = {"events": [], "unparsed": []}
t = datetime(2026, 6, 17, 17, 39)
res = ingest_feeding(d, "R 539", t, "parent_a")
assert res['closed_session'] is None, "No open session, should not close anything"
assert len(d['events']) == 1 and d['events'][0]['end'] is None, "Should open a new session"
print("  ✓ Test 5 passed: R 539 with no prior open session starts normally")

# Test 6: Side+time — open session for opposite side is NOT closed
print("\nTest 6: Side+time does not close an open session of the wrong side")
d = {"events": [], "unparsed": []}
t_open = datetime(2026, 6, 17, 17, 39)
ingest_feeding(d, "L 539", t_open, "parent_a")
assert len(d['events']) == 1 and d['events'][0]['side'] == 'L', "L 539 should open an L session"
t_close = datetime(2026, 6, 17, 18, 0)
res = ingest_feeding(d, "R 555", t_close, "parent_a")
print(f"  Events stored: {len(d['events'])}")
assert res['closed_session'] is None, "R 555 should not close the open L session"
assert len(d['events']) == 2, "Should now have two events (open L + open R)"
print("  ✓ Test 6 passed: R 555 did not close open L session")

# Test 7: Complete range is never treated as a toggle
print("\nTest 7: Complete range (R 555 - 610) is not treated as a toggle")
d = {"events": [], "unparsed": []}
t_open = datetime(2026, 6, 17, 17, 39)
ingest_feeding(d, "R 539", t_open, "parent_a")
t_msg = datetime(2026, 6, 17, 18, 0)
res = ingest_feeding(d, "R 555 - 610", t_msg, "parent_a")
print(f"  Events stored: {len(d['events'])}, closed_session={res['closed_session'] is not None}")
assert res['closed_session'] is None, "Complete range should not trigger toggle"
assert len(d['events']) == 2, "Should have two events: open R 539 and complete R 555-610"
print("  ✓ Test 7 passed: complete range not treated as toggle")

print("\n✓ All guard tests passed!")
