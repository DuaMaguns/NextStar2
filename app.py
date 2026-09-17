from flask import Flask, request, jsonify, redirect
import requests
import json
import re
import ast
import os
import time
import sqlite3
import threading
import uuid
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# ===== SQLite 存储（用户信息收集）=====
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'contacts.db')
_db_lock = threading.Lock()

def _init_db():
    """初始化 SQLite 数据库与 contacts 表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    conn.commit()
    conn.close()

_init_db()

# ===== 简单 IP 限流（SQLite 持久化，跨 worker 生效）=====
_rate_lock = threading.Lock()

def _init_rate_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS rate_limit (
            ip TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            window_start REAL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

_init_rate_table()


def _init_oauth_table():
    """知乎登录用：state 防重放表 + 知乎用户表（与 contacts 分开，不动既有表结构）"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            base_path TEXT DEFAULT '',
            created_at REAL DEFAULT 0
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS zhihu_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zhihu_uid TEXT DEFAULT '',
            name TEXT DEFAULT '',
            avatar TEXT DEFAULT '',
            headline TEXT DEFAULT '',
            home_url TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    # 知乎回调可能不回传 state（官方文档只列 redirect_uri/app_id/response_type）：
    # 加一列如实记录本次登录是否通过 CSRF 校验。旧库没这列时 ALTER 会报错，忽略即可。
    try:
        cur.execute("ALTER TABLE zhihu_users ADD COLUMN state_verified INTEGER DEFAULT 1")
    except Exception:
        pass
    for _col_sql in (
        "ALTER TABLE zhihu_users ADD COLUMN fullname TEXT DEFAULT ''",
        "ALTER TABLE zhihu_users ADD COLUMN gender TEXT DEFAULT ''",
        "ALTER TABLE zhihu_users ADD COLUMN phone TEXT DEFAULT ''",
        "ALTER TABLE zhihu_users ADD COLUMN email TEXT DEFAULT ''",
        "ALTER TABLE zhihu_users ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE zhihu_users ADD COLUMN interest_profile TEXT DEFAULT ''",
        "ALTER TABLE zhihu_users ADD COLUMN analyzed_at TEXT DEFAULT ''",
    ):
        try:
            cur.execute(_col_sql)
        except Exception:
            pass
    conn.commit()
    conn.close()


_init_oauth_table()


def _check_rate_limit(ip, max_requests=5, window_seconds=3600):
    """同一 IP 在 window 内最多 max_requests 次提交（基于 SQLite，跨 worker 一致）"""
    now = time.time()
    with _rate_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT count, window_start FROM rate_limit WHERE ip = ?", (ip,))
        row = cur.fetchone()
        if row:
            count, window_start = row
            if now - window_start > window_seconds:
                # 窗口已过，重置
                cur.execute(
                    "UPDATE rate_limit SET count = 1, window_start = ? WHERE ip = ?",
                    (now, ip)
                )
                conn.commit(); conn.close()
                return True
            if count >= max_requests:
                conn.close()
                return False
            cur.execute("UPDATE rate_limit SET count = count + 1 WHERE ip = ?", (ip,))
            conn.commit(); conn.close()
            return True
        else:
            cur.execute(
                "INSERT INTO rate_limit (ip, count, window_start) VALUES (?, 1, ?)",
                (ip, now)
            )
            conn.commit(); conn.close()
            return True

def _client_ip():
    """优先取 X-Forwarded-For（经 nginx 反代）"""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def load_api_key():
    """从环境变量或项目外的 config.json 读取 API Key"""
    # 1. 优先从环境变量读取
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key

    # 2. 从项目外的 config.json 读取（同级目录或上级目录）
    config_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json'),
        os.path.join(os.getcwd(), 'config.json'),
        os.path.join(os.getcwd(), '..', 'config.json'),
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    key = config.get("deepseek_api_key", "")
                    if key:
                        return key
            except Exception:
                pass

    return ""


DEFAULT_API_KEY = load_api_key()

# 复用同一条 HTTPS 长连接池：省掉每次调用都要重做的 TLS 握手（单次 0.2-0.5s）。
# 展开阶段一次并发 8 个请求，池子给到 16，避免排队重建连接。
HTTP_POOL = requests.Session()
HTTP_POOL.mount('https://', requests.adapters.HTTPAdapter(
    pool_connections=16, pool_maxsize=16))


def load_zhihu_api_key():
    """从环境变量或项目外的 config.json 读取知乎直答 Access Secret"""
    key = os.environ.get("ZHIHU_ACCESS_SECRET")
    if key:
        return key

    config_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json'),
        os.path.join(os.getcwd(), 'config.json'),
        os.path.join(os.getcwd(), '..', 'config.json'),
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    key = config.get("zhihu_access_secret", "")
                    if key:
                        return key
            except Exception:
                pass

    return ""


ZHIHU_API_KEY = load_zhihu_api_key()


# ===== 知乎 OAuth 2.0 登录（开放平台 openapi.zhihu.com）=====
# 协议已按知乎服务端自身报错逐项验证：
#   授权页   GET  https://openapi.zhihu.com/authorize
#   换 token POST https://openapi.zhihu.com/access_token
#            必填 app_id -> app_key -> grant_type=authorization_code -> redirect_uri -> code
#   取用户   GET  https://openapi.zhihu.com/user     头 Authorization: Bearer <token>
ZHIHU_AUTHORIZE_URL = 'https://openapi.zhihu.com/authorize'
ZHIHU_TOKEN_URL = 'https://openapi.zhihu.com/access_token'
ZHIHU_USER_URL = 'https://openapi.zhihu.com/user'
ZHIHU_MOMENTS_URL = 'https://openapi.zhihu.com/user/moments'
ZHIHU_CALLBACK_SUFFIX = '/api/auth/zhihu/callback'


def _load_zhihu_oauth_section():
    """从 config.json 读取 zhihu_oauth 段（与 deepseek_api_key 同一个文件）"""
    config_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json'),
        os.path.join(os.getcwd(), 'config.json'),
        os.path.join(os.getcwd(), '..', 'config.json'),
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                section = config.get('zhihu_oauth') or {}
                if section:
                    return section
            except Exception:
                pass
    return {}


def zhihu_oauth_conf():
    """合并环境变量与 config.json，得到知乎 OAuth 运行配置。

    每次请求读一次（config.json 只有几百字节），这样凭据填好后无需重启即可生效。
    """
    section = _load_zhihu_oauth_section()

    def pick(env_name, key):
        value = os.environ.get(env_name)
        if value:
            return str(value).strip()
        return str(section.get(key) or '').strip()

    app_id = pick('ZHIHU_OAUTH_APP_ID', 'app_id')
    app_key = pick('ZHIHU_OAUTH_APP_KEY', 'app_key')
    origin = pick('ZHIHU_OAUTH_ORIGIN', 'origin').rstrip('/')

    paths = section.get('paths') or []
    if isinstance(paths, str):
        paths = [paths]
    paths = [str(p).strip().rstrip('/') for p in paths if str(p).strip()]
    if not paths:
        paths = ['/nextstar', '/zhihunextstar']
    # 回调地址前缀：知乎只接受平台登记过的 redirect_uri，目前平台上只登记了
    # /nextstar 一个，所以两个实例统一用这一个，不再按实例前缀拼地址。
    callback_prefix = pick('ZHIHU_OAUTH_CALLBACK_PREFIX', 'callback_prefix').rstrip('/')
    if callback_prefix and not callback_prefix.startswith('/'):
        callback_prefix = '/' + callback_prefix
    if callback_prefix not in paths:
        callback_prefix = paths[0] if paths else ''

    return {
        'app_id': app_id,
        'app_key': app_key,
        'origin': origin,
        'paths': paths,
        'callback_prefix': callback_prefix,
        'enabled': bool(app_id and app_key and origin),
    }


def zhihu_redirect_uri(base_path):
    """回调地址：两实例统一发「已登记在知乎开放平台」的那一个。

    知乎只接受平台登记过的 redirect_uri，且 /authorize 与 /access_token 必须
    逐字节一致；平台上目前只登记了 /nextstar 一个，所以这里不再按实例前缀拼地址。
    base_path 仅保留兼容旧调用点。镜像用户的回调会被 nginx 路由到主站，
    由 zhihu_auth_callback 按 Cookie 里的实例前缀 302 交还给镜像自己处理。
    """
    conf = zhihu_oauth_conf()
    prefix = conf.get('callback_prefix') or ''
    if prefix not in conf['paths']:
        prefix = conf['paths'][0] if conf['paths'] else ''
    return conf['origin'] + prefix + ZHIHU_CALLBACK_SUFFIX


def _zhihu_base_from_request(conf):
    """判定这个请求来自哪个实例前缀。

    ?base= 优先（前端知道自己挂在哪个前缀下）；其次看 Referer（浏览器顶层跳转会
    带上当前页地址，同源默认策略是带全路径）；最后才回退到第一个白名单前缀。
    """
    base = (request.args.get('base') or '').strip().rstrip('/')
    if base in conf['paths']:
        return base

    ref = request.headers.get('Referer', '') or ''
    if ref:
        try:
            ref_path = urllib.parse.urlparse(ref).path or ''
        except Exception:
            ref_path = ''
        seg = ref_path.strip('/').split('/')[0] if ref_path.strip('/') else ''
        if seg and ('/' + seg) in conf['paths']:
            return '/' + seg

    return conf['paths'][0] if conf['paths'] else ''


def _redirect_path(path):
    """302 到站内路径，Location 保持为绝对路径（如 '/nextstar/app'）。

    不用 Flask 的 redirect()：Werkzeug 2.0.3 的 Response.autocorrect_location_header
    默认为 True，出站时会把 '/path' 改写成 'http://host/path'；经 nginx 反代时
    Werkzeug 拿不到真实协议，https 就被降级成 http，白白多一次跳转。
    """
    resp = app.response_class('', status=302)
    try:
        resp.autocorrect_location_header = False
    except Exception:
        pass
    resp.headers['Location'] = path
    return resp


def _deep_pick_dict(obj, marker_keys):
    """在嵌套 JSON 里找出「用户对象」：第一个自身含有候选字段的 dict。"""
    if isinstance(obj, dict):
        for k in marker_keys:
            if k in obj:
                return obj
        for v in obj.values():
            got = _deep_pick_dict(v, marker_keys)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _deep_pick_dict(v, marker_keys)
            if got:
                return got
    return {}


def _str_of(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ''


def load_mysql_config():
    """从 config.json 读取 MySQL 连接配置（预留，未配置时返回空字典）"""
    config_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json'),
        os.path.join(os.getcwd(), 'config.json'),
        os.path.join(os.getcwd(), '..', 'config.json'),
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    return config.get("mysql", {}) or {}
            except Exception:
                pass
    return {}


def save_contact_to_json(contact):
    """本地暂存联系方式到 data/contacts.json（MySQL 就绪前的兜底）"""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(project_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    file_path = os.path.join(data_dir, 'contacts.json')

    records = []
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                records = json.load(f)
        except Exception:
            records = []

    records.append({
        "name": contact["name"],
        "phone": contact["phone"],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    })

    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def save_contact_to_mysql(contact):
    """将联系方式写入 MySQL（预留）。
    建表 SQL：
    CREATE TABLE IF NOT EXISTS contacts (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(50) NOT NULL,
        phone VARCHAR(20) NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    try:
        import pymysql
    except ImportError:
        return False

    db = load_mysql_config()
    if not db or not db.get("host") or not db.get("user") or not db.get("database"):
        return False

    try:
        conn = pymysql.connect(
            host=db["host"],
            port=int(db.get("port", 3306)),
            user=db["user"],
            password=db.get("password", ""),
            database=db["database"],
            charset="utf8mb4"
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contacts (name, phone) VALUES (%s, %s)",
                (contact["name"], contact["phone"])
            )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


@app.route('/api/contact', methods=['POST'])
def save_contact():
    """收集用户联系方式：SQLite 存储 + 字段校验 + IP 限流"""
    try:
        ip = _client_ip()
        if not _check_rate_limit(ip):
            return jsonify({"code": -1, "msg": "提交过于频繁，请稍后再试"})

        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        phone = (data.get('phone') or '').strip()
        email = (data.get('email') or '').strip()

        # 字段校验
        if not name:
            return jsonify({"code": -1, "msg": "请填写姓名"})
        if len(name) > 50:
            return jsonify({"code": -1, "msg": "姓名过长"})
        if not phone:
            return jsonify({"code": -1, "msg": "请填写手机号"})
        if not re.fullmatch(r'1\d{10}', phone):
            return jsonify({"code": -1, "msg": "请输入正确的11位手机号"})
        if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            return jsonify({"code": -1, "msg": "请输入正确的邮箱地址"})

        # 写入 SQLite
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO contacts (name, phone, email, ip) VALUES (?, ?, ?, ?)",
                (name, phone, email, ip)
            )
            conn.commit()
            conn.close()

        return jsonify({"code": 0, "msg": "success"})
    except Exception as e:
        return jsonify({"code": -1, "msg": f"提交失败：{e}"})


# ===== 数据查看后台（简单密码保护）=====
ADMIN_PASSWORD = os.environ.get('CONTACTS_ADMIN_PASSWORD', 'nextster2026')

@app.route('/admin/contacts')
def admin_contacts():
    """查看已收集的联系方式（需密码），?format=csv 导出"""
    token = request.args.get('token', '')
    if token != ADMIN_PASSWORD:
        return jsonify({"code": -1, "msg": "无权限，请在 URL 后加 ?token=密码"}), 403

    fmt = request.args.get('format', '')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, name, phone, email, ip, created_at FROM contacts ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # CSV 导出
    if fmt == 'csv':
        import csv as csv_mod
        import io
        out = io.StringIO()
        writer = csv_mod.writer(out)
        writer.writerow(['ID', '姓名', '手机号', '邮箱', 'IP', '时间'])
        for r in reversed(rows):  # 按时间正序导出
            writer.writerow([r['id'], r['name'], r['phone'], r['email'], r['ip'], r['created_at']])
        csv_bytes = '\ufeff' + out.getvalue()  # BOM 便于 Excel 识别中文
        from flask import Response
        return Response(csv_bytes, mimetype='text/csv', headers={
            'Content-Disposition': 'attachment; filename=contacts.csv'
        })

    # HTML 表格（手机号表单）
    rows_html = ""
    for r in rows:
        rows_html += (
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                r['id'], r['name'], r['phone'], r['email'], r['ip'], r['created_at']
            )
        )

    # 知乎登录用户（各自一张表，统计互不干扰）
    zconn = sqlite3.connect(DB_PATH)
    zconn.row_factory = sqlite3.Row
    zcur = zconn.cursor()
    zcur.execute(
        "SELECT id, zhihu_uid, name, avatar, headline, home_url, phone, ip, created_at"
        " FROM zhihu_users ORDER BY id DESC"
    )
    zrows = [dict(z) for z in zcur.fetchall()]
    zconn.close()
    zhihu_html = ""
    for z in zrows:
        zhihu_html += (
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                z['id'], z['zhihu_uid'], z['name'], z['headline'], z['home_url'], z['phone'], z['created_at']
            )
        )

    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
    <title>联系方式收集数据</title>
    <style>body{{font-family:system-ui,sans-serif;margin:24px;background:#0c0c1e;color:#e2e8f0}}
    h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin-top:16px}}
    th,td{{border:1px solid #334155;padding:8px 12px;text-align:left;font-size:14px}}
    th{{background:#1e1e4e}} tr:nth-child(even){{background:#1a1a3e}}
    a{{color:#7c9cf5;display:inline-block;margin-top:16px}}
    h1.sec{{margin-top:36px}}</style></head><body>
    <h1>联系方式收集数据（共 {} 条）</h1>
    <a href="/admin/contacts?token={}&format=csv">导出 CSV</a>
    <table><thead><tr><th>ID</th><th>姓名</th><th>手机号</th><th>邮箱</th><th>IP</th><th>时间</th></tr></thead>
    <tbody>{}</tbody></table>
    <h1 class="sec">知乎登录用户（共 {} 条）</h1>
    <table><thead><tr><th>ID</th><th>知乎UID</th><th>昵称</th><th>签名</th><th>主页</th><th>手机号</th><th>时间</th></tr></thead>
    <tbody>{}</tbody></table></body></html>""".format(
        len(rows), ADMIN_PASSWORD, rows_html, len(zrows), zhihu_html)
    return html


# ===== 知乎 OAuth 登录路由 =====
ZHIHU_PROFILE_SYSTEM_PROMPT = (
    '你是用户兴趣分析专家。只输出合法 JSON，不输出任何解释文字，不使用 Markdown 代码块。'
)


def _fetch_zhihu_moments(access_token):
    """Fetch Zhihu moments feed; return None on any error (best effort)."""
    try:
        resp = requests.get(
            ZHIHU_MOMENTS_URL,
            headers={'Authorization': 'Bearer ' + access_token},
            timeout=10
        )
        data = resp.json()
    except Exception as e:
        print("[zhihu-oauth] moments fetch failed: %s" % e, flush=True)
        return None
    items = data.get('data') if isinstance(data, dict) else None
    return items if isinstance(items, list) else None


def _zhihu_profile_fallback(user_info):
    """Fallback profile built from headline/description when LLM fails."""
    text = ((user_info.get('headline') or '') + ' '
            + (user_info.get('description') or '')).strip()
    if not text:
        return None
    return {"keywords": [], "summary": text[:80], "source": "basic"}


def _analyze_zhihu_profile_bg(row_id, user_info, access_token):
    """Background thread: moments + profile -> interest JSON -> DB."""
    try:
        moments = _fetch_zhihu_moments(access_token)
        lines = []
        for i, it in enumerate((moments or [])[:20], 1):
            if not isinstance(it, dict):
                continue
            action = re.sub(r'\s+', ' ', str(it.get('action_text') or '')).strip()[:20]
            target = it.get('target') if isinstance(it.get('target'), dict) else {}
            title = re.sub(r'\s+', ' ', str(target.get('title') or '')).strip()[:60]
            excerpt = re.sub(r'\s+', ' ', str(target.get('excerpt') or '')).strip()[:100]
            if title or excerpt:
                lines.append("%d) %s：%s | %s"
                             % (i, action or '关注了', title, excerpt))
        profile = None
        base_text = ((user_info.get('headline') or '') + ' '
                     + (user_info.get('description') or '')).strip()[:200]
        user_content = (
            '下面是一位知乎用户的资料与最近的关注动态。\n'
            '昵称简介：' + base_text + "\n"
            '关注动态：\n' + ("\n".join(lines) if lines else '（暂无）') + "\n\n"
            '请分析这个人可能感兴趣的领域与学习方向，输出可用于学业与职业推荐的兴趣画像。只输出如下 JSON：\n'
            '{"keywords":["兴趣关键词4到8个，每个2到6字"],'
            '"summary":"不超过60字的整体兴趣画像"}'
        )
        parsed = _llm_json_with_prompt(
            ZHIHU_PROFILE_SYSTEM_PROMPT, user_content, max_tokens=400, temperature=0.3
        )
        if isinstance(parsed, dict) and (parsed.get('keywords') or parsed.get('summary')):
            kws = parsed.get('keywords')
            if not isinstance(kws, list):
                kws = []
            kws = [str(k).strip()[:12] for k in kws if str(k).strip()][:8]
            profile = {"keywords": kws, "summary": str(parsed.get('summary') or '')[:80],
                       "source": "llm"}
        if profile is None:
            profile = _zhihu_profile_fallback(user_info)
        if profile is None:
            return
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "UPDATE zhihu_users SET interest_profile = ?,"
                " analyzed_at = datetime('now', 'localtime') WHERE id = ?",
                (json.dumps(profile, ensure_ascii=False), row_id)
            )
            conn.commit()
            conn.close()
        print("[zhihu-oauth] interest profile saved for row %s" % row_id, flush=True)
    except Exception as e:
        print("[zhihu-oauth] profile analyze failed: %s" % e, flush=True)


def _load_zhihu_interest_profile():
    """Read interest profile by zhihu_uid cookie; None when absent."""
    try:
        uid = (request.cookies.get('zhihu_uid') or '').strip()
        if not uid:
            return None
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT interest_profile FROM zhihu_users"
            " WHERE zhihu_uid = ? AND interest_profile != ''"
            " ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        prof = json.loads(row[0])
        return prof if isinstance(prof, dict) else None
    except Exception:
        return None


def _mask_contact(v):
    """Mask phone/email for display."""
    v = (v or '').strip()
    if not v:
        return ''
    if '@' in v:
        head, _, tail = v.partition('@')
        shown = head[:2] if len(head) > 2 else head[:1]
        return shown + '***@' + tail
    if len(v) >= 8:
        return v[:3] + '****' + v[-4:]
    return '***'


@app.route('/api/auth/zhihu/me')
def zhihu_auth_me():
    """Zhihu login state info (for frontend hints); logged_in=false if absent."""
    uid = (request.cookies.get('zhihu_uid') or '').strip()
    if not uid:
        return jsonify({"code": 0, "logged_in": False})
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT name, avatar, phone, email, interest_profile FROM zhihu_users"
        " WHERE zhihu_uid = ? ORDER BY id DESC LIMIT 1",
        (uid,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"code": 0, "logged_in": False})
    name, avatar, phone, email, profile_text = row
    interests = []
    summary = ''
    try:
        prof = json.loads(profile_text or '')
        if isinstance(prof, dict):
            kws = prof.get('keywords')
            if isinstance(kws, list):
                interests = [str(k) for k in kws][:8]
            summary = str(prof.get('summary') or '')
    except Exception:
        pass
    return jsonify({
        "code": 0, "logged_in": True, "name": name or '知乎用户',
        "avatar": avatar or '', "phone": _mask_contact(phone),
        "email": _mask_contact(email), "interests": interests,
        "interest_summary": summary,
    })


