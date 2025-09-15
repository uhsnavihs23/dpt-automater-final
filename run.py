import os, time, traceback, random, requests, pytz
from datetime import datetime
import gspread
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ====================================================
# CONFIG
# ====================================================
CREDENTIALS_JSON = "credentials.json"   # service account file (store in secrets)
SHEET_KEY = os.environ.get("SHEET_KEY")
DOC_TEMPLATE_ID = "1m1enC465n6fOigHuhlBXeJI6W5cdmB-SBF2OvzMrIi8"
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbz8VO6m4NvL1SwnNqW4-PMrPvCD_ZEy-6sy2p2f9eQX5nlHwB0cg9GE-M5i10iDy5vy/exec"
IST = pytz.timezone("Asia/Kolkata")

# Gemini API keys (rotate)
api_keys = [
    os.environ.get("GEMINI_KEY_1"),
    os.environ.get("GEMINI_KEY_2"),
    os.environ.get("GEMINI_KEY_3"),
    os.environ.get("GEMINI_KEY_4"),
]
key_index = 0
def get_model(model_name="gemini-1.5-flash"):
    global key_index
    genai.configure(api_key=api_keys[key_index])
    model = genai.GenerativeModel(model_name)
    key_index = (key_index + 1) % len(api_keys)
    return model

# ====================================================
# 1) SHEETS SETUP
# ====================================================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/documents"]
creds = Credentials.from_service_account_file(CREDENTIALS_JSON, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_KEY)
today_tab = datetime.now().strftime("%Y-%m-%d")
worksheet = sh.worksheet(today_tab)
print(f"✅ Opened worksheet: {today_tab}")

# ====================================================
# 2) PROCESS RELEVANCE
# ====================================================
def generate_relevance(text):
    model = get_model("gemini-1.5-flash")
    prompt = f"""उत्तर प्रदेश की राजनीतिक/सरकारी गतिविधियों से संबंधित है?
सिर्फ 1 या 0 लौटाएँ।\n\n{text}"""
    try:
        resp = model.generate_content(prompt)
        return "1" if resp.text.strip().startswith("1") else "0"
    except:
        return "0"

