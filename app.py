import json
import os
from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.models import TextSendMessage

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DB_FILE = "nutrition_db.json"

# ---------- Storage ----------
class Storage:
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        if not os.path.exists(self.db_file):
            with open(self.db_file, "w") as f:
                json.dump({"food_db": {}, "records": {}, "targets": {}}, f)

    def _read_state(self):
        with open(self.db_file, "r") as f:
            return json.load(f)

    def _write_state(self, state):
        with open(self.db_file, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def list_foods(self):
        state = self._read_state()
        return list(state.get("food_db", {}).keys())

    def add_food(self, name, cal, protein, fat, carb):
        state = self._read_state()
        state.setdefault("food_db", {})[name] = {
            "cal": cal, "protein": protein, "fat": fat, "carb": carb
        }
        self._write_state(state)

    def get_food(self, name):
        state = self._read_state()
        return state.get("food_db", {}).get(name)

    def add_record(self, user_id, name, amount):
        state = self._read_state()
        state.setdefault("records", {}).setdefault(user_id, []).append({
            "food": name, "amount": amount
        })
        self._write_state(state)

    def get_records(self, user_id):
        state = self._read_state()
        return state.get("records", {}).get(user_id, [])

    def clear_records(self, user_id):
        state = self._read_state()
        state.get("records", {}).pop(user_id, None)
        self._write_state(state)

    def clear_record_item(self, user_id, food_name):
        state = self._read_state()
        records = state.get("records", {}).get(user_id, [])
        state["records"][user_id] = [r for r in records if r["food"] != food_name]
        self._write_state(state)

    def set_target(self, user_id, cal, protein, fat, carb):
        state = self._read_state()
        state.setdefault("targets", {})[user_id] = {
            "cal": cal, "protein": protein, "fat": fat, "carb": carb
        }
        self._write_state(state)

    def get_target(self, user_id):
        state = self._read_state()
        return state.get("targets", {}).get(user_id)

storage = Storage()

# ---------- LINE Webhook ----------
@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    try:
        handler.handle(body.decode(), signature)
    except Exception as e:
        print(e)
    return "OK"

@handler.add("message")
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    response_text = parse_text(user_id, text)
    if response_text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=response_text))

# ---------- Text Parser ----------
def parse_text(user_id, text):
    try:
        words = text.split()
        if not words:
            return None

        if words[0] in ["新增"]:
            if len(words) != 6:
                return "新增格式錯誤，請輸入：新增 食物名 cal protein fat carb"
            name = words[1]
            cal, protein, fat, carb = map(float, words[2:])
            storage.add_food(name, cal, protein, fat, carb)
            return f"已新增 {name} {cal} {protein} {fat} {carb}"

        if words[0] in ["列表", "list", "查食物", "食物表"]:
            foods = storage.list_foods()
            return "資料庫食物:\n" + "\n".join(foods) if foods else "資料庫空的"

        if words[0] in ["今日", "累計", "今日累計", "今日統計", "今日累積"]:
            records = storage.get_records(user_id)
            if not records:
                return "今日尚無紀錄"
            msg = "今日紀錄:\n"
            for r in records:
                msg += f"{r['food']} {r['amount']}\n"
            return msg

        if words[0] in ["清除今日"]:
            storage.clear_records(user_id)
            return "已清除今日所有紀錄"

        if words[0] in ["清除"]:
            if len(words) != 2:
                return "請輸入：清除 食物名"
            storage.clear_record_item(user_id, words[1])
            return f"已清除 {words[1]}"

        if words[0] in ["目標"]:
            if len(words) != 5:
                return "目標格式錯誤，請輸入數字：目標 cal protein fat carb"
            cal, protein, fat, carb = map(float, words[1:])
            storage.set_target(user_id, cal, protein, fat, carb)
            return f"已設定目標：{cal} {protein} {fat} {carb}"

        if words[0] in ["help", "幫助"]:
            return (
                "可用指令：\n"
                "新增 食物名 cal protein fat carb\n"
                "列表 / list / 查食物 / 食物表\n"
                "今日 / 累計 / 今日累計\n"
                "清除今日\n"
                "清除 食物名\n"
                "目標 cal protein fat carb\n"
            )

        # 如果是食物名稱 + 數量，自動計算比例
        if len(words) == 2 and words[0] in storage.list_foods():
            try:
                amount = float(words[1])
            except:
                return "請輸入數字"
            food = storage.get_food(words[0])
            scaled = {k: round(v * amount / 100, 2) for k, v in food.items()}
            return f"{words[0]} {amount}g: {scaled}"
        
    except Exception as e:
        print("parse_text error:", e)
        return "發生錯誤"

    return None