@app.route('/api/auth/zhihu/status')
def zhihu_auth_status():
    """前端据此决定「知乎登录」这个 tab 是否可用"""
    conf = zhihu_oauth_conf()
    return jsonify({"code": 0, "enabled": conf['enabled']})


@app.route('/api/auth/zhihu/start')
def zhihu_auth_start():
    """跳转到知乎授权页。?base=/zhihunextstar 指定所属实例前缀（决定回调地址）"""
    conf = zhihu_oauth_conf()
    if not conf['enabled']:
        return jsonify({"code": -1, "msg": "知乎登录尚未配置，请先用手机号提交"}), 503

    base = _zhihu_base_from_request(conf)

    state = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    now = time.time()
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        # 顺手清掉 30 分钟前的旧 state，避免表无限增长
        cur.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 1800,))
        cur.execute(
            "INSERT OR REPLACE INTO oauth_states (state, base_path, created_at) VALUES (?, ?, ?)",
            (state, base, now)
        )
        conn.commit()
        conn.close()

    params = {
        'app_id': conf['app_id'],
        'redirect_uri': zhihu_redirect_uri(base),
        'response_type': 'code',
        'state': state,
    }
    resp = _redirect_path(ZHIHU_AUTHORIZE_URL + '?' + urllib.parse.urlencode(params))
    # 实例前缀必须自己带过整个 OAuth 往返：nginx 会把 /<prefix>/ 前缀剥掉再转给
    # Flask，所以回调进来时 request.path 只剩 /api/auth/zhihu/callback；而实测知乎可能
    # 不回传 state（唯一的另一个载体）。用 Cookie 兜底：SameSite=Lax 的顶层 GET
    # 跳转会把 Cookie 带回来，换 token 时才能用与 /authorize 完全一致的
    # redirect_uri（不一致会被知乎拒）。
    try:
        resp.set_cookie('zhihu_oauth_base', base or '', max_age=1800,
                        path='/', httponly=True, samesite='Lax')
    except Exception:
        pass
    return resp


@app.route('/api/auth/zhihu/callback')
def zhihu_auth_callback():
    """知乎回调：校验 state -> 换 token -> 取用户信息 -> 入库 -> 进 /app"""
    conf = zhihu_oauth_conf()

    # 官方回调参数是 authorization_code（不是标准 OAuth 的 code），两者都认。
    code = (request.args.get('authorization_code') or request.args.get('code') or '').strip()
    state = (request.args.get('state') or '').strip()
    err = (request.args.get('error') or '').strip()

    # state 是我们自己发出的一次性随机串，用于自证这个回调不是伪造的（防 CSRF / 重放）。
    # ★ 实测知乎回调可能根本不回传 state（官方授权地址只列 redirect_uri / app_id /
    #   response_type 三个参数）。所以判定分两档，不能一律失败，否则真实登录永远进不来：
    #     回传了 state 但不匹配 -> 判定为伪造回调，失败；
    #     完全没有回传 state   -> 降级为「未经 CSRF 校验」，如实记录后继续。
    base = ''
    state_ok = False
    if state:
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT base_path FROM oauth_states WHERE state = ?", (state,))
            row = cur.fetchone()
            if row:
                state_ok = True
                base = row[0] or ''
            cur.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            conn.commit()
            conn.close()

    if not state_ok:
        # 没有 state 时前缀按三层兜底。原因：nginx 会把 /<prefix>/ 前缀剥掉再
        # 转给 Flask，回调进来时 request.path 只剩 /api/auth/zhihu/callback，光看路径认不出
        # 实例；Referer 又是知乎授权页，也不可用。
        cand = ''
        # 1) /start 时种下的 Cookie（SameSite=Lax的顶层 GET 跳转会被带回）
        try:
            cookie_base = (request.cookies.get('zhihu_oauth_base') or '').strip().rstrip('/')
            if cookie_base in conf['paths']:
                cand = cookie_base
        except Exception:
            cand = ''
        # 2) 万一某个部署没剥前缀，路径第一段就是前缀
        if not cand:
            seg = [s for s in (request.path or '').split('/') if s]
            seg_cand = ('/' + seg[0]) if seg else ''
            if seg_cand in conf['paths']:
                cand = seg_cand
        # 3) 都没有就留空，下面用相对路径兜底
        base = cand

    # ★ 两实例共用同一个已登记的 redirect_uri（/nextstar），所以镜像用户的回调
    #   会被 nginx 路由到主站实例，而 state 落在镜像自己的库里。这里按实例前缀
    #   把请求 302 交还给真正的主人，由它用自己的库完成换 token 与登录。
    #   用站内绝对路径（同域），既绕开 Werkzeug 改写 Location，也不会被降级成 http。
    #   ?fwd=1 表示已经转发过一次，不再二次转发（防环）。
    if not (request.args.get('fwd') or '').strip():
        hint = base
        if not hint:
            try:
                ck = (request.cookies.get('zhihu_oauth_base') or '').strip().rstrip('/')
            except Exception:
                ck = ''
            if ck in conf['paths']:
                hint = ck
        if hint and hint in conf['paths'] and hint != conf.get('callback_prefix'):
            qs = request.query_string.decode('utf-8', 'replace')
            print("[zhihu-oauth] forward callback to instance %s" % hint, flush=True)
            return _redirect_path(hint + ZHIHU_CALLBACK_SUFFIX
                                  + '?' + qs + ('&' if qs else '') + 'fwd=1')

    # 仍然判不出实例时，退回相对路径本站根：'../../../' 从
    # /<prefix>/api/auth/zhihu/callback 解析出来正好是 /<prefix>/，
    # 对 /nextstar 与 /zhihunextstar 同时成立，不会把镜像用户甩到主站。
    landing = (base + '/') if base else '../../../'

    def fail(reason):
        return _redirect_path(landing + '?zhihu_error=' + urllib.parse.quote(reason))

    if not conf['enabled']:
        return fail('知乎登录尚未配置')
    if state and not state_ok:
        return fail('登录状态已失效，请重新发起知乎登录')
    if err or not code:
        return fail('知乎授权未完成，请重试')
    if not _check_rate_limit(_client_ip()):
        return fail('操作过于频繁，请稍后再试')
    if not state_ok:
        print("[zhihu-oauth] callback without state: 仅适合临时联调，未经 CSRF 校验", flush=True)

    # 1) code 换 access_token
    # 换 token 时的 redirect_uri 必须与 /authorize 时逐字节一致，
    # 否则知乎直接拒。实例前缀平时由 state 或 Cookie 带回来；
    # 两者都缺（或平台只登记了其中一个回调地址）时会用错，
    # 所以按「猜到的前缀优先、其余白名单前缀兜底」逐个试一次，
    # 全部失败才算失败（失败不会消耗 code，成功才会）。
    # redirect_uri 现在与实例前缀无关（两实例统一同一个已登记地址），
    # 所以按「最终地址」去重，避免拿同一个 code 重复打上游。
    cand_bases = []
    _seen_redirect = []
    for cand in ([base] if base else []) + list(conf['paths']):
        _uri = zhihu_redirect_uri(cand)
        if _uri not in _seen_redirect:
            _seen_redirect.append(_uri)
            cand_bases.append(cand)
    if not cand_bases:
        cand_bases = ['']

    token_text = ''
    token_data = {}
    access_token = ''
    last_error = ''
    for cand in cand_bases:
        try:
            token_resp = requests.post(ZHIHU_TOKEN_URL, data={
                'app_id': conf['app_id'],
                'app_key': conf['app_key'],
                'grant_type': 'authorization_code',
                'redirect_uri': zhihu_redirect_uri(cand),
                'code': code,
            }, timeout=15)
            token_text = token_resp.text
            try:
                token_data = token_resp.json()
            except Exception:
                token_data = {}
        except Exception as e:
            print("[zhihu-oauth] token transport error (%s): %s"
                  % (cand or '-', e), flush=True)
            last_error = '网络异常'
            continue

        access_token = _str_of(
            _deep_pick_dict(token_data, ('access_token', 'accessToken', 'token')),
            'access_token', 'accessToken', 'token'
        )
        print("[zhihu-oauth] token exchange redirect_uri=%s -> %s"
              % (zhihu_redirect_uri(cand), token_text[:300]), flush=True)
        if access_token:
            # 不要用 cand 覆盖 base：base 为空意味着 state 与 Cookie
            # 都没把实例前缀带回来，此时猜出来的前缀不一定
            # 就是用户所在实例。落地跳转一律用相对路径
            # '../../../app'，它对两个前缀同时成立。
            break

        # 把上游的真实报错截断后带出去，方便最后一公里联调
        if isinstance(token_data, dict):
            code_v = token_data.get('code')
            data_v = (token_data.get('data') or token_data.get('msg')
                      or token_data.get('message'))
        else:
            code_v = None
            data_v = None
        if data_v is None:
            data_v = token_text[:120] if token_text else ''
        head = ('code=%s ' % code_v) if code_v is not None else ''
        last_error = (head + str(data_v)).strip()[:160]

    if not access_token:
        return fail('知乎未返回访问令牌：'
                    + (last_error or '未知原因'))

    # 2) 取用户信息
    try:
        user_resp = requests.get(
            ZHIHU_USER_URL,
            headers={'Authorization': 'Bearer ' + access_token},
            timeout=15
        )
        user_text = user_resp.text
        user_data = user_resp.json()
    except Exception as e:
        print("[zhihu-oauth] user fetch failed: %s" % e, flush=True)
        return fail('获取知乎用户信息失败，请重试')

    print("[zhihu-oauth] user response: %s" % user_text[:500], flush=True)

    u = _deep_pick_dict(
        user_data,
        ('name', 'nickname', 'fullname', 'avatar_url', 'avatar', 'avatar_path',
         'url_token', 'uid', 'phone_no', 'phone', 'mobile', 'email', 'mail',
         'gender', 'headline', 'description', 'bio')
    )
    zhihu_uid = _str_of(u, 'id', 'uid', 'url_token', 'account_id')
    name = _str_of(u, 'name', 'nickname', 'fullname') or '知乎用户'
    avatar = _str_of(u, 'avatar_url', 'avatar', 'avatarUrl')
    headline = _str_of(u, 'headline', 'description', 'bio')
    home_url = _str_of(u, 'url', 'home_url', 'homeUrl')
    # official /user fields: uid fullname gender headline description avatar_path phone_no email
    fullname = _str_of(u, 'fullname', 'full_name')
    gender = _str_of(u, 'gender')
    phone_no = _str_of(u, 'phone_no', 'phone', 'mobile')
    email = _str_of(u, 'email', 'mail')
    description = _str_of(u, 'description', 'bio')

    # 3) 入库（与手机号表单并存，单独一张表）
    try:
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            row_values = (zhihu_uid, name, avatar, headline, home_url, _client_ip())
            new_id = None
            try:
                cur.execute(
                    "INSERT INTO zhihu_users (zhihu_uid, name, avatar, headline, home_url, ip,"
                    " fullname, gender, phone, email, description, state_verified)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (zhihu_uid, name, avatar, headline, home_url, _client_ip(),
                     fullname, gender, phone_no, email, description,
                     1 if state_ok else 0)
                )
                new_id = cur.lastrowid
            except Exception:
                # 旧库迁移失败时退化为不带该列，保证登录不被日志字段拖垮
                try:
                    cur.execute(
                        "INSERT INTO zhihu_users (zhihu_uid, name, avatar, headline, home_url, ip, state_verified)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        row_values + (1 if state_ok else 0,)
                    )
                    new_id = cur.lastrowid
                except Exception:
                    cur.execute(
                        "INSERT INTO zhihu_users (zhihu_uid, name, avatar, headline, home_url, ip)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        row_values
                    )
                    new_id = cur.lastrowid
            conn.commit()
            conn.close()
    except Exception as e:
        print("[zhihu-oauth] insert failed: %s" % e, flush=True)
        return fail('记录登录信息失败，请重试')

    # 4) 与手机号表单一致：直接进入体验
    # 顺手记住知乎身份（30 天）：/api/learning_path 用它读取兴趣画像做个性化推荐
    dest = (base + '/app') if base else '../../../app'
    if zhihu_uid:
        try:
            resp = _redirect_path(dest)
            resp.set_cookie('zhihu_uid', zhihu_uid, max_age=2592000,
                            path='/', httponly=True, samesite='Lax')
        except Exception:
            pass
        # 5) 后台拉关注动态并提炼兴趣画像（不阻塞跳转，失败不影响登录）
        try:
            if new_id:
                _t = threading.Thread(
                    target=_analyze_zhihu_profile_bg,
                    args=(new_id, {'fullname': fullname, 'headline': headline,
                                   'description': description}, access_token),
                    daemon=True)
                _t.start()
        except Exception as _e:
            print("[zhihu-oauth] profile task spawn failed: %s" % _e, flush=True)
    return _redirect_path(dest)


@app.route('/api/<path:path>', methods=['OPTIONS'])
def options_handler(_path):
    response = app.response_class()
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.after_request
def after_request(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

SYSTEM_PROMPT = """你是专业大学生学业与职业规划顾问，基于学生客观背景与内心价值观输出规划报告，输出分5个板块：方向锚点、适配依据、核心路径、分阶段行动清单、避坑兜底建议。使用Markdown格式，客观务实，不制造焦虑，不替用户做最终选择。凡涉及多维度并列对比的内容（如要素分析、阶段清单），必须使用标准Markdown表格语法（| 列1 | 列2 |）呈现，禁止用空格或制表符对齐的伪表格。"""

CAREER_SUB_DIRECTIONS = {
    "软件工程师": ["前端开发工程师", "后端开发工程师", "全栈开发工程师", "移动端开发工程师", "嵌入式软件工程师"],
    "产品经理": ["产品经理（B端）", "产品经理（C端）", "数据产品经理", "增长产品经理", "AI产品经理"],
    "数据分析师": ["商业数据分析师", "用户行为分析师", "金融数据分析师", "大数据分析师", "量化分析师"],
    "大数据工程师": ["大数据开发工程师", "数据仓库工程师", "ETL工程师", "数据架构师", "实时数据工程师"],
    "人工智能工程师": ["机器学习工程师", "深度学习工程师", "NLP工程师", "计算机视觉工程师", "AI算法工程师"],
    "网络工程师": ["网络安全工程师", "系统运维工程师", "云计算工程师", "DevOps工程师", "IT基础设施工程师"],
    "信息安全分析师": ["渗透测试工程师", "安全运维工程师", "安全架构师", "红队工程师", "安全合规专员"],
    "UI设计师": ["UI设计师", "UX设计师", "交互设计师", "产品设计师", "视觉设计师"],
    "市场营销": ["数字营销专员", "品牌营销经理", "社交媒体运营", "内容营销经理", "营销策划师"],
    "金融分析师": ["投资分析师", "风控分析师", "财务分析师", "资产管理师", "金融顾问"],
    "教师": ["高中教师", "初中教师", "小学教师", "职业教育教师", "教育研究员"],
    "医生": ["内科医生", "外科医生", "儿科医生", "急诊科医生", "专科医生"],
    "律师": ["诉讼律师", "非诉律师", "公司法务", "知识产权律师", "刑事律师"],
    "会计师": ["注册会计师", "管理会计师", "税务会计师", "审计师", "财务顾问"],
    "建筑师": ["建筑设计师", "室内设计师", "景观设计师", "城市规划师", "建筑工程师"],
    "机械工程师": ["机械设计工程师", "智能制造工程师", "自动化工程师", "结构工程师", "设备工程师"],
    "电子工程师": ["硬件工程师", "嵌入式工程师", "集成电路工程师", "PCB工程师", "测试工程师"],
    "创业者": ["科技创业者", "电商创业者", "教育创业者", "文创创业者", "跨境电商创业者"]
}

def get_career_sub_directions(career_name):
    for key in CAREER_SUB_DIRECTIONS:
        if key in career_name or career_name in key:
            return CAREER_SUB_DIRECTIONS[key]
    return ["高级" + career_name, career_name + "专家", career_name + "管理者"]


def call_deepseek(api_key, user_content):
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-v4-flash",
        "temperature": 0.7,
        "max_tokens": 16000,
        "reasoning_effort": "none",
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    }
    res = HTTP_POOL.post(url, json=payload, headers=headers, timeout=240)
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"]