def process_relevance():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    raw_col = worksheet.col_values(idx_map["Raw"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]
    updates = []
    for i, raw in enumerate(raw_col):
        if not raw.strip():
            continue
        existing = relevance_col[i] if i < len(relevance_col) else ""
        if existing in ["0","1"]:
            continue
        flag = generate_relevance(raw[:3000])
        updates.append((i+2, idx_map["Relevance"], flag))
    batch_update(updates)

# ====================================================
# 3) CLEANING
# ====================================================
def clean_text(raw_text):
    model = get_model("gemini-1.5-flash")
    prompt = f"""यहाँ एक ट्वीट/समाचार का कच्चा टेक्स्ट है।
कृपया शुरुआत के "भारत समाचार…" और अंत के हैशटैग/mentions हटाएँ।
बाकी जस का तस रहने दें।

{raw_text}"""
    try:
        resp = model.generate_content(prompt)
        return (resp.text or "").strip()
    except:
        return raw_text[:3000]

def process_cleaning():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    raw_col = worksheet.col_values(idx_map["Raw"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]
    cleaned_col = worksheet.col_values(idx_map["Cleaned"])[1:]
    updates = []
    for i, raw in enumerate(raw_col):
        if not raw.strip():
            continue
        if relevance_col[i] != "1":
            continue
        if i < len(cleaned_col) and cleaned_col[i].strip():
            continue
        cleaned = clean_text(raw[:3000])
        updates.append((i+2, idx_map["Cleaned"], cleaned))
    batch_update(updates)

# ====================================================
# 4) REFINEMENT
# ====================================================
def refine_text(cleaned):
    model = get_model("gemini-1.5-flash")
    prompt = f"""इस समाचार को पेशेवर, प्रवाहपूर्ण शैली में परिष्कृत करें।
{cleaned}"""
    try:
        resp = model.generate_content(prompt)
        return resp.text.strip()
    except:
        return cleaned

def process_refinement():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    cleaned_col = worksheet.col_values(idx_map["Cleaned"])[1:]
    refined_col = worksheet.col_values(idx_map["Refined"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]
    updates = []
    for i, cleaned in enumerate(cleaned_col):
        if not cleaned.strip() or relevance_col[i]!="1":
            continue
        if i < len(refined_col) and refined_col[i].strip():
            continue
        refined = refine_text(cleaned[:3000])
        updates.append((i+2, idx_map["Refined"], refined))
    batch_update(updates)

# ====================================================
# 5) BUILD REPORT DOC
# ====================================================
def build_report():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    refined_col = worksheet.col_values(idx_map["Refined"])[1:]
    refined_news = [r.strip() for r in refined_col if r.strip()]
    if not refined_news:
        print("⚠️ No refined news found")
        return
    print(f"✅ Found {len(refined_news)} refined news")

    # Categorize + bold
    model = get_model("gemini-2.0-flash")
    prompt = """समाचारों को दो श्रेणियों में बाँटें:
- महत्वपूर्ण राजनीतिक गतिविधियां
- महत्वपूर्ण गवर्नेंस गतिविधियां
और चुनिंदा शब्दों को **बोल्ड** करें।
आउटपुट:
महत्वपूर्ण राजनीतिक गतिविधियां:
● …
महत्वपूर्ण गवर्नेंस गतिविधियां:
● …"""
    resp = model.generate_content(prompt + "\n\n".join(refined_news))
    categorized_text = resp.text.strip()

    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    today_date_str = datetime.now().strftime("%Y%m%d")
    new_doc_title = f"{today_date_str} Uttar Pradesh Daily Political Tracker"
    new_doc = drive_service.files().copy(fileId=DOC_TEMPLATE_ID, body={"name": new_doc_title}).execute()
    doc_id = new_doc.get("id")
    doc_link = f"https://docs.google.com/document/d/{doc_id}/edit"

    # Replace placeholders
    formatted_date = datetime.now(tz=IST).strftime("%B %dth, %Y")
    docs_service.documents().batchUpdate(documentId=doc_id, body={
        "requests":[
            {"replaceAllText":{"containsText":{"text":"{{DATE}}"},"replaceText":f"[{formatted_date}]"}},
            {"replaceAllText":{"containsText":{"text":"{{NEWS}}"},"replaceText":categorized_text}}
        ]
    }).execute()

    # Bold markers (**…** via Apps Script)
    requests.post(APPS_SCRIPT_URL, data={"docId": doc_id})
    print(f"✅ Report generated: {doc_link}")

    # Save link in Repository tab
    try:
        repo_ws = sh.worksheet("Repository")
    except gspread.exceptions.WorksheetNotFound:
        repo_ws = sh.add_worksheet(title="Repository", rows="1000", cols="2")
        repo_ws.insert_row(["Link","DateCreated"],1)
    ist_now = datetime.now(tz=IST)
    repo_ws.append_row([doc_link, ist_now.strftime("%I:%M %p · %d %b, %Y")])

# ====================================================
# UTILS
# ====================================================
def batch_update(updates):
    if not updates: return
    requests_payload = []
    for row,col,val in updates:
        requests_payload.append({
            "updateCells": {
                "rows": [{"values":[{"userEnteredValue":{"stringValue":str(val)}}]}],
                "fields": "userEnteredValue",
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": row-1, "endRowIndex": row,
                    "startColumnIndex": col-1, "endColumnIndex": col
                }
            }
        })
    worksheet.spreadsheet.batch_update({"requests":requests_payload})
    print(f"✅ Batch updated {len(updates)} cells")

# ====================================================
# MAIN
# ====================================================
if __name__=="__main__":
    process_relevance()
    process_cleaning()
    process_refinement()
    build_report()
