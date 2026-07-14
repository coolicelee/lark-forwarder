from aiohttp import web
import asyncio
import json
import os
import requests
import psycopg2  # PostgreSQL 驱动
import httpx
from datetime import datetime
import random
import string

# Render 会自动注入 PORT 环境变量，必须优先读取它
WS_PORT = int(os.getenv("PORT", 8080))

# 从 Render 环境变量中读取您的云数据库（如 Supabase 或 Neon）连接串
DATABASE_URL = "postgresql://larkmessage_user:XiMjKlzhLdhDfhw0q3oJ5AHNVHoaECfj@dpg-d9b18ptaeets73a3qesg-a/larkmessage"

#os.getenv("DATABASE_URL") or os.getenv("EXTERNAL_DATABASE_URL")

APP_ID = "cli_aacce5eec378de15" 
APP_SECRET = "FixMfrXIanGxZu4lX0AgDbtDfahQcbWR" 

active_connections = {}
authApprovalCode = "4FE436B1-7A53-44C1-B594-928D64F6BDBC"
userID = "ou_972e4f5ea44fb218db8125f9d1b3be43"

ROUTE_MAP = {
    "4FE436B1-7A53-44C1-B594-928D64F6BDBC": ["test"]
}

def generate_auth_code() -> str:
    time_str = datetime.now().strftime("%Y%m%d%H%M%S")
    pool = string.ascii_uppercase + string.digits
    random_str = "".join(random.choices(pool, k=5))
    return f"SQ-{time_str}-{random_str}"

def get_tenant_access_token():
    url = "https://larksuite.com"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = {"app_id": APP_ID, "app_secret": APP_SECRET}
    response = requests.post(url, headers=headers, json=payload)
    return response.json().get("tenant_access_token")