def call_zhihu(api_key, system_prompt, user_content):
    """调用知乎直答「快速回答」模型生成规划报告"""
    url = "https://developer.zhihu.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json"
    }
    payload = {
        "model": "zhida-fast-1p5",
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
    }
    res = HTTP_POOL.post(url, json=payload, headers=headers, timeout=120)
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"]


def call_zhihu_search(query, count=5):
    """调用知乎站内搜索，返回与查询相关的内容列表"""
    url = "https://developer.zhihu.com/api/v1/content/zhihu_search"
    headers = {
        "Authorization": f"Bearer {ZHIHU_API_KEY}",
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json"
    }
    params = {"Query": query, "Count": count}
    res = HTTP_POOL.get(url, params=params, headers=headers, timeout=30)
    res.raise_for_status()
    body = res.json()
    items = (body.get("Data") or {}).get("Items") or []
    result = []
    for it in items:
        result.append({
            "title": it.get("Title", ""),
            "content": it.get("ContentText", ""),
            "url": it.get("Url", ""),
            "author": it.get("AuthorName", ""),
            "vote_up": it.get("VoteUpCount", 0),
            "comment_count": it.get("CommentCount", 0),
            "content_type": it.get("ContentType", ""),
        })
    return result


FEELINGS_DISTILL_PROMPT = (
    "\u4f60\u662f\u4fe1\u606f\u63d0\u70bc\u4e13\u5bb6\u3002\u4f60\u53ea\u8f93\u51fa\u5408\u6cd5 JSON\uff0c"
    "\u4e0d\u8f93\u51fa\u4efb\u4f55\u89e3\u91ca\u6587\u5b57\uff0c\u4e0d\u4f7f\u7528 Markdown \u4ee3\u7801\u5757\u3002"
)


def _llm_json_with_prompt(system_prompt, user_content, max_tokens=900, temperature=0.3):
    """\u4e13\u7528\u4e8e\u672c\u6587\u7684\u5c0f\u578b LLM \u8c03\u7528\uff1a\u8fd4\u56de\u89e3\u6790\u597d\u7684 JSON \u5bf9\u8c61\uff0c"
    "\u4efb\u4f55\u5f02\u5e38\u8fd4\u56de None\u3002"""
    if not DEFAULT_API_KEY:
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": "Bearer " + DEFAULT_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        res = HTTP_POOL.post(url, json=payload, headers=headers, timeout=60)
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        print('[zhihu_feelings] distill call failed:', exc, flush=True)
        return None
    return _parse_json_loose(content)


def _distill_zhihu_feelings(items, career_name):
    """\u628a\u77e5\u53cb\u539f\u59cb\u5e16\u5b50\u62bd\u8c61\u6210\u82e5\u5e72\u5173\u952e\u8bcd + \u4e00\u53e5\u6c1b\u56f4\u3002"
    "\u4efb\u4f55\u95ee\u9898\u90fd\u8fd4\u56de None\uff0c\u8c03\u7528\u65b9\u4fdd\u7559\u539f\u59cb\u5217\u8868\u3002"""
    if not items:
        return None
    lines = []
    for i, it in enumerate(items[:6], 1):
        title = re.sub(r'\s+', ' ', (it.get('title') or '')).strip()[:60]
        content = re.sub(r'\s+', ' ', (it.get('content') or '')).strip()[:220]
        lines.append("%d) %s | %s | %s" % (i, title, content, it.get('author') or ''))
    user_content = (
        "\u4e0b\u9762\u662f\u77e5\u4e4e\u4e0a\u5173\u4e8e\u300c%s\u300d\u7684\u771f\u5b9e\u53d1\u8a00\u3002\n"
        "\u8bf7\u628a\u5b83\u4eec\u62bd\u8c61\u63d0\u70bc\u6210 4-6 \u4e2a\u5173\u952e\u8bcd\uff1a\n"
        "- \u6bcf\u4e2a\u5173\u952e\u8bcd 2-6 \u4e2a\u5b57\uff0c\u77ed\u3001\u5177\u4f53\u3001\u80fd\u5f53\u6807\u7b7e\uff1b\n"
        "- \u5fc5\u987b\u80fd\u4ece\u4e0a\u9762\u7684\u539f\u6587\u627e\u5230\u4f9d\u636e\uff0c\u4e0d\u8981\u7a7a\u8bdd\u5957\u8bdd\uff1b\n"
        "- \u5173\u952e\u8bcd\u4e4b\u95f4\u4e0d\u91cd\u53e0\u3002\n"
        "\u518d\u7ed9\u4e00\u53e5\u4e0d\u8d85\u8fc7 40 \u5b57\u7684\u6574\u4f53\u6c1b\u56f4\u603b\u7ed3\u3002\n\n"
        "\u53ea\u8f93\u51fa\u5982\u4e0b JSON\uff1a\n"
        '{"summary":"\u4e00\u53e5\u8bdd\u603b\u7ed3","keywords":[{"word":"\u4f8b\u5982\u52a0\u73ed\u591a",'
        '"tone":"warning","weight":80,"note":"\u4e0d\u8d85\u8fc7 16 \u5b57\u7684\u539f\u6587\u4f9d\u636e"}]}\n'
        "tone \u53ea\u80fd\u53d6 positive / neutral / warning\uff1b"
        "weight \u662f 0-100 \u7684\u6574\u6570\uff0c\u8868\u793a\u8be5\u5173\u952e\u8bcd\u5728\u539f\u6587\u91cc\u7684\u5f3a\u70c8\u7a0b\u5ea6\u3002\n\n"
        "\u539f\u6587\uff1a\n%s" % (career_name, "\n".join(lines))
    )
    parsed = _llm_json_with_prompt(FEELINGS_DISTILL_PROMPT, user_content)
    if not isinstance(parsed, dict):
        return None
    raw_keywords = parsed.get('keywords')
    if not isinstance(raw_keywords, list):
        return None
    out = []
    seen = set()
    for k in raw_keywords:
        if not isinstance(k, dict):
            continue
        word = re.sub(r'\s+', '', str(k.get('word') or ''))[:12]
        if not word or word in seen:
            continue
        tone = str(k.get('tone') or 'neutral').strip().lower()
        if tone not in ('positive', 'neutral', 'warning'):
            tone = 'neutral'
        try:
            weight = int(float(k.get('weight', 60)))
        except Exception:
            weight = 60
        weight = max(10, min(100, weight))
        note = re.sub(r'\s+', ' ', str(k.get('note') or '')).strip()[:40]
        seen.add(word)
        out.append({'word': word, 'tone': tone, 'weight': weight, 'note': note})
        if len(out) >= 6:
            break
    if len(out) < 2:
        return None
    summary = re.sub(r'\s+', ' ', str(parsed.get('summary') or '')).strip()[:80]
    print('[zhihu_feelings] distilled %d keywords for %s' % (len(out), career_name), flush=True)
    return {'keywords': out, 'summary': summary}


@app.route('/api/zhihu_feelings', methods=['POST'])
def zhihu_feelings():
    """根据职业名搜索知乎上从业者的真实感受与建议"""
    try:
        data = request.get_json() or {}
        career_name = (data.get('career_name') or '').strip()
        if not career_name:
            return jsonify({"code": -1, "msg": "缺少职业名称", "data": []})

        if not ZHIHU_API_KEY:
            return jsonify({"code": -1, "msg": "知乎密钥未配置", "data": []})

        query = f"{career_name} 从业 感受 建议"
        items = call_zhihu_search(query, count=5)
        payload = {"code": 0, "data": items, "msg": "success"}
        # distill is OPT-IN: the main site does not send it, so its behaviour is
        # unchanged. The mirror asks for keyword distillation and degrades to the
        # raw list whenever the LLM is unavailable.
        if data.get('distill'):
            distilled = _distill_zhihu_feelings(items, career_name)
            if distilled:
                payload['keywords'] = distilled['keywords']
                payload['summary'] = distilled['summary']
        return jsonify(payload)
    except Exception as e:
        return jsonify({"code": -1, "msg": f"搜索失败：{e}", "data": []})


@app.route('/')
def index():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(project_dir, 'poster', 'logo.html')
    with open(logo_path, 'r', encoding='utf-8') as f:
        content = f.read()
    response = app.response_class(content, mimetype='text/html; charset=utf-8')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/app')
def app_page():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(project_dir, 'static', 'index.html')
    with open(index_path, 'r', encoding='utf-8') as f:
        content = f.read()
    response = app.response_class(content, mimetype='text/html; charset=utf-8')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/api/recommend', methods=['POST'])
