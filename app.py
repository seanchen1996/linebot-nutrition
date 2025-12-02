# app.py
import os
import json
from datetime import datetime
from threading import Lock
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
from linebot.exceptions import InvalidSignatureError
import requests
import base64

# ------------ Config ------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_DATA_PATH = os.getenv("GITHUB_DATA_PATH", "nutrition_db.json").strip()

# ------------ Line client & FastAPI ------------
app = FastAPI()
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ------------ Thread-safety for storage ------------
lock = Lock()

# ------------ GitHub JSON Storage class ------------
class GitHubStorage:
    """
    Store all DBs in a single JSON in GitHub repo
    schema: {"nutrition":{}, "today":{}, "goals":{}}
    """
    def __init__(self, token, repo, path):
        self.token = token
        self.repo = repo
        self.path = path
        self.headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
        # ensure file exists
        if not self._get_file():
            self._write_file({"nutrition": {}, "today": {}, "goals": {}}, "init db")

    def _get_file(self):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers=self.headers)
        if r.status_code == 200:
            return r.json()
        return None

    def _write_file(self, data: dict, message="update"):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        content = json.dumps(data, ensure_ascii=False, indent=2)
        payload = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode("utf-8")}
        current = self._get_file()
        if current:
            payload["sha"] = current["sha"]
        r = requests.put(url, headers=self.headers, json=payload)
        if r.status_code not in (200,201):
            raise Exception(f"GitHub save failed: {r.status_code} {r.text}")
        return r.json()

    def load(self):
        with lock:
            f = self._get_file()
            if not f:
                return {"nutrition": {}, "today": {}, "goals": {}}
            content = base64.b64decode(f["content"]).decode("utf-8")
            return json.loads(content)

    def save(self, state: dict):
        with lock:
            self._write_file(state, "update data")

# ------------ Select storage ------------
if GITHUB_TOKEN and GITHUB_REPO and GITHUB_DATA_PATH:
    storage = GitHubStorage(GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_PATH)
    print("Using GitHub storage")
