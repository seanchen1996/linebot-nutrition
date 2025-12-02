# app.py
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Lock
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    FlexSendMessage,
)
from linebot.exceptions import InvalidSignatureError, LineBotApiError

# ------------ Config ------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

NUTRITION_DB_FILE = "nutrition_db.json"
TODAY_DB_FILE = "today_db.json"
GOALS_DB_FILE = "goals.json"

# ------------ Line client & FastAPI ------------
app = FastAPI()
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ------------ Thread-safety for file operations ------------
file_lock = Lock()

# ------------ JSON helpers ------------
def load_json_safe(path: str, default: Any) -> Any:
    """Read JSON file, if missing or corrupted return default and create file."""
    with file_lock:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # if corrupted, overwrite with default to recover
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            return default

def save_json_safe(path: str, data: Any) -> None:
    """Atomically save JSON (simple overwrite)."""
    with file_lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ------------ Time helper ------------
def now_taipei_str() -> str:
    # Use zoneinfo (Python 3.9+), format readable timestamp
    tz = ZoneInfo("Asia/Taipei")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

# ------------ Storage operations ------------
def get_nutrition_db() -> Dict[str, Dict[str, float]]:
    return load_json_safe(NUTRITION_DB_FILE, {})

def save_nutrition_db(db: Dict[str, Dict[str, float]]) -> None:
    save_json_safe(NUTRITION_DB_FILE, db)

def get_today_db() -> Dict[str, List[Dict[str, Any]]]:
    return load_json_safe(TODAY_DB_FILE, {})

def save_today_db(db: Dict[str, List[Dict[str, Any]]]) -> None:
    save_json_safe(TODAY_DB_FILE, db)

def get_goals_db() -> Dict[str, Dict[str, float]]:
    return load_json_safe(GOALS_DB_FILE, {})

def save_goals_db(db: Dict[str, Dict[str, float]]) -> None:
    save_json_safe(GOALS_DB_FILE, db)

# ------------ Business logic helpers ------------
def set_goal(user_id: str, protein: float, fat: float, carb: float) -> str:
    goals = get_goals_db()
    goals[user_id] = {"protein": protein, "fat": fat, "carb": carb}
    save_goals_db(goals)
    return f"已設定目標：蛋白質 {protein}g，脂肪 {fat}g，碳水 {carb}g"

def add_food_to_db(name: str, portion: float, protein: float, fat: float, carb: float) -> str:
    db = get_nutrition_db()
    db[name] = {"portion": portion, "protein": protein, "fat": fat, "carb": carb}
    save_nutrition_db(db)
    return f"已新增食物：{name} 每份 {portion}g → P:{protein} F:{fat} C:{carb}"

def list_foods_text() -> str:
    db = get_nutrition_db()
    if not db:
        return "食物資料庫目前為空。"
    lines = []
    for i, (k, v) in enumerate(db.items(), start=1):
        lines.append(f"{i}. {k} 份量 {v['portion']}g  P:{v['protein']} F:{v['fat']} C:{v['carb']}")
    return "\n".join(lines)

def record_food(user_id: str, food_name: str, grams: float) -> str:
    db = get_nutrition_db()
    if food_name not in db:
        return f"找不到食物：{food_name}（請先用 新增 指令加入資料庫）"
    item = db[food_name]
    portion = float(item["portion"])
    factor = grams / portion if portion != 0 else 0.0
    protein = round(float(item["protein"]) * factor, 3)
    fat = round(float(item["fat"]) * factor, 3)
    carb = round(float(item["carb"]) * factor, 3)
    entry = {
        "food": food_name,
        "amount": grams,
        "protein": protein,
        "fat": fat,
        "carb": carb,
        "timestamp": now_taipei_str()
    }
    today = get_today_db()
    today.setdefault(user_id, [])
    # combine same food by name (累加 grams & nutrients)
    for r in today[user_id]:
        if r["food"] == food_name:
            r["amount"] += grams
            r["protein"] = round(r["protein"] + protein, 3)
            r["fat"] = round(r["fat"] + fat, 3)
            r["carb"] = round(r["carb"] + carb, 3)
            save_today_db(today)
            return f"已累計：{food_name} {grams}g（合併到現有紀錄）"
    # else append new
    today[user_id].append(entry)
    save_today_db(today)
    return f"已記錄：{food_name} {grams}g（P:{protein} F:{fat} C:{carb}）"