def recommend_careers():
    try:
        data = request.get_json()
        basic_info = data.get('basic_info', {})
        
        if not basic_info:
            return jsonify({"code": -1, "msg": "请填写基础信息"})
        
        desired_career = basic_info.get('desired_career', '').strip()
        
        info_lines = [
            f"- 年级：{basic_info.get('grade', '')}",
            f"- 专业大类：{basic_info.get('major_category', '')}",
            f"- 专业名称：{basic_info.get('major_name', '')}",
            f"- 个人特长：{basic_info.get('skills', '')}",
            f"- 爱好：{basic_info.get('hobbies', '')}",
        ]
        
        if desired_career:
            info_lines.append(f"- 期望职业：{desired_career}")
        
        info_lines.append(f"- MBTI人格：{basic_info.get('mbti', '')}")
        
        requirement_lines = [
            "- 推荐的职业应基于用户的专业、特长、爱好、MBTI人格等信息进行**客观分析**，推荐真正适合用户的职业",
            "- 其他推荐职业不必局限于与期望职业相关的领域，可以跨专业推荐用户真正适合的宏观职业方向",
            "- **推荐的职业应是宏观的职业大类，而不是过于细分的职业方向**（例如：推荐\"软件工程师\"而不是\"前端开发工程师\"，推荐\"设计师\"而不是\"UI设计师\"）",
            "- 如果用户专业明确且与某些职业高度相关，可优先推荐相关专业方向；但如果用户的特长、爱好明显指向其他领域，也应客观推荐"
        ]
        
        if desired_career:
            requirement_lines.insert(0, f"- 如果用户填写了期望职业\"{desired_career}\"，**必须**将该期望职业作为推荐结果之一，且放在推荐列表的第一位")
            requirement_lines.insert(1, "- 期望职业的兴趣适配度应根据用户实际匹配情况客观评估，不要人为拔高")
            requirement_lines.insert(2, "- 其他推荐职业不必局限于与期望职业相关的领域")
        
        info_text = '\n'.join(info_lines)
        requirement_text = '\n'.join(requirement_lines)
        user_content = f"""
学生基础信息：
{info_text}

请根据以上信息，为该学生推荐10-12个适合的职业方向。

**重要要求：**
{requirement_text}

对于每个职业，请提供：
1. 职业名称（应为宏观职业大类）
2. 兴趣适配度（0-100的百分比，表示该职业与用户兴趣、特长的匹配程度）
3. 实现难度（0-100的百分比，表示从当前状态到达该职业的难易程度，难度越高百分比越大）
4. 推荐理由（简短说明为什么推荐该职业）
5. 职业介绍（100-150字，介绍该职业的工作内容、发展前景、核心能力要求等）

请使用严格的JSON格式输出，不要包含任何额外文本或Markdown格式。JSON结构如下：
{{
    "careers": [
        {{
            "name": "职业名称",
            "interest_match": 85,
            "difficulty": 60,
            "reason": "简短说明推荐理由",
            "description": "职业介绍（100-150字）"
        }}
    ]
}}
"""
        
        result = call_deepseek(DEFAULT_API_KEY, user_content)
        
        try:
            json_result = json.loads(result)
        except json.JSONDecodeError:
            start = result.find('{')
            end = result.rfind('}') + 1
            if start != -1 and end != -1:
                json_result = json.loads(result[start:end])
            else:
                raise ValueError("无法解析返回的JSON数据")
        
        return jsonify({"code": 0, "data": json_result, "msg": "success"})
    
    except Exception as e:
        import traceback
        print(f"[recommend] AI 推荐调用失败，回退默认数据：{type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return generate_fallback_careers(basic_info)


def generate_fallback_careers(basic_info):
    default_careers = [
        {"name": "软件工程师", "interest_match": 80, "difficulty": 65, "reason": "适合有编程兴趣和逻辑思维能力的学生", "description": "软件工程师负责设计、开发和维护各类软件系统，涵盖前端、后端、移动端等方向。需要掌握编程语言、数据结构、算法等核心技能，发展前景广阔，薪资水平较高，是数字化时代的核心职业之一。"},
        {"name": "产品经理", "interest_match": 75, "difficulty": 50, "reason": "适合善于沟通、有创意和用户思维的学生", "description": "产品经理负责产品的规划、设计和迭代，需要深入理解用户需求，协调研发、设计、运营等团队推进产品落地。核心能力包括用户调研、需求分析、项目管理和数据驱动决策，发展路径可走向产品总监或VP。"},
        {"name": "设计师", "interest_match": 70, "difficulty": 60, "reason": "适合有艺术天赋和审美能力的学生", "description": "设计师涵盖UI/UX设计、视觉设计、品牌设计等方向，负责产品的视觉呈现和用户体验。需要掌握设计工具、色彩理论、排版布局等技能，注重用户心理和交互逻辑，在互联网、广告、出版等行业需求旺盛。"},
        {"name": "数据分析师", "interest_match": 75, "difficulty": 65, "reason": "适合擅长数学、统计和逻辑分析的学生", "description": "数据分析师通过收集、处理和分析数据，为企业决策提供支持。需要掌握SQL、Python/R、统计学和数据可视化工具，核心工作包括数据清洗、建模分析和报告撰写，在金融、电商、互联网等行业应用广泛。"},
        {"name": "市场营销专员", "interest_match": 65, "difficulty": 45, "reason": "适合善于沟通、有创意和市场敏感度的学生", "description": "市场营销专员负责品牌推广、市场调研、活动策划和渠道运营等工作。需要了解消费者心理、掌握数字营销工具，具备内容创作和数据分析能力，发展方向包括品牌经理、市场总监等。"},
        {"name": "运营专员", "interest_match": 60, "difficulty": 40, "reason": "适合执行力强、善于数据分析的学生", "description": "运营专员负责用户增长、内容运营、活动策划等工作，是连接产品和用户的关键角色。需要具备数据分析、文案写作和活动执行能力，发展方向包括运营经理、运营总监，在互联网行业尤其重要。"},
        {"name": "创业家", "interest_match": 70, "difficulty": 85, "reason": "适合有商业头脑、敢于创新和承担风险的学生", "description": "创业家自主创办企业或项目，需要全面的商业能力，包括市场洞察、团队管理、融资和战略规划。风险高但回报上限大，适合有强烈事业心、抗压能力强的人，成功路径包括连续创业或企业并购退出。"},
        {"name": "教育工作者", "interest_match": 60, "difficulty": 55, "reason": "适合热爱教育、善于表达和有耐心的学生", "description": "教育工作者在学校、培训机构或在线平台从事教学工作，负责课程设计、知识传授和学生培养。需要扎实的专业知识、教学方法和沟通能力，发展方向包括高级教师、教研组长或教育管理者。"},
        {"name": "咨询师", "interest_match": 65, "difficulty": 70, "reason": "适合逻辑清晰、善于分析和沟通的学生", "description": "咨询师为企业提供战略、管理、技术等方面的专业建议，需要强大的分析能力、行业洞察和沟通技巧。工作内容涵盖调研诊断、方案设计和落地实施，在咨询公司或企业内部战略部门发展。"},
        {"name": "内容创作者", "interest_match": 70, "difficulty": 55, "reason": "适合有创作热情和表达能力的学生", "description": "内容创作者在自媒体平台、MCN机构或企业内容团队工作，负责图文、视频、播客等内容策划和制作。需要具备创意策划、文案写作和内容运营能力，变现路径包括广告、知识付费和电商。"},
        {"name": "游戏设计师", "interest_match": 75, "difficulty": 70, "reason": "适合热爱游戏、有创意和技术能力的学生", "description": "游戏设计师负责游戏玩法设计、关卡设计、数值平衡和剧情编排，需要兼顾创意和逻辑。核心技能包括游戏引擎使用、脚本编程和用户体验设计，在游戏行业需求旺盛，薪资待遇优厚。"},
        {"name": "网络安全工程师", "interest_match": 65, "difficulty": 80, "reason": "适合细心、有耐心和技术能力的学生", "description": "网络安全工程师负责保护企业和用户的数字资产安全，工作内容包括漏洞扫描、渗透测试、安全架构设计和应急响应。需要掌握网络协议、密码学、操作系统等底层知识，是高薪且紧缺的职业。"}
    ]
    
    desired_career = basic_info.get('desired_career', '').strip()
    if desired_career:
        existing_index = None
        for i, career in enumerate(default_careers):
            if career['name'] == desired_career:
                existing_index = i
                break
        if existing_index is not None:
            career = default_careers.pop(existing_index)
            default_careers.insert(0, career)
        else:
            default_careers.insert(0, {"name": desired_career, "interest_match": 85, "difficulty": 60, "reason": "用户期望职业"})

    major_category = basic_info.get('major_category', '')
    skills = basic_info.get('skills', '')
    hobbies = basic_info.get('hobbies', '')
    
    tech_keywords = ['计算机', '软件', '编程', '技术', '数据', '信息']
    design_keywords = ['设计', '艺术', '美术', '创意']
    business_keywords = ['管理', '经济', '营销', '商业']
    education_keywords = ['教育', '师范', '心理']
    
    if any(kw in major_category for kw in tech_keywords) or any(kw in skills for kw in tech_keywords):
        for career in default_careers:
            if career['name'] in ['软件工程师', '数据分析师', '网络安全工程师', '游戏设计师']:
                career['interest_match'] = min(95, career['interest_match'] + 10)
    elif any(kw in major_category for kw in design_keywords) or any(kw in hobbies for kw in design_keywords):
        for career in default_careers:
            if career['name'] in ['设计师', '内容创作者', '游戏设计师']:
                career['interest_match'] = min(95, career['interest_match'] + 10)
    elif any(kw in major_category for kw in business_keywords) or any(kw in skills for kw in business_keywords):
        for career in default_careers:
            if career['name'] in ['产品经理', '市场营销专员', '创业家', '咨询师']:
                career['interest_match'] = min(95, career['interest_match'] + 10)
    elif any(kw in major_category for kw in education_keywords):
        for career in default_careers:
            if career['name'] in ['教育工作者', '咨询师']:
                career['interest_match'] = min(95, career['interest_match'] + 10)
    
    return jsonify({"code": 0, "data": {"careers": default_careers}, "msg": "使用默认推荐数据", "service_error": True})


def generate_fallback_report(basic_info, deep_answers, career_name=''):
    major_category = basic_info.get('major_category', '')
    major_name = basic_info.get('major_name', '')
    skills = basic_info.get('skills', '')
    hobbies = basic_info.get('hobbies', '')
    mbti = basic_info.get('mbti', '')
    grade = basic_info.get('grade', '')
    desired_career = basic_info.get('desired_career', '')

    target_career = career_name or desired_career or '目标职业'

    q1 = str(deep_answers[0]).strip() if len(deep_answers) > 0 and str(deep_answers[0]).strip() else '未填写'
    q2 = str(deep_answers[1]).strip() if len(deep_answers) > 1 and str(deep_answers[1]).strip() else '未填写'
    q3 = str(deep_answers[2]).strip() if len(deep_answers) > 2 and str(deep_answers[2]).strip() else '未填写'
    q4 = str(deep_answers[3]).strip() if len(deep_answers) > 3 and str(deep_answers[3]).strip() else '未填写'

    report = f"""
## 方向锚点

### 个人画像

| 维度 | 信息 |
|------|------|
| 年级 | {grade} |
| 专业 | {major_name}（{major_category}） |
| 特长 | {skills or '未填写'} |
| 爱好 | {hobbies or '未填写'} |
| MBTI | {mbti or '未填写'} |
| 目标职业 | **{target_career}** |

### 职业方向判定

基于你的专业背景（{major_name}）和个人特质，**{target_career}** 是你的核心发展方向。该方向与你的专业高度匹配，同时能够发挥你的特长和性格优势。

---

## 适配依据

### 专业匹配分析

你的专业「{major_name}」为**{target_career}**提供了以下核心能力支撑：
- 专业知识体系与{target_career}的工作内容高度相关
- 专业课程训练培养了该职业所需的核心思维和能力
- 专业实践经历为职业发展奠定了基础

### 个人特质匹配

"""
    if q1 != '未填写':
        report += f"- **职业价值观**：你提到「{q1[:80]}」，这与{target_career}职业所能提供的价值高度吻合\n"
    if q2 != '未填写':
        report += f"- **抗压能力**：你应对困难的方式是「{q2[:80]}」，这表明你具备该职业所需的韧性\n"
    if q3 != '未填写':
        report += f"- **职业动机**：你选择{target_career}的原因是「{q3[:80]}」，这份内驱力将支撑你长期发展\n"
    if q4 != '未填写':
        report += f"- **自我认知**：你对自身优劣势的分析是「{q4[:80]}」，这种清醒的自我认知是职业发展的重要前提\n"
    if q1 == '未填写' and q2 == '未填写' and q3 == '未填写' and q4 == '未填写':
        report += f"- 建议你认真思考职业价值观和动机，这将帮助你更好地规划{target_career}方向的发展路径\n"

    if mbti:
        mbti_desc = {
            'INTJ': '战略思维型，擅长系统性规划和独立思考', 'INTP': '逻辑分析型，擅长理论研究和问题解决',
            'ENTJ': '领导决策型，擅长组织管理和战略执行', 'ENTP': '创新探索型，擅长创意发想和资源整合',
            'INFJ': '理想主义型，擅长洞察人心和价值驱动', 'INFP': '感性理想型，擅长创意表达和人文关怀',
            'ENFJ': '热情感染型，擅长团队激励和沟通协调', 'ENFP': '热情创意型，擅长人际交往和创意激发',
            'ISTJ': '严谨务实型，擅长执行和细节管理', 'ISFJ': '细致负责型，擅长服务和支持保障',
            'ESTJ': '果断管理型，擅长组织运营和流程管控', 'ESFJ': '热情关怀型，擅长团队协作和客户服务',
            'ISTP': '冷静实操型，擅长技术操作和问题排查', 'ISFP': '温和创意型，擅长审美表达和个性化创作',
            'ESTP': '灵活行动型，擅长应变和现场决策', 'ESFP': '热情表现型，擅长社交互动和氛围营造',
        }
        mbti_text = mbti_desc.get(mbti.upper(), '具备独特的性格优势')
        report += f"\n### MBTI性格分析\n\n你的MBTI类型为**{mbti}**，{mbti_text}。这一性格特质在{target_career}职业中能够发挥独特优势。\n"

    report += f"""

---

## 核心路径

### 你的专属发展路径：{grade} → {target_career}

**阶段一：专业筑基（当前-毕业）**
- 深入学习{major_name}核心课程，GPA保持在3.0以上
- 参与与{target_career}相关的课程项目或竞赛
- 考取该领域的基础证书或资格认证
- 建立行业认知，关注{target_career}领域的最新动态

**阶段二：实践突破（毕业后1-2年）**
- 寻找{target_career}相关的实习或初级岗位
- 在实战中积累项目经验，建立个人作品集
- 拓展行业人脉，参加专业社群和行业活动
- 持续学习行业前沿知识和工具

**阶段三：专业深耕（3-5年）**
- 在{target_career}领域建立专业深度，成为团队核心成员
- 承担更复杂的项目责任，积累管理经验
- 考虑进阶认证或学历提升（如MBA、专业硕士等）
- 开始规划下一步职业跃迁方向

---

## 分阶段行动清单

### 本学期行动

- [ ] 梳理{target_career}所需核心技能清单，对照自身查漏补缺
- [ ] 精读2-3本{target_career}领域经典书籍
- [ ] 关注5个以上行业公众号/博主，建立信息获取渠道
- [ ] 完成至少1个与{target_career}相关的实践项目

### 寒暑假行动

- [ ] 投递{target_career}相关实习岗位，争取实战机会
- [ ] 参加行业峰会或线上论坛，拓展人脉
- [ ] 复盘学习成果，调整下一阶段计划
- [ ] 准备求职材料（简历、作品集等）

### 毕业前行动

- [ ] 完善求职简历，突出与{target_career}的匹配度
- [ ] 模拟面试练习，准备常见面试问题
- [ ] 建立专业作品集或项目展示
- [ ] 投递目标岗位，积极求职

---

## 避坑兜底建议

### 可能遇到的挑战

1. **技能差距**：{target_career}对专业技能要求较高，需持续投入学习
2. **竞争激烈**：该方向求职竞争较大，需提前积累差异化优势
3. **方向迷茫**：实践中可能发现实际工作与预期不符

### 应对策略

1. **建立作品集**：用实际项目证明你的能力，比学历更有说服力
2. **找到导师**：寻找{target_career}领域的前辈指导，少走弯路
3. **保持灵活**：如果主方向受阻，可考虑相关领域作为过渡
4. **持续迭代**：定期复盘职业规划，根据实际情况调整方向

### 备选方案

"""
    sub_directions = get_career_sub_directions(target_career)
    for i, sub in enumerate(sub_directions[:3], 1):
        report += f"{i}. **{sub}** — 作为{target_career}的细分方向，可作为职业发展的备选路径\n"

    report += f"""
---

*本报告基于你的专业背景（{major_name}）、目标职业（{target_career}）及深层探索回答生成。如需更详细的分析，请在网络恢复后重新生成。*
"""
    return report.strip()


@app.route('/api/generate', methods=['POST'])
def generate_report():
    try:
        data = request.get_json()
        
        basic_info = data.get('basic_info', {})
        deep_answers = data.get('deep_answers', [])
        career_name = data.get('career_name', '').strip()

        if not basic_info:
            return jsonify({"code": -1, "msg": "请填写基础信息"})

        desired_career = basic_info.get('desired_career', '').strip()

        info_lines = [
            f"- 年级：{basic_info.get('grade', '')}",
            f"- 专业大类：{basic_info.get('major_category', '')}",
            f"- 专业名称：{basic_info.get('major_name', '')}",
            f"- 个人特长：{basic_info.get('skills', '')}",
            f"- 爱好：{basic_info.get('hobbies', '')}",
        ]

        if desired_career:
            info_lines.append(f"- 期望职业：{desired_career}")

        if career_name:
            info_lines.append(f"- 用户选择的职业方向：{career_name}")

        info_lines.append(f"- MBTI人格：{basic_info.get('mbti', '')}")

        info_text = '\n'.join(info_lines)
        user_content = f"""
学生基础信息：
{info_text}

深层价值观探索回答：
"""
        for i, answer in enumerate(deep_answers, 1):
            user_content += f"- 问题{i}：{str(answer)}\n"

        user_content += f"""

请根据以上信息，围绕用户选择的职业方向「{career_name or desired_career or '未明确'}」，为该学生生成一份专业的学业与职业规划报告。报告需包含以下5个板块：
1. 方向锚点：基于喜欢、擅长、有价值三要素，分析用户选择的方向是否适配，推荐最适配的职业发展方向。三要素适配分析必须以标准Markdown表格呈现，格式严格如下：
| 要素 | 你的情况 | 匹配度 |
|------|---------|--------|
| 喜欢 | （结合用户信息的具体描述） | 高/中/低 |
| 擅长 | （结合用户信息的具体描述） | 高/中/低 |
| 有价值 | （结合职业前景的具体描述） | 高/中/低 |
2. 适配依据：详细说明推荐方向的匹配理由，结合用户的专业、特长、MBTI和深层回答
3. 核心路径：从当前年级出发，围绕「{career_name or desired_career or '目标职业'}」方向的主要发展路径
4. 分阶段行动清单：按时间周期（学期/学年）规划具体行动步骤
5. 避坑兜底建议：可能遇到的挑战及备选方案

请使用Markdown格式输出，语言温和务实，不制造焦虑。
"""

        result = None
        used_zhihu = False
        if ZHIHU_API_KEY:
            try:
                result = call_zhihu(ZHIHU_API_KEY, SYSTEM_PROMPT, user_content)
                used_zhihu = True
            except Exception as e:
                print(f"知乎直答调用失败，回退 DeepSeek：{e}", flush=True)
                result = None

        if result is None:
            result = call_deepseek(DEFAULT_API_KEY, user_content)

        if used_zhihu:
            result = result.rstrip() + "\n\n---\n\n> 由 zhida-fast-1p5 生成"

        return jsonify({"code": 0, "data": result, "msg": "success"})

    except Exception as e:
        fallback_report = generate_fallback_report(basic_info, deep_answers, career_name)
        return jsonify({"code": 0, "data": fallback_report, "msg": f"生成报告失败，已使用默认报告：{str(e)}", "service_error": True})


# ==================== 学习路线：两阶段生成 ====================
# 阶段1 能力地图（只出层级与技能骨架）→ 阶段2 逐技能展开（分批并行）→ 阶段3 本地硬校验
# 目的：把"学习主题"降级为"可执行技能单元"——每个节点都要说清
#       学什么知识点(topics)、产出什么(deliverable)、怎么算学会(acceptance)

LEARNING_SYSTEM_PROMPT = (
    "你是职业学习路径设计专家。你只输出合法 JSON，不输出任何解释文字，不使用 Markdown 代码块。"
    "每个学习单元必须具体、可验收：说清学什么知识点、产出什么作品、怎么算学会。"
    "严禁输出「基础理论与核心概念」「综合解决方案」「行业最佳实践」这类没有信息量的表述。"
)

# 反模板黑名单：命中即视为无信息量的占位内容
LEARNING_BANNED_PHRASES = [
    '基础理论与核心概念', '综合解决方案', '行业最佳实践', '系统性能力',
    '全面掌握', '深入学习', '系统学习', '基础知识', '相关理论', '相关知识',
    '理论与实践相结合', '综合能力提升', '能力体系构建', '学习阶段',
]

STAGE_LABELS = {1: '打基础', 2: '进阶提升', 3: '实战与求职'}

# 每批展开的技能数（批越小，模型对单个技能的思考越充分）
EXPAND_BATCH_SIZE = 4
EXPAND_MAX_WORKERS = 8          # 并行展开的批次数：骨架 26-34 技能 → 7-9 批，一轮并发跑完
EXPAND_MAX_TOKENS = 16000
REGEN_MAX_WORKERS = 4           # 质量闭环里被打回节点的并行重生成数

# 阶段4 质量闭环：硬校验 + LLM 终审 → 问题节点打回重生成
CRITIC_REGEN_MAX = 8              # 单轮最多重生成的节点数（控制耗时）
TOPICS_OVERLAP_THRESHOLD = 0.55   # 跨节点 topics 相似度阈值（字符二元组 Jaccard）


def _is_banned_text(text):
    if not text:
        return False
    for phrase in LEARNING_BANNED_PHRASES:
        if phrase in text:
            return True
    return False


def _safe_int(value, default, low, high):
    try:
        num = int(float(value))
    except Exception:
        return default
    if num < low:
        num = low
    if num > high:
        num = high
    return num


def _clean_str_list(value, limit):
    result = []
    if not isinstance(value, list):
        return result
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


# acceptance 硬校验：必须同时具备「独立完成条件」与「限时」，否则视为不可自测
ACCEPTANCE_INDEPENDENT_WORDS = ['独立', '不查资料', '不看答案', '不看教程', '不看解析', '盲写', '闭卷', '不参考']
ACCEPTANCE_TIME_RE = re.compile(r'\d+\s*(分钟|小时)')


def _is_acceptance_valid(text):
    if not text:
        return False
    if not ACCEPTANCE_TIME_RE.search(text):
        return False
    for word in ACCEPTANCE_INDEPENDENT_WORDS:
        if word in text:
            return True
    return False


def _bigrams(text):
    text = re.sub(r'\s+', '', str(text))
    if not text:
        return set()
    if len(text) == 1:
        return set([text])
    return set(text[i:i + 2] for i in range(len(text) - 1))


def _topics_overlap_ratio(topics_a, topics_b):
    """两组 topics 的最大相似度（逐条字符二元组 Jaccard），用于跨节点查重"""
    if not topics_a or not topics_b:
        return 0.0
    best = 0.0
    for ta in topics_a:
        ga = _bigrams(ta)
        if not ga:
            continue
        for tb in topics_b:
            gb = _bigrams(tb)
            if not gb:
                continue
            union = len(ga | gb)
            if not union:
                continue
            ratio = float(len(ga & gb)) / union
            if ratio > best:
                best = ratio
    return best


def _parse_json_loose(text):
    """从模型输出中尽力提取 JSON 对象，失败返回 None"""
    if not text:
        return None
    raw = text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```\s*$', '', raw)
    raw = re.sub(r'//.*?$', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'/\*.*?\*/', '', raw, flags=re.DOTALL)
    raw = raw.strip()

    candidates = [raw]
    if '{' in raw and '}' in raw:
        candidates.append(raw[raw.find('{'):raw.rfind('}') + 1])
    for cand in candidates:
        if not cand:
            continue
        for variant in (cand, re.sub(r'```(?:json)?', '', cand).strip()):
            try:
                parsed = json.loads(variant)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            try:
                # strict=False 允许字符串内出现裸换行/制表符等控制字符
                parsed = json.loads(variant, strict=False)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    try:
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start != -1 and end > start:
            parsed = ast.literal_eval(raw[start:end])
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return None


def _extract_objects_after_key(text, key):
    """从可能被截断的 JSON 文本中，抽取 key 对应数组里的完整对象（丢掉末尾残缺对象）"""
    if not text:
        return []
    pos = text.find('"' + key + '"')
    if pos == -1:
        return []
    start_arr = text.find('[', pos)
    if start_arr == -1:
        return []
    objects = []
    depth = 0
    in_str = False
    escape = False
    obj_start = -1
    for i in range(start_arr + 1, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start != -1:
                    chunk = text[obj_start:i + 1]
                    for strict in (True, False):
                        try:
                            obj = json.loads(chunk, strict=strict)
                            if isinstance(obj, dict):
                                objects.append(obj)
                                break
                        except Exception:
                            pass
                    obj_start = -1
        elif ch == ']' and depth == 0:
            break
    return objects


def _call_llm_json(user_content, max_tokens=6000, temperature=0.6):
    """调用 DeepSeek 并解析为 JSON 对象，失败返回 None"""
    if not DEFAULT_API_KEY:
        print('[learning_path] 未配置 DeepSeek API Key')
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": "Bearer " + DEFAULT_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-v4-flash",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": LEARNING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    }
    try:
        res = HTTP_POOL.post(url, json=payload, headers=headers, timeout=180)
        res.raise_for_status()
        body = res.json()
        choice = body["choices"][0]
        content = choice["message"]["content"]
        finish = choice.get("finish_reason", "")
    except Exception as exc:
        print('[learning_path] 调用模型失败:', exc)
        return None

    parsed = _parse_json_loose(content)
    if parsed is not None:
        return parsed

    # JSON 被 max_tokens 截断时，抢救出已生成的完整对象
    salvaged = {}
    for key in ('units', 'skills', 'careers'):
        objs = _extract_objects_after_key(content, key)
        if objs:
            salvaged[key] = objs
    if salvaged:
        print('[learning_path] JSON 截断(finish=%s)，已抢救: %s'
              % (finish, dict((k, len(v)) for k, v in salvaged.items())))
        return salvaged
    print('[learning_path] JSON 解析失败(finish=%s)，输出前 200 字: %s' % (finish, (content or '')[:200]))
    return None


def _clear_prereq_cycles(skills):
    """用拓扑排序检测并打断 prereq 环，保证 DAG"""
    by_id = {}
    for s in skills:
        by_id[s['id']] = s
    for _ in range(20):
        indegree = {}
        for s in skills:
            indegree[s['id']] = 0
        for s in skills:
            for p in s['prereq']:
                if p in indegree:
                    indegree[s['id']] += 1
        queue = [nid for nid in indegree if indegree[nid] == 0]
        seen = 0
        while queue:
            cur = queue.pop()
            seen += 1
            for s in skills:
                if cur in s['prereq']:
                    indegree[s['id']] -= 1
                    if indegree[s['id']] == 0:
                        queue.append(s['id'])
        if seen == len(skills):
            return
        remaining = set(nid for nid in indegree if indegree[nid] > 0)
        for nid in remaining:
            node = by_id.get(nid)
            if node:
                node['prereq'] = [p for p in node['prereq'] if p not in remaining]
    return


def _build_skill_map(basic_info, career_name, career_knowledge, career_skills, career_goals):
    """阶段1：生成分层能力地图（技能骨架 + 细分职业终点）"""
    template = """你是职业能力地图设计专家。请为目标职业设计一张【分层技能地图】，本阶段只输出骨架，不要展开细节。

## 学生基础信息
- 年级：%(grade)s
- 专业大类：%(major_category)s
- 专业名称：%(major_name)s
- 爱好/个人特长：%(skills)s
- MBTI人格：%(mbti)s

## 目标职业
%(career)s

学生对目标职业的了解：%(knowledge)s
学生已掌握的技能：%(owned)s
学生想获得的成就：%(goals)s

## 认知四层（用于标注每个技能的深度）
1 识记：能说清概念、认得术语
2 模仿应用：能跟着教程做出结果
3 独立实践：能独立完成完整产出
4 迁移创造：能解决没学过的新问题

## 核心要求
1. 输出 26-34 个技能单元，每个单元约 8-20 小时可完成。
2. \u8282\u70b9\u540d\u5fc5\u987b\u662f\u5177\u4f53\u6280\u672f\u540d\u6216\u5177\u4f53\u5b66\u4e60\u4e3b\u9898\uff0c\u4e0d\u80fd\u662f\u62bd\u8c61\u9636\u6bb5/\u80fd\u529b\u53e3\u53f7\u3002\u5141\u8bb8\u4e24\u7c7b\u5199\u6cd5\uff1a
   (a) \u7eaf\u6280\u672f\u540d\uff1aJava\u3001Python\u3001Git\u3001\u6b63\u5219\u3001HTTP\u3001MySQL \u8fd9\u7c7b\u77ed\u6280\u672f\u540d\u4e5f\u7b97\u5177\u4f53\u6280\u80fd\uff0c\u4e0d\u5fc5\u6bcf\u4e2a\u540d\u5b57\u90fd\u53d8\u6210\u4e00\u4e2a\u53ef\u4ea4\u4ed8\u7684\u9879\u76ee\u4ea7\u7269\uff1b
   (b) \u5177\u4f53\u5b66\u4e60\u4e3b\u9898\uff1a\u5982\u300cJava \u96c6\u5408\u4e0e\u6cdb\u578b\u300d\u300cSQL \u591a\u8868\u8fde\u63a5\u300d\u300cpandas \u6570\u636e\u6e05\u6d17\u300d\u300c\u8d22\u52a1\u62a5\u8868\u6bd4\u7387\u5206\u6790\u300d\u3002
   \u4e25\u7981\u628a\u300c\u5b66\u4e60\u9636\u6bb5\u300d\u300c\u80fd\u529b\u63d0\u5347\u300d\u300c\u9879\u76ee\u5b9e\u6218\u300d\u7b49\u62bd\u8c61\u9636\u6bb5/\u80fd\u529b\u53e3\u53f7\u5f53\u4f5c\u72ec\u7acb\u8282\u70b9\u7684\u7a7a\u6cdb\u6807\u9898\uff1b\u8282\u70b9\u5fc5\u987b\u843d\u5230\u80fd\u7ec3\u7684\u5177\u4f53\u6280\u672f\u6216\u4e3b\u9898\u3002
   \u4ecd\u7981\u6b62\u300cXX\u57fa\u7840\u7406\u8bba\u4e0e\u6838\u5fc3\u6982\u5ff5\u300d\u300cXX\u7efc\u5408\u89e3\u51b3\u65b9\u6848\u300d\u300cXX\u884c\u4e1a\u6700\u4f73\u5b9e\u8df5\u300d\u300c\u6df1\u5165\u5b66\u4e60XX\u300d\u8fd9\u7c7b\u7a7a\u8bdd\u3002
3. 核心技能必须出现两次以形成层层深入：第二次出现在更高 stage，名字更具体、认知层级更高。例如：
   {"id":"excel-basic","name":"Excel 函数与数据透视表","stage":1,"cognitive":"模仿应用","prereq":[]}
   {"id":"excel-auto","name":"Excel 动态报表与自动化","stage":3,"cognitive":"迁移创造","prereq":["excel-basic"]}
4. stage 只能取 1、2、3：1=打基础（打牢基本功）、2=进阶提升（核心能力）、3=实战与求职（项目实战、作品集、实习与求职准备）。三个阶段都必须有内容，不能跳过任何一段。
5. prereq 只能引用本列表内已出现的 id，不能引用自己，整体必须构成无环图。
6. 技能必须贴合该职业的真实岗位要求（对标主流招聘网站 JD 的高频技能），学完即可对应岗位能力。
7. 另外给出 3-4 个细分职业终点（该职业成长后的具体岗位方向）。
8. 依赖必须“没有跳步”：每个技能的 prereq 最多 2 个，优先依赖紧邻的上一个技能，形成一条相对线性的主线（stage 1 → 2 → 3 逐层推进）。严禁 stage 1 的技能直接指向 stage 3，每一层都要有承接，让学习者能按编号顺序一步步走完。
9. skills 数组的排列顺序就是推荐学习顺序：第 1 项最先学，最后一项最后学。数组必须是一条真实可执行的学习序列——stage 从小到大不回头，同一 stage 内按真实学习先后排列（先学的在前），prereq 指向的技能必须排在依赖它的技能之前。

## 输出格式（纯 JSON，不要任何多余文字）
{"skills":[{"id":"英文短横线id","name":"具体技能名","stage":1,"cognitive":"识记|模仿应用|独立实践|迁移创造","prereq":["其他技能id"]}],
 "careers":[{"name":"细分职业名称","description":"该方向简介，40-60字"}]}
"""
    prompt = template % {
        'grade': basic_info.get('grade', ''),
        'major_category': basic_info.get('major_category', ''),
        'major_name': basic_info.get('major_name', ''),
        'skills': basic_info.get('skills', '') or basic_info.get('hobbies', ''),
        'mbti': basic_info.get('mbti', ''),
        'career': career_name,
        'knowledge': career_knowledge or '（未填写）',
        'owned': career_skills or '（未填写）',
        'goals': career_goals or '（未填写）',
    }

    data = _call_llm_json(prompt, max_tokens=6000, temperature=0.5)
    if not data or not isinstance(data.get('skills'), list):
        return None, None

    skills = []
    seen = set()
    for item in data['skills']:
        if not isinstance(item, dict):
            continue
        sid = str(item.get('id', '') or '').strip()
        name = str(item.get('name', '') or '').strip()
        if not sid or not name or sid in seen:
            continue
        if _is_banned_text(name):
            continue
        seen.add(sid)
        # 保持主线清晰：每个技能最多两个前置，减少跨列长边和视觉交叉。
        prereq = _clean_str_list(item.get('prereq'), 2)
        skills.append({
            'id': sid,
            'name': name,
            'stage': _safe_int(item.get('stage'), 1, 1, 3),
            'cognitive': str(item.get('cognitive', '') or '').strip(),
            'prereq': prereq,
        })

    if len(skills) < 8:
        print('[learning_path] 能力地图技能数不足:', len(skills))
        return None, None

    id_set = set(s['id'] for s in skills)
    for s in skills:
        s['prereq'] = [p for p in s['prereq'] if p in id_set and p != s['id']]
    _clear_prereq_cycles(skills)

    careers = []
    if isinstance(data.get('careers'), list):
        for c in data['careers']:
            if isinstance(c, dict) and str(c.get('name', '') or '').strip():
                careers.append({
                    'name': str(c['name']).strip(),
                    'description': str(c.get('description', '') or '').strip()
                })
    return skills, careers


def _expand_skill_batch(batch, basic_info, career_name, career_knowledge, career_skills, career_goals, ctx=None, feedback=None):
    """阶段2：把一批技能骨架展开为可执行学习单元（带前置/后继上下文与审核打回反馈）"""
    lines = []
    ctx = ctx or {}
    prev_map = ctx.get('prev') or {}
    succ_map = ctx.get('succ') or {}
    for s in batch:
        prev_names = prev_map.get(s['id']) or []
        succ_names = succ_map.get(s['id']) or []
        lines.append('- id=%s | 技能名=%s | 阶段=%s | 认知层级=%s | 前置技能=%s | 后继技能=%s' % (
            s['id'], s['name'], s.get('stage', 1), s.get('cognitive', ''),
            '、'.join(prev_names) if prev_names else '无',
            '、'.join(succ_names) if succ_names else '无'
        ))
    feedback_block = ''
    if feedback:
        feedback_block = "\n\n## 审核打回重做（该技能曾被审核打回，必须按反馈改进后重新展开）\n%s" % feedback
    template = """你是学习内容设计师。请把下面这批技能，逐个展开成【可执行的学习单元】。

## 学生与目标
- 学生：%(grade)s / %(major)s，爱好与特长：%(skills)s
- 目标职业：%(career)s
- 学生对该职业的了解：%(knowledge)s；已掌握：%(owned)s；想获得：%(goals)s

## 本批技能（共 %(count)d 个）
%(lines)s

## 上下文衔接要求
- 「前置技能」=本技能之前学什么：topics 不得重复前置技能已覆盖的知识点，必须在其基础上更进一步。
- 「后继技能」=本技能之后学什么：本技能要为后继做铺垫，学到的东西应能支撑后继技能的开展，避免知识断层。
%(feedback_block)s

## 每个技能必须返回以下字段
- id：原样返回传入的 id
- hours：学习时长，整数小时，取值 8-20
- difficulty：难度，1-5 整数
- importance：对该职业的重要程度，1-5 整数
- topics：3-6 条**具体知识点**，每条都要能独立打勾。写「GROUP BY 与 HAVING 的区别」而不是「了解聚合函数」。
- deliverable：**一个可以交出去、可以截图的产出物**。写「用 SQL 产出一份《各门店月度销售额 Top10》报表」，严禁写「掌握XX」「理解XX」。
- acceptance：**一条可以自测的验收标准**，必须同时包含「在什么条件下 + 独立完成什么 + 限时多久」。例如「给一张陌生的表结构，30 分钟内独立写出正确的多表聚合查询」。
- description：60-100 字，说明学什么、为什么对该职业重要、学完能做什么。
- resources：至少 3 个，其中至少 2 个是 B 站教学视频，每个含 name 与 url。

## 资源链接格式（严格遵守）
- B站视频：https://search.bilibili.com/all?keyword=具体搜索词
- 书籍：https://book.douban.com/subject_search?search_text=书名
- MOOC：https://www.icourse163.org/search.htm?search=课程名
- 知乎：https://www.zhihu.com/search?type=content&q=关键词

## \u786c\u6027\u7981\u6b62
- \u7981\u6b62\u51fa\u73b0\u300c\u57fa\u7840\u7406\u8bba\u4e0e\u6838\u5fc3\u6982\u5ff5\u300d\u300c\u7efc\u5408\u89e3\u51b3\u65b9\u6848\u300d\u300c\u884c\u4e1a\u6700\u4f73\u5b9e\u8df5\u300d\u300c\u6df1\u5165\u5b66\u4e60\u300d\u300c\u7cfb\u7edf\u5b66\u4e60\u300d\u7b49\u7a7a\u8bdd\u3002
- topics \u6bcf\u6761\u5fc5\u987b\u662f\u53ef\u9a8c\u8bc1\u7684\u5177\u4f53\u77e5\u8bc6\u70b9\uff0c\u4e0d\u8981\u300c\u4e86\u89e3/\u719f\u6089/\u638c\u63e1 XX\u300d\u8fd9\u79cd\u65e0\u6cd5\u6253\u52fe\u7684\u5199\u6cd5\u3002
- \u5185\u5bb9\u5fc5\u987b\u4e0e\u76ee\u6807\u804c\u4e1a\u9ad8\u5ea6\u76f8\u5173\uff0c\u4e0d\u8981\u51fa\u73b0\u4e0e\u8be5\u804c\u4e1a\u65e0\u5173\u7684\u6280\u672f\u6808\u3002
- \u8282\u70b9\u540d\u5fc5\u987b\u662f\u5177\u4f53\u6280\u672f\u540d\u6216\u5b66\u4e60\u4e3b\u9898\uff08\u5982 Java\u3001Git\u3001SQL \u591a\u8868\u8fde\u63a5\uff09\uff0c\u4e0d\u5f97\u7528\u300c\u5b66\u4e60\u9636\u6bb5\u300d\u300c\u80fd\u529b\u63d0\u5347\u300d\u300c\u9879\u76ee\u5b9e\u6218\u300d\u7b49\u7a7a\u6cdb\u6807\u9898\u5145\u5f53\u8282\u70b9\u540d\u3002

只输出 JSON：{"units":[{"id":"...","hours":12,"difficulty":3,"importance":4,"topics":["..."],"deliverable":"...","acceptance":"...","description":"...","resources":[{"name":"...","url":"..."}]}]}
"""
    prompt = template % {
        'grade': basic_info.get('grade', ''),
        'major': (basic_info.get('major_category', '') or '') + ' ' + (basic_info.get('major_name', '') or ''),
        'skills': basic_info.get('skills', '') or basic_info.get('hobbies', ''),
        'career': career_name,
        'knowledge': career_knowledge or '（未填写）',
        'owned': career_skills or '（未填写）',
        'goals': career_goals or '（未填写）',
        'count': len(batch),
        'lines': '\n'.join(lines),
        'feedback_block': feedback_block,
    }

    data = _call_llm_json(prompt, max_tokens=EXPAND_MAX_TOKENS, temperature=0.6)
    units = {}
    if not data or not isinstance(data.get('units'), list):
        return units
    valid_ids = set(s['id'] for s in batch)
    for item in data['units']:
        if not isinstance(item, dict):
            continue
        sid = str(item.get('id', '') or '').strip()
        if sid in valid_ids:
            units[sid] = item
    return units


def _build_expand_context(skills):
    """为每个技能构建前置/后继技能名上下文，供阶段2 展开时衔接上下文"""
    name_of = dict((s['id'], s['name']) for s in skills)
    ctx = {'prev': {}, 'succ': {}}
    for s in skills:
        ctx['prev'][s['id']] = [name_of[p] for p in s.get('prereq', []) if p in name_of]
        ctx['succ'][s['id']] = []
    for s in skills:
        for p in s.get('prereq', []):
            if p in ctx['succ'] and s['name'] not in ctx['succ'][p]:
                ctx['succ'][p].append(s['name'])
    return ctx


def _expand_skills(skills, basic_info, career_name, career_knowledge, career_skills, career_goals, ctx=None):
    """分批并行展开全部技能；整批丢失的技能自动缩小批次重试"""
    units = {}
    _t0 = time.time()
    ctx = ctx or _build_expand_context(skills)

    def _chunk(seq, size):
        out = []
        for i in range(0, len(seq), size):
            out.append(seq[i:i + size])
        return out

    def _run(chunks, workers):
        if not chunks:
            return
        pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks))))
        try:
            futures = []
            for batch in chunks:
                futures.append(pool.submit(
                    _expand_skill_batch, batch, basic_info, career_name,
                    career_knowledge, career_skills, career_goals, ctx
                ))
            for future in futures:
                try:
                    units.update(future.result(timeout=200))
                except Exception as exc:
                    print('[learning_path] 批次展开失败:', exc)
        finally:
            pool.shutdown(wait=False)

    _run(_chunk(skills, EXPAND_BATCH_SIZE), EXPAND_MAX_WORKERS)

    # 某批 JSON 整体丢失/截断不可救时，这批技能就没有 topics/deliverable/acceptance。
    # 缩小批次重试（先 2 个一批，再逐个单发），让每个技能都有可执行内容。
    for retry_size, retry_workers in ((2, 2), (1, 1)):
        missing = [s for s in skills if s['id'] not in units]
        if not missing:
            break
        print('[learning_path] 重试展开缺失技能 %d 个（batch=%d）' % (len(missing), retry_size))
        _run(_chunk(missing, retry_size), retry_workers)

    missing = [s for s in skills if s['id'] not in units]
    if missing:
        print('[learning_path] 仍有技能未展开:', len(missing))
    print('[learning_path] 展开耗时 %.1fs' % (time.time() - _t0))
    return units


