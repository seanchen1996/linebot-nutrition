"""
app.py - LINE Bot nutrition tracker
Features:
 - Set daily target: 目標 P F C
 - Add food to DB: 新增 名稱 基準(g) P(g) F(g) C(g) [類別]
 - Search foods: 搜尋 關鍵字 / 找 關鍵字
 - List foods: 查食物 / 查詢食物 / list / 表單
 - Record food: 名稱 重量  (e.g. 燕麥 100)
 - Today's list: 今日列表 / 今日紀錄
 - Today's visual: 今日累計 / 今日 / 今日累積 / 今日 累積  -> Flex + chart
 - Delete: 刪除 <id>
 - Clear: 清除全部 / 清除今天
 - Help: help / 幫助
 - Storage: MongoDB (if MONGO_URI) else GitHub (needs GITHUB_TOKEN/GITHUB_REPO/GITHUB_DATA_PATH)
 - Chart generation: Pillow -> /chart endpoint
Note: If incoming message doesn't match any command, the bot will not reply.
"""

import os, json, io, math, base64, traceback
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, FlexSendMessage, MessageEvent, TextMessage
import requests
from PIL import Image, ImageDraw, ImageFont

# Optional pymongo
try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

app = FastAPI()

# env
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise Exception("Please set LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

MONGO_URI = os.getenv("MONGO_URI", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_DATA_PATH = os.getenv("GITHUB_DATA_PATH", "data/nutrition_db.json").strip()
APP_URL = os.getenv("APP_URL", "").strip()

# ---------- Storage implementations ----------
class StorageBase:
    def set_target(self, user_id, p, f, c): raise NotImplementedError
    def get_target(self, user_id): raise NotImplementedError
    def add_food_db(self, food, base_weight, p, f, c, category="其他"): raise NotImplementedError
    def get_food(self, food): raise NotImplementedError
    def search_foods(self, keyword): raise NotImplementedError
    def list_foods(self): raise NotImplementedError
    def add_record(self, user_id, food, weight, p, f, c): raise NotImplementedError
    def get_today_records(self, user_id): raise NotImplementedError
    def delete_record(self, rec_id, user_id): raise NotImplementedError
    def clear_today(self, user_id): raise NotImplementedError

# Mongo storage
class MongoStorage(StorageBase):
    def __init__(self, uri):
        if MongoClient is None:
            raise Exception("pymongo not installed")
        self.client = MongoClient(uri)
        self.db = self.client.get_database()
        self.targets = self.db["targets"]
        self.foods = self.db["food_db"]
        self.records = self.db["records"]

    def set_target(self, user_id, p, f, c):
        self.targets.update_one({"user_id":user_id},{"$set":{"protein":p,"fat":f,"carbs":c}}, upsert=True)
    def get_target(self, user_id):
        return self.targets.find_one({"user_id":user_id}) or None
    def add_food_db(self, food, base_weight, p, f, c, category="其他"):
        self.foods.update_one({"food":food},{"$set":{"base_weight":base_weight,"protein":p,"fat":f,"carbs":c,"category":category}}, upsert=True)
    def get_food(self, food):
        return self.foods.find_one({"food":food})
    def search_foods(self, keyword):
        import re
        regex = {"$regex": keyword, "$options":"i"}
        return list(self.foods.find({"food":regex}))
    def list_foods(self):
        return list(self.foods.find())
    def add_record(self, user_id, food, weight, p, f, c):
        rec = {"user_id":user_id,"food":food,"weight":weight,"protein":p,"fat":f,"carbs":c,"time":datetime.utcnow().isoformat()}
        r = self.records.insert_one(rec)
        rec["id"] = str(r.inserted_id)
        return rec
    def get_today_records(self, user_id):
        rows = list(self.records.find({"user_id":user_id}))
        today = datetime.utcnow().date()
        out = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r.get("time"))
            except:
                continue
            if t.date() == today:
                rec = {"id":str(r.get("_id")), "food":r.get("food"), "weight":r.get("weight"), "protein":r.get("protein"), "fat":r.get("fat"), "carbs":r.get("carbs")}
                out.append(rec)
        return out
    def delete_record(self, rec_id, user_id):
        from bson import ObjectId
        res = self.records.delete_one({"_id":ObjectId(rec_id), "user_id":user_id})
        return res.deleted_count
    def clear_today(self, user_id):
        rows = list(self.records.find({"user_id":user_id}))
        today = datetime.utcnow().date()
        removed = 0
        for r in rows:
            t = datetime.fromisoformat(r.get("time"))
            if t.date() == today:
                self.records.delete_one({"_id":r.get("_id")})
                removed += 1
        return removed

# GitHub JSON storage
class GitHubStorage(StorageBase):
    def __init__(self, token, repo, path):
        if not token or not repo or not path:
            raise Exception("GITHUB_TOKEN,GITHUB_REPO,GITHUB_DATA_PATH required")
        self.token = token
        self.repo = repo
        self.path = path
        self.base = "https://api.github.com"
        self.headers = {"Authorization": f"token {self.token}", "Accept":"application/vnd.github.v3+json"}
        if not self._exists():
            init = {"targets":{}, "food_db":{}, "records":{}, "next_record_id":1}
            self._save(init, "Initialize data file")

    def _get(self):
        url = f"{self.base}/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers=self.headers)
        if r.status_code == 200:
            return r.json()
        return None
    def _exists(self):
        return True if self._get() else False
    def _read_state(self):
        f = self._get()
        if not f:
            return {"targets":{}, "food_db":{}, "records":{}, "next_record_id":1}
        content = base64.b64decode(f["content"]).decode("utf-8")
        return json.loads(content)
    def _save(self, data, message="update"):
        url = f"{self.base}/repos/{self.repo}/contents/{self.path}"
        content = json.dumps(data, ensure_ascii=False, indent=2)
        payload = {"message":message, "content": base64.b64encode(content.encode("utf-8")).decode("utf-8")}
        cur = self._get()
        if cur:
            payload["sha"] = cur["sha"]
        r = requests.put(url, headers=self.headers, json=payload)
        if r.status_code not in (200,201):
            raise Exception(f"GitHub save failed: {r.status_code} {r.text}")
        return r.json()

    # methods
    def set_target(self, user_id, p, f, c):
        state = self._read_state()
        state["targets"][user_id] = {"protein":p,"fat":f,"carbs":c}
        self._save(state,"set target")
    def get_target(self, user_id):
        return self._read_state()["targets"].get(user_id)
    def add_food_db(self, food, base_weight, p, f, c, category="其他"):
        state = self._read_state()
        state["food_db"][food] = {"base_weight":base_weight,"protein":p,"fat":f,"carbs":c,"category":category}
        self._save(state,"add food")
    def get_food(self, food):
        return self._read_state()["food_db"].get(food)
    def search_foods(self, keyword):
        state = self._read_state()
        out=[]
        for k,v in state["food_db"].items():
            if keyword.lower() in k.lower():
                d = dict(v); d["food"]=k; out.append(d)
        return out
    def list_foods(self):
        state = self._read_state()
        out=[]
        for k,v in state["food_db"].items():
            d = dict(v); d["food"]=k; out.append(d)
        return out
    def add_record(self, user_id, food, weight, p, f, c):
        state = self._read_state()
        rid = state.get("next_record_id",1)
        rec = {"id":rid,"food":food,"weight":weight,"protein":p,"fat":f,"carbs":c,"time":datetime.utcnow().isoformat()}
        state.setdefault("records",{}).setdefault(user_id,[]).append(rec)
        state["next_record_id"]=rid+1
        self._save(state,"add record")
        return rec
    def get_today_records(self, user_id):
        state = self._read_state()
        recs = state.get("records",{}).get(user_id,[])
        out=[]
        today = datetime.utcnow().date()
        for r in recs:
            t = datetime.fromisoformat(r["time"])
            if t.date() == today:
                out.append(r)
        return out
    def delete_record(self, rec_id, user_id):
        state = self._read_state()
        recs = state.get("records",{}).get(user_id,[])
        new = [r for r in recs if r["id"] != rec_id]
        changed = len(recs)-len(new)
        state["records"][user_id] = new
        self._save(state,"delete record")
        return changed
    def clear_today(self, user_id):
        state = self._read_state()
        recs = state.get("records",{}).get(user_id,[])
        today = datetime.utcnow().date()
        new = [r for r in recs if datetime.fromisoformat(r["time"]).date() != today]
        removed = len(recs) - len(new)
        state["records"][user_id] = new
        self._save(state,"clear today")
        return removed

# choose storage
if MONGO_URI:
    storage = MongoStorage(MONGO_URI)
    print("Using MongoDB storage")
elif GITHUB_TOKEN and GITHUB_REPO and GITHUB_DATA_PATH:
    storage = GitHubStorage(GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_PATH)
    print("Using GitHub storage")
else:
    # fallback ephemeral (not persistent)
    print("Using ephemeral local storage (not persistent)")
    class Local(StorageBase):
        def __init__(self):
            self.state = {"targets":{}, "food_db":{}, "records":{}, "next_record_id":1}
        def set_target(self,user_id,p,f,c):
            self.state["targets"][user_id]={"protein":p,"fat":f,"carbs":c}
        def get_target(self,user_id):
            return self.state["targets"].get(user_id)
        def add_food_db(self,food,base_weight,p,f,c,category="其他"):
            self.state["food_db"][food]={"base_weight":base_weight,"protein":p,"fat":f,"carbs":c,"category":category}
        def get_food(self,food): return self.state["food_db"].get(food)
        def search_foods(self,keyword):
            return [ {"food":k, **v} for k,v in self.state["food_db"].items() if keyword.lower() in k.lower() ]
        def list_foods(self):
            return [ {"food":k, **v} for k,v in self.state["food_db"].items() ]
        def add_record(self,user_id,food,weight,p,f,c):
            rid = self.state.get("next_record_id",1); rec={"id":rid,"food":food,"weight":weight,"protein":p,"fat":f,"carbs":c,"time":datetime.utcnow().isoformat()}
            self.state.setdefault("records",{}).setdefault(user_id,[]).append(rec); self.state["next_record_id"]=rid+1; return rec
        def get_today_records(self,user_id):
            out=[]; today=datetime.utcnow().date()
            for r in self.state.get("records",{}).get(user_id,[]):
                if datetime.fromisoformat(r["time"]).date() == today: out.append(r)
            return out
        def delete_record(self,rec_id,user_id):
            recs=self.state.get("records",{}).get(user_id,[]); new=[r for r in recs if r["id"]!=rec_id]; changed=len(recs)-len(new); self.state["records"][user_id]=new; return changed
        def clear_today(self,user_id):
            recs=self.state.get("records",{}).get(user_id,[]); today=datetime.utcnow().date(); new=[r for r in recs if datetime.fromisoformat(r["time"]).date()!=today]; removed=len(recs)-len(new); self.state["records"][user_id]=new; return removed
    storage = Local()

# ---------- Helpers ----------
def safe_float(s):
    try:
        return float(s)
    except:
        return None

def emoji_progress(pct):
    pct = max(0.0, min(100.0, pct))
    full = int(pct // 10)
    bar = "█" * full + "▁" * (10-full)
    if pct >= 100: emo="✅"
    elif pct >= 75: emo="🟢"
    elif pct >=50: emo="🟡"
    elif pct >=25: emo="🟠"
    else: emo="🔴"
    return f"{emo} {bar} {pct:.0f}%"

def build_flex_payload(records, target, base_url, user_id):
    total_p = sum(r["protein"] for r in records) if records else 0
    total_f = sum(r["fat"] for r in records) if records else 0
    total_c = sum(r["carbs"] for r in records) if records else 0
    tp = target.get("protein",0) if target else 0
    tf = target.get("fat",0) if target else 0
    tc = target.get("carbs",0) if target else 0
    p_pct = (total_p/tp*100) if tp>0 else 0
    f_pct = (total_f/tf*100) if tf>0 else 0
    c_pct = (total_c/tc*100) if tc>0 else 0
    p_bar = emoji_progress(p_pct); f_bar=emoji_progress(f_pct); c_bar=emoji_progress(c_pct)
    # image url
    base = base_url.rstrip("/") if base_url else ""
    chart_url = f"{base}/chart?type=pie&user_id={user_id}"
    # prepare body lines
    body_lines = []
    if records:
        for r in records[-8:]:
            body_lines.append({
                "type":"box","layout":"baseline","contents":[
                    {"type":"text","text":f"{r['food']} {r['weight']}g","flex":4,"size":"sm"},
                    {"type":"text","text":f"P:{r['protein']:.1f}","flex":1,"size":"sm","align":"end"},
                    {"type":"text","text":f"F:{r['fat']:.1f}","flex":1,"size":"sm","align":"end"},
                    {"type":"text","text":f"C:{r['carbs']:.1f}","flex":1,"size":"sm","align":"end"}
                ]
            })
    else:
        body_lines.append({"type":"text","text":"今天沒有紀錄","size":"sm"})
    bubble = {
        "type":"bubble",
        "hero":{"type":"image","url":chart_url,"size":"full","aspectRatio":"4:3","aspectMode":"cover"},
        "body":{
            "type":"box","layout":"vertical","contents":[
                {"type":"text","text":"今日攝取紀錄","weight":"bold","size":"lg"},
                {"type":"separator","margin":"md"},
                {"type":"box","layout":"vertical","contents":body_lines,"spacing":"sm"},
                {"type":"separator","margin":"md"},
                {"type":"text","text":f"總計  P:{total_p:.1f}g  F:{total_f:.1f}g  C:{total_c:.1f}g","size":"sm","weight":"bold"},
                {"type":"separator","margin":"md"},
                {"type":"text","text":"達成度","weight":"bold","size":"sm"},
                {"type":"text","text":f"蛋白質 {p_bar}","size":"sm"},
                {"type":"text","text":f"脂肪 {f_bar}","size":"sm"},
                {"type":"text","text":f"碳水 {c_bar}","size":"sm"},
            ]
        }
    }
    return {"type":"carousel","contents":[bubble]}

# ---------- Chart generation using Pillow ----------
def generate_chart_png(type_, user_id):
    recs = storage.get_today_records(user_id)
    total_p = sum(r["protein"] for r in recs) if recs else 0
    total_f = sum(r["fat"] for r in recs) if recs else 0
    total_c = sum(r["carbs"] for r in recs) if recs else 0
    labels = ["Protein","Fat","Carbs"]
    values = [max(0,total_p), max(0,total_f), max(0,total_c)]
    # canvas
    W, H = 800, 600
    img = Image.new("RGB",(W,H),(255,255,255))
    draw = ImageDraw.Draw(img)
    # fonts (use default)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 16)
        font_b = ImageFont.truetype("DejaVuSans.ttf", 20)
    except:
        font = ImageFont.load_default()
        font_b = ImageFont.load_default()
    # draw title
    draw.text((20,10),"今日營養分布", font=font_b, fill=(0,0,0))
    # draw pie
    total = sum(values)
    if total <= 0:
        draw.text((20,60),"今天尚無紀錄", font=font, fill=(80,80,80))
    else:
        # draw pie at left
        cx, cy, r = 260, 320, 160
        start = 0.0
        colors = [(66,133,244),(219,68,55),(244,180,0)]
        for i,v in enumerate(values):
            if v<=0: continue
            angle = 360.0 * v / total
            draw.pieslice([cx-r,cy-r,cx+r,cy+r], start, start+angle, fill=colors[i])
            start += angle
        # legend
        lx = 520; ly = 120; dy = 40
        for i,(lab,v) in enumerate(zip(labels,values)):
            draw.rectangle([lx,ly+i*dy,lx+20,ly+14+i*dy], fill=colors[i])
            draw.text((lx+30, ly+i*dy), f"{lab}: {v:.1f} g", font=font, fill=(0,0,0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# ---------- Command parser ----------
def parse_command(user_id, text, base_url):
    t = text.strip()
    if not t:
        return None
    # HELP
    if t.lower() in ("help","幫助"):
        help_text = (
            "指令總覽：\n"
            "目標 P F C -> 設定每日目標 (g)\n"
            "新增 名稱 基準(g) P(g) F(g) C(g) [類別] -> 新增食物到資料庫\n"
            "搜尋 關鍵字 / 找 關鍵字 -> 搜尋食物資料庫\n"
            "查食物 / 查詢食物 / list / 表單 -> 列出食物資料庫\n"
            "食物 重量 -> 記錄（食物需先新增） 例：燕麥 100\n"
            "今日列表 / 今日紀錄 -> 顯示今日文字清單\n"
            "今日累計 / 今日 / 今日累積 -> 顯示 Flex 視覺 + 圖表\n"
            "刪除 id -> 刪除紀錄\n"
            "清除全部 / 清除今天 -> 清除今日紀錄\n"
        )
        return {"type":"text","text":help_text}

    # 目標
    if t.startswith("目標"):
        parts = t.split()
        if len(parts) != 4:
            return {"type":"text","text":"目標格式：目標 蛋白質(g) 脂肪(g) 碳水(g) 例如：目標 125 64 256"}
        p = safe_float(parts[1]); f = safe_float(parts[2]); c = safe_float(parts[3])
        if p is None or f is None or c is None:
            return {"type":"text","text":"目標格式錯誤，請輸入數字"}
        storage.set_target(user_id, p, f, c)
        return {"type":"text","text":f"已設定每日目標：P:{p}g F:{f}g C:{c}g"}

    # 新增
    if t.startswith("新增"):
        parts = t.split()
        if len(parts) < 6:
            return {"type":"text","text":"新增格式：新增 名稱 基準(g) P(g) F(g) C(g) [類別]"}
        # parts[1]=name, 2=weight,3=p,4=f,5=c, 6=category optional
        name = parts[1]
        w = safe_float(parts[2]); p = safe_float(parts[3]); f = safe_float(parts[4]); c = safe_float(parts[5])
        cat = parts[6] if len(parts)>=7 else "其他"
        if None in (w,p,f,c):
            return {"type":"text","text":"新增格式錯誤：基準/營養需為數字"}
        try:
            storage.add_food_db(name, w, p, f, c, cat)
            return {"type":"text","text":f"已新增食物：{name}，基準{w}g → P{p} F{f} C{c} (類別:{cat})"}
        except Exception as e:
            return {"type":"text","text":"新增失敗：" + str(e)}

    # 搜尋 (搜尋 關鍵字) 或 (找 關鍵字)
    if t.startswith("搜尋 ") or t.startswith("找 "):
        kw = t.split(maxsplit=1)[1].strip()
        hits = storage.search_foods(kw)
        if not hits:
            return {"type":"text","text":"找不到符合的食物"}
        lines=[]
        for h in hits[:50]:
            lines.append(f"{h.get('food')} ({h.get('category','')}) {h.get('base_weight')}g P:{h.get('protein')} F:{h.get('fat')} C:{h.get('carbs')}")
        return {"type":"text","text":"搜尋結果：\n" + "\n".join(lines)}

    # list / 查食物 variants
    if t in ("查食物","查詢食物","list","表單"):
        foods = storage.list_foods()
        if not foods:
            return {"type":"text","text":"食物資料庫為空"}
        lines=[]
        for f in foods[:200]:
            lines.append(f"{f.get('food')} ({f.get('category','')}) {f.get('base_weight')}g P:{f.get('protein')} F:{f.get('fat')} C:{f.get('carbs')}")
        return {"type":"text","text":"食物資料庫：\n" + "\n".join(lines)}

    # 刪除
    if t.startswith("刪除"):
        parts = t.split()
        if len(parts)!=2:
            return {"type":"text","text":"刪除 指令格式：刪除 id"}
        try:
            rid = int(parts[1])
            changed = storage.delete_record(rid, user_id)
            return {"type":"text","text":("已刪除紀錄 "+str(rid)) if changed else ("找不到紀錄 "+str(rid))}
        except:
            return {"type":"text","text":"刪除格式錯誤，id 必須為數字"}

    # 清除全部
    if t in ("清除全部","清除今天"):
        removed = storage.clear_today(user_id)
        return {"type":"text","text":f"已刪除 {removed} 筆今日紀錄"}

    # 今日列表（文字）
    if t in ("今日列表","今日紀錄"):
        recs = storage.get_today_records(user_id)
        if not recs:
            return {"type":"text","text":"今天尚無紀錄"}
        lines=[]
        totp=totf=totc=0
        for r in recs:
            lines.append(f"id:{r['id']} {r['food']} {r['weight']}g P:{r['protein']:.1f} F:{r['fat']:.1f} C:{r['carbs']:.1f}")
            totp += r['protein']; totf += r['fat']; totc += r['carbs']
        lines.append(f"總計 P:{totp:.1f} F:{totf:.1f} C:{totc:.1f}")
        return {"type":"text","text":"\n".join(lines)}

    # 今日漂亮視覺（Flex）
    if t in ("今日累計","今日","今日累積","今日 累積"):
        recs = storage.get_today_records(user_id)
        target = storage.get_target(user_id) or {}
        base = APP_URL if APP_URL else ""
        flex = build_flex_payload(recs, target, base, user_id)
        return {"type":"flex","flex":flex, "user_id":user_id}

    # Record food: 格式 名稱 重量 (兩段)
    parts = t.split()
    if len(parts)==2:
        name = parts[0]; wt = safe_float(parts[1])
        if wt is None:
            return None  # 不回覆 (符合要求)
        f = storage.get_food(name)
        if not f:
            return {"type":"text","text":"找不到該食物，請先使用「新增 名稱 基準(g) P F C」加入資料庫"}
        base = f["base_weight"]; p = f["protein"]; fat = f["fat"]; carb = f["carbs"]
        factor = wt / base if base>0 else 0
        p_calc = p*factor; f_calc=fat*factor; c_calc=carb*factor
        rec = storage.add_record(user_id, name, wt, p_calc, f_calc, c_calc)
        return {"type":"text","text":f"已加入紀錄：{name} {wt}g → P:{p_calc:.1f} F:{f_calc:.1f} C:{c_calc:.1f}"}

    # else: not matched -> do not reply
    return None

# ---------- Chart endpoint ----------
@app.get("/chart")
def chart(type: str = "pie", user_id: Optional[str] = None):
    if not user_id:
        return JSONResponse({"error":"user_id required"}, status_code=400)
    buf = generate_chart_png(type, user_id)
    return StreamingResponse(buf, media_type="image/png")

# ---------- LINE webhook ----------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")
    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        return Response(status_code=400, content="Invalid signature")
    return Response(status_code=200, content="OK")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_id = event.source.user_id
        text = event.message.text.strip()
        # parse
        base = APP_URL if APP_URL else ""
        result = parse_command(user_id, text, base)
        # If result is None -> do not reply (user asked for silence)
        if result is None:
            return
        # handle types
        if result.get("type") == "text":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result["text"]))
            return
        if result.get("type") == "flex":
            flex = result["flex"]
            # send flex
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="今日攝取", contents=flex))
            return
        # fallback
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=str(result)))
    except Exception as e:
        # safe fallback error reply
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="發生錯誤，請稍後再試"))
        except:
            pass
        traceback.print_exc()

# ---------- run local ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT",8000)), reload=True)
