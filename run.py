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
import google.generativeai as genai

# Set timezone
IST = pytz.timezone("Asia/Kolkata")

# --- GLOBAL CONFIGURATION ---
MAX_RAW_LEN = 3000
BATCH_DELAY_SEC = 5 # Increased delay to 5s to respect the new RPM limits (max 15 RPM)


def ordinal(n):
    """Returns the English ordinal suffix for a number (e.g., 1 -> st, 2 -> nd)."""
    if 10 <= n % 100 <= 20:
        return 'th'
    return {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')

# === Helper to exit with error if env var missing ===
def get_env_var(name):
    val = os.getenv(name)
    if not val:
        # Check if the missing key is expected (e.g., GEMINI_KEY_6B_5 and above)
        if name.startswith("GEMINI_KEY_"):
             # If key is missing, return None and let the list comprehension handle the size
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

# Authenticate Google Sheets API (Code remains the same)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
try:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_KEY)
except Exception as e:
    print(f"⚠️ Google Sheets authentication failed: {e}")
    raise

today_tab = datetime.now().strftime("%Y-%m-%d")

# === Ensure today's sheet exists or create it === (Code remains the same)
try:
    worksheet = sh.worksheet(today_tab)
except gspread.exceptions.WorksheetNotFound:
    print(f"Worksheet '{today_tab}' not found, creating new worksheet.")
    worksheet = sh.add_worksheet(title=today_tab, rows="1000", cols="20")
    # Optionally add headers or initialization here if needed

print(f"✅ Opened worksheet: {today_tab}")

# === Batch update helper === (Code remains the same)
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
        print(f"✅ Batch updated {len(updates_list)} cells")
    except Exception as e:
        print(f"⚠️ Batch update failed: {e}")

# --- API Key Initialization & Model Name Correction ---

# 6B: Relevance (Pool size 8, using Flash-Lite for max RPD)
api_keys_6b = [get_env_var(f"GEMINI_KEY_6B_{i}") for i in range(1, 9) if get_env_var(f"GEMINI_KEY_6B_{i}")]
key_index_6b = 0
MODEL_6B = "gemini-2.5-flash-lite" 

# 6C: Cleaning (Pool size 3, using Flash-Lite for max RPD)
api_keys_6c = [get_env_var(f"GEMINI_KEY_6C_{i}") for i in range(1, 4) if get_env_var(f"GEMINI_KEY_6C_{i}")]
key_index_6c = 0
MODEL_6C = "gemini-2.5-flash-lite"

# 6D: Refine (Pool size 12, using Flash for better quality)
api_keys_6d = [get_env_var(f"GEMINI_KEY_6D_{i}") for i in range(1, 13) if get_env_var(f"GEMINI_KEY_6D_{i}")]
key_index_6d = 0
MODEL_6D = "gemini-2.5-flash"

# DOC: Reporting/Categorization (Pool size 3, using Flash for better quality)
api_keys_doc = [get_env_var(f"GEMINI_KEY_DOC_{i}") for i in range(1, 4) if get_env_var(f"GEMINI_KEY_DOC_{i}")]
key_index_doc = 0
MODEL_DOC = "gemini-2.5-flash"

# --- API Configuration and Rotation Helpers ---

def get_model_and_switch(api_key_list, key_index_ref, model_name):
    """Configures the Gemini API with the current key and returns the model."""
    
    # Map the current global index to the mutable reference [0]
    if api_key_list is api_keys_6b:
        key_index_ref[0] = globals().get('key_index_6b', 0)
    elif api_key_list is api_keys_6c:
        key_index_ref[0] = globals().get('key_index_6c', 0)
    elif api_key_list is api_keys_6d:
        key_index_ref[0] = globals().get('key_index_6d', 0)
    elif api_key_list is api_keys_doc:
        key_index_ref[0] = globals().get('key_index_doc', 0)
    
    current_key_index = key_index_ref[0]
    genai.configure(api_key=api_key_list[current_key_index])
    
    # Create and return the model instance
    return genai.GenerativeModel(model_name)