def add_lark_comment(instance_code, comment_text):
    token = get_tenant_access_token()
    if not token:
        print("由于未获取到 Token，追加评论终止。")
        return
    
    url = f"https://larksuite.com{instance_code}/comments"
    headers = {
        "Authorization": f"Bearer {token}", 
        "Content-Type": "application/json; charset=utf-8"
    }
    payload = {
        "content": comment_text
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        res_data = response.json()
        print(f"飞书追加评论响应结果: {res_data}")
    except Exception as e:
        print(f"网络请求追加评论异常: {e}")

#在外部独立云端数据库中初始化消息暂存表
def init_db():
    if not DATABASE_URL:
        print("警告：未配置 DATABASE_URL 环境变量，数据库功能将不可用！")
        return
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            # PostgreSQL 使用 SERIAL 自动递增主键
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS backlog_events (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                event_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
        conn.commit()

init_db()

# 从外部云数据库只补发特定客户端的积压数据
async def flush_backlog(client_id, ws):
    if not DATABASE_URL: return
    
    # 建立数据库会话连接
    with psycopg2.connect(dsn=DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            # 注意：PostgreSQL 占位符统一使用 %s 替换原 sqlite3 的 ?
            cursor.execute('SELECT id, event_data FROM backlog_events WHERE client_id = %s ORDER BY id ASC', (client_id,))
            rows = cursor.fetchall()
            if not rows:
                return
            print(f"发现 客户端 {client_id} 断线期间积压了 {len(rows)} 条数据，开始补发...")
            for row in rows:
                event_id, event_data = row
                try:
                    await ws.send_str(event_data)
                    # 确定客户端完整接收消息后，再执行删除操作
                    cursor.execute('DELETE FROM backlog_events WHERE id = %s', (event_id,))
                    conn.commit()
                except Exception as e:
                    print(f"客户端 {client_id} 补发中断: {e}")
                    return

async def home_handler(request):
    return web.Response(text="Server is Awake!")

# 核心修改：Render 的自唤醒保活任务（每10分钟虚拟请求一次主页，防止服务Idle挂起）
async def keep_alive_loop():
    APP_URL = os.getenv("RENDER_EXTERNAL_URL") 
    if not APP_URL:
        print("未配置 RENDER_EXTERNAL_URL，跳过自动保活。")
        return

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(600)  # 每隔 600 秒触发一次自身探针
                response = await client.get(f"{APP_URL}/")
                print(f"Render 自唤醒心跳发送成功，状态码: {response.status_code}")
            except Exception as e:
                print(f"Render 自唤醒心跳失败: {e}")

async def lark_webhook_handler(request):
    try:
        data = await request.json()
        
        # 响应飞书首次 Challenge 校验
        if data.get("type") == "url_verification" and "challenge" in data:
            print("收到飞书 URL 首次验证请求，成功响应 challenge")
            return web.json_response({"challenge": data["challenge"]})
        
        message = json.dumps(data)
        print(f"get message from lark {message}", flush=True)
        
        event_obj = data.get("event", {})
        approval_code = event_obj.get("approval_code")
        
        if not approval_code:
            print("警告：未在飞书事件中解析到 approval_code", flush=True)
            return web.json_response({"status": "no_approval_code"})
            
        # 从飞书推送的数据结构中尝试解析状态以及实例标识（注意：根据您自己飞书事件的版本调整具体的层级抓取）
        status = event_obj.get("status")
        instance_code = event_obj.get("approval_instance_id") # 或者是 instance_code

        # 根据您的最新业务调整：审批通过，直接调用异步线程往飞书添加评论，不再去更新控件值
        if approval_code == authApprovalCode and status == "APPROVED" and instance_code:
            my_auth_code = generate_auth_code() 
            comment_content = f"系统自动生成授权码：{my_auth_code}"
            
            # 使用 aiohttp 推荐的默认线程池执行阻塞的 requests 请求
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, add_lark_comment, instance_code, comment_content)

        # 根据映射表获取需要接收该审批的客户端列表
        target_clients = ROUTE_MAP.get(approval_code, [])
        if not target_clients:
            print(f"未找到审批流程 {approval_code} 的路由规则", flush=True)
            return web.json_response({"status": "no_route_matched"})
            
        # 分发逻辑（在线的直接推，不在线的存数据库）
        tasks = []
        for client_id in target_clients:
            ws = active_connections.get(client_id)
            if ws and not ws.closed:
                print(f"C# 客户端 {client_id} 在线，直推数据...")
                tasks.append(ws.send_str(message))
            else:
                print(f"C# 客户端 {client_id} 不在线，数据已存至云端 PostgreSQL 队列...")
                # 不在线时，数据安全地推入 PostgreSQL 云数据库暂存
                '''if DATABASE_URL:
                    with psycopg2.connect(DATABASE_URL) as conn:
                        with conn.cursor() as cursor:
                            cursor.execute('INSERT INTO backlog_events (client_id, event_data) VALUES (%s, %s)', (client_id, message))
                        conn.commit()'''
        
        if tasks:
            await asyncio.gather(*tasks)
        
        return web.json_response({"status": "processed"})
        
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    # 获取 C# 客户端连接时携带的 client_id 参数
    client_id = request.query.get("client_id")
    if not client_id:
        print("拒绝连接：C# 客户端未提供 client_id 参数")
        await ws.close(code=4000, message=b"Missing client_id")
        return ws
    
    print(f"C# 客户端 {client_id} 已成功连接！")
    active_connections[client_id] = ws
    
    # 触发专属补发任务
    asyncio.create_task(flush_backlog(client_id, ws))
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT and msg.data == "ping":
                await ws.send_str("pong")
    finally:
        print(f"C# 客户端 {client_id} 已断开")
        # 安全移除连接
        if active_connections.get(client_id) == ws:
            del active_connections[client_id]

    return ws

# 注册 aiohttp 应用启动时的保活协程后台常驻运行
async def start_background_tasks(app):
    app['keep_alive'] = asyncio.create_task(keep_alive_loop())

app = web.Application()
app.on_startup.append(start_background_tasks)

app.router.add_get('/', home_handler)
app.router.add_post('/lark-webhook', lark_webhook_handler)
app.router.add_get('/ws', websocket_handler)

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=WS_PORT)
