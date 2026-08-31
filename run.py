import os
import time
import random
import base64
import json
import pytz
import traceback
import requests
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
import joblib
import warnings
import re
from sklearn.exceptions import InconsistentVersionWarning
warnings.filterwarnings("ignore", category=InconsistentVersionWarning)

# Set timezone
IST = pytz.timezone("Asia/Kolkata")

# --- GLOBAL CONFIGURATION ---
MAX_RAW_LEN = 3000

# ============================================================
# DELAY BETWEEN ROWS (seconds)
# Free tier Flash-Lite = 15 RPM = 1 request per 4 seconds
# Free tier Flash      = 10 RPM = 1 request per 6 seconds
# Set these conservatively so we never hit RPM limits.
# ============================================================
BATCH_DELAY_6C = 5   # 5s between cleaning rows  (Flash-Lite, ~12 RPM effective)
BATCH_DELAY_6D = 7   # 7s between refining rows  (Flash, ~8 RPM effective)


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return 'th'
    return {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')


def get_env_var(name):
    val = os.getenv(name)
    if not val:
        if name.startswith("GEMINI_KEY_"):
            return None
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return val


# === Decode credentials.json from base64 env var ===
try:
    creds_json_b64 = get_env_var("CREDENTIALS_JSON_B64")
    creds_json = base64.b64decode(creds_json_b64).decode("utf-8")
    with open("credentials.json", "w") as f:
        f.write(creds_json)
except Exception as e:
    print(f"⚠️ Failed to decode/write credentials.json: {e}")
    raise

# === Load environment variables ===
SHEET_KEY = get_env_var("SHEET_KEY")
DOC_TEMPLATE_ID = get_env_var("DOC_TEMPLATE_ID")
APPS_SCRIPT_URL = get_env_var("APPS_SCRIPT_URL")
MY_EMAIL = get_env_var("MY_EMAIL")

# Authenticate Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
try:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    gc = gspread.authorize(creds)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            sh = gc.open_by_key(SHEET_KEY)
            break
        except gspread.exceptions.APIError as e:
            print(f"⚠️ Google Sheets API Error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                print("⏳ Retrying in 10 seconds...")
                time.sleep(10)
            else:
                print("❌ Failed to connect to Google Sheets after multiple attempts.")
                raise
except Exception as e:
    print(f"⚠️ Google Sheets authentication failed: {e}")
    raise

today_tab = datetime.now().strftime("%Y-%m-%d")

try:
    worksheet = sh.worksheet(today_tab)
except gspread.exceptions.WorksheetNotFound:
    print(f"Worksheet '{today_tab}' not found, creating new worksheet.")
    worksheet = sh.add_worksheet(title=today_tab, rows="1000", cols="20")

print(f"✅ Opened worksheet: {today_tab}")


def batch_update(updates_list):
    if not updates_list:
        return
    reqs = []
    for row, col, val in updates_list:
        reqs.append({
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
        worksheet.spreadsheet.batch_update({"requests": reqs})
        print(f"✅ Batch updated {len(updates_list)} cells")
    except Exception as e:
        print(f"⚠️ Batch update failed: {e}")


# ============================================================
# API KEY POOLS
#
# IMPORTANT — READ THIS:
# Google's free tier quota is PER PROJECT, not per key.
# If all your keys are from the same Gmail account / same
# Google Cloud project, they ALL share one quota bucket.
# Rotating between them gives you zero extra quota.
#
# To actually multiply your quota you need keys from
# DIFFERENT Google Cloud projects (different gmail accounts
# each with their own project, or multiple projects per account).
#
# Current free limits (as of April 2026):
#   gemini-2.5-flash-lite : 15 RPM, 1000 RPD  per project
#   gemini-2.5-flash      : 10 RPM,  500 RPD  per project
#
# Models that are confirmed FREE and WORKING (April 2026):
#   gemini-2.5-flash-lite  (fastest, highest quota)
#   gemini-2.5-flash       (better quality, lower quota)
# DO NOT use gemini-1.5-* — those are SHUT DOWN (404 error).
# DO NOT use gemini-2.0-* — deprecated, shutting down June 2026.
# ============================================================

MODEL_6B      = "gemini-2.5-flash-lite"   # Relevance (local ML, Gemini not used here)
MODEL_6C      = "gemini-2.5-flash-lite"   # Cleaning   (15 RPM / 1000 RPD free)
MODEL_6D      = "gemini-2.5-flash"        # Refining   (10 RPM / 500  RPD free)
MODEL_DOC     = "gemini-2.5-flash"        # Report     (10 RPM / 500  RPD free)

# Build key pools — only include keys that actually exist in env
api_keys_6b  = [k for k in [get_env_var(f"GEMINI_KEY_6B_{i}")  for i in range(1, 9)]  if k]
api_keys_6c  = [k for k in [get_env_var(f"GEMINI_KEY_6C_{i}")  for i in range(1, 4)]  if k]
api_keys_6d  = [k for k in [get_env_var(f"GEMINI_KEY_6D_{i}")  for i in range(1, 13)] if k]
api_keys_doc = [k for k in [get_env_var(f"GEMINI_KEY_DOC_{i}") for i in range(1, 4)]  if k]

# ============================================================
# BUG FIX: key index trackers must be MODULE-LEVEL lists so
# that key rotation persists ACROSS rows, not just within one
# row's retry loop. Previously, [0] was created fresh inside
# each clean_text() / refine_text() call, so Key 1 was always
# hammered and Key 2..N were almost never used.
# ============================================================
key_idx_6c  = [0]
key_idx_6d  = [0]
key_idx_doc = [0]


def call_gemini_with_rotation(prompt, api_key_list, key_index_ref, model_name, max_retries=8):
    """
    Calls Gemini with automatic key rotation on quota errors (429)
    and conservative backoff on server errors (503/500).
    """
    if not api_key_list:
        print(f"❌ No API keys configured for model {model_name}.")
        return ""

    for attempt in range(max_retries):
        current_key_index = key_index_ref[0]
        api_key = api_key_list[current_key_index]

        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            # Advance to next key for the NEXT call (round-robin load balancing)
            key_index_ref[0] = (current_key_index + 1) % len(api_key_list)
            return resp.text.strip()

        except Exception as e:
            err_msg = str(e)

            # Model not found — fail immediately, no point retrying
            if "404" in err_msg and "model" in err_msg.lower():
                print(f"❌ Model not found: {model_name}. Check the model name.")
                return ""

            # Quota exhausted (429) — switch key immediately
            if "429" in err_msg or "Resource exhausted" in err_msg or "quota" in err_msg.lower():
                old_idx = key_index_ref[0]
                key_index_ref[0] = (key_index_ref[0] + 1) % len(api_key_list)
                print(f"🔄 [Key {old_idx + 1} Quota Hit] Switching to Key {key_index_ref[0] + 1} and waiting 65s...")
                # Wait 65s — free tier quota refills per minute
                time.sleep(65)
                continue

            # Server busy (503/500) — wait briefly, same key is fine
            elif "503" in err_msg or "500" in err_msg or "unavailable" in err_msg.lower():
                # Cap backoff at 30s — 503s from Gemini usually resolve quickly
                wait = min(10 * (attempt + 1), 30)
                print(f"⚠️ [Key {current_key_index + 1}] Server busy. Waiting {wait}s (Attempt {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue

            # Any other error — exponential backoff, short
            else:
                wait = min(2 ** attempt, 30)
                print(f"⚠️ API error on Key {current_key_index + 1} (Attempt {attempt+1}/{max_retries}): {e}")
                time.sleep(wait)

    print(f"❌ Call completely failed after {max_retries} attempts.")
    return ""


# --- Helper: Clean Noise for ML Prediction ---
def clean_noise_for_prediction(text):
    text = str(text)
    ignore_list = ["भारत समाचार", "Bharat Samachar", "@bstvlive", "Translate post", "Views", "AM", "PM"]
    for word in ignore_list:
        text = text.replace(word, "")
    text = re.sub(r'\d{1,2}:\d{2}', '', text)
    text = re.sub(r'\n\s*\d+\s*\n', '\n', text)
    return text.strip()


# --- 6B) Relevance Check (Local ML Model) ---
def process_relevance():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    raw_col = worksheet.col_values(idx_map["Raw"])[1:]
    relevance_col = worksheet.col_values(idx_map["Relevance"])[1:]

    updates_relevance = []
    updates_runtime = []
    updates_confidence = []

    CONFIDENCE_THRESHOLD = 0.90

    print("🧠 Loading local relevance model from relevance_model.pkl...")
    try:
        relevance_model = joblib.load('relevance_model.pkl')
        model_loaded = True
        print("✅ Local Model loaded successfully!")
    except Exception as e:
        print(f"⚠️ Could not load 'relevance_model.pkl': {e}")
        print("➡️ Defaulting to '0' for safety.")
        model_loaded = False

    print(f"--- Starting Relevance Check (Threshold: {CONFIDENCE_THRESHOLD}) ---")

    for i, raw_text in enumerate(raw_col):
        row_number = i + 2
        raw_text = raw_text.strip()

        if not raw_text or raw_text in ["⚠️ fetch failed", ""]:
            continue

        existing_flag = str(relevance_col[i]).strip() if i < len(relevance_col) else ""

        if existing_flag == "1":
            continue

        flag = "0"
        confidence_score = 0.0

        if model_loaded:
            try:
                clean_text_ml = raw_text.replace("Bharat Samachar", "").replace("Translate post", "")
                probs = relevance_model.predict_proba([clean_text_ml])[0]
                confidence_score = probs[1]
                flag = "1" if confidence_score >= CONFIDENCE_THRESHOLD else "0"

                if flag == "1":
                    print(f"Row {row_number} → Flagged '1' (Confidence: {confidence_score:.2f})")
                elif confidence_score > 0.5:
                    print(f"Row {row_number} → Skipped (Score {confidence_score:.2f} < {CONFIDENCE_THRESHOLD})")

            except Exception as e:
                print(f"Row {row_number} → Prediction failed: {e}")
                flag = "0"

        if flag == "1" or existing_flag == "":
            updates_relevance.append((row_number, idx_map["Relevance"], flag))
            updates_runtime.append((row_number, idx_map["RunTime"],
                                    datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
            if flag == "1":
                score_str = f"{int(confidence_score * 100)}%"
                updates_confidence.append((row_number, 7, score_str))

    batch_update(updates_relevance)
    batch_update(updates_runtime)
    batch_update(updates_confidence)


# --- 6C) Cleaning Step ---
def clean_text(raw_text):
    prompt = f"""
आपको एक ट्वीट या समाचार का कच्चा टेक्स्ट दिया गया है। आपका काम है टेक्स्ट में से **केवल मुख्य समाचार सामग्री** को निकालना है।

सख्ती से निम्नलिखित तत्वों को हटा दें:
1. शुरूआती हैंडल जैसे "भारत समाचार | Bharat Samachar @bstvlive" या कोई भी चैनल/अकाउंट नाम।
2. सभी हैशटैग (# से शुरू होने वाले)।
3. सभी मेंशन (@ से शुरू होने वाले)।
4. सभी URLs/लिंक्स (जैसे youtube.com, https:// आदि)।
5. "Translate post", "Views", "Likes", "तारीख/समय", "1" या "6" जैसे सभी अतिरिक्त मेटाडेटा।
6. "पूरा इंटरव्यू देखने के लिए इस लिंक पर क्लिक करें" जैसे कोई भी कॉल-टू-एक्शन वाक्य या फुटर।

आउटपुट में **केवल** मुख्य समाचार या कथन ही होना चाहिए।
""" + raw_text

    # BUG FIX: Use module-level key_idx_6c so rotation persists across rows
    cleaned = call_gemini_with_rotation(
        prompt=prompt,
        api_key_list=api_keys_6c,
        key_index_ref=key_idx_6c,
        model_name=MODEL_6C,
        max_retries=8
    )
    return cleaned or raw_text[:3000]


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
        try:
            print(f"Row {row_number} → Cleaning...")
            cleaned_text = clean_text(raw_text[:3000])
            updates_cleaned.append((row_number, idx_map["Cleaned"], cleaned_text))
            updates_runtime.append((row_number, idx_map["RunTime"],
                                    datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
        except Exception:
            traceback.print_exc()
        time.sleep(BATCH_DELAY_6C)

    batch_update(updates_cleaned)
    batch_update(updates_runtime)


# --- 6D) Refine Step ---
def remove_nukta(text: str) -> str:
    replacements = {
        "क़": "क", "ख़": "ख", "ग़": "ग", "ज़": "ज",
        "फ़": "फ", "ऱ": "र", "ऩ": "न"
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def refine_text(cleaned_text):
    prompt = f"""
नीचे दिया गया समाचार पहले से साफ है। इसे पेशेवर, संक्षिप्त और प्रवाहपूर्ण रिपोर्टिंग शैली में परिष्कृत करें।
निर्देश:
- आउटपुट हमेशा एक ही अनुच्छेद (1–3 वाक्य) में दें।
- समाचार में जो काल (भूतकाल, वर्तमानकाल, भविष्यकाल) दिया गया है, उसे बिना बदले वैसा ही रखें।
  * यदि घटना आने वाली है (जैसे "करेंगे", "जाएंगे"), तो उसे भविष्यकाल में ही लिखें।
  * यदि घटना पूरी हो चुकी है, तो भूतकाल रखें।
- केवल उन्हीं तथ्यों का उपयोग करें जो इनपुट टेक्स्ट में हैं; कोई अनुमान या नया परिणाम न जोड़ें।
- सप्ताह का दिन और स्थान से शुरुआत केवल तभी करें, जब समाचार किसी विशिष्ट कार्यक्रम, बयान या सुनवाई से जुड़ा हो।
- व्यक्तियों के नाम पर उपसर्ग:
    * हिंदू पुरुष → "श्री"
    * हिंदू महिला → "श्रीमती" या "सुश्री"
    * दिवंगत → "स्व. श्री" / "स्व. श्रीमती"
    * अन्य धर्म → कोई उपसर्ग नहीं दें
- राजनीतिक दलों को केवल संक्षिप्त नाम से लिखें: सपा, भाजपा, कांग्रेस, बसपा, आप, आजाद समाज पार्टी।
- स्थान और संस्थानों के नाम हिंदी में लिखें, उदाहरण के लिए:
    * "यूपी" की जगह "उत्तर प्रदेश" का प्रयोग करें।
    * अंग्रेजी के संक्षिप्त शब्द जैसे "IPS" को हिंदी में "आईपीएस" लिखें।
- सभी विराम चिह्न सही हिंदी विराम चिह्नों (पूर्णविराम "।") का प्रयोग करें; सेमीकोलन या अंग्रेजी विराम चिह्नों से बचें।
- वाक्य संरचना साफ और तार्किक हो। दो बिंदुओं को जोड़ते समय उपयुक्त संयोजक ("साथ ही", "इसके अलावा") का प्रयोग करें; सेमीकोलन का प्रयोग न करें।
- केवल आवश्यक शब्दों का प्रयोग करें।
- हिंदी व्याकरण और वर्तनी पूर्णतः शुद्ध हो।
- नुक्ता केवल फारसी/उर्दू मूल शब्दों से हटाएँ (जैसे ज़→ज, फ़→फ), लेकिन हिंदी मूल शब्द जैसे "करोड़" में नुक्ता बनाएं रखें।
- केवल परिष्कृत समाचार लौटाएँ, कोई अतिरिक्त टिप्पणी न करें।

समाचार:

{cleaned_text}
"""
    # BUG FIX: Use module-level key_idx_6d so rotation persists across rows
    refined = call_gemini_with_rotation(
        prompt=prompt,
        api_key_list=api_keys_6d,
        key_index_ref=key_idx_6d,
        model_name=MODEL_6D,
        max_retries=8
    )

    if refined:
        refined = remove_nukta(refined)
        refined = " ".join(refined.split())
        return refined
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
        try:
            print(f"Row {row_number} → Refining...")
            refined_text = refine_text(cleaned_text[:3000])
            updates_refined.append((row_number, idx_map["Refined"], refined_text))
            updates_runtime.append((row_number, idx_map["RunTime"],
                                    datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
        except Exception:
            traceback.print_exc()
        time.sleep(BATCH_DELAY_6D)

    batch_update(updates_refined)
    batch_update(updates_runtime)


# --- 7) Report Generation ---

def categorize_and_clean(news_list):
    prompt = f"""
आपको उत्तर प्रदेश की **परिष्कृत (refined) समाचार बिंदुओं की सूची** दी जा रही है।
यह सूची पहले से ही सही हिंदी व्याकरण और **बोल्डिंग (**) मानकों** का पालन करती है।

कृपया:
1. समाचारों को दो श्रेणियों में बाँटें:
    - महत्वपूर्ण राजनीतिक गतिविधियां
    - महत्वपूर्ण गवर्नेंस गतिविधियां
2. प्रत्येक बिंदु पूरी तरह से एक पैराग्राफ में लिखें। यदि आवश्यक हो, तो तार्किक रूप से संबंधित बिंदुओं को एक पैराग्राफ में मर्ज करें।
3. प्रत्येक समाचार बिंदु में, निम्नलिखित **मुख्य तत्वों** को डबल एस्टेरिस्क (**) लगाकर **बोल्ड** करें:
    - **व्यक्तिगत नाम** और उनके **उपसर्ग** (**श्री**, **श्रीमती**, **सुश्री** आदि)।
    - **पदनाम** (जैसे **मुख्यमंत्री**, **सांसद**, **विधायक**, **अधिकारी**)।
    - **स्थान** और **भूगोल** (शहर, जिला, राज्य - जैसे **लखनऊ**, **उत्तर प्रदेश**)।
    - **पार्टी के नाम** (केवल संक्षिप्त रूप: **सपा**, **भाजपा**, **बसपा**, **कांग्रेस**, **आप**)।
    - समाचार की **मुख्य गतिविधि** या **विषय** (जैसे **गिरफ्तार**, **घोषणा**, **वचन**)।
    - **योजनाओं के नाम** (जैसे **किसान सम्मान निधि योजना**)।

आउटपुट फॉर्मेट इस प्रकार दें:
महत्वपूर्ण राजनीतिक गतिविधियां:
<समाचार 1>
<समाचार 2>
महत्वपूर्ण गवर्नेंस गतिविधियां:
<समाचार 3>
<समाचार 4>
"""
    # BUG FIX: Use module-level key_idx_doc
    categorized_text = call_gemini_with_rotation(
        prompt=prompt + "\n\n".join(news_list),
        api_key_list=api_keys_doc,
        key_index_ref=key_idx_doc,
        model_name=MODEL_DOC,
        max_retries=8
    )
    return categorized_text


def final_qc(news_list):
    prompt = """
आपको परिष्कृत समाचार बिंदुओं की एक सूची दी जा रही है। ये समाचार बिंदु पहले से ही बोल्डिंग (**) और हिंदी व्याकरण मानकों (उपसर्ग, नुक्ता, पूर्णविराम) का पालन करते हुए तैयार किए गए हैं।

आपका कार्य **सख्ती से** केवल दो बातों तक सीमित है:
1. **भाषा और प्रवाह में सुधार:** अनावश्यक विराम चिह्नों (जैसे सेमीकोलन/अंग्रेजी कॉमा) को सही हिंदी विराम चिह्नों (जैसे पूर्णविराम) से बदलें, और वाक्यों के बीच के प्रवाह को सुधारें।
2. **समाचारों का विलय:** यदि कोई समाचार बिंदु एक ही व्यक्ति या नेता के एक ही स्थान पर हुई कई गतिविधियों को वर्णित करता है, तो उन्हें एक ही पैराग्राफ में तार्किक रूप से संयुक्त करें।

**महत्वपूर्ण निर्देश (सख्ती से पालन करें):**
- नामों और उपसर्गों (जैसे श्री, श्रीमती आदि), नुक्ता (`ज़` को `ज` में बदलने की प्रक्रिया), और **बोल्ड मार्किंग** को **जस का तस** रखें। उन्हें न बदलें, न हटाएँ, न ही दोबारा लगाएँ।
- आउटपुट प्रत्येक समाचार को एक पूरा पैराग्राफ बनाएं, जहाँ सम्मिलित समाचार आपस में तार्किक और प्रवाही हों।
- आउटपुट में केवल समाचार बिंदु ही दें, खुद से कोई भूमिका, स्पष्टीकरण या अतिरिक्त वाक्य न लिखें।
"""
    # BUG FIX: Use module-level key_idx_doc
    qc_text = call_gemini_with_rotation(
        prompt=prompt + "\n\n".join(news_list),
        api_key_list=api_keys_doc,
        key_index_ref=key_idx_doc,
        model_name=MODEL_DOC,
        max_retries=8
    )

    if qc_text:
        paragraphs = [p.strip() for p in qc_text.strip().split('\n\n') if p.strip()]
        return paragraphs
    return news_list


def remove_filler_lines(lines):
    bad_phrases = [
        "यहां आपके द्वारा दिए गए समाचार बिंदुओं के सुधारित संस्करण हैं",
        "यह समाचार बिंदुओं का संशोधित संस्करण है",
        "यह आपके समाचार बिंदुओं का संक्षिप्त रूप है"
    ]
    return [l.strip() for l in lines if l.strip() and not any(bad in l for bad in bad_phrases)]


def generate_report():
    headers = worksheet.row_values(1)
    idx_map = {name: headers.index(name)+1 for name in headers}
    refined_col = worksheet.col_values(idx_map["Refined"])[1:]
    refined_news = [r.strip() for r in refined_col if r.strip()]
    if not refined_news:
        print("⚠️ No refined news found!")
        return

    print("→ Categorizing news...")
    categorized_text = categorize_and_clean(refined_news)

    if not categorized_text:
        print("⚠️ Categorization failed after retries. Skipping report generation.")
        return

    political_text, gov_text, section = [], [], None
    for line in categorized_text.splitlines():
        if "महत्वपूर्ण राजनीतिक गतिविधियां" in line:
            section = "political"
            continue
        elif "महत्वपूर्ण गवर्नेंस गतिविधियां" in line:
            section = "gov"
            continue
        elif line.strip():
            if section == "political":
                political_text.append(line.strip())
            elif section == "gov":
                gov_text.append(line.strip())

    print("→ Applying final quality check on Political news...")
    political_text = remove_filler_lines(final_qc(political_text))
    print("→ Applying final quality check on Governance news...")
    gov_text = remove_filler_lines(final_qc(gov_text))

    political_block = "\n".join(political_text) if political_text else "—"
    gov_block = "\n".join(gov_text) if gov_text else "—"

    try:
        creds = Credentials.from_service_account_file("credentials.json", scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ])
        docs_service = build("docs", "v1", credentials=creds)
        drive_service = build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"⚠️ Google Docs/Drive authentication failed: {e}")
        return

    today_date_str = datetime.now().strftime("%Y%m%d")
    new_doc_title = f"{today_date_str} Uttar Pradesh Daily Political Tracker"
    TARGET_FOLDER_ID = "1oR7_OjjA4QQLrH3f7-KgU4O8twIG8bF-"

    try:
        new_doc = drive_service.files().copy(
            fileId=DOC_TEMPLATE_ID,
            body={
                "name": new_doc_title,
                "parents": [TARGET_FOLDER_ID]
            }
        ).execute()
        doc_id = new_doc.get("id")
        doc_link = f"https://docs.google.com/document/d/{doc_id}/edit"
    except Exception as e:
        print(f"⚠️ Google Drive file copy failed: {e}")
        return

    MY_EMAIL_val = get_env_var("MY_EMAIL")
    emails = [e.strip() for e in MY_EMAIL_val.split(",") if e.strip()]
    for email in emails:
        try:
            drive_service.permissions().create(
                fileId=doc_id,
                body={'type': 'user', 'role': 'writer', 'emailAddress': email},
                sendNotificationEmail=False,
                fields='id'
            ).execute()
            print(f"✅ Shared document with {email}")
        except Exception as e:
            print(f"⚠️ Sharing doc failed for {email}: {e}")

    today = datetime.now()
    day_with_suffix = f"{today.day}{ordinal(today.day)}"
    formatted_date = today.strftime(f"%B {day_with_suffix}, %Y")

    try:
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={
                "requests": [
                    {"replaceAllText": {"containsText": {"text": "{{DATE}}"}, "replaceText": f"[{formatted_date}]"}},
                    {"replaceAllText": {"containsText": {"text": "{{POLITICAL_NEWS}}"}, "replaceText": political_block}},
                    {"replaceAllText": {"containsText": {"text": "{{GOV_NEWS}}"}, "replaceText": gov_block}},
                ]
            }
        ).execute()
    except Exception as e:
        print(f"⚠️ Document text replacement failed: {e}")

    print('----------')
    print('GOVERNANCE BLOCK below:')
    print(gov_block)
    print('----------')

    try:
        # Added the email string to the payload so Apps Script can read it and send HTML emails
        resp = requests.post(APPS_SCRIPT_URL, data={"docId": doc_id, "email": MY_EMAIL_val})
        print("Apps Script processing:", resp.text)
    except Exception as e:
        print(f"⚠️ Apps Script call failed: {e}")

    print(f"✅ Report generated: {doc_link}")

    try:
        repo_ws = sh.worksheet("Repository")
    except gspread.exceptions.WorksheetNotFound:
        repo_ws = sh.add_worksheet(title="Repository", rows="1000", cols="2")
        repo_ws.insert_row(["Link", "DateCreated"], 1)

    ist_now = datetime.now(tz=IST)
    try:
        repo_ws.append_row([doc_link, ist_now.strftime("%I:%M %p · %d %b, %Y")])
        print("✅ Link saved to Repository tab")
    except Exception as e:
        print(f"⚠️ Failed to save link to repository: {e}")


# === MAIN PIPELINE ===
def main():
    try:
        print("--- Starting Relevance Check (6B) ---")
        process_relevance()
        print("\n--- Starting Cleaning Step (6C) ---")
        process_cleaning()
        print("\n--- Starting Refinement Step (6D) ---")
        process_refinement()
    except Exception as e:
        print(f"⚠️ Error in processing pipeline: {e}")
        traceback.print_exc()

    # Wait for quota to recover before report generation.
    # After 6D runs many calls, keys may be near their per-minute limit.
    # A 90s wait lets the RPM window reset so report gen succeeds first try.
    print("\n⏳ Waiting 90s for quota windows to reset before report generation...")
    time.sleep(90)

    # Refresh worksheet for latest refined content
    global worksheet
    worksheet = sh.worksheet(today_tab)

    print("\n--- Starting Report Generation (7) ---")
    try:
        generate_report()
    except Exception as e:
        print(f"⚠️ Error generating report: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
