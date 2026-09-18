import csv, io, json, os, shutil, sys
import notion_tracker_backup as b

# --- fixture pages mirroring the REAL tracker schema types ---
def page(i, archived=False):
    return {"id": f"p{i}", "url": f"https://notion.so/p{i}",
            "created_time": f"2026-09-{10+i:02d}T00:00:00.000Z",
            "last_edited_time": "2026-09-17T00:00:00.000Z", "archived": archived,
            "properties": {
              "Company + Role": {"type":"title","title":[{"plain_text":f"Dscout — SE, India #{i}"}]},
              "Company": {"type":"rich_text","rich_text":[{"plain_text":'Ac"me, Inc\nIndia'}]},
              "Application ID": {"type":"unique_id","unique_id":{"prefix":None,"number":100+i}},
              "Match Score": {"type":"number","number":88.8},
              "Status": {"type":"select","select":{"name":"RESUME_PREPARED"}},
              "Decision": {"type":"select","select":None},
              "Date Found": {"type":"date","date":{"start":"2026-09-18"}},
              "Job URL": {"type":"url","url":"https://boards.greenhouse.io/x"},
              "Recruiter Email": {"type":"email","email":None},
              "Resume Attachment": {"type":"files","files":[{"name":f"R_{i}.docx","file":{"url":"https://s3/x"}}]},
              "Created": {"type":"created_time","created_time":"2026-09-17T10:00:00.000Z"},
            }}

def fake(batches):
    state={"n":0}
    def q(cursor):
        d=batches[state["n"]]; state["n"]+=1; return d
    return q

tmp="/home/claude/wk/_out"
shutil.rmtree(tmp, ignore_errors=True)

# TEST 1: happy path, 2 API pages
rc=b.run(fake([{"results":[page(1),page(2)],"has_more":True,"next_cursor":"c1"},
               {"results":[page(3)],"has_more":False}]), out_dir=tmp)
assert rc==0, rc
files=sorted(os.listdir(tmp)); print("files:", files)
assert any(f.endswith(".json") for f in files) and "latest.csv" in files
rows=list(csv.DictReader(open([os.path.join(tmp,f) for f in files if f.startswith("tracker_") and f.endswith(".csv")][0], newline="")))
assert len(rows)==3
assert rows[0]["Company"]=='Ac"me, Inc\nIndia', repr(rows[0]["Company"])   # quotes+comma+newline survive
assert rows[0]["Application ID"]=="101"
assert rows[0]["Resume Attachment"]=="R_1.docx"
assert rows[0]["Decision"]==""
hdr=open(os.path.join(tmp,"latest.csv")).readline().strip().split(",")
assert hdr[0]=="Application ID" and hdr[1]=="Company", hdr[:3]
raw=json.load(open([os.path.join(tmp,f) for f in files if f.endswith(".json")][0]))
assert len(raw)==3 and raw[0]["properties"]["Company"]["type"]=="rich_text"   # lossless
print("TEST 1 pass: 3 rows, CSV round-trips, raw JSON lossless, column order applied")

# TEST 2: zero rows must refuse to overwrite
rc=b.run(fake([{"results":[],"has_more":False}]), out_dir=tmp+"_empty")
assert rc==2, rc
assert not os.path.exists(tmp+"_empty"), "must not create a dir for an empty backup"
print("TEST 2 pass: empty result refused (rc=2), nothing written")

# TEST 3: missing token
os.environ.pop("NOTION_TOKEN", None)
assert b.main()==1
print("TEST 3 pass: missing NOTION_TOKEN -> rc=1")

# TEST 4: has_more loop guard
b.MAX_PAGES=5
loop=lambda c: {"results":[page(9)],"has_more":True,"next_cursor":"z"}
assert len(b.fetch_all(loop))==5
print("TEST 4 pass: pagination loop capped at MAX_PAGES")

# TEST 5: archived rows are preserved, not dropped
b.MAX_PAGES=100
rc=b.run(fake([{"results":[page(1),page(2,archived=True)],"has_more":False}]), out_dir=tmp+"_arch")
rows=list(csv.DictReader(open(os.path.join(tmp+"_arch","latest.csv"), newline="")))
assert len(rows)==2 and rows[1]["_archived"]=="TRUE"
print("TEST 5 pass: archived row kept and flagged")
print("\nALL BACKUP TESTS PASS")