def _rebuild_sequence_edges(nodes, start_id, end_ids):
    """按「真实学习顺序」重建中间节点连线，并给节点写入 order 序号。

    学习顺序 = Kahn 拓扑排序（可用节点里先取 level 小的，再取骨架列表顺序靠前的），
    保证所有 prereq 都排在依赖它的技能之前，且 level 单调不降。
    连线规则：
    - 每个中间节点恰好一条「主前置」(main=True)：声明的 prereq 中学习顺序最靠后的一个；
      没有声明 prereq 则接学习顺序里的上一个技能（首个接起点）——保证路线连续、没有跳步。
    - 其余有效 prereq（同图、非自环、level 不高于本级）作为副前置 (main=False)。
    - 所有边都从学习序号小的指向大的，与真实学习先后一致。
    返回 (connections, order_of)。"""
    middle = [n for n in nodes if n.get('type') not in ('start', 'end')]
    id_set = set(n['id'] for n in middle)
    lvl = {}
    orig = {}
    for i, n in enumerate(middle):
        lvl[n['id']] = _safe_int(n.get('level'), 1, 1, 9)
        orig[n['id']] = i

    # 有效前置：在图中、非自环、层级不越过本级（高级依赖低级是语义错误，直接剔除）
    valid_pred = {}
    for n in middle:
        ps = []
        for p in (n.get('prereq') or []):
            if p in id_set and p != n['id'] and lvl.get(p, 1) <= lvl[n['id']] and p not in ps:
                ps.append(p)
        valid_pred[n['id']] = ps

    # Kahn 拓扑 → 学习顺序；同批可用节点里取 (level, 骨架顺序) 最小的
    indeg = dict((nid, len(valid_pred[nid])) for nid in valid_pred)
    remaining = [n['id'] for n in middle]
    ordered = []
    order_of = {}
    while remaining:
        avail = [nid for nid in remaining if indeg.get(nid, 0) == 0]
        if not avail:
            # 环兜底（理论上来不到这里）：强制取最小者
            avail = [min(remaining, key=lambda i: (lvl[i], orig[i]))]
        pick = min(avail, key=lambda i: (lvl[i], orig[i]))
        remaining.remove(pick)
        ordered.append(pick)
        order_of[pick] = len(ordered)
        for nid in remaining:
            if pick in valid_pred[nid]:
                indeg[nid] -= 1

    connections = []

    def add_edge(src, dst, main):
        if not src or not dst or src == dst:
            return
        connections.append({'from': src, 'to': dst, 'main': bool(main)})

    prev_id = start_id
    for sid in ordered:
        ps = valid_pred[sid]
        if ps:
            primary = max(ps, key=lambda p: order_of[p])
            add_edge(primary, sid, True)
            for p in ps:
                if p != primary:
                    add_edge(p, sid, False)
        else:
            add_edge(prev_id, sid, True)
        prev_id = sid

    # 终点连线：最高层技能分流到各终点
    top_ids = [sid for sid in ordered if lvl[sid] == max(lvl.values())] if ordered else []
    if not top_ids:
        top_ids = ordered[:]
    end_count = max(1, len(end_ids))
    for j, eid in enumerate(end_ids or []):
        parents = top_ids[j::end_count][:3]
        if not parents:
            parents = top_ids[-3:]
        for p in parents:
            add_edge(p, eid, False)

    for n in nodes:
        if n.get('type') not in ('start', 'end') and n['id'] in order_of:
            n['order'] = order_of[n['id']]

    return connections, order_of


