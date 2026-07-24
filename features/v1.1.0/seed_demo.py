"""Seed a throwaway database with the exact data behind screens/*.png.

Reproduce every screenshot in this folder locally:

    uv run python features/v1.1.0/seed_demo.py /tmp/demo.db
    SYS_BUDDY_PORT=8799 SYS_BUDDY_DB=/tmp/demo.db uv run sys-buddy local
    open "http://127.0.0.1:8799/ui?v=sbv_hosttoken"

Three tasks are created on purpose:
  signin   - HAS TODOS: six deliverables, one pending / one dropped / one verified.
  checkout - NO todos: proves the pre-todo dashboard is unchanged.
  dbg      - debug mode: also unchanged.

Demo data only. Never point this at a real database - it writes directly to the
schema and skips the broker's validation entirely.
"""
import json, time, sys
from pathlib import Path
from sys_buddy import db
from sys_buddy.config import Config, set_config
from sys_buddy.identity import sha256_hex

dbfile = Path(sys.argv[1])
set_config(Config(mode="local", db_path=dbfile))
db.init_db(dbfile)
c = db.connect(dbfile)
now = time.time()

def task(tid, title, state, roles, mode="contract"):
    c.execute("INSERT INTO tasks (id, title, state, roles_json, mode, created_at) VALUES (?,?,?,?,?,?)",
              (tid, title, state, json.dumps(list(roles)), mode, now))

def agent(tid, role, name, ready=1, status="passed", listening=False):
    cur = c.execute("INSERT INTO agents (task_id, name, role, token_hash, created_at, ready, readiness_status, listening_until, listening_since) VALUES (?,?,?,?,?,?,?,?,?)",
              (tid, name, role, sha256_hex("k_"+tid+role), now, ready, status,
               (now+300) if listening else None, (now-2520) if listening else None))
    return cur.lastrowid

def viewer(label, token, tid=None):
    c.execute("INSERT INTO viewers (task_id, label, token_hash, created_at) VALUES (?,?,?,?)",
              (tid, label, sha256_hex(token), now))

def todo(tid, title, scope, parties, state, version=1, strikes=0, verified=False, dropped=False, dropped_by=None, drop_reason=None, ago=0):
    cur = c.execute("INSERT INTO todos (task_id, title, scope, parties_json, version, state, strikes, proposed_role, created_at, verified_at, dropped_at, dropped_by, drop_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (tid, title, scope, json.dumps(list(parties)), version, state, strikes, parties[0], now-ago,
               (now-ago+60) if verified else None, (now-ago+30) if dropped else None, dropped_by, drop_reason))
    return cur.lastrowid

def decide(todo_id, version, role, agent_id, decision="accepted", reason=None):
    c.execute("INSERT INTO todo_decisions (todo_id, version, role, agent_id, decision, reason, created_at) VALUES (?,?,?,?,?,?,?)",
              (todo_id, version, role, agent_id, decision, reason, now))

def drop_consent(todo_id, role, agent_id, reason=None):
    c.execute("INSERT INTO todo_drop_consents (todo_id, role, agent_id, reason, created_at) VALUES (?,?,?,?,?)",
              (todo_id, role, agent_id, reason, now))

def contract(tid, version, spec, status="draft", todo_id=None, signers=None):
    cur = c.execute("INSERT INTO contracts (task_id, version, spec_json, status, todo_id, locked_at, created_at) VALUES (?,?,?,?,?,?,?)",
              (tid, version, json.dumps(spec), status, todo_id, now if status=="locked" else None, now))
    cid = cur.lastrowid
    for ag in (signers or []):
        c.execute("INSERT INTO contract_signatures (contract_id, agent_id, signed_at) VALUES (?,?,?)", (cid, ag, now))
    return cid

def msg(tid, agent_id, mtype, body, ago, to_role=None, todo_id=None):
    c.execute("INSERT INTO messages (task_id, from_agent_id, type, body_json, state_at_send, created_at, to_role, todo_id) VALUES (?,?,?,?,?,?,?,?)",
              (tid, agent_id, mtype, json.dumps(body), "open", now-ago, to_role, todo_id))

SPEC = {"staging_url":"https://api-staging.example.dev","endpoints":[
    {"method":"POST","path":"/auth/login","summary":"Exchange credentials for a session token.",
     "request":[{"n":"email","t":"string","req":True,"note":"user email"},{"n":"password","t":"string","req":True}],
     "response":[{"n":"token","t":"string"},{"n":"expires_in","t":"int","note":"seconds"}],
     "resStatus":"200 OK","errors":[{"code":"401","name":"invalid_credentials","note":"bad email/pw"}]}]}

# ---- Task 1: signin — HAS TODOS (6, various states) ----
task("signin", "Sign-in & account", "backend_live", ("backend","frontend","mobile"))
b = agent("signin","backend","alex-be", listening=True)
f = agent("signin","frontend","robin-fe")
m = agent("signin","mobile","sam-mob")

