import os
import json
from datetime import datetime
from fastapi import FastAPI, Request, Response
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
import requests
import base64

app = FastAPI()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_DATA_PATH = os.getenv("GITHUB_DATA_PATH", "data.json")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# =========================================================
# GitHub Storage
# =========================================================
class GitHubStorage:
    def __init__(self, token, repo, path):
        self.token = token
        self.repo = repo
        self.path = path
        self.headers = {"Authorization": f"token {token}"}

        if not self._get_file():
            initial = {
                "nutrition_db": {},
                "records": {},
                "targets": {},
                "next_id": 1
            }
            self._save_file(initial, "init")

    def _get_file(self):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers=self.headers)
        if r.status_code == 200:
            return r.json()
        return None

    def _save_file(self, data, msg="update"):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"

        encoded = base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode()
        ).decode()

        payload = {"message": msg, "content": encoded}

        current = self._get_file()
        if current:
            payload["sha"] = current["sha"]

        r = requests.put(url, headers=self.headers, json=payload)
        if r.status_code not in (200, 201):
            raise Exception(f"GitHub save failed {r.status_code}")

    def _read_state(self):
        f = self._get_file()
        content = base64.b64decode(f["content"]).decode()
        return json.loads(content)

    def _write_state(self, state):
        self._save_file(state)

    # ===== Targets =====
    def set_target(self, user_id, p, f, c):
        state = self._read_state()
        state["targets"][user_id] = {"protein": p, "fat": f, "carbs": c}
        self._write_state(state)

    def get_target(self, user_id):
        return self._read_state()["targets"].get(user_id)

    # ===== Nutrition DB =====
    def add_food_db(self, food, base, p, fat, carbs, category="其他"):
        state = self._read_state()
        state["nutrition_db"][food] = {
            "base": base,
            "protein": p,
            "fat": fat,
            "carbs": carbs,
            "category": category
        }
        self._write_state(state)

    def get_food(self, food):
        return self._read_state()["nutrition_db"].get(food)

    def list_foods(self):
        state = self._read_state()
        return [{**v, "food": k} for k, v in state["nutrition_db"].items()]

    # ===== Daily records =====
    def add_record(self, user_id, food, weight, p, fat, carbs):
        state = self._read_state()
        rid = state.get("next_id", 1)

        rec = {
            "id": rid,
            "food": food,
            "weight": weight,
            "protein": p,
            "fat": fat,
            "carbs": carbs,
            "time": datetime.utcnow().isoformat()
        }

        state.setdefault("records", {}).setdefault(user_id, []).append(rec)
        state["next_id"] = rid + 1
        self._write_state(state)

        return rec

    def get_today_records(self, user_id):
        state = self._read_state()
        recs = state.get("records", {}).get(user_id, [])
        today = datetime.utcnow().date()

        return [
            r for r in recs
            if datetime.fromisoformat(r["time"]).date() == today
        ]

    def delete_record(self, user_id, rec_id):
        state = self._read_state()
        recs = state.get("records", {}).get(user_id, [])
        new_list = [r for r in recs if r["id"] != rec_id]

        removed = len(recs) - len(new_list)
        state["records"][user_id] = new_list
        self._write_state(state)
        return removed

    def clear_today(self, user_id):
        state = self._read_state()
        recs = state.get("records", {}).get(user_id, [])
        today = datetime.utcnow().date()

        new_list = [r for r in recs if datetime.fromisoformat(r["time"]).date() != today]
        removed = len(recs) - len(new_list)

        state["records"][user_id] = new_list
        self._write_state(state)

        return removed


storage = GitHubStorage(GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_PATH)

