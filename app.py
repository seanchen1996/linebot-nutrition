from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import sqlite3
import os
from datetime import date

app = FastAPI()

# LINE 設定
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# SQLite 資料庫
DB_FILE = "nutrition.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 使用者目標
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        protein_goal REAL,
        fat_goal REAL,
        carbs_goal REAL
    )""")
    # 食物資料表
    c.execute("""CREATE TABLE IF NOT EXISTS foods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        protein REAL,
        fat REAL,
        carbs REAL
    )""")
    # 食物紀錄
    c.execute("""CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        food_name TEXT,
        weight REAL,
        protein REAL,
        fat REAL,
        carbs REAL,
        date TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

# 初始化一些常用食物
def add_food_if_not_exists():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    foods = [
        ("雞胸肉", 23, 1.9, 0),
        ("白飯", 2.5, 0.3, 28),
        ("地瓜", 1.6, 0.1, 20),
        ("蛋", 12, 10, 1),
    ]
    for f in foods:
        try:
            c.execute("INSERT INTO foods (name, protein, fat, carbs) VALUES (?, ?, ?, ?)", f)
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()

add_food_if_not_exists()

# webhook
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
    text = event.message.text.strip()
    user_id = event.source.user_id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 設定每日目標
    # 格式：目標 蛋白質 100 脂肪 50 碳水 200
    if text.startswith("目標"):
        try:
            parts = text.split()
            protein_goal = float(parts[2])
            fat_goal = float(parts[4])
            carbs_goal = float(parts[6])
            c.execute("INSERT OR REPLACE INTO users (user_id, protein_goal, fat_goal, carbs_goal) VALUES (?, ?, ?, ?)",
                      (user_id, protein_goal, fat_goal, carbs_goal))
            conn.commit()
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text=f"已設定每日目標：蛋白質 {protein_goal}g, 脂肪 {fat_goal}g, 碳水 {carbs_goal}g"))
        except:
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="設定目標格式錯誤，請用：目標 蛋白質 100 脂肪 50 碳水 200"))
        conn.close()
        return

    # 記錄食物
    # 格式：雞胸肉 150
    try:
        food_name, weight = text.split()
        weight = float(weight)
        c.execute("SELECT protein, fat, carbs FROM foods WHERE name=?", (food_name,))
        result = c.fetchone()
        if not result:
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text=f"食物 {food_name} 不在資料庫"))
            conn.close()
            return
        protein_per100, fat_per100, carbs_per100 = result
        protein = protein_per100 * weight / 100
        fat = fat_per100 * weight / 100
        carbs = carbs_per100 * weight / 100
        today = str(date.today())
        c.execute("INSERT INTO records (user_id, food_name, weight, protein, fat, carbs, date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (user_id, food_name, weight, protein, fat, carbs, today))
        conn.commit()

        # 計算今日總攝取
        c.execute("SELECT SUM(protein), SUM(fat), SUM(carbs) FROM records WHERE user_id=? AND date=?",
                  (user_id, today))
        total_p, total_f, total_c = c.fetchone()

        # 查目標
        c.execute("SELECT protein_goal, fat_goal, carbs_goal FROM users WHERE user_id=?", (user_id,))
        goal = c.fetchone()
        if goal:
            rem_p = goal[0] - total_p
            rem_f = goal[1] - total_f
            rem_c = goal[2] - total_c
            reply = (f"已紀錄 {food_name} {weight}g\n"
                     f"今日累計：蛋白質 {total_p:.1f}g, 脂肪 {total_f:.1f}g, 碳水 {total_c:.1f}g\n"
                     f"剩餘目標：蛋白質 {rem_p:.1f}g, 脂肪 {rem_f:.1f}g, 碳水 {rem_c:.1f}g")
        else:
            reply = (f"已紀錄 {food_name} {weight}g\n"
                     f"今日累計：蛋白質 {total_p:.1f}g, 脂肪 {total_f:.1f}g, 碳水 {total_c:.1f}g\n"
                     f"尚未設定每日目標，可用格式：目標 蛋白質 100 脂肪 50 碳水 200")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    except:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="格式錯誤，食物請輸入：食物名稱 重量(克)"))
    conn.close()
