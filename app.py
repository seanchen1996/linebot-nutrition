import json
import os
from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.models import TextSendMessage, MessageEvent, TextMessage, FlexSendMessage

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

    # --- Food DB ---
    def list_foods(self):
        state = self._read_state()
        return state.get("food_db", {})

    def add_food(self, name, portion, protein, fat, carb):
        state = self._read_state()
        state.setdefault("food_db", {})[name] = {
            "portion": portion,
            "protein": protein,
            "fat": fat,
            "carb": carb
        }
        self._write_state(state)

    def get_food(self, name):
        state = self._read_state()
        return state.get("food_db", {}).get(name)

    # --- Records ---
    def add_record(self, user_id, name, grams):
        state = self._read_state()
        state.setdefault("records", {}).setdefault(user_id, [])
        # 如果同食物已存在，累加
        for r in state["records"][user_id]:
            if r["food"] == name:
                r["grams"] += grams
                break
        else:
            state["records"][user_id].append({"food": name, "grams": grams})
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

    # --- Target ---
    def set_target(self, user_id, protein, fat, carb):
        state = self._read_state()
        state.setdefault("targets", {})[user_id] = {"protein": protein, "fat": fat, "carb": carb}
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
        print("LINE handler error:", e)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    msg = parse_text(user_id, text)
    if msg:
        if isinstance(msg, FlexSendMessage):
            line_bot_api.reply_message(event.reply_token, msg)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

# ---------- Text Parser ----------
def parse_text(user_id, text):
    try:
        words = text.split()
        if not words:
            return None

        # --- 新增食物: 新增 名稱 份量 P F C ---
        if words[0] in ["新增"]:
            if len(words) != 6:
                return "新增格式錯誤：新增 食物名 份量(protion) P F C"
            name = words[1]
            portion, protein, fat, carb = map(float, words[2:])
            storage.add_food(name, portion, protein, fat, carb)
            return f"已新增 {name} 每份{portion}g P:{protein} F:{fat} C:{carb}"

        # --- 列出資料庫 ---
        if words[0] in ["列表", "list", "查食物", "食物表"]:
            foods = storage.list_foods()
            if not foods:
                return "資料庫空的"
            lines = []
            for name, nut in foods.items():
                lines.append(f"{name} {nut['portion']}g P:{nut['protein']} F:{nut['fat']} C:{nut['carb']}")
            return "\n".join(lines)

        # --- 今日紀錄 ---
        if words[0] in ["今日", "累計", "今日累計", "今日統計", "今日累積"]:
            records = storage.get_records(user_id)
            if not records:
                return "今日尚無紀錄"
            return flex_today_records(user_id, records)

        # --- 清除今日 ---
        if words[0] in ["清除今日"]:
            storage.clear_records(user_id)
            return "已清除今日所有紀錄"

        # --- 清除某筆 ---
        if words[0] in ["清除"]:
            if len(words) != 2:
                return "請輸入：清除 食物名"
            storage.clear_record_item(user_id, words[1])
            return f"已清除 {words[1]}"

        # --- 設定目標 ---
        if words[0] in ["目標"]:
            if len(words) != 4:
                return "目標格式錯誤：目標 P F C"
            protein, fat, carb = map(float, words[1:])
            storage.set_target(user_id, protein, fat, carb)
            return f"已設定目標 P:{protein} F:{fat} C:{carb}"

        # --- help ---
        if words[0] in ["help", "幫助"]:
            return (
                "可用指令：\n"
                "新增 食物名 份量 P F C\n"
                "列表 / list / 查食物 / 食物表\n"
                "今日 / 累計 / 今日累計\n"
                "清除今日\n"
                "清除 食物名\n"
                "目標 P F C\n"
            )

        # --- 記錄食物 ---
        if len(words) == 2:
            food_name = words[0]
            if food_name in storage.list_foods():
                try:
                    grams = float(words[1])
                except:
                    return "請輸入數字"
                storage.add_record(user_id, food_name, grams)
                return f"{food_name} {grams}g 已加入今日紀錄"

    except Exception as e:
        print("parse_text error:", e)
        return "發生錯誤"

    return None

# ---------- Flex Message Table ----------
def flex_today_records(user_id, records):
    foods = storage.list_foods()
    target = storage.get_target(user_id) or {"protein": 100, "fat": 100, "carb": 100}

    total = {"protein": 0, "fat": 0, "carb": 0}
    lines = []
    for r in records:
        f = foods.get(r["food"])
        if not f:
            continue
        factor = r["grams"] / f["portion"]
        scaled = {k: round(f[k] * factor, 2) for k in ["protein","fat","carb"]}
        for k in total:
            total[k] += scaled.get(k,0)
        lines.append(f"{r['food']} {r['grams']}g P:{scaled['protein']} F:{scaled['fat']} C:{scaled['carb']}")

    percent = {k: min(round(total[k]/target[k]*100),100) for k in target}

    flex = {
        "type": "flex",
        "altText": "今日紀錄",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type":"text","text":"今日紀錄","weight":"bold","size":"md"},
                    *[{"type":"text","text":line,"size":"sm"} for line in lines],
                    {"type":"text","text":"達成率","weight":"bold","size":"md"},
                    {"type":"text","text":f"P:{percent['protein']}% F:{percent['fat']}% C:{percent['carb']}%","size":"sm"}
                ]
            }
        }
    }
    return FlexSendMessage(alt_text="今日紀錄", contents=flex)_