# =========================================================
# Utility
# =========================================================
def emoji_progress(pct):
    pct = max(0, min(100, pct))
    filled = int(pct // 10)
    bar = "█" * filled + "▁" * (10 - filled)

    if pct >= 100:
        e = "✅"
    elif pct >= 75:
        e = "🟢"
    elif pct >= 50:
        e = "🟡"
    elif pct >= 25:
        e = "🟠"
    else:
        e = "🔴"

    return f"{e} {bar} {pct:.0f}%"


# =========================================================
# Command Parser
# =========================================================
def parse_text(user_id, text):
    text = text.strip()
    text = text.lower()

    # === 設目標 ===
    if text.startswith("目標"):
        parts = text.split()
        if len(parts) != 4:
            return "格式：目標 蛋白質 脂肪 碳水"

        try:
            p, f, c = map(float, parts[1:])
        except:
            return "數字格式錯誤"

        storage.set_target(user_id, p, f, c)
        return f"已設定目標：P{p} F{f} C{c}"

    # === 新增食物 ===
    if text.startswith("新增"):
        parts = text.split()
        if len(parts) < 6:
            return "格式：新增 名稱 基準量 蛋白質 脂肪 碳水 [類別]"

        food, base, p, fat, carbs = parts[1:6]
        category = parts[6] if len(parts) >= 7 else "其他"

        try:
            storage.add_food_db(food, float(base), float(p), float(fat), float(carbs), category)
        except:
            return "新增格式錯誤"

        return f"已新增：{food} ({category})"

    # === 查詢食物庫 ===
    if text in ["list", "列表", "資料庫", "食物庫"]:
        items = storage.list_foods()
        if not items:
            return "目前食物庫是空的"

        out = []
        for f in items:
            out.append(f"{f['food']} ({f['category']}) {f['base']}g P:{f['protein']} F:{f['fat']} C:{f['carbs']}")
        return "\n".join(out)

    # === 今日紀錄統計 ===
    if text in ["今日", "今日累計", "今日攝取", "今日累積"]:
        recs = storage.get_today_records(user_id)
        target = storage.get_target(user_id)

        total_p = sum(r["protein"] for r in recs)
        total_f = sum(r["fat"] for r in recs)
        total_c = sum(r["carbs"] for r in recs)

        if target:
            t_p = target["protein"]
            t_f = target["fat"]
            t_c = target["carbs"]
        else:
            t_p = t_f = t_c = 100

        out = f"📅 今日 {datetime.utcnow().date()}\n\n"
        for r in recs:
            out += f"{r['id']}. {r['food']} {r['weight']}g  P:{r['protein']:.1f} F:{r['fat']:.1f} C:{r['carbs']:.1f}\n"

        out += "\n=== 總計 ===\n"
        out += f"P: {total_p:.1f}/{t_p}  {emoji_progress(total_p/t_p*100)}\n"
        out += f"F: {total_f:.1f}/{t_f}  {emoji_progress(total_f/t_f*100)}\n"
        out += f"C: {total_c:.1f}/{t_c}  {emoji_progress(total_c/t_c*100)}\n"

        return out

    # === 刪除紀錄 ===
    if text.startswith("刪除"):
        parts = text.split()

        # 刪除指定編號
        if len(parts) == 2 and parts[1].isdigit():
            rid = int(parts[1])
            removed = storage.delete_record(user_id, rid)
            return f"已刪除 {removed} 筆" if removed else "找不到紀錄"

        # 刪除今日
        if text in ["刪除今日", "清除今日", "清除全部"]:
            removed = storage.clear_today(user_id)
            return f"已清除今日 {removed} 筆紀錄"
            
    # === 直接加入食物到今日===
    if text.startswith("加入"):
        parts = text.split()
        if len(parts) < 4:
            return "格式：加入 名稱 蛋白質 脂肪 碳水"

        food, p, fat, carbs = parts[1:4]
        storage.add_record(user_id, food,  1.0, p, fat, carbs)

        return f"已記錄：{food} {weight}g\nP:{p:.1f} F:{fat:.1f} C:{carbs:.1f}"
        
    # === 普通吃食物：食物 重量 ===
    parts = text.split()
    if len(parts) == 2:
        food, val = parts
        try:
            weight = float(val)
        except:
            return "格式錯誤，請輸入：食物 重量"

        f = storage.get_food(food)
        if not f:
            return f"{food} 不在資料庫，請先新增"

        factor = weight / f["base"]
        p = f["protein"] * factor
        fat = f["fat"] * factor
        c = f["carbs"] * factor

        storage.add_record(user_id, food, weight, p, fat, c)

        return f"已記錄：{food} {weight}g\nP:{p:.1f} F:{fat:.1f} C:{c:.1f}"

    
    # === Help ===
    return (
        "📘 指令列表\n"
        "目標 P F C\n"
        "加入 食物 P F C\n"
        "新增 名稱 基準量 P F C [類別]\n"
        "list / 列表\n"
        "食物 重量\n"
        "今日 / 今日累計 / 今日攝取 / 今日累積\n"
        "刪除 編號\n"
        "刪除今日\n"
    )


# =========================================================
# LINE Webhook
# =========================================================
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        handler.handle(body_str, signature)
    except InvalidSignatureError:
        return Response(status_code=400)

    return Response(status_code=200)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    uid = event.source.user_id
    text = event.message.text
    res = parse_text(uid, text)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=res)
    )

# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True
    )
