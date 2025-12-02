"""
app.py - LINE Bot for nutrition tracking with GitHub or MongoDB persistence,
Flex Message, emoji completion indicator, searchable food DB, categories,
and chart image generation.

Requirements:
- Set environment variables:
    LINE_CHANNEL_SECRET
    LINE_CHANNEL_ACCESS_TOKEN
    (Either)
    MONGO_URI
    (Or)
    GITHUB_TOKEN
    GITHUB_REPO
    GITHUB_DATA_PATH

- requirements.txt should include:
    fastapi, uvicorn, line-bot-sdk, pymongo, requests, matplotlib, pillow

- Deploy to Render (or other) and set webhook to https://<your-url>/callback

Note: If using GitHub storage, the app stores a JSON file at GITHUB_DATA_PATH in GITHUB_REPO.
"""

import os
import json
import time
import math
import io
from typing import Optional, Dict, Any, List
from datetime import datetime, date

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

# LINE
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage, ImageSendMessage
)

# Optional DB libs
try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

app = FastAPI()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise Exception("Please set LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN env vars")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Storage selection
MONGO_URI = os.getenv("MONGO_URI", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_DATA_PATH = os.getenv("GITHUB_DATA_PATH", "data/nutrition_db.json").strip()

# App URL (optional) - used for chart image links
APP_URL = os.getenv("APP_URL", "").strip()

# ---------- Storage interface ----------
class Storage:
    def get_state(self) -> Dict[str, Any]:
        raise NotImplementedError
    def save_state(self, state: Dict[str, Any]) -> None:
        raise NotImplementedError

# ---------- MongoDB Storage ----------
class MongoStorage(Storage):
    def __init__(self, uri):
        if MongoClient is None:
            raise Exception("pymongo not installed")
        self.client = MongoClient(uri)
        self.db = self.client.get_database()  # default database in URI
        # Collections: targets, records, food_db
        self.targets = self.db["targets"]
        self.records = self.db["records"]
        self.foods = self.db["food_db"]

    # targets: one doc per user_id
    def set_target(self, user_id, p, f, c):
        self.targets.update_one({"user_id": user_id},
                                {"$set":{"protein":p,"fat":f,"carbs":c}}, upsert=True)
    def get_target(self, user_id):
        doc = self.targets.find_one({"user_id":user_id})
        return doc
    def add_food_db(self, food, base_weight, p, f, c, category="其他"):
        self.foods.update_one({"food":food},
                              {"$set": {"base_weight":base_weight,"protein":p,"fat":f,"carbs":c,"category":category}},
                              upsert=True)
    def get_food(self, food):
        return self.foods.find_one({"food":food})
    def search_foods(self, keyword):
        regex = {"$regex": keyword, "$options":"i"}
        return list(self.foods.find({"food": regex}))
    def list_foods(self):
        return list(self.foods.find())
    def add_record(self, user_id, food, weight, p, f, c):
        self.records.insert_one({"user_id":user_id,"food":food,"weight":weight,"protein":p,"fat":f,"carbs":c,"time":datetime.utcnow()})
    def get_today_records(self, user_id):
        today = datetime.utcnow().date()
        docs = list(self.records.find({"user_id":user_id, "time": {"$gte": datetime(today.year,today.month,today.day)}}))
        return docs
    def delete_record(self, rec_id, user_id):
        from bson import ObjectId
        res = self.records.delete_one({"_id": ObjectId(rec_id), "user_id":user_id})
        return res.deleted_count
    def clear_today(self, user_id):
        today = datetime.utcnow().date()
        res = self.records.delete_many({"user_id":user_id, "time": {"$gte": datetime(today.year,today.month,today.day)}})
        return res.deleted_count

# ---------- GitHub JSON Storage ----------
class GitHubStorage(Storage):
    """
    Stores everything in a single JSON file in a GitHub repo using the Contents API.
    Schema:
    {
      "targets": { user_id: {protein,fat,carbs} },
      "records": { user_id: [ {id, food, weight, protein, fat, carbs, time}, ... ] },
      "food_db": { food: {base_weight, protein, fat, carbs, category}, ... },
      "next_record_id": 1
    }
    """
    def __init__(self, token, repo, path):
        if not token or not repo or not path:
            raise Exception("GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_PATH required for GitHub storage")
        self.token = token
        self.repo = repo
        self.path = path
        self.api_base = "https://api.github.com"
        self.headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
        # Ensure file exists
        if not self._get_file():
            init = {"targets":{}, "records":{}, "food_db":{}, "next_record_id":1}
            self._save_file(init, "Initialize data file")

    def _get_file(self):
        url = f"{self.api_base}/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers=self.headers)
        if r.status_code == 200:
            return r.json()
        return None

    def _save_file(self, data, message="update"):
        # get current file to obtain sha if exists
        url = f"{self.api_base}/repos/{self.repo}/contents/{self.path}"
        content = json.dumps(data, ensure_ascii=False, indent=2)
        b64 = content.encode("utf-8")
        import base64
        payload = {"message": message, "content": base64.b64encode(b64).decode("utf-8")}
        current = self._get_file()
        if current:
            payload["sha"] = current["sha"]
        r = requests.put(url, headers=self.headers, json=payload)
        if r.status_code not in (200,201):
            raise Exception(f"GitHub save failed: {r.status_code} {r.text}")
        return r.json()

    def _read_state(self):
        f = self._get_file()
        if not f:
            return {"targets":{}, "records":{}, "food_db":{}, "next_record_id":1}
        import base64
        content = base64.b64decode(f["content"]).decode("utf-8")
        return json.loads(content)

    def _write_state(self, state):
        self._save_file(state, "update data")

    # high-level ops
    def set_target(self, user_id, p, f, c):
        state = self._read_state()
        state["targets"][user_id] = {"protein":p,"fat":f,"carbs":c}
        self._write_state(state)
    def get_target(self, user_id):
        state = self._read_state()
        return state["targets"].get(user_id)
    def add_food_db(self, food, base_weight, p, f, c, category="其他"):
        state = self._read_state()
        state["food_db"][food] = {"base_weight":base_weight,"protein":p,"fat":f,"carbs":c,"category":category}
        self._write_state(state)
    def get_food(self, food):
        state = self._read_state()
        return state["food_db"].get(food)
    def search_foods(self, keyword):
        state = self._read_state()
        out = []
        for k,v in state["food_db"].items():
            if keyword.lower() in k.lower():
                copy = dict(v); copy["food"]=k; out.append(copy)
        return out
    def list_foods(self):
        state = self._read_state()
        out = []
        for k,v in state["food_db"].items():
            copy = dict(v); copy["food"]=k; out.append(copy)
        return out
    def add_record(self, user_id, food, weight, p, f, c):
        state = self._read_state()
        rid = state.get("next_record_id",1)
        rec = {"id": rid, "food":food, "weight":weight, "protein":p, "fat":f, "carbs":c, "time": datetime.utcnow().isoformat()}
        state.setdefault("records", {}).setdefault(user_id, []).append(rec)
        state["next_record_id"] = rid + 1
        self._write_state(state)
        return rec
    def get_today_records(self, user_id):
        state = self._read_state()
        recs = state.get("records", {}).get(user_id, [])
        # filter by UTC date
        today = datetime.utcnow().date()
        out = []
        for r in recs:
            t = datetime.fromisoformat(r["time"])
            if t.date() == today:
                out.append(r)
        return out
    def delete_record(self, rec_id, user_id):
        state = self._read_state()
        recs = state.get("records", {}).get(user_id, [])
        new = [r for r in recs if r["id"] != rec_id]
        changed = len(recs) - len(new)
        state["records"][user_id] = new
        self._write_state(state)
        return changed
    def clear_today(self, user_id):
        state = self._read_state()
        recs = state.get("records", {}).get(user_id, [])
        today = datetime.utcnow().date()
        new = [r for r in recs if datetime.fromisoformat(r["time"]).date() != today]
        removed = len(recs) - len(new)
        state["records"][user_id] = new
        self._write_state(state)
        return removed

# Select storage
storage = None
if MONGO_URI:
    storage = MongoStorage(MONGO_URI)
    print("Using MongoDB storage")
elif GITHUB_TOKEN and GITHUB_REPO and GITHUB_DATA_PATH:
    storage = GitHubStorage(GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_PATH)
    print("Using GitHub storage")
else:
    # fallback: local in-memory (not persistent across restarts)
    class LocalStorage(GitHubStorage):  # reuse interface but keep local file
        def __init__(self):
            self.state = {"targets":{}, "records":{}, "food_db":{}, "next_record_id":1}
        def _read_state(self):
            return self.state
        def _write_state(self, state):
            self.state = state
        def set_target(self,user_id,p,f,c):
            state=self._read_state(); state["targets"][user_id]={"protein":p,"fat":f,"carbs":c}; self._write_state(state)
        def get_target(self,user_id): return self._read_state()["targets"].get(user_id)
        def add_food_db(self,food,base_weight,p,f,c,category="其他"):
            state=self._read_state(); state["food_db"][food]={"base_weight":base_weight,"protein":p,"fat":f,"carbs":c,"category":category}; self._write_state(state)
        def get_food(self,food): return self._read_state()["food_db"].get(food)
        def search_foods(self,keyword):
            state=self._read_state(); return [{"food":k, **v} for k,v in state["food_db"].items() if keyword.lower() in k.lower()]
        def list_foods(self): return [{"food":k, **v} for k,v in self._read_state()["food_db"].items()]
        def add_record(self,user_id,food,weight,p,f,c):
            state=self._read_state(); rid = state.get("next_record_id",1); rec={"id":rid,"food":food,"weight":weight,"protein":p,"fat":f,"carbs":c,"time":datetime.utcnow().isoformat()}; state.setdefault("records",{}).setdefault(user_id,[]).append(rec); state["next_record_id"]=rid+1; self._write_state(state); return rec
        def get_today_records(self,user_id):
            today=datetime.utcnow().date(); recs=self._read_state().get("records",{}).get(user_id,[]); return [r for r in recs if datetime.fromisoformat(r["time"]).date()==today]
        def delete_record(self,rec_id,user_id):
            state=self._read_state(); recs=state.get("records",{}).get(user_id,[]); new=[r for r in recs if r["id"]!=rec_id]; changed=len(recs)-len(new); state["records"][user_id]=new; self._write_state(state); return changed
        def clear_today(self,user_id):
            state=self._read_state(); recs=state.get("records",{}).get(user_id,[]); today=datetime.utcnow().date(); new=[r for r in recs if datetime.fromisoformat(r["time"]).date()!=today]; removed=len(recs)-len(new); state["records"][user_id]=new; self._write_state(state); return removed
    storage = LocalStorage()
    print("Using local (ephemeral) storage - not recommended")

# ---------- Utilities ----------
def emoji_progress(pct: float) -> str:
    """Return a simple emoji progress bar for percentage 0..100"""
    pct = max(0.0, min(100.0, pct))
    full_blocks = int(pct // 10)
    parts = "█" * full_blocks + "▁" * (10 - full_blocks)
    # use emoji color marker by percent
    if pct >= 100:
        emoji = "✅"
    elif pct >= 75:
        emoji = "🟢"
    elif pct >= 50:
        emoji = "🟡"
    elif pct >= 25:
        emoji = "🟠"
    else:
        emoji = "🔴"
    return f"{emoji} {parts} {pct:.0f}%"

def build_flex_today(records: List[Dict], target: Optional[Dict], base_url: str):
    """
    Build a Flex message with table of today's records, totals, progress bars, and an image link to chart
    """
    # totals
    total_p = sum([r["protein"] for r in records]) if records else 0
    total_f = sum([r["fat"] for r in records]) if records else 0
    total_c = sum([r["carbs"] for r in records]) if records else 0

    if target:
        tp = target.get("protein",0)
        tf = target.get("fat",0)
        tc = target.get("carbs",0)
    else:
        tp=tf=tc=0

    # progress emojis
    p_pct = (total_p/tp*100) if tp>0 else 0
    f_pct = (total_f/tf*100) if tf>0 else 0
    c_pct = (total_c/tc*100) if tc>0 else 0

    p_emoji = emoji_progress(p_pct)
    f_emoji = emoji_progress(f_pct)
    c_emoji = emoji_progress(c_pct)

    # chart URL
    chart_url = f"{base_url}chart?type=pie&user_id={{USER_ID}}"  # placeholder, will be replaced on send

    # Build Flex bubble
    header = {
        "type":"box","layout":"vertical","contents":[
            {"type":"text","text":"今日攝取紀錄","weight":"bold","size":"lg"}
        ]
    }
    # list foods (max 8 lines)
    body_contents = []
    for r in records[-8:]:
        name = r.get("food")
        w = r.get("weight")
        p = r.get("protein")
        f = r.get("fat")
        c = r.get("carbs")
        body_contents.append({
            "type":"box","layout":"baseline","contents":[
                {"type":"text","text":f"{name} {w}g","flex":3,"size":"sm"},
                {"type":"text","text":f"P:{p:.1f}","flex":1,"size":"sm","align":"end"},
                {"type":"text","text":f"F:{f:.1f}","flex":1,"size":"sm","align":"end"},
                {"type":"text","text":f"C:{c:.1f}","flex":1,"size":"sm","align":"end"}
            ]
        })
    if not body_contents:
        body_contents.append({"type":"text","text":"今天還沒紀錄任何食物","size":"sm"})

    totals_block = {
        "type":"box","layout":"vertical","contents":[
            {"type":"text","text":f"總計  P:{total_p:.1f}  F:{total_f:.1f}  C:{total_c:.1f}","size":"sm","weight":"bold"}
        ]
    }

    progress_block = {
        "type":"box","layout":"vertical","contents":[
            {"type":"text","text":"達成度","weight":"bold","size":"sm"},
            {"type":"text","text":f"蛋白質 {p_emoji}","size":"sm"},
            {"type":"text","text":f"脂肪 {f_emoji}","size":"sm"},
            {"type":"text","text":f"碳水 {c_emoji}","size":"sm"}
        ]
    }

    image_block = {
        "type":"image",
        "url": base_url.rstrip("/") + f"/chart?type=pie&user_id={{USER_ID}}",
        "size":"full",
        "aspectRatio":"4:3",
        "aspectMode":"cover"
    }

    bubble = {
      "type":"bubble",
      "hero": image_block,
      "body": {
        "type":"box",
        "layout":"vertical",
        "contents": [
            header,
            {"type":"separator","margin":"md"},
            {"type":"box","layout":"vertical","contents": body_contents, "spacing":"sm"},
            {"type":"separator","margin":"md"},
            totals_block,
            {"type":"separator","margin":"md"},
            progress_block
        ]
      }
    }
    flex = {"type":"carousel","contents":[bubble]}
    return flex

# ---------- Message parsing and commands ----------
def parse_text(user_id: str, text: str, request_base_url: str):
    """Main command parser"""
    text = text.strip()
    # set target: 目標 125 64 256
    if text.startswith("目標"):
        parts = text.split()
        if len(parts) != 4:
            return "設定目標格式：目標 蛋白質(g) 脂肪(g) 碳水(g)，例如：目標 125 64 256"
        try:
            _, p, f, c = parts
            p,f,c = float(p), float(f), float(c)
            storage.set_target(user_id, p, f, c)
            return f"已設定每日目標：蛋白質 {p}g / 脂肪 {f}g / 碳水 {c}g"
        except Exception as e:
            return "格式錯誤，請輸入數字。"

    # 新增食物資料庫: 新增 燕麥 37.5 4.9 3 25.3 類別
    if text.startswith("新增"):
        # 支援可選最後一項為類別
        parts = text.split()
        if len(parts) not in (6,7):
            return "新增格式：新增 食物名 基準重量(g) 蛋白質(g) 脂肪(g) 碳水(g) [類別]"
        _, food, weight, p, f, c = parts[:6]
        category = parts[6] if len(parts)==7 else "其他"
        try:
            weight = float(weight); p=float(p); f=float(f); c=float(c)
            storage.add_food_db(food, weight, p, f, c, category)
            return f"已新增食物：{food} ({category})，基準 {weight}g → P{p} F{f} C{c}"
        except Exception as e:
            return "新增格式錯誤，請確認數字格式。"

    # 查詢所有食物
    if text == "查食物":
        foods = storage.list_foods()
        if not foods:
            return "食物資料庫為空"
        lines = []
        for f in foods[:100]:
            name = f.get("food")
            base = f.get("base_weight")
            cat = f.get("category","")
            lines.append(f"{name} ({cat}) {base}g P:{f.get('protein')} F:{f.get('fat')} C:{f.get('carbs')}")
        return "\n".join(lines)

    # 搜尋食物: 搜尋 燕麥
    if text.startswith("搜尋"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            return "搜尋 指令格式：搜尋 關鍵字"
        kw = parts[1].strip()
        hits = storage.search_foods(kw)
        if not hits:
            return "找不到符合關鍵字的食物"
        lines = []
        for h in hits[:50]:
            lines.append(f"{h.get('food')} ({h.get('category','')}) {h.get('base_weight')}g P:{h.get('protein')} F:{h.get('fat')} C:{h.get('carbs')}")
        return "\n".join(lines)

    # 加入紀錄：格式 食物 重量 (若找不到食物, 回覆要先新增)
    parts = text.split()
    if len(parts)==2:
        food = parts[0]; 
        try:
            weight = float(parts[1])
        except:
            return "輸入格式錯誤：食物名稱 重量(g)，例如：雞胸 200"
        f = storage.get_food(food)
        if not f:
            return "找不到該食物於資料庫，請使用「新增」先加入資料庫"
        base = f["base_weight"]; p = f["protein"]; fat = f["fat"]; carb = f["carbs"]; cat = f.get("category","")
        factor = weight / base if base>0 else 0
        p_calc = p * factor; f_calc = fat * factor; c_calc = carb * factor
        rec = storage.add_record(user_id, food, weight, p_calc, f_calc, c_calc)
        return f"已加入紀錄：{food} {weight}g → P:{p_calc:.1f} F:{f_calc:.1f} C:{c_calc:.1f} （類別：{cat}）"

    # 刪除單筆: 刪除 3
    if text.startswith("刪除"):
        parts = text.split()
        if len(parts)!=2:
            return "刪除 指令格式：刪除 record_id（可於 '今日列表' 查看 id）"
        try:
            rec_id = int(parts[1])
            changed = storage.delete_record(rec_id, user_id)
            if changed:
                return f"已刪除紀錄 {rec_id}"
            else:
                return f"找不到紀錄 {rec_id}"
        except:
            return "刪除格式錯誤，請輸入數字 id"

    # 清除全部
    if text == "清除全部":
        removed = storage.clear_today(user_id)
        return f"已刪除 {removed} 筆今日紀錄"

    # 顯示今日列表（簡易文本）
    if text == "今日列表":
        recs = storage.get_today_records(user_id)
        if not recs:
            return "今天尚無紀錄"
        lines = []
        total_p=total_f=total_c=0
        for r in recs:
            lines.append(f"id:{r['id']} {r['food']} {r['weight']}g P:{r['protein']:.1f} F:{r['fat']:.1f} C:{r['carbs']:.1f}")
            total_p += r['protein']; total_f += r['fat']; total_c += r['carbs']
        lines.append(f"總計 P:{total_p:.1f} F:{total_f:.1f} C:{total_c:.1f}")
        return "\n".join(lines)

    # 顯示今日（Flex + 圖片） -> return special dict instructing to send Flex
    if text == "今日累計":
        recs = storage.get_today_records(user_id)
        target = storage.get_target(user_id)
        # Build flex payload; we will replace placeholder {USER_ID} with actual id before sending
        base_url = APP_URL if APP_URL else request_base_url
        flex = build_flex_today(recs, target, base_url)
        # Put a marker to indicate this should be sent as Flex
        return {"type":"flex", "flex": flex, "user_id": user_id}

    # fallback: help
    help_text = (
        "可用指令:\n"
        "目標 P F C  -> 設定每日目標，例如：目標 125 64 256\n"
        "新增 名稱 重量(g) P(g) F(g) C(g) [類別] -> 新增食物資料庫\n"
        "搜尋 關鍵字 -> 搜尋食物資料庫\n"
        "查食物 -> 顯示所有食物\n"
        "食物 重量 -> 記錄（食物需先在資料庫）例如：雞胸 200\n"
        "今日列表 -> 顯示今日紀錄 (含 id)\n"
        "今日累計 -> 顯示漂亮的 Flex 視覺（含圖表）\n"
        "刪除 id -> 刪除紀錄\n"
        "清除全部 -> 清除今日所有紀錄\n"
    )
    return help_text

# ---------- Chart endpoint ----------
@app.get("/chart")
def chart(type: str = "pie", user_id: Optional[str] = None):
    """
    Generate a chart image for user_id (pie or bar). If user_id missing returns error.
    Example: /chart?type=pie&user_id=xxxxx
    """
    if not user_id:
        return JSONResponse({"error":"user_id required"}, status_code=400)
    recs = storage.get_today_records(user_id)
    total_p = sum([r["protein"] for r in recs]) if recs else 0
    total_f = sum([r["fat"] for r in recs]) if recs else 0
    total_c = sum([r["carbs"] for r in recs]) if recs else 0

    labels = ["Protein","Fat","Carbs"]
    values = [max(0,total_p), max(0,total_f), max(0,total_c)]

    fig, ax = plt.subplots(figsize=(6,4))
    if type == "pie":
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
    else:
        ax.bar(labels, values)
        ax.set_ylabel("grams")
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

# ---------- LINE webhook ----------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body_bytes = await request.body()
    body = body_bytes.decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return Response(status_code=400, content="Invalid signature")
    return Response(status_code=200, content="OK")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    # parse
    # get base url for chart links
    base_url = APP_URL
    if not base_url:
        # try to build from request - we cannot access request here, so fallback to empty base;
        # but the build_flex_today expects base_url to be provided when generating the flex in parse_text
        # Instead, we'll use a placeholder and replace later when sending
        base_url = ""  # will be replaced when building final Flex (we have to construct absolute)
    result = parse_text(user_id, text, request_base_url=base_url)
    # If parse_text returns a special dict for flex
    if isinstance(result, dict) and result.get("type")=="flex":
        flex = result["flex"]
        uid = result["user_id"]
        # Replace placeholder {USER_ID} in image url with actual id
        # Attempt to build a base_url from APP_URL, else try to guess from LINE's domain is impossible here;
        # So we will use APP_URL if available. If not, the chart image may not be reachable externally.
        base = APP_URL if APP_URL else ""
        # replace image url inside flex
        flex_json = json.dumps(flex)
        flex_json = flex_json.replace("{USER_ID}", uid)
        flex = json.loads(flex_json)
        # send Flex
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="今日攝取", contents=flex))
        return
    # Normal text reply
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=str(result)))

# ---------- Run local helper ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