def _assemble_learning_path(skills, units, careers, career_name):
    """阶段2.5：把骨架 + 展开结果组装成节点与连接"""
    nodes = []
    for s in skills:
        s['stage'] = _safe_int(s.get('stage'), 1, 1, 3)

    max_stage = 1
    for s in skills:
        if s['stage'] > max_stage:
            max_stage = s['stage']

    nodes.append({
        'id': 'start',
        'name': '当前起点',
        'type': 'start',
        'description': '你的当前知识与技能水平，从这里出发。'
    })

    for s in skills:
        unit = units.get(s['id']) or {}
        nodes.append({
            'id': s['id'],
            'name': s['name'],
            'type': None,
            'level': s['stage'],
            'stage_label': STAGE_LABELS.get(s['stage'], ''),
            'cognitive': s.get('cognitive', ''),
            'prereq': list(s.get('prereq', [])),
            'topics': _clean_str_list(unit.get('topics'), 6),
            'deliverable': str(unit.get('deliverable', '') or '').strip(),
            'acceptance': str(unit.get('acceptance', '') or '').strip(),
            'difficulty': _safe_int(unit.get('difficulty'), min(5, s['stage'] + 1), 1, 5),
            'duration': _safe_int(unit.get('hours'), 12, 2, 300),
            'importance': _safe_int(unit.get('importance'), 3, 1, 5),
            'description': str(unit.get('description', '') or '').strip(),
            'resources': unit.get('resources') if isinstance(unit.get('resources'), list) else []
        })

    end_nodes = []
    for idx, c in enumerate((careers or [])[:4]):
        end_nodes.append({
            'id': 'end%d' % (idx + 1),
            'name': c['name'],
            'type': 'end',
            'description': c.get('description', '')
        })
    if len(end_nodes) < 3:
        existing = set(e['name'] for e in end_nodes)
        for name in get_career_sub_directions(career_name):
            if len(end_nodes) >= 3:
                break
            if name in existing:
                continue
            end_nodes.append({
                'id': 'end%d' % (len(end_nodes) + 1),
                'name': name,
                'type': 'end',
                'description': '专注于%s方向的职业发展路径' % name
            })
    nodes.extend(end_nodes)

    # 按真实学习顺序重建连线：主前置连续无跳步，副前置为额外声明依赖
    end_ids = [e['id'] for e in end_nodes]
    connections, _order_of = _rebuild_sequence_edges(nodes, 'start', end_ids)

    return {'nodes': nodes, 'connections': connections}


def _validate_learning_path(data, career_name):
    """阶段3：硬校验——反模板词、去重、数值收敛、断环、补孤点"""
    if not data or not isinstance(data.get('nodes'), list):
        return None

    nodes = []
    seen_ids = set()
    dropped_banned = 0
    for item in data['nodes']:
        if not isinstance(item, dict):
            continue
        nid = str(item.get('id', '') or '').strip()
        name = str(item.get('name', '') or '').strip()
        if not nid or not name or nid in seen_ids:
            continue
        is_core = item.get('type') in ('start', 'end')
        if not is_core and _is_banned_text(name):
            dropped_banned += 1
            continue
        seen_ids.add(nid)
        node = dict(item)
        node['id'] = nid
        node['name'] = name
        if not is_core:
            node['duration'] = _safe_int(node.get('duration'), 12, 2, 300)
            node['difficulty'] = _safe_int(node.get('difficulty'), 3, 1, 5)
            node['importance'] = _safe_int(node.get('importance'), 3, 1, 5)
            node['topics'] = _clean_str_list(node.get('topics'), 6)
            deliverable = str(node.get('deliverable', '') or '').strip()
            acceptance = str(node.get('acceptance', '') or '').strip()
            if _is_banned_text(deliverable):
                deliverable = ''
            if _is_banned_text(acceptance):
                acceptance = ''
            node['deliverable'] = deliverable
            node['acceptance'] = acceptance
        nodes.append(node)

    if dropped_banned:
        print('[learning_path] 丢弃空话节点:', dropped_banned)

    middle = [n for n in nodes if n.get('type') not in ('start', 'end')]
    end_nodes = [n for n in nodes if n.get('type') == 'end']
    if len(middle) < 6:
        return None

    start_node = None
    for n in nodes:
        if n.get('type') == 'start':
            start_node = n
            break
    if not start_node:
        start_node = {'id': 'start', 'name': '当前起点', 'type': 'start', 'description': '你的当前知识与技能水平。'}
        nodes.insert(0, start_node)

    if len(end_nodes) < 3:
        existing_names = set(n['name'] for n in end_nodes)
        for name in get_career_sub_directions(career_name):
            if len(end_nodes) >= 3:
                break
            if name in existing_names:
                continue
            new_end = {
                'id': 'end%d' % (len(end_nodes) + 1),
                'name': name,
                'type': 'end',
                'description': '专注于%s方向的职业发展路径' % name
            }
            nodes.append(new_end)
            end_nodes.append(new_end)

    # 按真实学习顺序重建连线：写入 order 序号 + 主前置 main 标记，
    # 所有边只从小序号指向大序号，与学习先后一致（顺带完成断环与补孤点）
    end_ids = [n['id'] for n in end_nodes]
    connections, _order_of = _rebuild_sequence_edges(nodes, start_node['id'], end_ids)

    if not connections:
        return None

    result = {'nodes': nodes, 'connections': connections}

    # 资源兜底与 URL 清洗（复用已有工具函数）
    try:
        ensure_learning_resources(nodes, career_name)
        sanitize_resource_urls(nodes)
    except Exception as exc:
        print('[learning_path] 资源处理异常:', exc)

    return result


def _hard_check_nodes(nodes):
    """阶段4a：本地硬校验，收集问题节点 {id: [原因,...]}
    检查项：topics 不足、缺 deliverable、acceptance 不可自测、跨节点 topics 重复"""
    middle = [n for n in nodes if n.get('type') not in ('start', 'end')]
    problems = {}
    # \u672c\u5730\u7cbe\u786e\u5339\u914d\u7a7a\u6cdb\u6807\u9898\u96c6\u5408\uff1a\u547d\u4e2d\u5373\u6253\u56de\u91cd\u751f\u6210\uff0c\u6539\u4e3a\u5177\u4f53\u6280\u672f\u540d/\u5b66\u4e60\u4e3b\u9898\u3002
    # \u4ec5 exact-match\uff0c\u4e0d\u7528\u300c\u5fc5\u987b\u542b\u62c9\u4e01\u5b57\u7b26\u300d\u767d\u540d\u5355\uff08\u5426\u5219\u8bef\u4f24\u7eaf\u4e2d\u6587\u6280\u672f\u804c\u4e1a\u8282\u70b9\uff09\u3002
    # \u5141\u8bb8 Java / Java\u57fa\u7840 / Python\u57fa\u7840 \u7b49\u660e\u786e\u6280\u672f\u540d\uff08\u4e0d\u5728\u672c\u96c6\u5408\u5185\uff0c\u6b63\u5e38\u901a\u8fc7\uff09\u3002
    _EMPTY_NODE_NAMES = {
        '\u5b66\u4e60\u9636\u6bb5', '\u80fd\u529b\u63d0\u5347', '\u9879\u76ee\u5b9e\u6218', '\u7efc\u5408\u5b9e\u6218', '\u57fa\u7840\u9636\u6bb5', '\u8fdb\u9636\u9636\u6bb5',
        '\u6253\u57fa\u7840', '\u8fdb\u9636\u63d0\u5347', '\u5b9e\u6218\u4e0e\u6c42\u804c', '\u80fd\u529b\u4f53\u7cfb\u6784\u5efa', '\u7cfb\u7edf\u5b66\u4e60', '\u6df1\u5165\u5b66\u4e60',
        '\u57fa\u7840\u7406\u8bba\u4e0e\u6838\u5fc3\u6982\u5ff5', '\u7efc\u5408\u89e3\u51b3\u65b9\u6848', '\u884c\u4e1a\u6700\u4f73\u5b9e\u8df5', '\u77e5\u8bc6\u68b3\u7406', '\u7406\u8bba\u5de9\u56fa',
        '\u7efc\u5408\u80fd\u529b\u63d0\u5347', '\u7406\u8bba\u4e0e\u5b9e\u8df5\u76f8\u7ed3\u5408', '\u7cfb\u7edf\u80fd\u529b\u63d0\u5347',
    }
    for n in middle:
        reasons = []
        _nm = (n.get('name') or '').strip()
        if _nm in _EMPTY_NODE_NAMES:
            reasons.append('\u6280\u80fd\u540d\u662f\u7a7a\u6cdb\u6807\u9898\uff08%s\uff09\uff0c\u5fc5\u987b\u6539\u4e3a\u5177\u4f53\u6280\u672f\u540d\u6216\u5b66\u4e60\u4e3b\u9898\uff08\u5982 Java/Python/SQL \u591a\u8868\u8fde\u63a5\uff09' % _nm)
        topics = n.get('topics') or []
        if len(topics) < 2:
            reasons.append('topics \u4e0d\u8db3\u4e24\u6761\uff0c\u77e5\u8bc6\u70b9\u4e0d\u5177\u4f53')
        if not n.get('deliverable'):
            reasons.append('\u7f3a\u5c11\u53ef\u4ea4\u4ed8\u7684\u4ea7\u51fa\u7269')
        if not _is_acceptance_valid(n.get('acceptance', '')):
            reasons.append('acceptance \u7f3a\u5c11\u72ec\u7acb\u5b8c\u6210\u6761\u4ef6\u6216\u9650\u65f6\uff0c\u65e0\u6cd5\u81ea\u6d4b')
        if reasons:
            problems[n['id']] = reasons

    for i in range(len(middle)):
        for j in range(i + 1, len(middle)):
            a, b = middle[i], middle[j]
            ratio = _topics_overlap_ratio(a.get('topics') or [], b.get('topics') or [])
            if ratio >= TOPICS_OVERLAP_THRESHOLD:
                victim = b['id'] if b['id'] not in problems else a['id']
                reasons = problems.get(victim) or []
                reasons.append('topics 与节点「%s」实质重复（相似度 %.2f）' % (a['name'] if victim == b['id'] else b['name'], ratio))
                problems[victim] = reasons
    return problems


def _critic_learning_path(nodes, career_name):
    """阶段4b：LLM 终审全图节点，返回 {id: {'issue':..., 'suggestion':...}}"""
    middle = [n for n in nodes if n.get('type') not in ('start', 'end')]
    if not middle:
        return {}
    lines = []
    for n in middle:
        topics = '；'.join((n.get('topics') or [])[:6]) or '（空）'
        lines.append('- id=%s | 名称=%s | 阶段=%s | topics=%s | acceptance=%s' % (
            n['id'], n['name'], n.get('stage_label', ''), topics,
            n.get('acceptance', '') or '（空）'
        ))
    template = """你是苛刻的教研质量审核员。下面是一份「%(career)s」学习路线的全部技能节点。请逐个检查，只报告有**明显**问题的节点，没有问题不要编造：

1. \u540d\u5b57\u62bd\u8c61\uff1a\u6280\u80fd\u540d\u6ca1\u6709\u843d\u5230\u5177\u4f53\u6280\u672f\u3001\u5de5\u5177\u6216\u65b9\u6cd5\uff08\u5982\u300cXX \u57fa\u7840\u300d\u300cXX \u5e94\u7528\u300d\u8fd9\u7c7b\u7a7a\u58f3\u540d\uff09\u3002\u5141\u8bb8 Java/Python/Git \u8fd9\u7c7b\u77ed\u6280\u672f\u540d\u4e0e\u300cJava \u96c6\u5408\u4e0e\u6cdb\u578b\u300d\u300cSQL \u591a\u8868\u8fde\u63a5\u300d\u8fd9\u7c7b\u5177\u4f53\u4e3b\u9898\uff1b\u4e25\u7981\u300c\u5b66\u4e60\u9636\u6bb5\u300d\u300c\u80fd\u529b\u63d0\u5347\u300d\u300c\u9879\u76ee\u5b9e\u6218\u300d\u7b49\u7a7a\u6cdb\u6807\u9898\u4f5c\u4e3a\u72ec\u7acb\u8282\u70b9\u540d\u3002
2. topics 空泛：出现「了解/熟悉/掌握 XX」这类无法打勾的表述，或知识点与技能名无关。
3. acceptance 不可自测：没有写清「什么条件下 + 独立完成什么 + 限时多久」。
4. 内容跑题：出现与目标职业无关的技术栈。
5. 实质重复：与另一个节点覆盖了几乎相同的知识点（在 issue 中指出重复的节点名）。

## 节点列表（共 %(count)d 个）
%(lines)s

只输出 JSON：{"problems":[{"id":"节点id","issue":"一句话说明问题","suggestion":"具体改进建议，例如给出更具体的技能名"}]}
没有问题时输出 {"problems":[]}
"""
    prompt = template % {
        'career': career_name,
        'count': len(middle),
        'lines': '\n'.join(lines),
    }
    data = _call_llm_json(prompt, max_tokens=3000, temperature=0.2)
    problems = {}
    if not data or not isinstance(data.get('problems'), list):
        return problems
    valid_ids = set(n['id'] for n in middle)
    for p in data['problems']:
        if not isinstance(p, dict):
            continue
        pid = str(p.get('id', '') or '').strip()
        if pid not in valid_ids or pid in problems:
            continue
        problems[pid] = {
            'issue': str(p.get('issue', '') or '').strip(),
            'suggestion': str(p.get('suggestion', '') or '').strip(),
        }
    return problems


def _regen_flagged_units(flagged, feedback_map, skills, units, basic_info, career_name,
                         career_knowledge, career_skills, career_goals, ctx):
    """阶段4c：对被打回的技能 batch=1 重生成展开内容，拿到完整结果才覆盖"""
    by_id = dict((s['id'], s) for s in skills)
    targets = [sid for sid in flagged if sid in by_id][:CRITIC_REGEN_MAX]
    if not targets:
        return 0

    def _one(sid):
        try:
            data = _expand_skill_batch(
                [by_id[sid]], basic_info, career_name, career_knowledge,
                career_skills, career_goals, ctx=ctx, feedback=feedback_map.get(sid)
            )
            unit = data.get(sid)
            if (isinstance(unit, dict) and unit.get('topics')
                    and unit.get('deliverable') and unit.get('acceptance')):
                units[sid] = unit
                return 1
        except Exception as exc:
            print('[learning_path] 重生成节点失败 %s: %s' % (sid, exc))
        return 0

    done = 0
    pool = ThreadPoolExecutor(max_workers=max(1, min(REGEN_MAX_WORKERS, len(targets))))
    try:
        futures = dict((sid, pool.submit(_one, sid)) for sid in targets)
        for sid, future in futures.items():
            try:
                done += future.result(timeout=200)
            except Exception:
                pass
    finally:
        pool.shutdown(wait=False)
    return done


def _quality_review_loop(result, skills, units, careers, basic_info, career_name,
                         career_knowledge, career_skills, career_goals, ctx):
    """阶段4：质量闭环——本地硬校验 + LLM 终审 → 问题节点打回重生成一轮 → 重组装重校验"""
    try:
        nodes = result.get('nodes') or []
        problems = _hard_check_nodes(nodes)
        hard_count = len(problems)
        hard_ids = list(problems.keys())
        hard_id_set = set(hard_ids)
        critic = {}
        done = 0

        # 提速：硬校验命中的节点先带本地反馈开始重生成，与 LLM 终审并行跑，
        # 省掉「先等终审、再重生成」的串行等待；终审只负责补上硬校验漏掉的节点。
        if hard_ids:
            hard_feedback = dict((pid, '；'.join(problems[pid] or [])) for pid in hard_ids)
            with ThreadPoolExecutor(max_workers=2) as _sidepool:
                _regen_future = _sidepool.submit(
                    _regen_flagged_units, hard_ids, hard_feedback, skills, units,
                    basic_info, career_name, career_knowledge, career_skills,
                    career_goals, ctx
                )
                if hard_count < CRITIC_REGEN_MAX:
                    try:
                        critic = _critic_learning_path(nodes, career_name)
                    except Exception as exc:
                        print('[learning_path] 终审调用失败(跳过):', exc)
                        critic = {}
                else:
                    # 重生成配额已被硬校验占满，终审结果无处可用，跳过省一次调用
                    print('[learning_path] 硬校验已命中 %d 个(额度已满)，跳过终审' % hard_count)
                try:
                    done += _regen_future.result(timeout=300)
                except Exception as exc:
                    print('[learning_path] 首轮重生成失败:', exc)
        else:
            try:
                critic = _critic_learning_path(nodes, career_name)
            except Exception as exc:
                print('[learning_path] 终审调用失败(跳过):', exc)
                critic = {}

        for pid, info in critic.items():
            reasons = problems.get(pid) or []
            reasons.append('终审: %s' % (info.get('issue') or '质量问题'))
            problems[pid] = reasons

        if not problems:
            print('[learning_path] 质量闭环: 无问题节点(硬校验 %d, 终审 0)' % hard_count)
            return result

        print('[learning_path] 质量闭环: 硬校验 %d 个 + 终审 %d 个 = %d 个待重做'
              % (hard_count, len(critic), len(problems)))

        # 终审额外发现的节点再补一轮；总配额仍受 CRITIC_REGEN_MAX 限制
        budget_left = CRITIC_REGEN_MAX - min(hard_count, CRITIC_REGEN_MAX)
        critic_only = [pid for pid in critic.keys() if pid not in hard_id_set][:max(0, budget_left)]
        if critic_only:
            critic_feedback = {}
            for pid in critic_only:
                suggestion = (critic.get(pid) or {}).get('suggestion', '')
                text = '；'.join(problems.get(pid) or [])
                if suggestion:
                    text += '；建议: ' + suggestion
                critic_feedback[pid] = text
            done += _regen_flagged_units(
                critic_only, critic_feedback, skills, units, basic_info,
                career_name, career_knowledge, career_skills, career_goals, ctx
            )
        if not done:
            print('[learning_path] 质量闭环: 重生成全部失败, 保留原结果')
            return result

        assembled = _assemble_learning_path(skills, units, careers, career_name)
        rebuilt = _validate_learning_path(assembled, career_name)
        old_middle = len([n for n in nodes if n.get('type') not in ('start', 'end')])
        if rebuilt is None:
            print('[learning_path] 质量闭环: 重组装校验失败, 保留原结果')
            return result
        new_middle = len([n for n in rebuilt['nodes'] if n.get('type') not in ('start', 'end')])
        if new_middle < old_middle - 2:
            print('[learning_path] 质量闭环: 重组装节点数异常(%d -> %d), 保留原结果' % (old_middle, new_middle))
            return result
        print('[learning_path] 质量闭环: %d 个节点已重做并重组装' % done)
        return rebuilt
    except Exception as exc:
        print('[learning_path] 质量闭环异常(忽略):', exc)
        return result


