# app.py
import os
import json
import io
from datetime import datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
from linebot.exceptions import InvalidSignatureError
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

app = FastAPI()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_DATA_PATH = os.getenv("GITHUB_DATA_PATH", "data.json")
APP_URL = os.getenv("APP_URL", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------- GitHub Storage ----------
class GitHubStorage:
    """JSON 存在 GitHub"""
    def __init__(self, token, repo, path):
        self.token = token
        self.repo = repo
        self.path = path
        self.headers = {"Authorization": f"token {token}"}
        if not self._get_file():
            self._save_file({"nutrition_db":{}, "records":{}, "targets":{}, "next_id":1}, "init")

    def _get_file(self):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        r = requests.get(url, headers=self.headers)
        if r.status_code==200:
            return r.json()
        return None

    def _save_file(self, data, msg="update"):
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}"
        import base64
        b64 = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode()).decode()
        payload = {"message": msg, "content": b64}
        current = self._get_file()
        if current:
            payload["sha"] = current["sha"]
        r = requests.put(url, headers=self.headers, json=payload)
        if r.status_code not in (200,201):
            raise Exception(f"GitHub save failed {r.status_code}")
        return r.json()

    def _read_state(self):
        f = self._get_file()
        import base64
        return json.loads(base64.b64decode(f["content"]).decode()) if f else {"nutrition_db":{}, "records":{}, "targets":{}, "next_id":1}

    def _write_state(self, state):
        self._save_file(state)

    def set_target(self,user_id,p,f,c):
        state=self._read_state()
        state["targets"][user_id]={"protein":p,"fat":f,"carbs":c}
        self._write_state(state)
    def get_target(self,user_id):
        return self._read_state()["targets"].get(user_id)
    def add_food_db(self,food,base,p,fat,carbs,category="其他"):
        state=self._read_state()
        state["nutrition_db"][food]={"base":base,"protein":p,"fat":fat,"carbs":carbs,"category":category}
        self._write_state(state)
    def get_food(self,food):
        return self._read_state()["nutrition_db"].get(food)
    def search_foods(self,kw):
        state=self._read_state()
        return [{**v,"food":k} for k,v in state["nutrition_db"].items() if kw.lower() in k.lower()]
    def list_foods(self):
        state=self._read_state()
        return [{**v,"food":k} for k,v in state["nutrition_db"].items()]
    def add_record(self,user_id,food,weight,p,fat,carbs):
        state=self._read_state()
        rid=state.get("next_id",1)
        rec={"id":rid,"food":food,"weight":weight,"protein":p,"fat":fat,"carbs":carbs,"time":datetime.utcnow().isoformat()}
        state.setdefault("records",{}).setdefault(user_id,[]).append(rec)
        state["next_id"]=rid+1
        self._write_state(state)
        return rec
    def get_today_records(self,user_id):
        state=self._read_state()
        recs=state.get("records",{}).get(user_id,[])
        today=datetime.utcnow().date()
        return [r for r in recs if datetime.fromisoformat(r["time"]).date()==today]
    def delete_record(self,user_id,rec_id):
        state=self._read_state()
        recs=state.get("records",{}).get(user_id,[])
        new=[r for r in recs if r["id"]!=rec_id]
        state["records"][user_id]=new
        self._write_state(state)
        return len(recs)-len(new)
    def clear_today(self,user_id):
        state=self._read_state()
        recs=state.get("records",{}).get(user_id,[])
        today=datetime.utcnow().date()
        new=[r for r in recs if datetime.fromisoformat(r["time"]).date()!=today]
        removed=len(recs)-len(new)
        state["records"][user_id]=new
        self._write_state(state)
        return removed

storage = GitHubStorage(GITHUB_TOKEN,GITHUB_REPO,GITHUB_DATA_PATH)