def call_gemini_with_rotation(prompt, api_key_list, key_index_ref, model_name, max_retries=4):
    """
    Handles API calls with key rotation and exponential backoff, specifically
    for 404 Model Not Found and 429 Resource Exhausted errors.
    """
    if not api_key_list:
        print(f"❌ No API keys found for model {model_name}. Skipping call.")
        return ""

    for attempt in range(max_retries):
        try:
            model = get_model_and_switch(api_key_list, key_index_ref, model_name)
            resp = model.generate_content(prompt)
            
            # Update the global index for the *next* successful call's start
            next_index = (key_index_ref[0] + 1) % len(api_key_list)
            if api_key_list is api_keys_6b:
                globals()['key_index_6b'] = next_index
            elif api_key_list is api_keys_6c:
                globals()['key_index_6c'] = next_index
            elif api_key_list is api_keys_6d:
                globals()['key_index_6d'] = next_index
            elif api_key_list is api_keys_doc:
                globals()['key_index_doc'] = next_index

            return resp.text.strip()
            
        except Exception as e:
            err_msg = str(e)
            
            if "404" in err_msg and "model" in err_msg.lower():
                print(f"❌ Model Not Found Error: {model_name}. Please check the model name is correct for your account/API.")
                return ""
            
            if "429" in err_msg or "Resource exhausted" in err_msg:
                # 1. Quota Error: Switch key immediately
                key_index_ref[0] = (key_index_ref[0] + 1) % len(api_key_list)
                print(f"⚠️ Quota exceeded on key pool. Switching to key index {key_index_ref[0]} and waiting 60s...")
                time.sleep(60) # Longer wait after rate limit error
                continue
            
            elif "503" in err_msg or "unavailable" in err_msg.lower():
                # 2. Service Error: Use exponential backoff
                wait = min(30, 2 ** attempt)
                print(f"⚠️ Service unavailable (503). Retrying in {wait}s...")
                time.sleep(wait)
                continue
            
            else:
                # 3. Other Errors: Use exponential backoff
                print(f"⚠️ API call failed (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)

    return "" # Fallback on final failure

# --- 6b) Process Relevance (1/0) ---

def generate_relevance(text):
    prompt = f"""
क्या नीचे दिया गया समाचार **सख्ती से** उत्तर प्रदेश की **उच्च स्तरीय राजनीतिक** (दल, नेता, सरकार, विपक्ष, विधानसभा, लोकसभा) या **प्रमुख नीति/योजना संबंधी सरकारी गतिविधियों** से संबंधित है?

**निम्नलिखित विषयों को **अस्वीकार (Reject)** करें और सिर्फ **0** लौटाएँ:**
1. सामान्य अपराध (हत्या, चोरी, यातायात उल्लंघन, अवैध तस्करी)।
2. दुर्घटनाएँ, आग, या प्राकृतिक आपदाएँ।
3. स्थानीय बिजली, पानी, सड़क या नगर निगम के सामान्य प्रशासनिक कार्य (जैसे मीटर लगाना, अतिक्रमण हटाना)।
4. अदालती मामले जो केवल व्यक्तिगत या सामान्य अपराध से संबंधित हों, न कि राजनीतिक विवादों से।

यदि हाँ, तो सिर्फ **1** लौटाएँ। यदि नहीं, तो सिर्फ **0** लौटाएँ। अन्य कुछ न लिखें।

समाचार:
{text}
"""
    key_index_arr = [0]
    flag = call_gemini_with_rotation(
        prompt=prompt,
        api_key_list=api_keys_6b,
        key_index_ref=key_index_arr,
        model_name=MODEL_6B,
        max_retries=4
    )
    
    # Check if the output starts with '1' (case-insensitive and whitespace-stripped)
    if flag.strip().startswith("1"):
        return "1"
    # Check if the output starts with '0'
    elif flag.strip().startswith("0"):
        return "0"
    # If the output is empty or garbage, return a failure status
    else:
        # This will now write "⚠️ AI failed" to the Relevance column
        print(f"Row relevance failed to parse output: '{flag}'")
        return "⚠️ AI failed"

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
            print(f"Row {row_number} → Checking relevance...")
            flag = generate_relevance(raw_text[:MAX_RAW_LEN])
            print(f"Row {row_number} → Relevance = {flag}")
            updates_relevance.append((row_number, idx_map["Relevance"], flag))
            updates_runtime.append((row_number, idx_map["RunTime"],
                                    datetime.now().astimezone(IST).strftime("%I:%M %p · %d %b, %Y")))
        except Exception:
            traceback.print_exc()
        time.sleep(BATCH_DELAY_SEC) # 5 seconds delay here
    batch_update(updates_relevance)
    batch_update(updates_runtime)

# --- 6c) Cleaning step ---

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

    key_index_arr = [0] 
    cleaned = call_gemini_with_rotation(
        prompt=prompt, 
        api_key_list=api_keys_6c, 
        key_index_ref=key_index_arr,
        model_name=MODEL_6C, 
        max_retries=4
    )
    
    return cleaned or raw_text[:3000] # Fallback to raw text if empty

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
        time.sleep(BATCH_DELAY_SEC) # 5 seconds delay here
    batch_update(updates_cleaned)
    batch_update(updates_runtime)

# --- 6d) Refine step ---

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
    key_index_arr = [0] 
    refined = call_gemini_with_rotation(
        prompt=prompt, 
        api_key_list=api_keys_6d, 
        key_index_ref=key_index_arr,
        model_name=MODEL_6D,
        max_retries=4
    )

    if refined:
        refined = remove_nukta(refined) # cleanup nukta
        refined = " ".join(refined.split()) # collapse whitespace
        return refined
    
    return cleaned_text # fallback


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
        time.sleep(BATCH_DELAY_SEC) # 5 seconds delay here
    batch_update(updates_refined)
    batch_update(updates_runtime)

# --- 7) Generate Google Doc Report ---

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
    key_index_arr = [0] 
    categorized_text = call_gemini_with_rotation(
        prompt=prompt + "\n\n".join(news_list), 
        api_key_list=api_keys_doc, 
        key_index_ref=key_index_arr,
        model_name=MODEL_DOC, 
        max_retries=3
    )
    
    return categorized_text

def final_qc(news_list):
    # The prompt is good, retaining it.
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
    key_index_arr = [0] 
    qc_text = call_gemini_with_rotation(
        prompt=prompt + "\n\n".join(news_list), 
        api_key_list=api_keys_doc, 
        key_index_ref=key_index_arr,
        model_name=MODEL_DOC, 
        max_retries=3
    )

    if qc_text:
        # Split by double newline to preserve paragraphs intact
        paragraphs = [p.strip() for p in qc_text.strip().split('\n\n') if p.strip()]
        return paragraphs
    return news_list # Fallback

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

    # Join paragraphs with two newlines for clear paragraph breaks in Google Doc
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
    try:
        new_doc = drive_service.files().copy(fileId=DOC_TEMPLATE_ID, body={"name": new_doc_title}).execute()
        doc_id = new_doc.get("id")
        doc_link = f"https://docs.google.com/document/d/{doc_id}/edit"
    except Exception as e:
        print(f"⚠️ Google Drive file copy failed: {e}")
        return

    MY_EMAIL = get_env_var("MY_EMAIL")
    emails = [e.strip() for e in MY_EMAIL.split(",") if e.strip()]
    for email in emails:
        try:
            drive_service.permissions().create(
                fileId=doc_id,
                body={'type': 'user', 'role': 'writer', 'emailAddress': email},
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
                    {"replaceAllText": {"containsText": {"text": "{{DATE}}"}, "replaceText": f"[{formatted_date}]" }},
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

    # Call Apps Script to apply bold formatting on markers
    try:
        resp = requests.post(APPS_SCRIPT_URL, data={"docId": doc_id})
        print("Apps Script processing:", resp.text)
    except Exception as e:
        print(f"⚠️ Apps Script call failed: {e}")

    print(f"✅ Report generated: {doc_link}")

    # Save link in Repository tab
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
        
    print("\n⏳ Waiting for Sheets to sync updates...")
    time.sleep(10)  # Extended wait to allow Google Sheets to fully sync before report generation
    
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