@app.route('/api/learning_path', methods=['POST'])
def generate_learning_path():
    career_name = ''
    try:
        data = request.get_json()
        
        basic_info = data.get('basic_info', {})
        career_name = data.get('career_name', '')
        career_knowledge = data.get('career_knowledge', '')

        # 知乎登录用户：把兴趣画像并入爱好信息，实现个性化推荐
        personalized = False
        _zhihu_prof = _load_zhihu_interest_profile()
        if _zhihu_prof:
            _kws = [str(k).strip() for k in (_zhihu_prof.get('keywords') or [])
                    if str(k).strip()][:8]
            _extra = '知乎兴趣画像：' + str(_zhihu_prof.get('summary') or '').strip()
            if _kws:
                _extra += '（关键词：' + '、'.join(_kws) + '）'
            _extra = _extra.strip()
            if _extra.replace('知乎兴趣画像：', '').strip():
                _old_h = str(basic_info.get('hobbies') or '').strip()
                basic_info['hobbies'] = (_old_h + '；' + _extra) if _old_h else _extra
                personalized = True

        career_skills = data.get('career_skills', '')
        career_goals = data.get('career_goals', '')
        
        if not career_name:
            return jsonify({"code": -1, "msg": "请选择职业"})
        
        # ===== 两阶段生成：能力地图 → 逐技能展开 → 本地硬校验 =====
        json_result = None
        _t_total = time.time()
        try:
            skills, careers = _build_skill_map(
                basic_info, career_name, career_knowledge, career_skills, career_goals
            )
            if skills:
                ctx = _build_expand_context(skills)
                units = _expand_skills(
                    skills, basic_info, career_name,
                    career_knowledge, career_skills, career_goals, ctx
                )
                assembled = _assemble_learning_path(skills, units, careers, career_name)
                json_result = _validate_learning_path(assembled, career_name)
                if json_result:
                    # ===== 阶段4 质量闭环：硬校验 + LLM 终审 → 问题节点打回重生成 =====
                    json_result = _quality_review_loop(
                        json_result, skills, units, careers, basic_info,
                        career_name, career_knowledge, career_skills, career_goals, ctx
                    )
        except Exception as exc:
            print('[learning_path] 生成异常:', exc)
            json_result = None

        service_error = False
        if json_result is None:
            json_result = generate_fallback_learning_path(career_name)
            service_error = True
        
        print('[learning_path] 生成总耗时 %.1fs' % (time.time() - _t_total))
        response = jsonify({"code": 0, "data": json_result, "msg": "success",
                            "service_error": service_error, "personalized": personalized})
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    
    except Exception as e:
        fallback_result = generate_fallback_learning_path(career_name)
        response = jsonify({
            "code": 0, 
            "data": fallback_result, 
            "msg": f"生成学习路线失败，已使用默认路线：{str(e)}",
            "service_error": True
        })
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response


def generate_fallback_learning_path(career_name):
    tech_keywords = ['软件', '工程师', '开发', '程序员', '编程', '技术', '算法', '数据', '计算机', '前端', '后端', '全栈']
    design_keywords = ['设计', '设计师', '视觉', '插画', 'UI', 'UX', '交互', '产品设计']
    education_keywords = ['教育', '教师', '培训', '讲师']
    finance_keywords = ['金融', '投资', '银行', '证券', '保险', '基金', '理财']
    marketing_keywords = ['营销', '市场', '品牌', '广告', '推广', '公关']
    
    branch_professions = []
    end_professions = []
    learning_nodes = []
    connections = []
    
    if any(kw in career_name for kw in tech_keywords):
        branch_professions = [
            {"name": "前端开发工程师", "id": "branch1", "desc": "专注用户界面开发"},
            {"name": "后端开发工程师", "id": "branch2", "desc": "专注服务端开发"},
            {"name": "全栈开发工程师", "id": "branch3", "desc": "掌握前后端技术"}
        ]
        end_professions = [
            {"name": "前端架构师", "desc": "主导前端技术架构"},
            {"name": "后端技术专家", "desc": "深耕后端技术"},
            {"name": "全栈技术负责人", "desc": "统筹全栈技术方案"},
            {"name": "数据算法工程师", "desc": "专注数据分析和算法"}
        ]
        learning_nodes = [
            {"id": "node1", "name": "学习Python基础语法", "difficulty": 2, "duration": 50, "importance": 5,
             "resources": [{"name": "B站：Python零基础入门教程", "url": "https://search.bilibili.com/all?keyword=Python零基础入门教程"}],
             "description": "系统学习Python基础语法"},
            {"id": "node2", "name": "掌握数据结构与算法", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：算法导论", "url": "https://book.douban.com/subject_search?search_text=算法导论"}],
             "description": "学习数据结构和算法"},
            {"id": "node3", "name": "学习MySQL数据库", "difficulty": 3, "duration": 40, "importance": 5,
             "resources": [{"name": "B站：MySQL数据库入门教程", "url": "https://search.bilibili.com/all?keyword=MySQL数据库入门教程"}],
             "description": "掌握数据库原理和SQL"},
            {"id": "node5", "name": "掌握Git版本控制", "difficulty": 2, "duration": 20, "importance": 4,
             "resources": [{"name": "GitHub：官方文档", "url": "https://github.com/git-guides"}],
             "description": "学习Git版本控制"},
            {"id": "node6a", "name": "学习HTML/CSS/JavaScript", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "MDN：Web开发指南", "url": "https://developer.mozilla.org/zh-CN/docs/Learn"}],
             "description": "学习前端开发基础"},
            {"id": "node6b", "name": "学习Vue3框架", "difficulty": 4, "duration": 80, "importance": 5,
             "resources": [{"name": "Vue官方文档", "url": "https://cn.vuejs.org/"}],
             "description": "掌握Vue3前端框架"},
            {"id": "node7a", "name": "学习Django框架", "difficulty": 4, "duration": 80, "importance": 5,
             "resources": [{"name": "Django官方文档", "url": "https://docs.djangoproject.com/zh-hans/"}],
             "description": "学习后端开发框架"},
            {"id": "node8a", "name": "前端项目实战", "difficulty": 5, "duration": 100, "importance": 5,
             "resources": [{"name": "GitHub：前端实战项目", "url": "https://github.com/search?q=frontend+project"}],
             "description": "参与前端项目开发"},
            {"id": "node8b", "name": "后端项目实战", "difficulty": 5, "duration": 100, "importance": 5,
             "resources": [{"name": "GitHub：后端实战项目", "url": "https://github.com/search?q=backend+project"}],
             "description": "参与后端项目开发"},
            {"id": "node10", "name": "作品集与简历优化", "difficulty": 4, "duration": 40, "importance": 5,
             "resources": [{"name": "知乎：技术面试经验", "url": "https://www.zhihu.com/search?type=content&q=技术面试经验"}],
             "description": "准备求职材料"}
        ]
        connections = [
            {"from": "start", "to": "node1"}, {"from": "start", "to": "node2"},
            {"from": "start", "to": "node3"}, {"from": "start", "to": "node5"},
            {"from": "node1", "to": "branch1"}, {"from": "node1", "to": "branch2"},
            {"from": "node1", "to": "branch3"}, {"from": "node2", "to": "branch1"},
            {"from": "node2", "to": "branch2"}, {"from": "node2", "to": "branch3"},
            {"from": "branch1", "to": "node6a"}, {"from": "node6a", "to": "node6b"},
            {"from": "branch2", "to": "node7a"}, {"from": "branch3", "to": "node6a"},
            {"from": "branch3", "to": "node7a"}, {"from": "node6b", "to": "node8a"},
            {"from": "node7a", "to": "node8b"}, {"from": "node8a", "to": "node10"},
            {"from": "node8b", "to": "node10"}, {"from": "node10", "to": "end1"},
            {"from": "node10", "to": "end2"}, {"from": "node10", "to": "end3"},
            {"from": "node10", "to": "end4"}
        ]
        
    elif any(kw in career_name for kw in finance_keywords):
        branch_professions = [
            {"name": "投资分析师", "id": "branch1", "desc": "专注投资分析和研究"},
            {"name": "金融分析师", "id": "branch2", "desc": "专注财务分析和估值"},
            {"name": "理财顾问", "id": "branch3", "desc": "为客户提供理财建议"}
        ]
        end_professions = [
            {"name": "资深投资分析师", "desc": "主导投资研究和决策"},
            {"name": "金融衍生品专家", "desc": "精通金融衍生品定价"},
            {"name": "财富管理总监", "desc": "统筹财富管理业务"},
            {"name": "量化投资策略师", "desc": "开发量化投资策略"}
        ]
        learning_nodes = [
            {"id": "node1", "name": "经济学原理", "difficulty": 2, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：经济学原理", "url": "https://book.douban.com/subject_search?search_text=经济学原理"}],
             "description": "学习宏观和微观经济学基础"},
            {"id": "node2", "name": "会计学基础", "difficulty": 3, "duration": 80, "importance": 5,
             "resources": [{"name": "MOOC：会计学基础", "url": "https://www.icourse163.org/search.htm?search=会计学基础"}],
             "description": "掌握会计核算和财务报表"},
            {"id": "node3", "name": "统计学基础", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：统计学导论", "url": "https://book.douban.com/subject_search?search_text=统计学导论"}],
             "description": "学习统计方法和数据分析"},
            {"id": "node5", "name": "金融市场概论", "difficulty": 2, "duration": 40, "importance": 5,
             "resources": [{"name": "B站：金融市场入门", "url": "https://search.bilibili.com/all?keyword=金融市场入门"}],
             "description": "了解金融市场结构和运作"},
            {"id": "node6a", "name": "投资分析与组合管理", "difficulty": 4, "duration": 80, "importance": 5,
             "resources": [{"name": "书籍：投资学", "url": "https://book.douban.com/subject_search?search_text=投资学"}],
             "description": "学习投资分析方法和组合构建"},
            {"id": "node6b", "name": "金融建模与估值", "difficulty": 4, "duration": 80, "importance": 5,
             "resources": [{"name": "书籍：财务报表分析", "url": "https://book.douban.com/subject_search?search_text=财务报表分析"}],
             "description": "掌握金融建模和企业估值"},
            {"id": "node7a", "name": "风险管理", "difficulty": 4, "duration": 60, "importance": 5,
             "resources": [{"name": "MOOC：风险管理", "url": "https://www.icourse163.org/search.htm?search=风险管理"}],
             "description": "学习风险识别和管理策略"},
            {"id": "node8a", "name": "金融实习实践", "difficulty": 5, "duration": 120, "importance": 5,
             "resources": [{"name": "知乎：金融实习经验", "url": "https://www.zhihu.com/search?type=content&q=金融实习经验"}],
             "description": "参与金融机构实习"},
            {"id": "node10", "name": "CFA考试准备", "difficulty": 5, "duration": 100, "importance": 5,
             "resources": [{"name": "CFA官方教材", "url": "https://www.cfainstitute.org/"}],
             "description": "准备CFA资格考试"}
        ]
        connections = [
            {"from": "start", "to": "node1"}, {"from": "start", "to": "node2"},
            {"from": "start", "to": "node3"}, {"from": "start", "to": "node5"},
            {"from": "node1", "to": "branch1"}, {"from": "node1", "to": "branch2"},
            {"from": "node1", "to": "branch3"}, {"from": "node2", "to": "branch2"},
            {"from": "node3", "to": "branch1"}, {"from": "branch1", "to": "node6a"},
            {"from": "branch2", "to": "node6b"}, {"from": "branch3", "to": "node7a"},
            {"from": "node6a", "to": "node8a"}, {"from": "node6b", "to": "node8a"},
            {"from": "node7a", "to": "node8a"}, {"from": "node8a", "to": "node10"},
            {"from": "node10", "to": "end1"}, {"from": "node10", "to": "end2"},
            {"from": "node10", "to": "end3"}, {"from": "node10", "to": "end4"}
        ]
        
    elif any(kw in career_name for kw in marketing_keywords):
        branch_professions = [
            {"name": "市场营销专员", "id": "branch1", "desc": "制定营销策略"},
            {"name": "品牌经理", "id": "branch2", "desc": "管理品牌形象"},
            {"name": "数字营销专员", "id": "branch3", "desc": "负责数字营销"}
        ]
        end_professions = [
            {"name": "市场总监", "desc": "统筹市场战略"},
            {"name": "品牌总监", "desc": "主导品牌发展"},
            {"name": "数字营销专家", "desc": "精通数字营销技术"},
            {"name": "营销策划总监", "desc": "统筹营销策划"}
        ]
        learning_nodes = [
            {"id": "node1", "name": "市场营销原理", "difficulty": 2, "duration": 50, "importance": 5,
             "resources": [{"name": "书籍：市场营销原理", "url": "https://book.douban.com/subject_search?search_text=市场营销原理"}],
             "description": "学习市场营销基本理论"},
            {"id": "node2", "name": "消费者行为学", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：消费者行为学", "url": "https://book.douban.com/subject_search?search_text=消费者行为学"}],
             "description": "了解消费者心理和行为"},
            {"id": "node3", "name": "市场调研方法", "difficulty": 3, "duration": 50, "importance": 5,
             "resources": [{"name": "MOOC：市场调研", "url": "https://www.icourse163.org/search.htm?search=市场调研"}],
             "description": "掌握市场调研技术"},
            {"id": "node5", "name": "广告学基础", "difficulty": 2, "duration": 40, "importance": 4,
             "resources": [{"name": "B站：广告学入门", "url": "https://search.bilibili.com/all?keyword=广告学入门"}],
             "description": "学习广告理论和实践"},
            {"id": "node6a", "name": "社交媒体营销", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "知乎：社交媒体营销", "url": "https://www.zhihu.com/search?type=content&q=社交媒体营销"}],
             "description": "学习社交媒体运营"},
            {"id": "node6b", "name": "营销数据分析", "difficulty": 4, "duration": 60, "importance": 5,
             "resources": [{"name": "MOOC：营销数据分析", "url": "https://www.icourse163.org/search.htm?search=营销数据分析"}],
             "description": "掌握营销数据处理"},
            {"id": "node7a", "name": "品牌管理", "difficulty": 4, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：品牌管理", "url": "https://book.douban.com/subject_search?search_text=品牌管理"}],
             "description": "学习品牌策略和管理"},
            {"id": "node8a", "name": "营销项目实战", "difficulty": 5, "duration": 100, "importance": 5,
             "resources": [{"name": "知乎：营销项目经验", "url": "https://www.zhihu.com/search?type=content&q=营销项目经验"}],
             "description": "参与真实营销项目"},
            {"id": "node10", "name": "营销作品集准备", "difficulty": 4, "duration": 40, "importance": 5,
             "resources": [{"name": "B站：营销简历技巧", "url": "https://search.bilibili.com/all?keyword=营销简历技巧"}],
             "description": "准备求职材料"}
        ]
        connections = [
            {"from": "start", "to": "node1"}, {"from": "start", "to": "node2"},
            {"from": "start", "to": "node3"}, {"from": "start", "to": "node5"},
            {"from": "node1", "to": "branch1"}, {"from": "node1", "to": "branch2"},
            {"from": "node1", "to": "branch3"}, {"from": "node2", "to": "branch1"},
            {"from": "node3", "to": "branch1"}, {"from": "branch1", "to": "node6a"},
            {"from": "branch2", "to": "node7a"}, {"from": "branch3", "to": "node6b"},
            {"from": "node6a", "to": "node8a"}, {"from": "node6b", "to": "node8a"},
            {"from": "node7a", "to": "node8a"}, {"from": "node8a", "to": "node10"},
            {"from": "node10", "to": "end1"}, {"from": "node10", "to": "end2"},
            {"from": "node10", "to": "end3"}, {"from": "node10", "to": "end4"}
        ]
        
    elif any(kw in career_name for kw in design_keywords):
        branch_professions = [
            {"name": "UI设计师", "id": "branch1", "desc": "专注用户界面设计"},
            {"name": "UX设计师", "id": "branch2", "desc": "专注用户体验"},
            {"name": "品牌设计师", "id": "branch3", "desc": "专注品牌视觉"}
        ]
        end_professions = [
            {"name": "设计总监", "desc": "主导设计团队"},
            {"name": "产品设计专家", "desc": "整合设计与商业"},
            {"name": "插画艺术家", "desc": "专注插画创作"},
            {"name": "交互设计师", "desc": "专注交互设计"}
        ]
        learning_nodes = [
            {"id": "node1", "name": "设计基础", "difficulty": 2, "duration": 50, "importance": 5,
             "resources": [{"name": "书籍：设计基础", "url": "https://book.douban.com/subject_search?search_text=设计基础"}],
             "description": "学习设计基本理论"},
            {"id": "node2", "name": "色彩理论", "difficulty": 3, "duration": 40, "importance": 5,
             "resources": [{"name": "B站：色彩理论教程", "url": "https://search.bilibili.com/all?keyword=色彩理论教程"}],
             "description": "掌握色彩搭配原则"},
            {"id": "node3", "name": "构成原理", "difficulty": 3, "duration": 50, "importance": 5,
             "resources": [{"name": "书籍：构成设计", "url": "https://book.douban.com/subject_search?search_text=构成设计"}],
             "description": "学习平面构成和立体构成"},
            {"id": "node5", "name": "手绘基础", "difficulty": 2, "duration": 60, "importance": 4,
             "resources": [{"name": "B站：手绘入门教程", "url": "https://search.bilibili.com/all?keyword=手绘入门教程"}],
             "description": "练习手绘表达能力"},
            {"id": "node6a", "name": "UI设计实战", "difficulty": 4, "duration": 80, "importance": 5,
             "resources": [{"name": "B站：UI设计教程", "url": "https://search.bilibili.com/all?keyword=UI设计教程"}],
             "description": "学习界面设计技巧"},
            {"id": "node6b", "name": "交互设计", "difficulty": 4, "duration": 80, "importance": 5,
             "resources": [{"name": "书籍：交互设计精髓", "url": "https://book.douban.com/subject_search?search_text=交互设计精髓"}],
             "description": "掌握交互设计方法"},
            {"id": "node7a", "name": "设计工具精通", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "Figma官方教程", "url": "https://www.figma.com/learn/"}],
             "description": "精通设计软件"},
            {"id": "node8a", "name": "设计项目实战", "difficulty": 5, "duration": 100, "importance": 5,
             "resources": [{"name": "Dribbble：优秀设计作品", "url": "https://dribbble.com/"}, {"name": "Behance：设计作品集", "url": "https://www.behance.net/"}],
             "description": "参与设计项目"},
            {"id": "node10", "name": "作品集制作", "difficulty": 4, "duration": 60, "importance": 5,
             "resources": [{"name": "知乎：设计作品集", "url": "https://www.zhihu.com/search?type=content&q=设计作品集"}],
             "description": "制作个人作品集"}
        ]
        connections = [
            {"from": "start", "to": "node1"}, {"from": "start", "to": "node2"},
            {"from": "start", "to": "node3"}, {"from": "start", "to": "node5"},
            {"from": "node1", "to": "branch1"}, {"from": "node1", "to": "branch2"},
            {"from": "node1", "to": "branch3"}, {"from": "node2", "to": "branch1"},
            {"from": "branch1", "to": "node6a"}, {"from": "branch2", "to": "node6b"},
            {"from": "branch3", "to": "node7a"}, {"from": "node6a", "to": "node8a"},
            {"from": "node6b", "to": "node8a"}, {"from": "node7a", "to": "node8a"},
            {"from": "node8a", "to": "node10"}, {"from": "node10", "to": "end1"},
            {"from": "node10", "to": "end2"}, {"from": "node10", "to": "end3"},
            {"from": "node10", "to": "end4"}
        ]
        
    elif any(kw in career_name for kw in education_keywords):
        branch_professions = [
            {"name": "学校教师", "id": "branch1", "desc": "在学校从事教学"},
            {"name": "在线教育讲师", "id": "branch2", "desc": "开发在线课程"},
            {"name": "职业培训师", "id": "branch3", "desc": "提供职业培训"}
        ]
        end_professions = [
            {"name": "高级教师", "desc": "成为学科带头人"},
            {"name": "教育产品经理", "desc": "设计教育产品"},
            {"name": "教育机构负责人", "desc": "管理教育机构"},
            {"name": "教育技术专家", "desc": "推动教育创新"}
        ]
        learning_nodes = [
            {"id": "node1", "name": "教育学原理", "difficulty": 2, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：教育学原理", "url": "https://book.douban.com/subject_search?search_text=教育学原理"}],
             "description": "学习教育基本理论"},
            {"id": "node2", "name": "教育心理学", "difficulty": 3, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：教育心理学", "url": "https://book.douban.com/subject_search?search_text=教育心理学"}],
             "description": "了解学生心理发展"},
            {"id": "node3", "name": "教学方法", "difficulty": 3, "duration": 50, "importance": 5,
             "resources": [{"name": "MOOC：教学方法", "url": "https://www.icourse163.org/search.htm?search=教学方法"}],
             "description": "掌握教学策略和方法"},
            {"id": "node5", "name": "课程设计", "difficulty": 3, "duration": 50, "importance": 5,
             "resources": [{"name": "书籍：课程设计", "url": "https://book.douban.com/subject_search?search_text=课程设计"}],
             "description": "学习课程开发设计"},
            {"id": "node6a", "name": "课堂管理", "difficulty": 3, "duration": 40, "importance": 5,
             "resources": [{"name": "知乎：课堂管理", "url": "https://www.zhihu.com/search?type=content&q=课堂管理"}],
             "description": "掌握课堂管理技巧"},
            {"id": "node6b", "name": "教育技术应用", "difficulty": 3, "duration": 50, "importance": 4,
             "resources": [{"name": "MOOC：教育技术", "url": "https://www.icourse163.org/search.htm?search=教育技术"}],
             "description": "学习教育技术工具"},
            {"id": "node7a", "name": "教育评估", "difficulty": 4, "duration": 50, "importance": 5,
             "resources": [{"name": "书籍：教育测量与评价", "url": "https://book.douban.com/subject_search?search_text=教育测量与评价"}],
             "description": "掌握教育评价方法"},
            {"id": "node8a", "name": "教育实习", "difficulty": 5, "duration": 120, "importance": 5,
             "resources": [{"name": "知乎：教育实习经验", "url": "https://www.zhihu.com/search?type=content&q=教育实习经验"}],
             "description": "参与教育实习"},
            {"id": "node10", "name": "教师资格证考试", "difficulty": 4, "duration": 60, "importance": 5,
             "resources": [{"name": "B站：教师资格证教程", "url": "https://search.bilibili.com/all?keyword=教师资格证教程"}],
             "description": "准备教师资格考试"}
        ]
        connections = [
            {"from": "start", "to": "node1"}, {"from": "start", "to": "node2"},
            {"from": "start", "to": "node3"}, {"from": "start", "to": "node5"},
            {"from": "node1", "to": "branch1"}, {"from": "node1", "to": "branch2"},
            {"from": "node1", "to": "branch3"}, {"from": "node2", "to": "branch1"},
            {"from": "branch1", "to": "node6a"}, {"from": "branch2", "to": "node6b"},
            {"from": "branch3", "to": "node7a"}, {"from": "node6a", "to": "node8a"},
            {"from": "node6b", "to": "node8a"}, {"from": "node7a", "to": "node8a"},
            {"from": "node8a", "to": "node10"}, {"from": "node10", "to": "end1"},
            {"from": "node10", "to": "end2"}, {"from": "node10", "to": "end3"},
            {"from": "node10", "to": "end4"}
        ]
        
    else:
        branch_professions = [
            {"name": career_name + "（技术方向）", "id": "branch1", "desc": "深耕专业技术"},
            {"name": career_name + "（管理方向）", "id": "branch2", "desc": "走向管理岗位"},
            {"name": career_name + "（创业方向）", "id": "branch3", "desc": "实现商业价值"}
        ]
        end_professions = [
            {"name": career_name + "（高级专家）", "desc": "成为行业权威"},
            {"name": career_name + "（团队负责人）", "desc": "带领团队发展"},
            {"name": career_name + "（创业者）", "desc": "创办企业"},
            {"name": career_name + "（行业顾问）", "desc": "提供专业咨询"}
        ]
        learning_nodes = [
            {"id": "node1", "name": "学习" + career_name + "基础理论与行业认知", "difficulty": 2, "duration": 60, "importance": 5,
             "resources": [{"name": "书籍：专业导论", "url": "https://book.douban.com/subject_search?search_text=专业导论"}],
             "description": "学习" + career_name + "基础理论与行业认知"},
            {"id": "node2", "name": "掌握" + career_name + "核心方法与工具", "difficulty": 3, "duration": 80, "importance": 5,
             "resources": [{"name": "MOOC：专业技能", "url": "https://www.icourse163.org/search.htm?search=专业技能"}],
             "description": "掌握" + career_name + "核心方法与工具"},
            {"id": "node3", "name": "参与" + career_name + "真实项目实践", "difficulty": 4, "duration": 100, "importance": 5,
             "resources": [{"name": "知乎：行业经验", "url": "https://www.zhihu.com/search?type=content&q=行业经验"}],
             "description": "参与" + career_name + "真实项目实践"},
            {"id": "node10", "name": "准备" + career_name + "求职与职业资格", "difficulty": 4, "duration": 60, "importance": 5,
             "resources": [{"name": "B站：职业资格考试", "url": "https://search.bilibili.com/all?keyword=职业资格考试"}],
             "description": "准备" + career_name + "求职材料与职业资格认证"}
        ]
        connections = [
            {"from": "start", "to": "node1"}, {"from": "node1", "to": "node2"},
            {"from": "node2", "to": "branch1"}, {"from": "node2", "to": "branch2"},
            {"from": "node2", "to": "branch3"}, {"from": "branch1", "to": "node3"},
            {"from": "branch2", "to": "node3"}, {"from": "branch3", "to": "node3"},
            {"from": "node3", "to": "node10"}, {"from": "node10", "to": "end1"},
            {"from": "node10", "to": "end2"}, {"from": "node10", "to": "end3"},
            {"from": "node10", "to": "end4"}
        ]
    
    nodes = [
        {"id": "start", "name": "当前起点", "type": "start", "description": "你的当前知识水平和学习起点"}
    ]

    nodes.extend(learning_nodes)

    nodes.extend([
        {"id": branch_professions[0]["id"], "name": branch_professions[0]["name"], "type": "branch",
         "description": branch_professions[0]["desc"], "difficulty": 4, "duration": 80, "importance": 5},
        {"id": branch_professions[1]["id"], "name": branch_professions[1]["name"], "type": "branch",
         "description": branch_professions[1]["desc"], "difficulty": 4, "duration": 80, "importance": 5},
        {"id": branch_professions[2]["id"], "name": branch_professions[2]["name"], "type": "branch",
         "description": branch_professions[2]["desc"], "difficulty": 4, "duration": 80, "importance": 5},
        {"id": "end1", "name": end_professions[0]["name"], "type": "end", "description": end_professions[0]["desc"]},
        {"id": "end2", "name": end_professions[1]["name"], "type": "end", "description": end_professions[1]["desc"]},
        {"id": "end3", "name": end_professions[2]["name"], "type": "end", "description": end_professions[2]["desc"]},
        {"id": "end4", "name": end_professions[3]["name"], "type": "end", "description": end_professions[3]["desc"]}
    ])

    ensure_learning_resources(nodes, career_name)

    return {
        "nodes": nodes,
        "connections": connections
    }