# ---------- Utility ----------
def emoji_progress(pct):
    pct=max(0,min(100,pct))
    full=int(pct//10)
    bars="█"*full+"▁"*(10-full)
    if pct>=100: emoji="✅"
    elif pct>=75: emoji="🟢"
    elif pct>=50: emoji="🟡"
    elif pct>=25: emoji="🟠"
    else: emoji="🔴"
    return f"{emoji} {bars} {pct:.0f}%"

# ---------- LINE Command Parser ----------
def parse_text(user_id,text):
    text=text.strip()
    if text.lower() in ["hi","hello"]:
        return "Hi"

    # 設目標
    if text.startswith("目標"):
        parts=text.split()
        if len(parts)!=4: return "格式：目標 蛋白質 脂肪 碳水"
        try: p,f,c=map(float,parts[1:]); storage.set_target(user_id,p,f,c)
        except: return "格式錯誤"
        return f"已設定目標 P{p} F{f} C{c}"

    # 新增食物
    if text.startswith("新增"):
        parts=text.split()
        if len(parts)<6: return "新增 格式錯誤"
        food,base,p,fat,carbs=parts[1:6]
        category=parts[6] if len(parts)>=7 else "其他"
        try: storage.add_food_db(food,float(base),float(p),float(fat),float(carbs),category)
        except: return "新增格式錯誤"
        return f"新增食物 {food} 類別 {category} P{p} F{fat} C{carbs}"

    # 查所有食物
    if text in ["list","列表","食物列表","食物庫","資料庫"]:
        foods=storage.list_foods()
        if not foods: return "食物庫空"
        return "\n".join([f"{f['food']} ({f.get('category','')}) {f['base']}g P:{f['protein']} F:{f['fat']} C:{f['carbs']}" for f in foods])

    # 今日累計
    if text in ["今日","今日攝取","今日累計","今日累積"]:
        recs=storage.get_today_records(user_id)
        target=storage.get_target(user_id)
        total_p=sum(r["protein"] for r in recs)
        total_f=sum(r["fat"] for r in recs)
        total_c=sum(r["carbs"] for r in recs)
        t_p=target["protein"] if target else 100
        t_f=target["fat"] if target else 100
        t_c=target["carbs"] if target else 100
        res=f"今日 {datetime.utcnow().date()} 紀錄：\n"
        for r in recs:
            res+=f"{r['food']} {r['weight']}g P:{r['protein']:.1f} F:{r['fat']:.1f} C:{r['carbs']:.1f}\n"
        res+=f"總計 P:{total_p:.1f}/{t_p} F:{total_f:.1f}/{t_f} C:{total_c:.1f}/{t_c}\n"
        res+="達成度:\n"
        res+=f"P:{emoji_progress(total_p/t_p*100)}\nF:{emoji_progress(total_f/t_f*100)}\nC:{emoji_progress(total_c/t_c*100)}"
        return res

    # 刪除紀錄
    if text.startswith("刪除"):
        parts=text.split()
        if len(parts)==2 and parts[1].isdigit():
            rid=int(parts[1])
            removed=storage.delete_record(user_id,rid)
            return f"刪除 {removed} 筆" if removed else "找不到紀錄"
        if text in ["刪除今日","清除今日","清除全部"]:
            removed=storage.clear_today(user_id)
            return f"已刪除 {removed} 筆今日紀錄"

    # 新增今日攝取 食物 重量
    parts=text.split()
    if len(parts)==2:
        food=parts[0]
        try: weight=float(parts[1])
        except: return "輸入格式錯誤"
        f=storage.get_food(food)
        if not f: return "食物不存在，請先新增"
        factor=weight/f["base"]
        p=f["protein"]*factor; fat=f["fat"]*factor; c=f["carbs"]*factor
        storage.add_record(user_id,food,weight,p,fat,c)
        return f"已記錄 {food} {weight}g P:{p:.1f} F:{fat:.1f} C:{c:.1f}"

    # help
    return "指令：\n目標 P F C\n新增 名稱 重量 P F C [類別]\nlist/列表\n食物 重量\n今日/今日累計\n刪除 id\n刪除今日/清除全部\nhi"

# ---------- LINE Webhook ----------
@app.post("/callback")
async def callback(req: Request):
    signature=req.headers.get("X-Line-Signature","")
    body=await req.body()
    body_str=body.decode("utf-8")
    try: handler.handle(body_str,signature)
    except InvalidSignatureError: return Response(status_code=400)
    return Response(status_code=200)

@handler.add(MessageEvent, message=TextMessage)
def handle_msg(event):
    uid=event.source.user_id
    text=event.message.text
    res=parse_text(uid,text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=res))

# ---------- Chart Endpoint ----------
@app.get("/chart")
def chart(user_id:str):
    recs=storage.get_today_records(user_id)
    vals=[sum(r[k] for r in recs) for k in ["protein","fat","carbs"]]
    labels=["Protein","Fat","Carbs"]
    fig,ax=plt.subplots()
    ax.pie(vals,labels=labels,autopct="%1.1f%%")
    buf=io.BytesIO(); plt.savefig(buf,format="png"); plt.close(fig); buf.seek(0)
    return StreamingResponse(buf,media_type="image/png")

# ---------- Run ----------
if __name__=="__main__":
    import uvicorn
    uvicorn.run("app:app",host="0.0.0.0",port=int(os.getenv("PORT",8000)),reload=True)
