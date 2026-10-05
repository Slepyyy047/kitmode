# Application API reference

Python 3.12; standard-library sqlite3, zoneinfo, urllib, unittest; tzdata on Windows.
Package `kitmode`. Telegram conversations, service rules, SQLite, reminders,
localization and HTTPS integration are separate modules. Source and regression
tests are the authoritative behavior; all examples operate on fictional data.

DB(path): .conn sqlite3.Connection with sqlite3.Row; .transaction() context manager
(supports nested savepoints); .one(sql, params=()), .all(), .execute(); .close().
Tables include users(id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT),
habits(id TEXT PRIMARY KEY,user_id INTEGER,title TEXT,sensitive INTEGER,archived INTEGER,
created_at TEXT), versions(habit_id,effective TEXT,spec TEXT,PRIMARY KEY(habit_id,effective)),
records(habit_id,day,status,value,note,verification,evidence,snapshot,updated_at,
PRIMARY KEY(habit_id,day)), actions(user_id,key,kind,habit_id,day,before,after,created_at,
PRIMARY KEY(user_id,key)), sessions(user_id PRIMARY KEY,data TEXT),
buttons(token PRIMARY KEY,user_id,payload TEXT,expires TEXT,used INTEGER),
jobs(id TEXT PRIMARY KEY,user_id,due TEXT,day TEXT,kind TEXT,payload TEXT,state TEXT,
attempts INTEGER), rewards(habit_id,opportunity,user_id,xp,PRIMARY KEY(habit_id,opportunity)).
Extras owns migration table creation via DB.executescript in its constructor (IF NOT EXISTS).
SQLite timestamps are UTC ISO aware. Dates are YYYY-MM-DD. JSON UTF-8.

Service(db, clock=lambda: datetime.now(timezone.utc)), .db, .now(), .extras=Extras(self).
Core DomainError carries .key translatable error key (error_input/error_access/error_old/
error_timezone/error_future/error_stale/error_sensitive/error_limit).
All user-dependent access must call .habit(user_id,hid).

Public Service API:
- user(uid,name=''): returns dict defaults lang=uk,name,tz=None,sleep=23:00,
  boundary=04:00,quiet_start=23:00,quiet_end=08:00,tone=friendly,gamification=True,
  reminders=True,ai_consent=False,ai_sensitive=False,last_seen ISO,inactivity_sent=False.
- settings(uid, **changes): validates and updates, returns dict. tz must be IANA.
- day(uid): current personal date; timezone unset uses UTC only for display, no reminders.
- create(uid,spec): dict spec includes title,kind(binary/quantity/limit),target float,
  unit,minimum optional float,schedule {type:daily/weekdays/weekly/interval,days:[0..6],
  n:int}, start ISO date,end optional,description,category,icon,routine,sensitive bool,
  private_title='Private habit',verify='none'/'text'/'photo'/'timer'/'friend',reminders:[HH:MM],
  repeat:int minutes default0,independent:bool defaultFalse. returns hid string.
- habit(uid,hid,day=None): dict id,title,sensitive,archived,spec; validates ownership.
- habits(uid,include_archived=False): list as habit() dicts.
- preview_edit(uid,hid,changes,effective=None): validates without writes; returns spec/effective.
- edit(uid,hid,changes,effective=None): defaults tomorrow or next Monday for weekly plans.
- pause(uid,hid,start,end), archive(uid,hid); pause end inclusive or null.
- today(uid): list dict habit + record optional dict; only scheduled current opportunities.
- record(uid,hid,action_key,status='done',amount=None,note='',day=None,
  verification=None,evidence=None): dict, idempotent; quantity amounts add; limit amount
  is absolute period total and required; status done/minimum/fail/skip/none; verification default
  pending if configured, none otherwise. Computes full/minimum from prior threshold.
- correct_record(uid,hid,action_key,status='done',amount=None,note='',day=None):
  absolute replacement preserving the historical snapshot, including zero quantity.
- note(uid,hid,note,day=None): adds an owner-only note to an existing last-seven-day record.
- evidence(uid,hid,method,note='',file_id=None,day=None,seconds=None): separate submission.
- undo(uid,action_key): restores the latest report; later notes/evidence do not block it.
- bulk(uid,action_key): list results excluding upper limits and verification requiring input.
- history(uid,hid,days=7): list date+record+scheduled; timer start/finish in Extras.
- stats(uid,start=None,end=None,hid=None): dict completed,full,minimum,fail,skip,missing,
  opportunities,rate,current_streak,best_streak,quantity,calendar list,previous_rate,xp,level,
  badges, independence list hids, quantity_by_unit. Historical units follow each snapshot.
  Weekly quota counts ONE completed week opportunity.
- session(uid,data=None): persists dict; None reads. empty {} clears.
- button(uid,payload,ttl=86400): opaque short token; payload arbitrary JSON dict.
- consume_button(uid,token): returns payload or DomainError; one-use on mutation buttons
  payload.get('once',True), reusable navigation set once=False. Expiry enforced.
- export payload/dialog operations are in service.extras (below).

Extras API:
invite(uid) -> token; accept_invite(uid,token) -> friend id; friends(uid) -> list;
revoke(uid,friend_id); visibility(uid,hid,friend_id,visible); shared(viewer,owner)->list;
consent_verifier(uid,friend_id,enabled=True): uid volunteers to verify friend_id;
request_verification(uid,hid,day,friend_id)->request id; decide_verification(friend_id,rid,
approve)->None; start_timer(uid,hid)->dict; finish_timer(uid,hid)->integer seconds;
urge(uid,hid,intensity,trigger='',episode=False)->None; journal(uid,hid)->list;
export_json(uid)->str; export_csv(uid)->str; preview_import(uid,text)->dict;
import_data(uid,text)->dict; delete_user(uid)->None; feedback(uid,text)->id;
admin(uid,admin_ids)->dict aggregate; block(admin,uid,admin_ids,blocked=True);
challenge(uid,title,target,start,end)->id; challenges(uid)->list;
share_card(uid)->str text preview excludes sensitive regardless of visibility;
ai(uid,prompt,config,requester=None)->str, no external call unless enabled AND consent;
AIConfig dataclass enabled=False,key='',model='',daily_limit=3,timeout=10.
No AI context includes diary or evidence; sensitive title only with separate consent.

Telegram Bot(service,transport,admin_ids=(),ai_config=None): .handle(update dict).
Transport.send(chat_id,text,keyboard=None), .edit(chat_id,message_id,text,keyboard=None),
.answer(callback_id,text=''), .document(chat_id,filename,data bytes), .photo(chat_id,path,
caption=''); .file(file_id)->bytes with MAX1MB for imports; .username string optional.
FakeTransport stores events. TelegramAPI implements these and .updates(offset,timeout).
Bot persists update dedup via DB table updates; all mutation button tokens one-use.
ReminderEngine(service,transport): .tick(); creates durable jobs and drains at most 20;
generates callbacks payload through Service.button; previous-day jobs expire, no floods.
CLI: python -m kitmode [run|demo|check], env loads .env without overwriting OS values.
