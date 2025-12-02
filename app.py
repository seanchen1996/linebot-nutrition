from fastapi import FastAPI, Request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
from linebot.exceptions import InvalidSignatureError
import json, os, requests, base64
import matplotlib.pyplot as plt
from io import BytesIO
import base64 as b64

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_DATA_PATH = "nutrition_db.json"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------------- GitHub Storage ----------------
class GitHubStorage:
    def __init__(self, token, repo, path):
        self.token = token
        self.repo = repo
        self.path = path
        if not self._file_exists():
            self._save_state({"users": {}, "food_db": {}})

    def _file_exists(self):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers={"Authorization": f"token {self.token}"})
        return r.status_code == 200

    def _read_state(self):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers={"Authorization": f"token {self.token}"})
        if r.status_code != 200:
            return {"users": {}, "food_db": {}}
        content = r.json().get("content", "")
        if content:
            content = base64.b64decode(content).decode()
            try:
                state = json.loads(content)
            except:
                state = {"users": {}, "food_db": {}}
        else:
            state = {"users": {}, "food_db": {}}
        # key 保護
        if "users" not in state:
            state["users"] = {}
        if "food_db" not in state:
            state["food_db"] = {}
        return state

    def _save_state(self, state, msg="update"):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers={"Authorization": f"token {self.token}"})
        sha = r.json().get("sha") if r.status_code == 200 else None
        content = base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()
        data = {"message": msg, "content": content}
        if sha:
            data["sha"] = sha
        r2 = requests.put(url, headers={"Authorization": f"token {self.token}"}, json=data)
        if r2.status_code not in [200, 201]:
            raise Exception(f"GitHub save failed: {r2.status_code} {r2.text}")

    # ---------------- 操作介面 ----------------
    def set_user_target(self, user_id, protein, fat, carb):
        state = self._read_state()
        state["users"].setdefault(user_id, {})
        state["users"][user_id]["target"] = {"protein": protein, "fat": fat, "carb": carb}
        self._save_state(state, f"Set target for {user_id}")

    def get_user_target(self, user_id):
        state = self._read_state()
        return state["users"].get(user_id, {}).get("target", {})

    def add_food_db(self, name, weight, protein, fat, carb):
        state = self._read_state()
        state.setdefault("food_db", {})
        state["food_db"][name] = {"weight": weight, "protein": protein, "fat": fat, "carb": carb}
        self._save_state(state, f"Add food_db {name}")

    def list_foods(self):
        state = self._read_state()
        return state.get("food_db", {})

    def add_food(self, user_id, name, protein, fat, carb):
        state = self._read_state()
        state["users"].setdefault(user_id, {})
        state["users"][user_id].setdefault("today", [])
        state["users"][user_id]["today"].append({"name": name, "protein": protein, "fat": fat, "carb": carb})
        self._save_state(state, f"Add food {name} for {user_id}")

    def get_user_today(self, user_id):
        state = self._read_state()
        return state["users"].get(user_id, {}).get("today", [])

    def clear_user_today(self, user_id):
        state = self._read_state()
        if user_id in state["users"]:
            state["users"][user_id]["today"] = []
            self._save_state(state, f"Clear today for {user_id}")

    def remove_food_today(self, user_id, food_index):
        state = self._read_state()
        if user_id in state["users"] and 0 <= food_index < len(state["users"][user_id].get("today", [])):
            removed = state["users"][user_id]["today"].pop(food_index)
            self._save_state(state, f"Remove food {removed['name']} at index {food_index}")

storage = GitHubStorage(GITHUB_TOKEN, GITHUB_REPO, GITHUB_DATA_PATH)

# ---------------- 文字解析 ----------------
def is_number(s):
    try:
        float(s)
        return True
    except:
        return False

def create_nutrition_chart(today, target):
    total_p = sum(f["protein"] for f in today)
    total_f = sum(f["fat"] for f in today)
    total_c = sum(f["carb"] for f in today)
    labels = ["Protein", "Fat", "Carb"]
    values = [total_p, total_f, total_c]
    target_values = [target.get("protein", 0), target.get("fat",0), target.get("carb",0)]
    fig, ax = plt.subplots()
    ax.bar(labels, values, color="skyblue", alpha=0.7, label="今日攝取")
    ax.plot(labels, target_values, color="orange", marker="o", label="目標")
    ax.set_ylabel("g")
    ax.legend()
    buf = BytesIO()
    plt.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return b64.b64encode(buf.read()).decode()

