# ====================================================
# run.py – Uttar Pradesh Daily Political Tracker
# ====================================================
import os, time, random, base64, json, pytz, traceback, requests
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ====================================================
# 0) Setup
# ====================================================
IST = pytz.timezone("Asia/Kolkata")

# Decode credentials.json from GitHub Secret
creds_json = base64.b64decode(os.getenv("CREDENTIALS_JSON_B64")).decode("utf-8")
with open("credentials.json", "w") as f:
    f.write(creds_json)

SHEET_KEY = os.getenv("SHEET_KEY")
DOC_TEMPLATE_ID = os.getenv("DOC_TEMPLATE_ID")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
MY_EMAIL = os.getenv("MY_EMAIL")

# GSpread Authentication
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_KEY)

today_tab = datetime.now().strftime("%Y-%m-%d")
worksheet = sh.worksheet(today_tab)
print(f"✅ Opened worksheet: {today_tab}")

# ====================================================
# 6b) Process Relevance (1/0)
# ====================================================
api_keys_6b = [
    os.getenv("GEMINI_KEY_6B_1"),
    os.getenv("GEMINI_KEY_6B_2"),
    os.getenv("GEMINI_KEY_6B_3"),
    os.getenv("GEMINI_KEY_6B_4"),
]
key_index_6b = 0
MAX_RAW_LEN = 3000
BATCH_DELAY_SEC = 2

def get_next_model_6b():
    global key_index_6b
    genai.configure(api_key=api_keys_6b[key_index_6b])
    model = genai.GenerativeModel("gemini-1.5-flash")
    key_index_6b = (key_index_6b + 1) % len(api_keys_6b)
    return model

