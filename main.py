from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import json
import os
import time
import sqlite3
import threading
import http.server
import urllib.parse
import urllib.request
import urllib.error
from astrbot.api import AstrBotConfig

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(PLUGIN_DIR, "verify.db")

pending_codes = {}
web_server = None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS verify_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            player_name TEXT NOT NULL,
            xuid TEXT NOT NULL,
            unique_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            expire_at INTEGER
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS verify_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qq_number TEXT NOT NULL,
            player_name TEXT NOT NULL,
            xuid TEXT NOT NULL,
            unique_id TEXT NOT NULL,
            verify_code TEXT,
            status TEXT,
            created_at INTEGER
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS qq_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qq_number TEXT NOT NULL,
            attempt_count INTEGER DEFAULT 0,
            last_attempt INTEGER
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_type TEXT NOT NULL,
            qq_number TEXT,
            player_name TEXT,
            xuid TEXT,
            unique_id TEXT,
            verify_code TEXT,
            message TEXT,
            created_at INTEGER
        )
    ''')
    
    conn.commit()
    conn.close()

def add_log(log_type, qq_number="", player_name="", xuid="", unique_id="", verify_code="", message=""):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO logs (log_type, qq_number, player_name, xuid, unique_id, verify_code, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (log_type, qq_number, player_name, xuid, unique_id, verify_code, message, int(time.time()))
    )
    conn.commit()
    conn.close()
    logger.info(f"[QQ-MC验证] {log_type}: {message}")

def is_player_bound(player_name):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM verify_records WHERE player_name = ? AND status = 'success'", (player_name,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def is_qq_verified(qq_number):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM verify_records WHERE qq_number = ? AND status = 'success'", (qq_number,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_qq_attempt_count(qq_number):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT attempt_count FROM qq_attempts WHERE qq_number = ?", (qq_number,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_qq_attempt(qq_number):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT attempt_count FROM qq_attempts WHERE qq_number = ?", (qq_number,))
    result = cursor.fetchone()
    if result:
        cursor.execute(
            "UPDATE qq_attempts SET attempt_count = attempt_count + 1, last_attempt = ? WHERE qq_number = ?",
            (int(time.time()), qq_number)
        )
    else:
        cursor.execute(
            "INSERT INTO qq_attempts (qq_number, attempt_count, last_attempt) VALUES (?, 1, ?)",
            (qq_number, int(time.time()))
        )
    conn.commit()
    conn.close()

def store_verify_code(code, player_name, xuid, unique_id, expire_seconds=180):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO verify_codes (code, player_name, xuid, unique_id, status, created_at, expire_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (code, player_name, xuid, unique_id, int(time.time()), int(time.time()) + expire_seconds)
    )
    conn.commit()
    conn.close()

def get_verify_code(code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM verify_codes WHERE code = ? AND status = 'pending'", (code,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return {
            "id": result[0],
            "code": result[1],
            "player_name": result[2],
            "xuid": result[3],
            "unique_id": result[4],
            "status": result[5],
            "created_at": result[6],
            "expire_at": result[7]
        }
    return None

def update_verify_code_status(code, status):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE verify_codes SET status = ? WHERE code = ?", (status, code))
    conn.commit()
    conn.close()

def add_verify_record(qq_number, player_name, xuid, unique_id, verify_code, status):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO verify_records (qq_number, player_name, xuid, unique_id, verify_code, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (qq_number, player_name, xuid, unique_id, verify_code, status, int(time.time()))
    )
    conn.commit()
    conn.close()

def notify_mc_success(mc_url, player_name, qq_number):
    url = mc_url + "/api/give_reward"
    data = {
        "player_name": player_name,
        "qq_number": qq_number
    }
    
    logger.info(f"[QQ-MC验证] 通知MC端验证成功: {url}")
    logger.info(f"[QQ-MC验证] 发送数据: {json.dumps(data, ensure_ascii=False)}")
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=10)
        result = json.loads(response.read().decode('utf-8'))
        logger.info(f"[QQ-MC验证] MC端响应: {json.dumps(result, ensure_ascii=False)}")
        return result.get("status") == "ok"
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else '无响应体'
        logger.error(f"[QQ-MC验证] MC端返回HTTP错误: {e.code} {e.reason}")
        logger.error(f"[QQ-MC验证] 错误响应体: {error_body}")
        return False
    except Exception as e:
        logger.error(f"[QQ-MC验证] 通知MC端失败: {type(e).__name__}: {e}")
        return False

def notify_mc_failed(mc_url, player_name, reason):
    url = mc_url + "/api/verify_failed"
    data = {
        "player_name": player_name,
        "reason": reason
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=10)
        result = json.loads(response.read().decode('utf-8'))
        logger.info(f"[QQ-MC验证] 通知MC端验证失败: {result}")
        return True
    except Exception as e:
        logger.error(f"[QQ-MC验证] 通知MC端失败: {e}")
        return False

def clean_expired_codes():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    current_time = int(time.time())
    cursor.execute("UPDATE verify_codes SET status = 'expired' WHERE expire_at < ? AND status = 'pending'", (current_time,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted > 0:
        logger.info(f"[QQ-MC验证] 已清理 {deleted} 个过期验证码")

def clean_old_logs(days=30):
    cutoff = int(time.time()) - (days * 86400)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted > 0:
        logger.info(f"[QQ-MC验证] 已清理 {deleted} 条过期日志")

class WebHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
    
    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == "/api/status":
            self.send_json({"status": "ok"})
        
        elif parsed.path == "/api/pending":
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT code, player_name, xuid, unique_id, created_at, expire_at FROM verify_codes WHERE status = 'pending'")
            rows = cursor.fetchall()
            conn.close()
            data = [{"code": r[0], "player_name": r[1], "xuid": r[2], "unique_id": r[3], "created_at": r[4], "expire_at": r[5]} for r in rows]
            self.send_json({"pending": data})
        
        elif parsed.path == "/api/records":
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM verify_records ORDER BY created_at DESC LIMIT 100")
            rows = cursor.fetchall()
            conn.close()
            data = []
            for r in rows:
                data.append({
                    "id": r[0], "qq_number": r[1], "player_name": r[2], "xuid": r[3],
                    "unique_id": r[4], "verify_code": r[5], "status": r[6], "created_at": r[7]
                })
            self.send_json({"records": data})
        
        elif parsed.path == "/api/logs":
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 100")
            rows = cursor.fetchall()
            conn.close()
            data = []
            for r in rows:
                data.append({
                    "id": r[0], "log_type": r[1], "qq_number": r[2], "player_name": r[3],
                    "xuid": r[4], "unique_id": r[5], "verify_code": r[6], "message": r[7], "created_at": r[8]
                })
            self.send_json({"logs": data})
        
        else:
            self.send_json({"error": "Not found"}, 404)
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        
        try:
            data = json.loads(body)
        except:
            self.send_json({"error": "Invalid JSON"}, 400)
            return
        
        logger.info(f"[QQ-MC验证] 收到请求: {parsed.path} -> {json.dumps(data, ensure_ascii=False)}")
        
        if parsed.path == "/api/mc_verify_request":
            code = data.get("code", "")
            player_name = data.get("player_name", "")
            xuid = data.get("xuid", "")
            unique_id = data.get("unique_id", "")
            
            if not all([code, player_name, xuid, unique_id]):
                self.send_json({"error": "Missing parameters"}, 400)
                return
            
            if is_player_bound(player_name):
                self.send_json({"error": "该玩家已绑定QQ号"}, 400)
                return
            
            store_verify_code(code, player_name, xuid, unique_id, 180)
            add_log("verify_request", player_name=player_name, xuid=xuid, unique_id=unique_id, verify_code=code, message="MC端发起验证请求")
            
            logger.info(f"[QQ-MC验证] 验证码已存储: {code} -> {player_name}")
            self.send_json({"status": "ok", "message": "验证码已记录"})
        
        elif parsed.path == "/api/check_binding":
            player_name = data.get("player_name", "")
            xuid = data.get("xuid", "")
            
            if is_player_bound(player_name):
                self.send_json({"bound": True, "message": "该玩家已绑定QQ号"})
            else:
                self.send_json({"bound": False, "message": "该玩家未绑定"})
        
        else:
            self.send_json({"error": "Not found"}, 404)

def start_web_server(port, ready_event=None):
    global web_server
    try:
        server_address = ("0.0.0.0", port)
        web_server = http.server.HTTPServer(server_address, WebHandler)
        logger.info(f"[QQ-MC验证] Web服务器启动成功，端口: {port}")
        if ready_event:
            ready_event.set()
        web_server.serve_forever()
    except OSError as e:
        if "10048" in str(e) or "Address already in use" in str(e) or "98" in str(e):
            logger.error(f"[QQ-MC验证] 端口 {port} 已被占用，请修改配置中的web_port")
        else:
            logger.error(f"[QQ-MC验证] Web服务器启动失败(OSError): {e}")
    except Exception as e:
        logger.error(f"[QQ-MC验证] Web服务器启动失败: {type(e).__name__}: {e}")

@register("qq_mc_verify", "zhaisir", "QQ群验证我的世界账号插件", "1.0.0")
class QQMCVerifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        init_db()
        logger.info("[QQ-MC验证] 数据库初始化完成")
        
        if config.get("clear_database", False):
            self._do_clear_database()
            config["clear_database"] = False
            config.save_config()
        
        web_port = config.get("web_port", 9090)
        logger.info(f"[QQ-MC验证] 准备启动Web服务器，端口: {web_port}")
        
        ready_event = threading.Event()
        
        def run_server():
            try:
                start_web_server(web_port, ready_event)
            except Exception as e:
                logger.error(f"[QQ-MC验证] Web服务器线程异常: {e}")
        
        t = threading.Thread(target=run_server, daemon=True)
        t.start()
        
        ready_event.wait(timeout=5)
        
        if ready_event.is_set():
            logger.info(f"[QQ-MC验证] Web服务器已成功监听端口 {web_port}")
        else:
            logger.error(f"[QQ-MC验证] Web服务器启动失败，请检查端口 {web_port} 是否被占用")
        
        clean_expired_codes()
        clean_old_logs(config.get("log_retention_days", 30))
    
    def _do_clear_database(self):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM verify_codes")
        cursor.execute("DELETE FROM verify_records")
        cursor.execute("DELETE FROM qq_attempts")
        cursor.execute("DELETE FROM logs")
        
        conn.commit()
        conn.close()
        
        logger.info("[QQ-MC验证] 数据库已通过配置清空")
    
    async def initialize(self):
        logger.info("[QQ-MC验证] 插件初始化完成")
    
    @filter.command("验证")
    async def verify(self, event: AstrMessageEvent, code: str):
        '''验证我的世界账号
        
        Args:
            code(string): 验证码
        '''
        qq_number = event.get_sender_id()
        
        if is_qq_verified(qq_number):
            add_log("verify_rejected", qq_number=qq_number, verify_code=code, message="QQ号已验证过")
            yield event.plain_result("您的QQ号已绑定过游戏账号")
            return
        
        attempt_count = get_qq_attempt_count(qq_number)
        if attempt_count >= 3:
            add_log("verify_rejected", qq_number=qq_number, verify_code=code, message="验证次数超限")
            yield event.plain_result("您的验证次数已达上限，请联系管理员")
            return
        
        verify_data = get_verify_code(code)
        if not verify_data:
            increment_qq_attempt(qq_number)
            add_log("verify_failed", qq_number=qq_number, verify_code=code, message="验证码无效或已过期")
            yield event.plain_result("验证码无效或已过期")
            return
        
        current_time = int(time.time())
        if verify_data["expire_at"] < current_time:
            update_verify_code_status(code, "expired")
            increment_qq_attempt(qq_number)
            add_log("verify_failed", qq_number=qq_number, verify_code=code, message="验证码已过期")
            yield event.plain_result("验证码已过期")
            return
        
        update_verify_code_status(code, "used")
        add_verify_record(
            qq_number,
            verify_data["player_name"],
            verify_data["xuid"],
            verify_data["unique_id"],
            code,
            "success"
        )
        add_log("verify_success", qq_number, verify_data["player_name"], verify_data["xuid"], verify_data["unique_id"], code, "验证成功")
        
        mc_url = self.config.get("mc_bot_url", "http://localhost:9091")
        if notify_mc_success(mc_url, verify_data["player_name"], qq_number):
            yield event.plain_result(f"验证成功！您的QQ号已绑定游戏账号: {verify_data['player_name']}")
        else:
            yield event.plain_result(f"验证成功！但通知游戏服务器失败，请联系管理员\n已绑定账号: {verify_data['player_name']}")
    
    @filter.command("验证状态")
    async def verify_status(self, event: AstrMessageEvent):
        '''查看验证状态'''
        qq_number = event.get_sender_id()
        
        if is_qq_verified(qq_number):
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT player_name, created_at FROM verify_records WHERE qq_number = ? AND status = 'success'", (qq_number,))
            result = cursor.fetchone()
            conn.close()
            if result:
                time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result[1]))
                yield event.plain_result(f"您已验证账号: {result[0]}\n验证时间: {time_str}")
        else:
            attempt_count = get_qq_attempt_count(qq_number)
            yield event.plain_result(f"您尚未验证账号\n已使用验证次数: {attempt_count}/3")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("验证列表")
    async def verify_list(self, event: AstrMessageEvent):
        '''查看待验证列表（管理员）'''
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT code, player_name, created_at, expire_at FROM verify_codes WHERE status = 'pending'")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            yield event.plain_result("当前没有待验证的请求")
            return
        
        msg = "待验证列表:\n"
        for r in rows:
            expire_str = time.strftime("%H:%M:%S", time.localtime(r[3]))
            msg += f"验证码: {r[0]} 玩家: {r[1]} 过期: {expire_str}\n"
        yield event.plain_result(msg)
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("验证记录")
    async def verify_records(self, event: AstrMessageEvent):
        '''查看验证记录（管理员）'''
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT qq_number, player_name, status, created_at FROM verify_records ORDER BY created_at DESC LIMIT 10")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            yield event.plain_result("暂无验证记录")
            return
        
        msg = "最近验证记录:\n"
        for r in rows:
            time_str = time.strftime("%m-%d %H:%M", time.localtime(r[3]))
            status = "成功" if r[2] == "success" else "失败"
            msg += f"QQ: {r[0]} 玩家: {r[1]} 状态: {status} 时间: {time_str}\n"
        yield event.plain_result(msg)
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清除数据库")
    async def clear_database(self, event: AstrMessageEvent):
        '''一键清除所有数据库（管理员）'''
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM verify_codes")
        codes_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM verify_records")
        records_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM qq_attempts")
        attempts_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM logs")
        logs_count = cursor.fetchone()[0]
        
        cursor.execute("DELETE FROM verify_codes")
        cursor.execute("DELETE FROM verify_records")
        cursor.execute("DELETE FROM qq_attempts")
        cursor.execute("DELETE FROM logs")
        
        conn.commit()
        conn.close()
        
        add_log("database_cleared", message="数据库已清空")
        
        msg = f"数据库已清空!\n"
        msg += f"清除验证码: {codes_count} 条\n"
        msg += f"清除验证记录: {records_count} 条\n"
        msg += f"清除尝试记录: {attempts_count} 条\n"
        msg += f"清除日志: {logs_count} 条"
        
        yield event.plain_result(msg)
    
    async def terminate(self):
        global web_server
        if web_server:
            web_server.shutdown()
        logger.info("[QQ-MC验证] 插件已卸载")