def calc_today_summary(user_id: str) -> Dict[str, Any]:
    today = get_today_db().get(user_id, [])
    total = {"protein": 0.0, "fat": 0.0, "carb": 0.0}
    items = []
    for idx, e in enumerate(today, start=1):
        items.append({
            "idx": idx,
            "food": e["food"],
            "amount": e["amount"],
            "protein": e["protein"],
            "fat": e["fat"],
            "carb": e["carb"],
            "timestamp": e.get("timestamp", "")
        })
        total["protein"] += float(e.get("protein", 0))
        total["fat"] += float(e.get("fat", 0))
        total["carb"] += float(e.get("carb", 0))
    # round totals
    total = {k: round(v, 3) for k, v in total.items()}
    return {"items": items, "total": total, "date": datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")}

def delete_record_by_index(user_id: str, index: int) -> str:
    today = get_today_db()
    arr = today.get(user_id, [])
    if not arr:
        return "今日沒有紀錄可以刪除。"
    if index < 0 or index >= len(arr):
        return f"索引錯誤，請輸入 1 到 {len(arr)} 之間的數字。"
    removed = arr.pop(index)
    today[user_id] = arr
    save_today_db(today)
    return f"已刪除第 {index+1} 筆：{removed['food']} {removed['amount']}g"

def delete_last_record(user_id: str) -> str:
    today = get_today_db()
    arr = today.get(user_id, [])
    if not arr:
        return "今日沒有紀錄可以刪除。"
    removed = arr.pop(-1)
    today[user_id] = arr
    save_today_db(today)
    return f"已刪除最後一筆：{removed['food']} {removed['amount']}g"

def clear_today_records(user_id: str) -> str:
    today = get_today_db()
    if user_id in today:
        today[user_id] = []
        save_today_db(today)
    return "已清除今日所有紀錄。"

def delete_food_from_db(food_name: str) -> str:
    db = get_nutrition_db()
    if food_name not in db:
        return f"資料庫沒有此食物：{food_name}"
    db.pop(food_name)
    save_nutrition_db(db)
    return f"已從資料庫刪除：{food_name}"

# ------------ Flex builder (純文字型) ------------
def build_flex_today(user_id: str) -> FlexSendMessage:
    summary = calc_today_summary(user_id)
    items = summary["items"]
    totals = summary["total"]
    goals = get_goals_db().get(user_id, None)
    # prepare lines
    body_contents = []
    body_contents.append({"type": "text", "text": f"📅 今日攝取：{summary['date']}", "weight": "bold", "size": "md"})
    if not items:
        body_contents.append({"type": "text", "text": "今日尚無紀錄", "size": "sm"})
    else:
        for it in items:
            body_contents.append(
                {"type": "text", "text": f"{it['idx']}. {it['food']} {it['amount']}g — P:{it['protein']} F:{it['fat']} C:{it['carb']}", "size": "sm"}
            )
    # totals line
    body_contents.append({"type": "text", "text": "——", "size": "sm"})
    body_contents.append({"type": "text", "text": f"總計 — P:{totals['protein']} / F:{totals['fat']} / C:{totals['carb']}", "size": "sm", "weight": "bold"})
    # remaining to goal
    if goals:
        remain_p = round(max(goals.get("protein", 0) - totals["protein"], 0), 3)
        remain_f = round(max(goals.get("fat", 0) - totals["fat"], 0), 3)
        remain_c = round(max(goals.get("carb", 0) - totals["carb"], 0), 3)
        body_contents.append({"type": "text", "text": f"距離目標還需 — P:{remain_p} F:{remain_f} C:{remain_c}", "size": "sm"})
        # percent
        def pct(now, goal):
            if not goal or goal <= 0:
                return 0
            return min(round(now / goal * 100), 100)
        p_pct = pct(totals["protein"], goals.get("protein", 0))
        f_pct = pct(totals["fat"], goals.get("fat", 0))
        c_pct = pct(totals["carb"], goals.get("carb", 0))
        body_contents.append({"type": "text", "text": f"達成率 — P:{p_pct}% F:{f_pct}% C:{c_pct}%", "size": "sm"})
    # assemble flex
    flex = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents
        }
    }
    return FlexSendMessage(alt_text="今日攝取", contents=flex)