def parse_text(user_id, text):
    text = text.strip()
    if not text:
        return None
    parts = text.split()
    cmd = parts[0]

    # 目標
    if cmd == "目標":
        if len(parts) != 4 or not all(is_number(p) for p in parts[1:]):
            return "目標格式錯誤，請輸入數字"
        protein, fat, carb = map(float, parts[1:])
        storage.set_user_target(user_id, protein, fat, carb)
        return f"已設定每日目標：蛋白質 {protein}, 脂肪 {fat}, 碳水 {carb}"

    # 新增
    if cmd == "新增":
        if len(parts) != 6 or not all(is_number(p) for p in parts[2:]):
            return "新增格式錯誤，請輸入數字"
        name = parts[1]
        weight, protein, fat, carb = map(float, parts[2:])
        storage.add_food_db(name, weight, protein, fat, carb)
        return f"食物 {name} 已加入資料庫"

    # 列出資料庫
    if cmd in ["查食物", "list", "食物表", "列表"]:
        foods = storage.list_foods()
        if not foods:
            return "資料庫沒有食物"
        text_list = "\n".join([f"{k}: {v['protein']}P {v['fat']}F {v['carb']}C ({v['weight']}g)" for k,v in foods.items()])
        return text_list

    # 使用資料庫累計
    foods_db = storage.list_foods()
    if len(parts) == 2 and parts[0] in foods_db and is_number(parts[1]):
        name = parts[0]
        qty = float(parts[1])
        data = foods_db[name]
        ratio = qty / data["weight"]
        storage.add_food(user_id, name, data["protein"]*ratio, data["fat"]*ratio, data["carb"]*ratio)
        return f"{name} {qty}g 已加入今日紀錄"

    # 今日累計
    if cmd in ["今日", "今日累計", "今日累積", "累積"]:
        today = storage.get_user_today(user_id)
        if not today:
            return "今日尚無紀錄"
        total_p = sum(f["protein"] for f in today)
        total_f = sum(f["fat"] for f in today)
        total_c = sum(f["carb"] for f in today)
        target = storage.get_user_target(user_id)
        chart_b64 = create_nutrition_chart(today, target)
        flex_message = FlexSendMessage(
            alt_text="今日營養統計",
            contents={
                "type": "bubble",
                "body": {"type": "box", "layout": "vertical", "contents":[
                    {"type": "text", "text": f"今日累計：蛋白質 {total_p:.1f}, 脂肪 {total_f:.1f}, 碳水 {total_c:.1f}"},
                    {"type":"image", "url": f"data:image/png;base64,{chart_b64}", "size":"full"}
                ]}
            }
        )
        return flex_message

    # 清除今日
    if cmd in ["清除", "清除今日"]:
        storage.clear_user_today(user_id)
        return "已清除今日紀錄"

    # 清除今日單筆
    if cmd == "刪除" and len(parts) == 2 and parts[1].isdigit():
        idx = int(parts[1]) - 1
        storage.remove_food_today(user_id, idx)
        return f"已刪除今日第 {parts[1]} 筆紀錄"

    # 幫助
    if cmd in ["help", "幫助"]:
        return ("用法:\n"
                "目標 [蛋白質] [脂肪] [碳水]\n"
                "新增 [食物] [重量] [蛋白] [脂肪] [碳水]\n"
                "[食物] [重量] → 加入今日紀錄\n"
                "查食物/list/食物表/列表 → 顯示資料庫\n"
                "今日/今日累計/今日累積/累積 → 顯示今日累計 + 圖表\n"
                "清除/清除今日 → 清空今日紀錄\n"
                "刪除 [編號] → 刪除今日單筆紀錄")

    return None

# ---------------- LINE Webhook ----------------
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode(), signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    result = parse_text(user_id, text)
    if result:
        if isinstance(result, FlexSendMessage):
            line_bot_api.reply_message(event.reply_token, result)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