def safe_generate_content(model, prompt, max_retries=3):
    for attempt in range(max_retries):
        try:
            resp = model.generate_content(prompt)
            return resp.text.strip()
        except Exception as e:
            print(f"⚠️ API call failed (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    return ""

def generate_relevance(text):
    model = get_next_model_6b()
    prompt = f"""
उत्तर प्रदेश की राजनीतिक/सरकारी गतिविधियों (दल, नेता, सरकार, विपक्ष, योजनाएँ,
विधानसभा, लोकसभा, राजनीतिक कोर्ट केस) से संबंधित है?
सिर्फ 1 या 0 लौटाएँ। अन्य कुछ न लिखें।

{text}
"""
    flag = safe_generate_content(model, prompt)
    return "1" if flag.strip().startswith("1") else "0"

def process_relevance():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    raw_col = worksheet.col_values(idx_map["Raw"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]

    updates_relevance, updates_runtime = [], []
    for i, raw_text in enumerate(raw_col):
        row_number = i + 2
        raw_text = raw_text.strip()
        if not raw_text or raw_text in ["⚠️ fetch failed", ""]:
            continue
        existing_flag = relevance_col[i] if i < len(relevance_col) else ""
        if existing_flag in ["0", "1"]:
            continue
        try:
            flag = generate_relevance(raw_text[:MAX_RAW_LEN])
            updates_relevance.append((row_number, idx_map["Relevance"], flag))
            updates_runtime.append((row_number, idx_map["RunTime"],
                                    datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
        except Exception:
            traceback.print_exc()
        time.sleep(BATCH_DELAY_SEC)

    batch_update(updates_relevance)
    batch_update(updates_runtime)

# ====================================================
# 6c) Cleaning step
# ====================================================
api_keys_6c = [
    os.getenv("GEMINI_KEY_6C_1"),
    os.getenv("GEMINI_KEY_6C_2"),
]
key_index_6c = 0

def get_next_model_6c():
    global key_index_6c
    genai.configure(api_key=api_keys_6c[key_index_6c])
    model = genai.GenerativeModel("gemini-1.5-flash")
    key_index_6c = (key_index_6c + 1) % len(api_keys_6c)
    return model

def clean_text(raw_text, max_retries=3):
    model = get_next_model_6c()
    prompt = f"""
यहाँ एक ट्वीट/समाचार का कच्चा टेक्स्ट दिया गया है।
कृपया केवल शुरुआत में आने वाले "भारत समाचार | Bharat Samachar @bstvlive"
और अंत में आने वाले हैशटैग्स, mentions (@something), "Translate post",
तारीख/समय, Views/Likes की जानकारी हटा दें।
बाकी टेक्स्ट को जस का तस रहने दें।
"""
    for attempt in range(max_retries):
        try:
            resp = model.generate_content(prompt + raw_text)
            cleaned = (resp.text or "").strip()
            if cleaned:
                return cleaned
        except Exception as e:
            if "429" in str(e):
                model = get_next_model_6c()
                time.sleep(2 ** attempt)
                continue
            else:
                break
    return raw_text[:3000]

def process_cleaning():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    raw_col = worksheet.col_values(idx_map["Raw"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]
    cleaned_col = worksheet.col_values(idx_map["Cleaned"])[1:]

    updates_cleaned, updates_runtime = [], []
    for i, raw_text in enumerate(raw_col):
        row_number = i + 2
        raw_text = raw_text.strip()
        if not raw_text or raw_text in ["⚠️ fetch failed", ""]:
            continue
        flag = relevance_col[i] if i < len(relevance_col) else ""
        existing_cleaned = cleaned_col[i] if i < len(cleaned_col) else ""
        if flag != "1" or existing_cleaned:
            continue
        cleaned_text = clean_text(raw_text[:3000])
        updates_cleaned.append((row_number, idx_map["Cleaned"], cleaned_text))
        updates_runtime.append((row_number, idx_map["RunTime"],
                                datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
        time.sleep(2)

    batch_update(updates_cleaned)
    batch_update(updates_runtime)

# ====================================================
# 6d) Refine step
# ====================================================
api_keys_6d = [
    os.getenv("GEMINI_KEY_6D_1"),
    os.getenv("GEMINI_KEY_6D_2"),
    os.getenv("GEMINI_KEY_6D_3"),
    os.getenv("GEMINI_KEY_6D_4"),
    os.getenv("GEMINI_KEY_6D_5"),
    os.getenv("GEMINI_KEY_6D_6"),
    os.getenv("GEMINI_KEY_6D_7"),
]
key_index_6d = 0

def switch_api_key_6d():
    global key_index_6d, model_6d
    key_index_6d = (key_index_6d + 1) % len(api_keys_6d)
    genai.configure(api_key=api_keys_6d[key_index_6d])
    model_6d = genai.GenerativeModel("gemini-1.5-flash")

genai.configure(api_key=api_keys_6d[key_index_6d])
model_6d = genai.GenerativeModel("gemini-1.5-flash")

def remove_nukta(text: str) -> str:
    replacements = {"क़":"क","ख़":"ख","ग़":"ग","ज़":"ज","ड़":"ड","ढ़":"ढ","फ़":"फ","ऱ":"र","ऩ":"न"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

def refine_text(cleaned_text):
    prompt = f"""
नीचे दिया गया समाचार पहले से साफ है। इसे पेशेवर, संक्षिप्त और प्रवाहपूर्ण रिपोर्टिंग शैली में परिष्कृत करें।
{cleaned_text}
"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = model_6d.generate_content(prompt)
            refined = remove_nukta(resp.text.strip())
            refined = " ".join(refined.split())
            return refined
        except Exception as e:
            if "429" in str(e):
                switch_api_key_6d()
                continue
            elif "503" in str(e):
                time.sleep(min(30, 2 ** attempt))
                continue
            return cleaned_text
    return cleaned_text

def process_refinement():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    cleaned_col = worksheet.col_values(idx_map["Cleaned"])[1:]
    refined_col = worksheet.col_values(idx_map["Refined"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]

    updates_refined, updates_runtime = [], []
    for i, cleaned_text in enumerate(cleaned_col):
        row_number = i + 2
        cleaned_text = cleaned_text.strip()
        flag = relevance_col[i] if i < len(relevance_col) else ""
        existing_refined = refined_col[i] if i < len(refined_col) else ""
        if not cleaned_text or flag != "1" or existing_refined:
            continue
        refined_text = refine_text(cleaned_text[:3000])
        updates_refined.append((row_number, idx_map["Refined"], refined_text))
        updates_runtime.append((row_number, idx_map["RunTime"],
                                datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
        time.sleep(2)

    batch_update(updates_refined)
    batch_update(updates_runtime)

# ====================================================
# Helper: Batch update
# ====================================================
def batch_update(updates_list):
    if not updates_list:
        return
    requests = []
    for row, col, val in updates_list:
        requests.append({
            "updateCells": {
                "rows": [{"values": [{"userEnteredValue": {"stringValue": str(val)}}]}],
                "fields": "userEnteredValue",
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col - 1,
                    "endColumnIndex": col
                }
            }
        })
    try:
        worksheet.spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"⚠️ Batch update failed: {e}")

# ====================================================
# 7) Generate Google Doc Report
# ====================================================
api_keys_doc = [
    os.getenv("GEMINI_KEY_DOC_1"),
    os.getenv("GEMINI_KEY_DOC_2"),
    os.getenv("GEMINI_KEY_DOC_3"),
]
key_index_doc = 0

def get_next_model_doc():
    global key_index_doc
    genai.configure(api_key=api_keys_doc[key_index_doc])
    model = genai.GenerativeModel("gemini-2.0-flash")
    key_index_doc = (key_index_doc + 1) % len(api_keys_doc)
    return model

def categorize_and_clean(news_list):
    model = get_next_model_doc()
    prompt = "समाचारों को राजनीतिक और गवर्नेंस श्रेणियों में बाँटें, और चयनित शब्दों को **बोल्ड** करें।"
    resp = model.generate_content(prompt + "\n\n".join(news_list))
    return resp.text.strip()

def final_qc(news_list):
    model = get_next_model_doc()
    prompt = "केवल व्याकरण और भाषा सुधारें, तथ्य जस के तस रखें।"
    resp = model.generate_content(prompt + "\n\n".join(news_list))
    return resp.text.strip().splitlines()

def remove_filler_lines(lines):
    bad_phrases = ["यहां आपके द्वारा दिए गए", "यह समाचार बिंदुओं"]
    return [l.strip() for l in lines if l.strip() and not any(bad in l for bad in bad_phrases)]

# ====================================================
# MAIN PIPELINE
# ====================================================
process_relevance()
process_cleaning()
process_refinement()

# ✅ Ensure Sheets has applied all writes before generating Doc
print("⏳ Waiting for Sheets to sync updates...")
time.sleep(5)

# ✅ Re-open worksheet to pull latest Refined values
worksheet = sh.worksheet(today_tab)

generate_report()

def generate_report():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    refined_col = worksheet.col_values(idx_map["Refined"])[1:]
    refined_news = [r.strip() for r in refined_col if r.strip()]
    if not refined_news:
        print("⚠️ No refined news found!")
        return
    categorized_text = categorize_and_clean(refined_news)

    # Split sections
    political_text, gov_text, section = [], [], None
    for line in categorized_text.splitlines():
        if "महत्वपूर्ण राजनीतिक गतिविधियां" in line: section="political"; continue
        elif "महत्वपूर्ण गवर्नेंस गतिविधियां" in line: section="gov"; continue
        elif line.strip():
            (political_text if section=="political" else gov_text).append(line.strip())

    political_text = remove_filler_lines(final_qc(political_text))
    gov_text = remove_filler_lines(final_qc(gov_text))

    political_block = "\n".join(political_text) if political_text else "—"
    gov_block = "\n".join(gov_text) if gov_text else "—"

    # Docs + Drive
    creds = Credentials.from_service_account_file("credentials.json", scopes=[
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ])
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    today_date_str = datetime.now().strftime("%Y%m%d")
    new_doc_title = f"{today_date_str} Uttar Pradesh Daily Political Tracker"
    new_doc = drive_service.files().copy(fileId=DOC_TEMPLATE_ID, body={"name": new_doc_title}).execute()
    doc_id = new_doc.get("id")
    doc_link = f"https://docs.google.com/document/d/{doc_id}/edit"

    # Share with your email
    drive_service.permissions().create(fileId=doc_id, body={'type': 'user', 'role': 'writer', 'emailAddress': MY_EMAIL}).execute()

    # Replace placeholders
    formatted_date = datetime.now().strftime("%B %dth, %Y")
    docs_service.documents().batchUpdate(documentId=doc_id, body={
        "requests": [
            {"replaceAllText": {"containsText": {"text": "{{DATE}}"}, "replaceText": f"[{formatted_date}]"}},
            {"replaceAllText": {"containsText": {"text": "{{POLITICAL_NEWS}}"}, "replaceText": political_block}},
            {"replaceAllText": {"containsText": {"text": "{{GOV_NEWS}}"}, "replaceText": gov_block}}
        ]
    }).execute()

    # Apps Script call to apply bolds
    resp = requests.post(APPS_SCRIPT_URL, data={"docId": doc_id})
    print("Apps Script:", resp.text)
    print(f"✅ Report generated: {doc_link}")

    # Save to Repository tab
    try:
        repo_ws = sh.worksheet("Repository")
    except gspread.exceptions.WorksheetNotFound:
        repo_ws = sh.add_worksheet(title="Repository", rows="1000", cols="2")
        repo_ws.insert_row(["Link", "DateCreated"], 1)
    ist_now = datetime.now(tz=IST)
    repo_ws.append_row([doc_link, ist_now.strftime("%I:%M %p · %d %b, %Y")])

# ====================================================
# MAIN PIPELINE
# ====================================================
process_relevance()
process_cleaning()
process_refinement()
generate_report()