def ensure_learning_resources(nodes, career_name):
    """确保每个学习节点至少包含3个资源，其中至少2个是B站视频。"""
    bilibili_templates = [
        ("B站：{name}基础入门教程", "https://search.bilibili.com/all?keyword={name}%20基础入门教程"),
        ("B站：{name}实战项目教程", "https://search.bilibili.com/all?keyword={name}%20实战项目教程"),
        ("B站：{name}进阶技巧", "https://search.bilibili.com/all?keyword={name}%20进阶技巧"),
        ("B站：{name}面试求职经验", "https://search.bilibili.com/all?keyword={name}%20面试求职经验"),
    ]
    other_templates = [
        ("MOOC：{name}专业课程", "https://www.icourse163.org/search.htm?search={name}"),
        ("知乎：{name}学习路线", "https://www.zhihu.com/search?type=content&q={name}%20学习路线"),
        ("书籍：{name}入门到精通", "https://book.douban.com/subject_search?search_text={name}%20入门到精通"),
    ]

    for node in nodes:
        if node.get('type') in ('start', 'end', 'branch'):
            continue
        resources = node.get('resources') or []
        if not isinstance(resources, list):
            resources = []
        existing_urls = {r.get('url') for r in resources if isinstance(r, dict) and r.get('url')}

        def add_resource(name_tmpl, url_tmpl):
            url = url_tmpl.replace('{name}', career_name)
            if url in existing_urls:
                return False
            existing_urls.add(url)
            resources.append({
                "name": name_tmpl.replace('{name}', career_name),
                "url": url
            })
            return True

        # 先补齐B站视频到至少2个
        bilibili_count = sum(1 for r in resources if isinstance(r, dict) and 'bilibili.com' in (r.get('url') or ''))
        for name_tmpl, url_tmpl in bilibili_templates:
            if bilibili_count >= 2:
                break
            if add_resource(name_tmpl, url_tmpl):
                bilibili_count += 1

        # 再补齐总数到至少3个
        idx = 0
        while len(resources) < 3:
            name_tmpl, url_tmpl = other_templates[idx % len(other_templates)]
            add_resource(name_tmpl, url_tmpl)
            idx += 1

        node['resources'] = resources

def sanitize_resource_urls(nodes):
    url_pattern = re.compile(r'^https?://[^\s<>"\']+$')
    
    for node in nodes:
        if not node.get('resources'):
            continue
        
        sanitized_resources = []
        seen_urls = set()
        
        for resource in node['resources']:
            if isinstance(resource, dict):
                name = resource.get('name', '')
                url = resource.get('url', '')
                if not url or not url_pattern.match(url):
                    continue
                if not name:
                    name = extract_resource_name_from_url(url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sanitized_resources.append({"name": name, "url": url})
            elif isinstance(resource, str):
                url_match = re.search(r'https?://[^\s<>"\']+', resource)
                if not url_match:
                    continue
                url = url_match.group(0)
                if not url_pattern.match(url):
                    continue
                text_before = resource[:url_match.start()].strip()
                name = text_before or extract_resource_name_from_url(url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sanitized_resources.append({"name": name, "url": url})
        
        node['resources'] = sanitized_resources


def extract_resource_name_from_url(url):
    if 'bilibili.com' in url:
        return 'B站视频'
    if 'xuetangx.com' in url or 'icourse163.org' in url:
        return 'MOOC课程'
    if 'book.douban.com' in url:
        return '书籍/教材'
    if 'github.com' in url:
        return '代码仓库'
    if 'zhihu.com' in url:
        return '知乎文章'
    if 'coursera.org' in url:
        return '在线课程'
    return '学习资源'


def validate_and_enhance_learning_path(data, career_name):
    if not data or not data.get('nodes') or not data.get('connections'):
        return generate_fallback_learning_path(career_name)
    
    nodes = data['nodes']
    connections = data['connections']
    
    sanitize_resource_urls(nodes)
    
    node_count = len(nodes)
    end_nodes = [n for n in nodes if n.get('type') == 'end']
    middle_nodes = [n for n in nodes if n.get('type') not in ['start', 'end']]
    
    if node_count < 6:
        return generate_fallback_learning_path(career_name)
    
    if len(end_nodes) < 3:
        existing_end_ids = set(n['id'] for n in end_nodes)
        existing_end_names = set(n['name'] for n in end_nodes)
        sub_directions = get_career_sub_directions(career_name)
        for i in range(len(end_nodes), 3):
            new_end_id = f"end{i+1}"
            if new_end_id not in existing_end_ids:
                direction_name = sub_directions[i] if i < len(sub_directions) else f"{career_name}专家"
                if direction_name in existing_end_names:
                    direction_name = direction_name + "（资深）"
                nodes.append({
                    "id": new_end_id,
                    "name": direction_name,
                    "type": "end",
                    "description": f"专注于{direction_name}方向的职业发展路径"
                })
    
    if len(middle_nodes) < 3:
        existing_node_ids = set(n['id'] for n in nodes)
        node_num = len(middle_nodes) + 1
        stage_templates = [
            ("学习{name}基础理论与核心概念", "掌握{name}入门知识，建立基础认知框架"),
            ("掌握{name}核心方法与工具", "学习{name}常用方法论和工具操作"),
            ("参与{name}真实项目实战", "通过实际项目提升{name}应用能力"),
            ("深入研究{name}进阶技术与案例", "学习{name}高级技能与行业最佳实践"),
            ("构建{name}领域综合解决方案", "整合所学知识，形成{name}系统性能力")
        ]
        while len(middle_nodes) < 5:
            new_node_id = f"node{node_num}"
            if new_node_id not in existing_node_ids:
                stage_name, stage_desc = stage_templates[node_num - 1] if node_num <= len(stage_templates) else (
                    f"{career_name}学习阶段{node_num}",
                    f"{career_name}学习阶段{node_num}的专项内容"
                )
                stage_name = stage_name.replace('{name}', career_name)
                stage_desc = stage_desc.replace('{name}', career_name)
                nodes.append({
                    "id": new_node_id,
                    "name": stage_name,
                    "difficulty": min(5, node_num + 1),
                    "duration": 40 + node_num * 20,
                    "importance": min(5, node_num),
                    "resources": [
                        {"name": f"B站：{career_name}基础入门教程", "url": f"https://search.bilibili.com/all?keyword={career_name}%20基础入门教程"},
                        {"name": f"B站：{career_name}实战项目教程", "url": f"https://search.bilibili.com/all?keyword={career_name}%20实战项目"},
                        {"name": f"MOOC：{career_name}专业课程", "url": f"https://www.icourse163.org/search.htm?search={career_name}"}
                    ],
                    "description": stage_desc
                })
                existing_node_ids.add(new_node_id)
                middle_nodes = [n for n in nodes if n.get('type') not in ['start', 'end']]
            node_num += 1
    
    start_node = None
    for n in nodes:
        if n.get('type') == 'start':
            start_node = n
            break
    
    if not start_node:
        nodes.insert(0, {"id": "start", "name": "当前起点", "type": "start", "description": "用户当前的知识水平"})
        start_node = nodes[0]
    
    node_id_set = set(n['id'] for n in nodes)
    valid_connections = []
    for conn in connections:
        try:
            if isinstance(conn, dict) and 'from' in conn and 'to' in conn:
                if conn['from'] in node_id_set and conn['to'] in node_id_set and conn['from'] != conn['to']:
                    valid_connections.append(conn)
        except Exception:
            pass
    data['connections'] = valid_connections
    connections = valid_connections
    
    middle_ids = [n['id'] for n in middle_nodes]
    end_ids = [n['id'] for n in end_nodes]
    
    if start_node['id'] not in [c['from'] for c in connections]:
        if middle_ids:
            connections.append({"from": start_node['id'], "to": middle_ids[0], "difficulty": "简单", "duration": "40小时"})
    
    # 仅对没有任何入边的中间节点补边：从起点直连（它们属于可直接开始的基础内容）。
    # 不做 middle[i]→middle[i+1] 链式补连，避免把并列的职业分支错误串联。
    incoming_targets = set(c['to'] for c in connections)
    for mid in middle_ids:
        if mid not in incoming_targets:
            connections.append({
                "from": start_node['id'],
                "to": mid,
                "difficulty": "简单",
                "duration": "40小时"
            })
            incoming_targets.add(mid)
    
    last_middle = middle_ids[-1] if middle_ids else start_node['id']
    for end_id in end_ids:
        if end_id not in [c['to'] for c in connections]:
            connections.append({
                "from": last_middle,
                "to": end_id,
                "difficulty": "中等",
                "duration": "60小时"
            })
    
    return {"nodes": nodes, "connections": connections}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)