else:
    # fallback local
    STORAGE_FILE = "local_db.json"
    class LocalStorage:
        def load(self):
            with lock:
                if not os.path.exists(STORAGE_FILE):
                    default = {"nutrition": {}, "today": {}, "goals": {}}
                    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
                        json.dump(default, f, ensure_ascii=False, indent=2)
                    return default
                try:
                    with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                        return json.load(f)
                except:
                    default = {"nutrition": {}, "today": {}, "goals": {}}
                    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
                        json.dump(default, f, ensure_ascii=False, indent=2)
                    return default
        def save(self, state):
            with lock:
                with open(STORAGE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
    storage = LocalStorage()
    print("Using local storage")

# ------------ Helpers ------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_db():
    state = storage.load()
    return state

def save_db(state):
    storage.save(state)

# ------------ Business logic ------------
def set_goal(user_id: str, protein: float, fat: float, carb: float) -> str:
    state = get_db()
    state.setdefault("goals", {})
    state["goals"][user_id] = {"protein": protein, "fat": fat, "carb": carb}
    save_db(state)
    return f"已設定目標：P {protein} F {fat} C {carb}"

def add_food_to_db(name: str, portion: float, protein: float, fat: float, carb: float) -> str:
    state = get_db()
    state.setdefault("nutrition", {})
    state["nutrition"][name] = {"portion": portion, "protein": protein, "fat": fat, "carb": carb}
    save_db(state)
    return f"已新增食物：{name} 每份 {portion}g → P:{protein} F:{fat} C:{carb}"

def list_foods_text() -> str:
    state = get_db()
    db = state.get("nutrition", {})
    if not db:
        return "食物資料庫為空"
    lines = []
    for i, (k,v) in enumerate(db.items(), start=1):
        lines.append(f"{i}. {k} 份量 {v['portion']}g P:{v['protein']} F:{v['fat']} C:{v['carb']}")
    return "\n".join(lines)

def record_food(user_id: str, name: str, grams: float) -> str:
    state = get_db()
    if name not in state.get("nutrition", {}):
        return f"找不到食物：{name}，請先新增"
    item = state["nutrition"][name]
    factor = grams / item["portion"]
    entry = {"food": name, "amount": grams, "protein": round(item["protein"]*factor,3),
             "fat": round(item["fat"]*factor,3), "carb": round(item["carb"]*factor,3), "timestamp": now_str()}
    state.setdefault("today", {}).setdefault(user_id, [])
    # 合併相同食物
    merged = False
    for r in state["today"][user_id]:
        if r["food"] == name:
            r["amount"] += grams
            r["protein"] += entry["protein"]
            r["fat"] += entry["fat"]
            r["carb"] += entry["carb"]
            merged = True
            break
    if not merged:
        state["today"][user_id].append(entry)
    save_db(state)
    if merged:
        return f"已累計 {name} {grams}g"
    else:
        return f"已記錄 {name} {grams}g"

def calc_today_summary(user_id: str) -> dict:
    state = get_db()
    items = state.get("today", {}).get(user_id, [])
    total = {"protein":0.0,"fat":0.0,"carb":0.0}
    for e in items:
        total["protein"] += e["protein"]
        total["fat"] += e["fat"]
        total["carb"] += e["carb"]
    total = {k: round(v,3) for k,v in total.items()}
    return {"items": items, "total": total}

def build_flex_today(user_id: str) -> FlexSendMessage:
    summary = calc_today_summary(user_id)
    items = summary["items"]
    total = summary["total"]
    state = get_db()
    goals = state.get("goals", {}).get(user_id)
    body = []
    body.append({"type":"text","text":"📅 今日紀錄","weight":"bold","size":"md"})
    if not items:
        body.append({"type":"text","text":"尚無紀錄","size":"sm"})
    else:
        for i,e in enumerate(items,start=1):
            body.append({"type":"text","text":f"{i}. {e['food']} {e['amount']}g P:{e['protein']} F:{e['fat']} C:{e['carb']}", "size":"sm"})
    body.append({"type":"text","text":f"總計 P:{total['protein']} F:{total['fat']} C:{total['carb']}", "size":"sm","weight":"bold"})
    if goals:
        body.append({"type":"text","text":f"目標 P:{goals['protein']} F:{goals['fat']} C:{goals['carb']}", "size":"sm"})
    flex = {"type":"bubble","body":{"type":"box","layout":"vertical","contents":body}}
    return FlexSendMessage(alt_text="今日攝取", contents=flex)

# ------------ Command parser ------------
def parse_and_execute(user_id: str, text: str) -> Any:
    text = text.strip()
    parts = text.split()
    if not parts:
        return None
    cmd = parts[0]
    if cmd == "目標" and len(parts)==4:
        try:
            p,f,c = map(float, parts[1:])
            return set_goal(user_id,p,f,c)
        except:
            return "目標格式錯誤"
    if cmd == "新增" and len(parts)==6:
        try:
            name = parts[1]
            portion = float(parts[2])
            p,f,c = map(float, parts[3:])
            return add_food_to_db(name,portion,p,f,c)
        except:
            return "新增格式錯誤"
    if len(parts)==2:
        try:
            name = parts[0]
            grams = float(parts[1])
            return record_food(user_id,name,grams)
        except:
            return "格式錯誤"
    if cmd in ["今日","今日累計","今日攝取"]:
        return build_flex_today(user_id)
    if cmd in ["list","列表","食物庫","食物列表"]:
        return list_foods_text()
    return "指令無效或尚未實作"

# ------------ LINE webhook ------------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body = body.decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    result = parse_and_execute(user_id, text)
    if isinstance(result, str):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
    elif isinstance(result, FlexSendMessage):
        line_bot_api.reply_message(event.reply_token, result)

# ------------ Run locally ------------
if __name__=="__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT",8000)), reload=True)