t1 = todo("signin","Password reset flow","Email a reset link, /auth/reset endpoints",["backend","frontend"],"open",ago=600)
decide(t1,1,"backend",b)
t2 = todo("signin","Session refresh","Rotate tokens on the /auth/refresh path",["backend","frontend"],"open",ago=1200)
decide(t2,1,"backend",b); decide(t2,1,"frontend",f)
t3 = todo("signin","Login endpoint","POST /auth/login, JWT session",["backend","frontend"],"backend_live",ago=2400)
decide(t3,1,"backend",b); decide(t3,1,"frontend",f)
contract("signin",1,SPEC,status="locked",todo_id=t3,signers=[b,f])
t4 = todo("signin","Logout everywhere","Invalidate all sessions for a user",["backend","mobile"],"testing",strikes=1,ago=3000)
decide(t4,1,"backend",b); decide(t4,1,"mobile",m)
contract("signin",2,SPEC,status="locked",todo_id=t4,signers=[b,m])
t5 = todo("signin","Rate limiting","Throttle failed logins",["backend"],"verified",verified=True,ago=3600)
decide(t5,1,"backend",b)
contract("signin",3,SPEC,status="locked",todo_id=t5,signers=[b])
t6 = todo("signin","SMS 2FA","Text-message second factor",["backend","mobile"],"open",dropped=True,dropped_by="frontend",drop_reason="descoped for v1",ago=4200)
decide(t6,1,"backend",b)
drop_consent(t6,"backend",b,"agreed"); drop_consent(t6,"mobile",m,"agreed")

def event(tid, kind, detail, ago):
    c.execute("INSERT INTO events (task_id, kind, detail_json, created_at) VALUES (?,?,?,?)",
              (tid, kind, json.dumps(detail), now-ago))

msg("signin",b,"status_update","Kicking off the login work.",700)
# PATH 1 (new, authoritative): todo_id column set, body never names the deliverable
msg("signin",b,"status_update","Backend side of the login work is deployed to staging.",690,todo_id=3)
# PATH 3 (guard): a stale/foreign reference must get NO chip
msg("signin",f,"question","Does todo #999 block us?",685)
# PATH 2 (legacy fallback): column NULL, "todo #N" scraped from prose
msg("signin",b,"todo_proposal","Proposed todo #3: Login endpoint. Scope: POST /auth/login, JWT session.",680)
msg("signin",f,"todo_accept","Accepted todo #3 v1 (Login endpoint) — every party has agreed on WHAT.",660)
msg("signin",b,"contract_proposal","Proposed a contract on the login deliverable.",650)
msg("signin",f,"question","Should the token be a JWT or opaque?",600,to_role="backend")
msg("signin",b,"answer","JWT with a 1h expiry.",550,to_role="frontend")
msg("signin",b,"todo_proposal","Proposed todo #1: Password reset flow. Scope: Email a reset link. Waiting on frontend.",480)
msg("signin",m,"todo_accept","Accepted todo #4 v1 (Logout everywhere).",400)
msg("signin",b,"test_result",{"body":"Logout test failed against staging.","strike":1},300)
msg("signin",b,"status_update","General update, not tied to any deliverable.",120)

event("signin","task",{"title":"Sign-in & account"},900)
event("signin","todo",{"action":"todo_proposed","todo_id":3,"title":"Login endpoint","by":"backend"},680)
event("signin","todo",{"action":"todo_accepted","todo_id":3,"title":"Login endpoint","by":"frontend"},660)
event("signin","lock",{"version":1},640)
event("signin","todo",{"action":"todo_dropped","todo_id":6,"title":"SMS 2FA","by":"frontend"},500)
event("signin","todo",{"action":"todo_proposed","todo_id":1,"title":"Password reset flow","by":"backend"},480)

# ---- Task 2: checkout — NO TODOS (regression) ----
task("checkout", "Checkout & payments", "testing", ("backend","frontend"))
cb = agent("checkout","backend","dana-be")
cf = agent("checkout","frontend","lee-fe", listening=True)
contract("checkout",1,SPEC,status="locked",todo_id=None,signers=[cb,cf])
msg("checkout",cb,"status_update","Backend is live on staging.",500)
msg("checkout",cf,"test_result",{"body":"Checkout happy-path passes."},200)

# ---- Task 3: debug — DEBUG MODE (regression) ----
task("dbg", "Fix flaky CI", "open", ("backend","frontend"), mode="debug")
db1 = agent("dbg","backend","cid-be")
agent("dbg","frontend","cid-fe")
msg("dbg", db1, "status_update", "Looking at the flaky test.", 100)

# A long listening window so the "listening - 42m" presence pills are visible
# whenever this seed is run. The broker itself sets this from wait_for_message;
# here we stamp it directly because no agent is really parked.
c.execute("UPDATE agents SET listening_until = ?, listening_since = ? WHERE task_id='signin' AND role='backend'", (now+7200, now-2520))
c.execute("UPDATE agents SET listening_until = ?, listening_since = ? WHERE task_id='signin' AND role='mobile'",  (now+7200, now-180))

viewer("host", "sbv_hosttoken", None)
c.commit()
print("seeded", dbfile)
