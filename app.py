from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
import sqlite3

app = FastAPI()

# LINE Bot Token
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DB_FILE = "nutrition.db"

# ---------- 初始化資料庫 ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 使用者目標營養
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_target (
            user_id TEXT PRIMARY KEY,
            protein REAL,
            fat REAL,
            carbs REAL
        )
    """)
    # 今日攝取
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            food TEXT,
            weight REAL,
            protein REAL,
            fat REAL,
            carbs REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 食物資料庫
    c.execute("""
        CREATE TABLE IF NOT EXISTS food_db (
            food TEXT PRIMARY KEY,
            base_weight REAL,
            protein REAL,
            fat REAL,
            carbs REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------- 解析訊息 ----------
def parse_message(user_id, text):
    text = text.strip()
    # 設定目標: 目標 125 64 256
    if text.startswith("目標"):
        try:
            _, p, f, c = text.split()
            p, f, c = float(p), float(f), float(c)
            conn = sqlite3.connect(DB_FILE)
            c_obj = conn.cursor()
            c_obj.execute("INSERT OR REPLACE INTO user_target(user_id,protein,fat,carbs) VALUES(?,?,?,?)",
                          (user_id, p, f, c))
            conn.commit()
            conn.close()
            return f"已設定每日目標：蛋白質 {p}g 脂肪 {f}g 碳水 {c}g"
        except:
            return "目標格式錯誤，請輸入: 目標 蛋白質 脂肪 碳水"
    
    # 新增食物到資料庫: 新增 燕麥 37.5 4.9 3 25.3
    elif text.startswith("新增"):
        try:
            _, food, weight, p, f, c = text.split()
            weight, p, f, c = float(weight), float(p), float(f), float(c)
            conn = sqlite3.connect(DB_FILE)
            c_obj = conn.cursor()
            c_obj.execute("INSERT OR REPLACE INTO food_db(food,base_weight,protein,fat,carbs) VALUES(?,?,?,?,?)",
                          (food, weight, p, f, c))
            conn.commit()
            conn.close()
            return f"已新增食物 {food}"
        except:
            return "新增格式錯誤，請輸入: 新增 食物名稱 重量 蛋白質 脂肪 碳水"
    
    # 列出所有食物資料庫
    elif text == "查食物":
        conn = sqlite3.connect(DB_FILE)
        c_obj = conn.cursor()
        c_obj.execute("SELECT * FROM food_db")
        rows = c_obj.fetchall()
        conn.close()
        if not rows:
            return "資料庫為空"
        msg = "食物資料庫：\n"
        for r in rows:
            msg += f"{r[0]} {r[1]}g P:{r[2]} F:{r[3]} C:{r[4]}\n"
        return msg
    
    # 清除所有今日紀錄
    elif text == "清除全部":
        conn = sqlite3.connect(DB_FILE)
        c_obj = conn.cursor()
        c_obj.execute("DELETE FROM user_record WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return "已清除今日所有紀錄"

    # 顯示今日累計
    elif text == "今日累計":
        conn = sqlite3.connect(DB_FILE)
        c_obj = conn.cursor()
        c_obj.execute("SELECT food,weight,protein,fat,carbs FROM user_record WHERE user_id=?", (user_id,))
        rows = c_obj.fetchall()
        if not rows:
            conn.close()
            return "今天還沒紀錄食物"
        msg = "今日攝取：\n"
        total_p = total_f = total_c = 0
        for r in rows:
            msg += f"{r[0]} {r[1]}g P:{r[2]} F:{r[3]} C:{r[4]}\n"
            total_p += r[2]
            total_f += r[3]
            total_c += r[4]
        msg += f"總計 P:{total_p} F:{total_f} C:{total_c}"
        conn.close()
        return msg

    # 刪除單筆紀錄: 刪除 3 (id)
    elif text.startswith("刪除"):
        try:
            _, rec_id = text.split()
            rec_id = int(rec_id)
            conn = sqlite3.connect(DB_FILE)
            c_obj = conn.cursor()
            c_obj.execute("DELETE FROM user_record WHERE id=? AND user_id=?", (rec_id, user_id))
            conn.commit()
            conn.close()
            return f"已刪除紀錄 {rec_id}"
        except:
            return "刪除格式錯誤，請輸入: 刪除 編號"

    # 記錄食物 PFC (支援 food_db 自動計算比例)
    else:
        # 嘗試解析格式: 食物 重量
        try:
            parts = text.split()
            if len(parts) != 2:
                return "輸入格式錯誤: 食物名稱 重量"
            food_name, weight = parts[0], float(parts[1])
            conn = sqlite3.connect(DB_FILE)
            c_obj = conn.cursor()
            # 查 food_db
            c_obj.execute("SELECT base_weight,protein,fat,carbs FROM food_db WHERE food=?", (food_name,))
            row = c_obj.fetchone()
            if row:
                base_weight, p, f, c_ = row
                factor = weight / base_weight
                p *= factor
                f *= factor
                c_ *= factor
            else:
                return "食物不在資料庫，請先新增"
            # insert 到 user_record
            c_obj.execute("INSERT INTO user_record(user_id,food,weight,protein,fat,carbs) VALUES(?,?,?,?,?,?)",
                          (user_id, food_name, weight, p, f, c_))
            conn.commit()
            conn.close()
            return f"{food_name} {weight}g 已加入紀錄，P:{p:.1f} F:{f:.1f} C:{c_:.1f}"
        except:
            return "輸入錯誤，格式: 食物名稱 重量"

# ---------- LINE Webhook ----------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body = body.decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature"
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    reply = parse_message(user_id, text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@app.get("/")
def home():
    return {"status": "ok"}