# ------------ Command parser ------------
def parse_and_execute(user_id: str, text: str) -> Optional[Any]:
    """
    Return:
      - TextSendMessage / FlexSendMessage object to send
      - Or a plain string (converted to TextSendMessage by caller)
      - Or None to do nothing
    """
    if not text:
        return None
    text = text.strip()
    parts = text.split()
    cmd = parts[0]

    # HELP
    if cmd in ["help", "Help", "幫助", "救我"]:
        help_text = (
            "可用指令：\n"
            "• 目標 [protein] [fat] [carb]\n"
            "  例：目標 128 64 256\n"
            "• 新增 [食物名] [份量(g)] [protein] [fat] [carb]\n"
            "  例：新增 燕麥 37.5 4.9 3 25.3\n"
            "• [食物名] [攝取量(g)] → 記錄今日攝取\n"
            "  例：燕麥 20\n"
            "• 今日 / 今日攝取 / 今日累計 / 今日累積 → 顯示今日紀錄\n"
            "• 刪除 [編號] / 刪除 最後 / 刪除 上一筆 → 刪除今日項目\n"
            "• 刪除今日 / 刪除全部 / 清除今日 → 清除今日所有\n"
            "• 刪除資料庫食物 [食物名] → 從資料庫移除\n"
            "• list / 列表 / 食物庫 / 食物列表 → 顯示資料庫"
        )
        return help_text

    # SET GOAL
    if cmd == "目標":
        if len(parts) != 4:
            return "目標格式錯誤，請輸入：目標 protein fat carb"
        try:
            p, f, c = map(float, parts[1:])
        except ValueError:
            return "目標格式錯誤，請輸入數字"
        return set_goal(user_id, p, f, c)

    # ADD FOOD TO NUTRITION DB
    if cmd == "新增":
        # 新增 [食物] [portion] [protein] [fat] [carb]
        if len(parts) != 6:
            return "新增格式錯誤，請輸入：新增 食物 份量(g) protein fat carb"
        try:
            name = parts[1]
            portion = float(parts[2])
            p, f, c = map(float, parts[3:])
        except ValueError:
            return "新增格式錯誤，請確認數字"
        return add_food_to_db(name, portion, p, f, c)

    # LIST DB
    if cmd.lower() in ["list", "列表", "食物列表", "食物庫", "資料庫"]:
        return list_foods_text()

    # DELETE FROM DB
    if cmd == "刪除資料庫食物":
        if len(parts) != 2:
            return "請輸入：刪除資料庫食物 食物名"
        return delete_food_from_db(parts[1])

    # TODAY QUERIES
    if cmd in ["今日", "今日攝取", "今日累計", "今日累積"]:
        return build_flex_today(user_id)

    # CLEAR TODAY ALL
    if cmd in ["刪除今日", "刪除全部", "清除今日", "清除全部"]:
        return clear_today_records(user_id)

    # DELETE ITEM BY INDEX / LAST / PREV
    if cmd == "刪除":
        if len(parts) == 2:
            arg = parts[1]
            if arg in ["最後", "上一步", "上一筆", "最後一筆"]:
                return delete_last_record(user_id)
            # numeric?
            try:
                idx = int(arg)
                return delete_record_by_index(user_id, idx - 1)
            except ValueError:
                return "刪除格式錯誤，請輸入 刪除 數字 或 刪除 最後"
        else:
            return "刪除格式錯誤，請輸入 刪除 編號 / 刪除 最後"

    # If command is exactly a food + grams e.g. "燕麥 20"
    if len(parts) == 2:
        name = parts[0]
        try:
            grams = float(parts[1])
        except ValueError:
            grams = None
        if grams is not None:
            # treat as record attempt
            return record_food(user_id, name, grams)

    # If nothing matched
    return None

# ------------ Webhook endpoint ------------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8")
    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        return {"status": "invalid signature"}, 400
    except Exception as e:
        # log and return ok (to prevent repeated retries)
        print("LINE handler error:", e)
    return "OK"

# ------------ LINE event handler ------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    user_text = (event.message.text or "").strip()
    try:
        result = parse_and_execute(user_id, user_text)
        if result is None:
            # no keyword matched, do nothing (per your request)
            return
        # If result is FlexSendMessage object (already built)
        if isinstance(result, FlexSendMessage):
            try:
                line_bot_api.reply_message(event.reply_token, result)
            except LineBotApiError as e:
                print("LINE handler error:", e)
        else:
            # Plain text
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=str(result)))
            except LineBotApiError as e:
                print("LINE handler error:", e)
    except Exception as e:
        print("handler exception:", e)
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="發生錯誤，請稍後再試"))
        except Exception:
            pass
