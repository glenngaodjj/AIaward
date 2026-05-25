#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI创新应用月度评审系统 — 在线版 v4
====================================
· 员工提交：姓名 + 简要说明 + 多文件上传 + 网址链接
· 管理员：一键AI评审，自动读取所有文件/链接内容，综合评分 + 详细评语
"""

import os, re, json, sqlite3, secrets, base64, io, subprocess, tempfile
from datetime import datetime
from functools import wraps
from urllib.request import urlopen, Request
from urllib.error import URLError
from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, g, send_file, abort
)
import anthropic

# ── App Config ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB (gunicorn streams to disk)

ADMIN_PASSWORD    = os.environ.get('ADMIN_PASSWORD', 'admin123')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH           = os.environ.get('DB_PATH', 'submissions.db')
UPLOAD_DIR        = os.environ.get('UPLOAD_DIR', 'uploaded_files')
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {
    # Documents
    'pdf', 'doc', 'docx', 'txt', 'md', 'rtf',
    # Web / Code
    'html', 'htm', 'php', 'py', 'js', 'ts', 'jsx', 'tsx',
    'css', 'scss', 'less', 'vue', 'rb', 'java', 'cpp', 'c',
    'h', 'cs', 'go', 'rs', 'swift', 'kt', 'sh', 'sql',
    # Data
    'json', 'xml', 'yaml', 'yml', 'csv', 'tsv',
    # Images
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg',
    # Video
    'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v',
    # Presentation / Spreadsheet (read as zip/text)
    'pptx', 'xlsx', 'xls',
}

def file_cat(ext):
    ext = ext.lower()
    if ext == 'pdf':
        return 'pdf'
    if ext in ('doc','docx'):
        return 'word'
    if ext in ('html','htm'):
        return 'html'
    if ext in ('jpg','jpeg','png','gif','webp','bmp'):
        return 'image'
    if ext in ('mp4','mov','avi','mkv','webm','m4v'):
        return 'video'
    if ext in ('pptx',):
        return 'pptx'
    if ext in ('xlsx','xls'):
        return 'xlsx'
    # All remaining: treat as plain text (code, data, markup, etc.)
    return 'text'


# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    with app.app_context():
        db = sqlite3.connect(DB_PATH)
        # Create tables (new installs)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                department  TEXT,
                note        TEXT,
                links       TEXT,
                month       TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS submission_files (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL,
                file_name     TEXT NOT NULL,
                file_ext      TEXT NOT NULL,
                file_path     TEXT NOT NULL,
                FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                month        TEXT NOT NULL,
                results_json TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
        # Migration: add new columns to submissions if missing
        for col, definition in [
            ('note', 'TEXT'), ('links', 'TEXT'),
            ('dim_innovation', 'TEXT'), ('dim_growth', 'TEXT'),
            ('dim_learning', 'TEXT'), ('dim_impact', 'TEXT'),
            ('is_deployed', 'TEXT'), ('email', 'TEXT'),
        ]:
            try:
                db.execute(f"ALTER TABLE submissions ADD COLUMN {col} {definition}")
            except Exception:
                pass

        # Migration: rebuild submissions table if old schema (has 'title')
        cols = [r[1] for r in db.execute("PRAGMA table_info(submissions)").fetchall()]
        if 'title' in cols:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS submissions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    department TEXT, note TEXT, links TEXT, month TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                INSERT INTO submissions_new (id,name,department,note,month,created_at)
                    SELECT id,name,department,
                           COALESCE(description,title,'（已迁移）'),month,created_at
                    FROM submissions;
                DROP TABLE submissions;
                ALTER TABLE submissions_new RENAME TO submissions;
            """)

        # Migration: rebuild submission_files if it still uses file_data BLOB
        fcols = [r[1] for r in db.execute("PRAGMA table_info(submission_files)").fetchall()]
        if 'file_data' in fcols:
            # Old BLOB rows: save data to disk, replace table
            old_rows = db.execute(
                "SELECT id,submission_id,file_name,file_ext,file_data FROM submission_files"
            ).fetchall()
            db.execute("DROP TABLE submission_files")
            db.execute("""
                CREATE TABLE submission_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL, file_ext TEXT NOT NULL, file_path TEXT NOT NULL,
                    FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE
                )
            """)
            import uuid
            for row in old_rows:
                if row[4]:
                    fname = f"{uuid.uuid4().hex}_{row[2]}"
                    fpath = os.path.join(UPLOAD_DIR, fname)
                    with open(fpath, 'wb') as fh:
                        fh.write(bytes(row[4]))
                    db.execute(
                        "INSERT INTO submission_files (id,submission_id,file_name,file_ext,file_path) VALUES (?,?,?,?,?)",
                        (row[0], row[1], row[2], row[3], fpath)
                    )

        db.commit()
        db.close()

init_db()


# ── Auth ───────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# ── File & URL Processing ──────────────────────────────────────────────────────
def extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            texts = [p.extract_text() or '' for p in pdf.pages[:25]]
        return '\n'.join(texts)[:8000]
    except Exception as e:
        return f'[PDF解析失败: {e}]'

def extract_docx(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())[:8000]
    except Exception as e:
        return f'[Word解析失败: {e}]'

def extract_text(data: bytes) -> str:
    """Read any plain-text file (code, JSON, CSV, Markdown, etc.)"""
    for enc in ('utf-8', 'gbk', 'latin-1'):
        try:
            return data.decode(enc)[:8000]
        except Exception:
            continue
    return '[文件编码无法识别]'

def extract_pptx(data: bytes) -> str:
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(data))
        lines = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, 'text') and shape.text.strip():
                    lines.append(shape.text.strip())
        return '\n'.join(lines)[:8000]
    except Exception as e:
        return f'[PPT解析失败: {e}]'

def extract_xlsx(data: bytes) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f'[表格: {ws.title}]')
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 100: break
                row_txt = '\t'.join(str(c) if c is not None else '' for c in row)
                if row_txt.strip():
                    lines.append(row_txt)
        return '\n'.join(lines)[:8000]
    except Exception as e:
        return f'[Excel解析失败: {e}]'

def extract_html_bytes(data: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(data, 'html.parser')
        for tag in soup(['script','style','head']):
            tag.decompose()
        return soup.get_text(separator='\n', strip=True)[:8000]
    except Exception as e:
        return f'[HTML解析失败: {e}]'

def fetch_url(url: str) -> str:
    try:
        req = Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urlopen(req, timeout=10) as r:
            raw = r.read()
        ct = r.headers.get('Content-Type','')
        if 'html' in ct:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, 'html.parser')
            for tag in soup(['script','style','head','nav','footer']):
                tag.decompose()
            return soup.get_text(separator='\n', strip=True)[:5000]
        return raw.decode('utf-8','ignore')[:5000]
    except Exception as e:
        return f'[链接无法访问: {e}]'

def image_to_b64(data: bytes, ext: str):
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode not in ('RGB','RGBA'):
            img = img.convert('RGB')
        max_px = 1568
        if max(img.width, img.height) > max_px:
            ratio = max_px / max(img.width, img.height)
            img = img.resize((int(img.width*ratio), int(img.height*ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = 'JPEG' if ext.lower() in ('jpg','jpeg','bmp') else 'PNG'
        img.save(buf, format=fmt)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        mt = 'image/jpeg' if fmt == 'JPEG' else 'image/png'
        return b64, mt
    except Exception:
        return base64.standard_b64encode(data).decode(), f'image/{ext.lower()}'

def extract_video_frames(data: bytes, ext: str):
    frames = []
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as vf:
            vf.write(data); vpath = vf.name
        out_dir = tempfile.mkdtemp()
        cmd = ['ffmpeg', '-i', vpath,
               '-vf', "select='not(mod(n\\,floor(nb_frames/4+1)))'",
               '-vsync','vfr','-frames:v','4','-q:v','3',
               os.path.join(out_dir,'frame%02d.jpg'),
               '-y','-loglevel','error']
        subprocess.run(cmd, timeout=30, check=True)
        for fname in sorted(os.listdir(out_dir)):
            if fname.endswith('.jpg'):
                with open(os.path.join(out_dir,fname),'rb') as f:
                    b64 = base64.standard_b64encode(f.read()).decode()
                frames.append((b64,'image/jpeg'))
        os.unlink(vpath)
    except Exception:
        pass
    return frames

def describe_with_vision(client, frames, context_note: str) -> str:
    content = [{"type":"text","text":
        f"以下是员工AI作品的截图/画面。员工简要说明：「{context_note}」\n"
        "请详细描述画面中展示的AI应用内容、功能、界面、操作流程等，用于后续AI评审打分。中文，200字以内。"}]
    for b64, mt in frames[:4]:
        content.append({"type":"image","source":{"type":"base64","media_type":mt,"data":b64}})
    try:
        msg = client.messages.create(
            model='claude-opus-4-6', max_tokens=512,
            messages=[{"role":"user","content":content}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f'[视觉分析失败: {e}]'


def process_submission(sub: dict, files: list, client) -> str:
    """Build full content string for a submission (all files + links)"""
    note  = sub.get('note','')
    links = json.loads(sub.get('links') or '[]')
    parts = []

    # Process each file (read from disk)
    for frow in files:
        fname = frow['file_name']
        ext   = frow['file_ext'].lower()
        fpath = frow.get('file_path','')
        if not fpath or not os.path.exists(fpath):
            parts.append(f'── 文件：{fname} ──\n[文件已不存在]')
            continue
        with open(fpath, 'rb') as fh:
            data = fh.read()
        cat   = file_cat(ext)
        parts.append(f'── 文件：{fname} ──')
        if cat == 'pdf':
            parts.append(extract_pdf(data))
        elif cat == 'word':
            parts.append(extract_docx(data))
        elif cat == 'html':
            parts.append(extract_html_bytes(data))
        elif cat == 'text':
            parts.append(extract_text(data))
        elif cat == 'pptx':
            parts.append(extract_pptx(data))
        elif cat == 'xlsx':
            parts.append(extract_xlsx(data))
        elif cat == 'image':
            b64, mt = image_to_b64(data, ext)
            desc = describe_with_vision(client, [(b64,mt)], note)
            parts.append(f'[图片内容] {desc}')
        elif cat == 'video':
            frames = extract_video_frames(data, ext)
            if frames:
                desc = describe_with_vision(client, frames, note)
                parts.append(f'[视频内容] {desc}')
            else:
                parts.append('[视频文件（无法提取帧）]')
        else:
            parts.append(f'[不支持的文件类型: .{ext}]')

    # Fetch each URL
    for url in links:
        url = url.strip()
        if not url:
            continue
        parts.append(f'── 链接：{url} ──')
        parts.append(fetch_url(url))

    return '\n\n'.join(parts) if parts else '（未上传文件或链接）'


# ── AI Evaluation ──────────────────────────────────────────────────────────────
COMPANY_CONTEXT = """
【公司背景与核心理念】
DJJ是一家总部位于澳大利亚的叉车租赁与销售公司。

这次AI创新评选，不仅仅是一次活动，而是DJJ推动组织进入AI时代的重要起点。
公司的核心价值观：开放 · 成长 · 创新 · 长期。

我们真正希望推动的方向：
- 主动学习，而非被动等待
- 愿意改变，而非守旧执行
- 系统化思维，而非碎片化操作
- 推动结果，而非停留在概念
- 长期沉淀，而非一次性使用
- 组织协同，而非单点突破

【评审视角】
这次评审，不是在评"谁会用AI工具"，而是在评：
谁体现了开放学习、主动成长、创新思维与长期建设的能力。

参赛方向面向全公司所有职能，包括但不限于：
销售、客服、运营、财务、维修、组织管理、团队协同、流程优化、系统建设、数据分析、培训、自动化、知识沉淀等。

AI不是额外工具，而是未来新的工作方式。
凡是真正帮助工作方式升级、效率提升、组织能力建设与长期价值沉淀的AI应用，均属于高价值方向。
"""

def build_eval_prompt(subs_with_content: list) -> str:
    block = ''
    for i, s in enumerate(subs_with_content, 1):
        links = json.loads(s.get('links') or '[]')
        file_count = s.get('file_count', 0)

        dim_section = ''
        if any(s.get(k) for k in ('dim_innovation','dim_growth','dim_learning','dim_impact','is_deployed')):
            dim_section = f"""
【员工四维度自述】
💡 创新性自述：{s.get('dim_innovation') or '（未填写）'}
⚙️ 实用性自述：{s.get('dim_growth') or '（未填写）'}
   📍 落地状态：{s.get('is_deployed') or '（未选择）'}
📚 开放学习自述：{s.get('dim_learning') or '（未填写）'}
🌟 长期影响力自述：{s.get('dim_impact') or '（未填写）'}
"""
        block += f"""
{'='*60}
【参赛作品 {i}】
员工姓名：{s['name']}
部门：{s['department'] or '未填写'}
简要说明：{s['note'] or '（未填写）'}
附件数量：{file_count} 个文件  |  链接数量：{len(links)} 个
提交时间：{s['created_at']}
{dim_section}
【作品内容详情（文件/链接）】
{s.get('content','（无内容）')}
"""
    return f"""你是专业公正的AI创新评审专家，正在评选本月"最佳AI创新应用奖"。

{COMPANY_CONTEXT}

【评审标准（每项满分25分，合计100分）】

1. 创新性 Innovation（25分）
- 是否利用AI重新定义工作方式
- 是否具备主动探索与创新意识
- 是否创造性组合AI工具、流程或系统
- 是否突破传统执行模式，体现对未来工作方式的思考
- 高分标准：真正体现AI时代的新工作逻辑，而不仅是简单工具使用

2. 实用性 Growth（25分）
- 是否真正提升工作效率或业务结果
- 是否解决实际问题，具备落地能力，能长期稳定使用
- 是否对团队、流程、协同或管理产生实际帮助
- 说明：不仅限于销售或叉车业务，组织、系统、流程、管理类AI应用同样属于高价值方向
- 高分：已实际使用并产生明显效果；中高分：已完成核心测试，具备落地潜力；中低分：概念多于实际落地

3. 开放学习能力 Open Learning（25分）
- 是否主动学习AI，具备持续迭代能力
- 是否真正理解AI逻辑，而非机械使用
- 是否体现开放思维与学习速度
- 高分标准：不仅"会用AI"，而是真正开始理解AI时代的工作逻辑

4. 长期影响力 Long-term Impact（25分）
- 是否具备长期价值，可复制、可推广
- 是否能沉淀为组织能力，改变团队工作方式
- 是否对未来协同、培训、管理或系统建设有帮助
- 说明：不只看当前使用人数，也看长期组织价值与未来扩展潜力

【评审要求】
- 【硬性规则，不可违反】第一名必须是落地状态为"已完全落地，正在日常使用中"的作品。如果没有任何作品完全落地，则第一名空缺，排名从第二名开始。凡落地状态为"部分落地"或"尚未落地"的作品，无论其他维度得分多高，最终排名不得列第一。
- 员工的四维度自述是重要参考依据，结合自述与实际作品内容综合评分，自述写得好但作品内容不匹配时适当扣分
- 评分必须有区分度，不要平均分，优秀作品应明显拉开差距
- 每人写一段100-150字的综合评语，必须具体、有针对性，同时指出亮点与改进方向，不要只夸奖，不要套话
- 总分 = 四项之和，按总分降序排列
- 严格输出纯JSON，无其他文字

参赛作品：
{block}

输出格式（JSON数组，按总分降序）：
[
  {{
    "name": "姓名",
    "innovation": 分数,
    "practicality": 分数,
    "learning_depth": 分数,
    "impact": 分数,
    "total": 合计,
    "highlights": "亮点（1句话）",
    "comment": "100-150字综合评语，具体指出作品特点、优势及改进建议"
  }}
]"""


# ══════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

BASE_STYLE = """
<style>
  :root {
    --p:#6366f1;--pd:#4f46e5;--pl:#ede9fe;
    --g:#10b981;--r:#ef4444;--y:#f59e0b;
    --bg:#f1f5f9;--card:#fff;
    --t1:#1e293b;--t2:#475569;--t3:#94a3b8;
    --bd:#e2e8f0;
    --sh:0 1px 3px rgba(0,0,0,.08);
    --shm:0 4px 8px rgba(0,0,0,.07),0 2px 4px rgba(0,0,0,.04);
    --shl:0 12px 24px rgba(0,0,0,.08),0 4px 8px rgba(0,0,0,.04);
  }
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
       background:var(--bg);color:var(--t1);min-height:100vh;}
  input,textarea{width:100%;padding:10px 13px;border:1.5px solid var(--bd);border-radius:9px;
    font-size:14px;font-family:inherit;color:var(--t1);background:#fff;
    transition:border-color .15s,box-shadow .15s;outline:none;}
  input:focus,textarea:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(99,102,241,.1);}
  textarea{resize:vertical;line-height:1.6;}
  .btn{padding:11px 20px;border:none;border-radius:9px;font-size:14px;font-weight:600;
       cursor:pointer;transition:all .18s;display:inline-flex;align-items:center;
       justify-content:center;gap:6px;font-family:inherit;text-decoration:none;}
  .btn-p{background:var(--p);color:#fff;}
  .btn-p:hover{background:var(--pd);transform:translateY(-1px);}
  .btn-d{background:#fee2e2;color:var(--r);}
  .btn-d:hover{background:var(--r);color:#fff;}
  .btn-g{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;
         box-shadow:0 4px 14px rgba(99,102,241,.3);}
  .btn-g:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(99,102,241,.38);}
  .btn:disabled{opacity:.45;cursor:not-allowed;transform:none!important;box-shadow:none!important;}
  .btn-w{width:100%;}
  .fg{margin-bottom:16px;}
  .lbl{display:block;font-size:12px;font-weight:600;color:var(--t2);
       text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px;}
  .req{color:var(--r);}
  .hint{font-size:12px;color:var(--t3);margin-top:5px;line-height:1.5;}
  .toast{position:fixed;bottom:24px;right:24px;padding:12px 18px;border-radius:10px;
         color:#fff;font-size:13px;font-weight:500;transform:translateY(70px);opacity:0;
         transition:all .28s;z-index:2000;box-shadow:var(--shl);max-width:320px;}
  .toast.show{transform:translateY(0);opacity:1;}
  .toast.ok{background:var(--g);}.toast.err{background:var(--r);}
</style>
"""

# ── Employee Submission Form ───────────────────────────────────────────────────
SUBMIT_HTML = BASE_STYLE + r"""
<style>
  .hero{background:linear-gradient(135deg,#4338ca 0%,#7c3aed 55%,#a855f7 100%);
        padding:40px 24px;text-align:center;color:#fff;}
  .hero .badge{display:inline-block;background:rgba(255,255,255,.2);border-radius:999px;
               padding:5px 16px;font-size:13px;font-weight:600;margin-bottom:14px;}
  .hero h1{font-size:26px;font-weight:800;margin-bottom:8px;}
  .hero p{font-size:14px;opacity:.82;max-width:480px;margin:0 auto;line-height:1.7;}
  .wrap{max-width:640px;margin:0 auto;padding:28px 20px 60px;}
  .sec{font-size:13px;font-weight:700;color:var(--p);text-transform:uppercase;letter-spacing:.8px;
       margin:24px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--pl);}

  /* Multi-file drop zone */
  .drop-zone{border:2px dashed var(--bd);border-radius:12px;background:#fafbff;
             padding:28px 20px;text-align:center;cursor:pointer;transition:all .2s;position:relative;}
  .drop-zone:hover,.drop-zone.over{border-color:var(--p);background:var(--pl);}
  .dz-icon{font-size:36px;margin-bottom:8px;}
  .dz-title{font-size:15px;font-weight:600;margin-bottom:4px;}
  .dz-sub{font-size:12px;color:var(--t3);line-height:1.6;}
  .drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;}

  /* File list */
  .file-list{margin-top:10px;display:flex;flex-direction:column;gap:6px;}
  .file-item{display:flex;align-items:center;gap:10px;background:#f0f9ff;
             border:1.5px solid #bae6fd;border-radius:8px;padding:8px 12px;}
  .fi-icon{font-size:20px;flex-shrink:0;}
  .fi-info{flex:1;min-width:0;}
  .fi-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .fi-size{font-size:11px;color:var(--t3);}
  .fi-rm{background:none;border:none;cursor:pointer;color:var(--r);font-size:18px;padding:2px 6px;flex-shrink:0;}

  /* URL inputs */
  .url-list{display:flex;flex-direction:column;gap:8px;}
  .url-row{display:flex;gap:6px;align-items:center;}
  .url-row input{flex:1;}
  .url-rm{background:none;border:none;cursor:pointer;color:var(--r);font-size:18px;padding:4px 8px;flex-shrink:0;}
  .add-url-btn{background:var(--pl);color:var(--p);border:1.5px dashed var(--p);
               border-radius:8px;padding:8px 14px;font-size:13px;font-weight:600;
               cursor:pointer;width:100%;margin-top:6px;transition:all .15s;}
  .add-url-btn:hover{background:var(--p);color:#fff;}

  .char-count{text-align:right;font-size:11px;color:var(--t3);margin-top:4px;}
  .submit-btn{padding:14px;font-size:16px;border-radius:12px;margin-top:8px;}
  .footer{text-align:center;font-size:12px;color:var(--t3);margin-top:32px;}
  .info-box{background:linear-gradient(135deg,#f0fdf4,#ecfdf5);border:1px solid #86efac;
            border-radius:12px;padding:14px 18px;margin-bottom:20px;font-size:13px;color:#166534;line-height:1.7;}
</style>

<div class="hero">
  <div class="badge">🏆 {{ month }} · DJJ AI 创新评选</div>
  <h1>提交你的 AI 创新作品</h1>
  <p>这不只是一次比赛。<br>
  这是 DJJ 迈入 AI 时代的第一步 —— 我们真正奖励的是：<br>
  <strong>开放学习 · 主动成长 · 创新思维 · 长期建设</strong></p>
</div>

<div class="wrap">
{% if success %}
  <div style="text-align:center;padding:48px 20px">
    <div style="font-size:64px;margin-bottom:16px">🎉</div>
    <h2 style="font-size:22px;margin-bottom:10px">提交成功！</h2>
    <p style="color:var(--t2);font-size:14px;line-height:1.8;max-width:360px;margin:0 auto">
      感谢 <strong>{{ submitted_name }}</strong> 的参与！<br>
      {% if file_count %}上传了 {{ file_count }} 个文件{% endif %}
      {% if link_count %}{% if file_count %}，{% endif %}{{ link_count }} 个链接{% endif %}<br>
      评审结果将由管理员公布，加油！💪
    </p>
    <a href="/" class="btn btn-p" style="margin-top:28px;display:inline-flex">再提交一份</a>
  </div>
{% else %}

{% if error %}
<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:10px;
            padding:12px 16px;margin-bottom:20px;font-size:13px;color:#991b1b">
  ⚠️ {{ error }}
</div>
{% endif %}

<div class="info-box">
  📋 评审从四个维度打分（各25分，共100分）：<br>
  💡 <strong>创新性</strong> · ⚙️ <strong>实用性</strong> · 📚 <strong>开放学习能力</strong> · 🌟 <strong>长期影响力</strong>
</div>

<form method="POST" action="/" enctype="multipart/form-data" id="subForm">

  <div class="sec">👤 基本信息</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
    <div class="fg">
      <label class="lbl">姓名 <span class="req">*</span></label>
      <input type="text" name="name" placeholder="你的姓名" required maxlength="50">
    </div>
    <div class="fg">
      <label class="lbl">部门（可选）</label>
      <input type="text" name="department" placeholder="如：销售部、运维部" maxlength="50">
    </div>
  </div>
  <div class="fg">
    <label class="lbl">邮箱 <span class="req">*</span></label>
    <input type="email" name="email" placeholder="your@email.com" required maxlength="100">
    <p class="hint">评审结果和个人评语将发送到此邮箱</p>
  </div>

  <div class="sec">💬 简要说明</div>
  <div class="fg">
    <textarea name="note" rows="3" maxlength="600" required
      placeholder="用2-4句话描述：用了什么AI工具、解决了什么问题、达到了什么效果？"
      oninput="updateCount(this,'nc')"></textarea>
    <div class="char-count"><span id="nc">0</span> / 600</div>
  </div>

  <div class="sec">📸 作品截图（可多张）</div>
  <div class="fg">
    <p class="hint" style="margin-bottom:10px">上传能直观展示你作品的截图，帮助评审更好理解你的工作成果</p>
    <input type="file" name="screenshots" id="screenshotInput" multiple style="display:none"
           accept=".jpg,.jpeg,.png,.gif,.webp,.bmp">
    <div class="drop-zone" id="screenshotZone" onclick="document.getElementById('screenshotInput').click()">
      <div class="dz-icon">📸</div>
      <div class="dz-title">点击或拖放上传截图</div>
      <div class="dz-sub">最多 10 张 · 每张不超过 10MB · 支持 JPG / PNG / GIF / WebP</div>
    </div>
    <div class="file-list" id="screenshotList"></div>
  </div>

  <div class="sec">💡 四维度自述（帮助AI更准确评分）</div>
  <div style="display:grid;gap:14px;margin-bottom:4px">
    <div class="fg" style="margin-bottom:0">
      <label class="lbl" style="color:#6366f1">💡 创新性 — 你的作品如何体现创新？</label>
      <textarea name="dim_innovation" rows="2" maxlength="400"
        placeholder="例：我用 AI 重新设计了报价流程，从手动填表改为语音输入自动生成，打破了传统工作方式…"
        oninput="updateCount(this,'nc1')"></textarea>
      <div class="char-count"><span id="nc1">0</span> / 400</div>
    </div>
    <div class="fg" style="margin-bottom:0">
      <label class="lbl" style="color:#10b981">⚙️ 实用性 — 它解决了什么实际问题？效果如何？</label>
      <textarea name="dim_growth" rows="2" maxlength="400"
        placeholder="例：每周节省约3小时的手动整理时间，错误率从15%降到2%，已在日常工作中稳定使用2个月…"
        oninput="updateCount(this,'nc2')"></textarea>
      <div class="char-count"><span id="nc2">0</span> / 400</div>
      <div style="margin-top:10px">
        <div style="font-size:12px;font-weight:600;color:#475569;margin-bottom:8px">📍 作品落地状态</div>
        <div style="display:flex;flex-direction:column;gap:6px">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;
                        background:#f0fdf4;border:1.5px solid #86efac;border-radius:8px;padding:8px 12px">
            <input type="radio" name="is_deployed" value="已完全落地，正在日常使用中" style="width:auto;accent-color:#10b981">
            <span>✅ 已完全落地，正在日常使用中</span>
          </label>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;
                        background:#fff7ed;border:1.5px solid #fed7aa;border-radius:8px;padding:8px 12px">
            <input type="radio" name="is_deployed" value="部分落地，仍在测试和完善中" style="width:auto;accent-color:#f59e0b">
            <span>🔧 部分落地，仍在测试和完善中</span>
          </label>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;
                        background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:8px;padding:8px 12px">
            <input type="radio" name="is_deployed" value="尚未落地，目前为概念/原型阶段" style="width:auto;accent-color:#94a3b8">
            <span>💡 尚未落地，目前为概念/原型阶段</span>
          </label>
        </div>
      </div>
    </div>
    <div class="fg" style="margin-bottom:0">
      <label class="lbl" style="color:#f59e0b">📚 开放学习 — 你是如何学习和迭代的？</label>
      <textarea name="dim_learning" rows="2" maxlength="400"
        placeholder="例：我从零开始学习 Prompt 工程，失败了8次后找到有效方法，还在团队内部分享了学习过程…"
        oninput="updateCount(this,'nc3')"></textarea>
      <div class="char-count"><span id="nc3">0</span> / 400</div>
    </div>
    <div class="fg" style="margin-bottom:0">
      <label class="lbl" style="color:#8b5cf6">🌟 长期影响力 — 它对团队或公司有什么长期价值？</label>
      <textarea name="dim_impact" rows="2" maxlength="400"
        placeholder="例：这套流程已整理成 SOP，其他同事可以直接复用，预计可推广到3个部门…"
        oninput="updateCount(this,'nc4')"></textarea>
      <div class="char-count"><span id="nc4">0</span> / 400</div>
    </div>
  </div>

  <div class="sec">📎 上传文件（可多选）</div>
  <div class="fg">
    <!-- Hidden inputs -->
    <input type="file" name="files" id="fileInput" multiple style="display:none"
           accept=".pdf,.doc,.docx,.txt,.md,.html,.htm,.php,.py,.js,.ts,.jsx,.tsx,.css,.json,.xml,.yaml,.yml,.csv,.jpg,.jpeg,.png,.gif,.webp,.bmp,.svg,.mp4,.mov,.avi,.mkv,.webm,.m4v,.pptx,.xlsx,.xls,.rb,.java,.cpp,.c,.go,.rs,.swift,.sh,.sql">
    <input type="file" name="files" id="folderInput" multiple webkitdirectory style="display:none">

    <div class="drop-zone" id="dropZone">
      <div class="dz-icon">📂</div>
      <div class="dz-title">将文件拖放到此处</div>
      <div class="dz-sub">或使用下方按钮选择文件 / 整个文件夹</div>
    </div>

    <div style="display:flex;gap:10px;margin-top:10px">
      <button type="button" onclick="document.getElementById('fileInput').click()"
        style="flex:1;padding:10px;border:1.5px solid var(--p);border-radius:9px;
               background:var(--pl);color:var(--p);font-size:13px;font-weight:600;cursor:pointer">
        📄 选择文件（多选）
      </button>
      <button type="button" onclick="document.getElementById('folderInput').click()"
        style="flex:1;padding:10px;border:1.5px solid var(--g);border-radius:9px;
               background:#f0fdf4;color:#166534;font-size:13px;font-weight:600;cursor:pointer">
        📁 选择整个文件夹
      </button>
    </div>

    <div class="file-list" id="fileList"></div>
  </div>

  <div class="sec">🔗 相关链接（可选）</div>
  <div class="fg">
    <p class="hint" style="margin-bottom:10px">如有演示视频、在线工具、Google Drive 文档等，可在此添加链接</p>
    <div class="url-list" id="urlList">
      <div class="url-row">
        <input type="url" name="links" placeholder="https://..." class="url-input">
        <button type="button" class="url-rm" onclick="removeUrl(this)" title="删除">✕</button>
      </div>
    </div>
    <button type="button" class="add-url-btn" onclick="addUrl()">＋ 添加更多链接</button>
  </div>

  <button type="submit" class="btn btn-g btn-w submit-btn" id="submitBtn">
    🚀 提交作品
  </button>
</form>

<div class="footer">提交内容仅用于内部AI创新评审 · 如有问题请联系管理员</div>
{% endif %}
</div>

<script>
const EXT_ICONS={pdf:'📕',doc:'📘',docx:'📘',html:'🌐',htm:'🌐',
  jpg:'🖼️',jpeg:'🖼️',png:'🖼️',gif:'🖼️',webp:'🖼️',bmp:'🖼️',
  mp4:'🎬',mov:'🎬',avi:'🎬',mkv:'🎬',webm:'🎬',m4v:'🎬'};

// Screenshot upload
let selectedScreenshots=[];
const ssi=document.getElementById('screenshotInput');
const ssz=document.getElementById('screenshotZone');
const ssl=document.getElementById('screenshotList');
const SS_MAX_COUNT = 10;
const SS_MAX_BYTES = 10 * 1024 * 1024; // 10MB per image

function addScreenshots(files){
  let warned = false;
  Array.from(files).forEach(f=>{
    if(selectedScreenshots.length >= SS_MAX_COUNT){
      if(!warned){ alert(`截图最多上传 ${SS_MAX_COUNT} 张，多余的已忽略`); warned=true; }
      return;
    }
    if(f.size > SS_MAX_BYTES){
      alert(`「${f.name}」超过 10MB 限制，已跳过`); return;
    }
    if(!selectedScreenshots.find(x=>x.name===f.name&&x.size===f.size))
      selectedScreenshots.push(f);
  });
  syncScreenshots(); renderScreenshots();
}

ssi.addEventListener('change',()=>{ addScreenshots(ssi.files); ssi.value=''; });
ssz.addEventListener('dragover',e=>{e.preventDefault();ssz.classList.add('over');});
ssz.addEventListener('dragleave',()=>ssz.classList.remove('over'));
ssz.addEventListener('drop',e=>{
  e.preventDefault();ssz.classList.remove('over');
  addScreenshots(Array.from(e.dataTransfer.files).filter(f=>/\.(jpe?g|png|gif|webp|bmp)$/i.test(f.name)));
});
function syncScreenshots(){
  const dt=new DataTransfer();
  selectedScreenshots.forEach(f=>dt.items.add(f));
  ssi.files=dt.files;
}
function renderScreenshots(){
  ssl.innerHTML='';
  if(selectedScreenshots.length===0) return;
  const counter=document.createElement('div');
  const full=selectedScreenshots.length>=SS_MAX_COUNT;
  counter.style.cssText=`margin-bottom:6px;font-size:12px;font-weight:600;
    color:${full?'#b45309':'#166534'};`;
  counter.textContent=`${full?'⚠️':'✅'} 已选 ${selectedScreenshots.length} / ${SS_MAX_COUNT} 张`;
  ssl.appendChild(counter);
  selectedScreenshots.forEach((f,i)=>{
    const url=URL.createObjectURL(f);
    const div=document.createElement('div');
    div.className='file-item';
    div.innerHTML=`<img src="${url}" style="width:48px;height:48px;object-fit:cover;border-radius:6px;flex-shrink:0">
      <div class="fi-info"><div class="fi-name">${f.name}</div><div class="fi-size">${fmtSize(f.size)}</div></div>
      <button type="button" class="fi-rm" onclick="removeScreenshot(${i})">✕</button>`;
    ssl.appendChild(div);
  });
}
function removeScreenshot(idx){selectedScreenshots.splice(idx,1);syncScreenshots();renderScreenshots();}

function fmtSize(b){
  if(b<1024)return b+'B';
  if(b<1048576)return (b/1024).toFixed(1)+'KB';
  return (b/1048576).toFixed(1)+'MB';
}
function updateCount(el,id){document.getElementById(id).textContent=el.value.length;}

let selectedFiles=[];

const fi=document.getElementById('fileInput');
const foi=document.getElementById('folderInput');
const dz=document.getElementById('dropZone');
const fl=document.getElementById('fileList');

function addFilesToList(newFiles){
  Array.from(newFiles).forEach(f=>{
    if(!selectedFiles.find(x=>x.name===f.name&&x.size===f.size))
      selectedFiles.push(f);
  });
  syncInputFiles();
  renderFiles();
}

function syncInputFiles(){
  const dt=new DataTransfer();
  selectedFiles.forEach(f=>dt.items.add(f));
  fi.files=dt.files;
}

foi.addEventListener('change',()=>{ addFilesToList(foi.files); foi.value=''; });

function renderFiles(){
  fl.innerHTML='';
  const MAX_BYTES=200*1024*1024;
  let total=0;
  selectedFiles.forEach((f,i)=>{
    total+=f.size;
    const ext=f.name.split('.').pop().toLowerCase();
    const div=document.createElement('div');
    div.className='file-item';
    div.innerHTML=`<span class="fi-icon">${EXT_ICONS[ext]||'📄'}</span>
      <div class="fi-info"><div class="fi-name">${f.name}</div><div class="fi-size">${fmtSize(f.size)}</div></div>
      <button type="button" class="fi-rm" onclick="removeFile(${i})">✕</button>`;
    fl.appendChild(div);
  });
  // Show total size warning if needed
  const existing=document.getElementById('sizeWarning');
  if(existing)existing.remove();
  if(selectedFiles.length>0){
    const over=total>MAX_BYTES;
    const warn=document.createElement('div');
    warn.id='sizeWarning';
    warn.style.cssText=`margin-top:8px;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:600;
      background:${over?'#fef2f2':'#f0fdf4'};color:${over?'#991b1b':'#166534'};
      border:1px solid ${over?'#fca5a5':'#86efac'}`;
    warn.textContent=`${over?'⚠️ 超出限制！':'✅'} 已选 ${selectedFiles.length} 个文件，总大小：${fmtSize(total)} / 200MB 上限${over?'  请删除部分文件，或将大文件改用链接提交':''}`;
    fl.appendChild(warn);
  }
}

function removeFile(idx){
  selectedFiles.splice(idx,1);
  updateFileInput();
  renderFiles();
}

function updateFileInput(){
  const dt=new DataTransfer();
  selectedFiles.forEach(f=>dt.items.add(f));
  fi.files=dt.files;
}

fi.addEventListener('change',()=>{
  Array.from(fi.files).forEach(f=>{
    if(!selectedFiles.find(x=>x.name===f.name&&x.size===f.size))
      selectedFiles.push(f);
  });
  updateFileInput();renderFiles();
});

dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{
  e.preventDefault();dz.classList.remove('over');
  Array.from(e.dataTransfer.files).forEach(f=>{
    if(!selectedFiles.find(x=>x.name===f.name&&x.size===f.size))
      selectedFiles.push(f);
  });
  updateFileInput();renderFiles();
});

function addUrl(){
  const row=document.createElement('div');
  row.className='url-row';
  row.innerHTML=`<input type="url" name="links" placeholder="https://..." class="url-input">
    <button type="button" class="url-rm" onclick="removeUrl(this)">✕</button>`;
  document.getElementById('urlList').appendChild(row);
}
function removeUrl(btn){
  const rows=document.querySelectorAll('.url-row');
  if(rows.length>1) btn.closest('.url-row').remove();
  else btn.closest('.url-row').querySelector('input').value='';
}

document.getElementById('subForm')?.addEventListener('submit',function(e){
  // Check total file size before submitting
  const MAX_BYTES = 200 * 1024 * 1024; // 200MB
  const total = selectedFiles.reduce((s,f)=>s+f.size, 0);
  if(total > MAX_BYTES){
    e.preventDefault();
    alert(`⚠️ 文件总大小 ${fmtSize(total)} 超过限制（最大 200MB）。\n\n建议：\n• 删除部分大文件\n• 视频建议上传到 YouTube/Google Drive，再粘贴链接\n• 图片可以压缩后再上传`);
    return;
  }
  const btn=document.getElementById('submitBtn');
  btn.disabled=true;btn.textContent='⏳ 提交中（大文件可能需要1-2分钟）…';
});
</script>
"""

# ── Admin Login ────────────────────────────────────────────────────────────────
LOGIN_HTML = BASE_STYLE + """
<style>
  body{display:flex;align-items:center;justify-content:center;min-height:100vh;}
  .box{width:100%;max-width:380px;padding:20px;}
  .logo{text-align:center;margin-bottom:32px;}
  .logo .icon{font-size:52px;margin-bottom:10px;}
  .logo h1{font-size:20px;font-weight:700;}
  .logo p{font-size:13px;color:var(--t2);margin-top:4px;}
  .err{background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;
       padding:10px 14px;font-size:13px;color:#991b1b;margin-bottom:16px;}
</style>
<div class="box">
  <div class="logo">
    <div class="icon">🔐</div>
    <h1>管理员登录</h1>
    <p>AI创新评审系统 · 后台管理</p>
  </div>
  <div style="background:#fff;border-radius:16px;box-shadow:var(--shm);padding:28px">
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="POST" action="/admin">
      <div class="fg">
        <label class="lbl">管理员密码</label>
        <input type="password" name="password" placeholder="请输入密码" autofocus required>
      </div>
      <button type="submit" class="btn btn-p btn-w" style="margin-top:4px">登录</button>
    </form>
  </div>
</div>
"""

# ── Admin Dashboard ────────────────────────────────────────────────────────────
DASHBOARD_HTML = BASE_STYLE + r"""
<style>
  .topbar{background:linear-gradient(135deg,#4338ca,#7c3aed);color:#fff;
          position:sticky;top:0;z-index:100;box-shadow:var(--shm);}
  .topbar-in{max-width:1100px;margin:0 auto;padding:14px 28px;display:flex;align-items:center;gap:12px;}
  .topbar h1{font-size:17px;font-weight:700;}
  .topbar p{font-size:12px;opacity:.75;margin-top:1px;}
  .topbar-actions{margin-left:auto;display:flex;gap:8px;align-items:center;}
  .pill{background:rgba(255,255,255,.2);border-radius:999px;padding:4px 12px;font-size:12px;font-weight:600;}
  .wrap{max-width:1100px;margin:0 auto;padding:28px;}

  .month-tabs{display:flex;gap:6px;margin-bottom:22px;flex-wrap:wrap;}
  .mtab{padding:7px 18px;border-radius:999px;border:1.5px solid var(--bd);background:#fff;
        font-size:13px;font-weight:500;cursor:pointer;transition:all .15s;color:var(--t2);}
  .mtab.active{background:var(--p);color:#fff;border-color:var(--p);}
  .mtab:hover:not(.active){border-color:var(--p);color:var(--p);}

  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px;}
  .stat{padding:18px 20px;border-radius:12px;background:#fff;box-shadow:var(--sh);}
  .stat .num{font-size:30px;font-weight:800;color:var(--p);line-height:1;}
  .stat .lbl{font-size:12px;color:var(--t2);margin-top:4px;}

  .sub-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px;}
  .sub-card{border:1.5px solid var(--bd);border-radius:12px;padding:16px 18px;background:#fff;}
  .sub-card:hover{border-color:var(--p);box-shadow:0 0 0 3px rgba(99,102,241,.07);}
  .sub-top{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;}
  .av{width:40px;height:40px;border-radius:10px;
      background:linear-gradient(135deg,#6366f1,#a855f7);
      display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:16px;flex-shrink:0;}
  .sub-name{font-weight:700;font-size:15px;}
  .tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;display:inline-block;margin-top:4px;margin-right:4px;}
  .tag-dept{background:var(--pl);color:var(--p);}
  .tag-file{background:#f0f9ff;color:#0369a1;}
  .tag-link{background:#f0fdf4;color:#166534;}
  .sub-note{font-size:13px;color:var(--t2);line-height:1.6;margin-top:8px;
            display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}
  .sub-meta{font-size:11px;color:var(--t3);margin-top:8px;}
  .sub-foot{display:flex;align-items:center;flex-wrap:wrap;gap:6px;
            margin-top:12px;padding-top:10px;border-top:1px solid var(--bd);}

  /* Eval box */
  .eval-box{background:#fff;border-radius:16px;padding:24px 28px;margin-bottom:24px;
            box-shadow:var(--sh);border:1.5px solid var(--bd);}
  .eval-box h3{font-size:16px;font-weight:700;margin-bottom:6px;}
  .eval-box p{font-size:13px;color:var(--t2);margin-bottom:16px;line-height:1.6;}
  .eval-row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;}
  .api-wrap{flex:1;min-width:240px;}
  .api-wrap label{font-size:11px;font-weight:600;color:var(--t2);
                  text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:6px;}
  .no-api-warn{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;
               padding:12px 16px;font-size:13px;color:#9a3412;margin-bottom:16px;}

  /* Results */
  .res-card{border:1.5px solid var(--bd);border-radius:16px;overflow:hidden;margin-bottom:16px;}
  .res-card.r1{border-color:#f59e0b;} .res-card.r2{border-color:#9ca3af;}
  .res-card.r3{border-color:#b45309;}
  .res-hd{padding:16px 20px;background:#fafafa;display:flex;align-items:center;gap:12px;}
  .medal{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;
         justify-content:center;font-size:22px;flex-shrink:0;}
  .medal.r1{background:#fef3c7;} .medal.r2{background:#f3f4f6;}
  .medal.r3{background:#fde68a;} .medal.rn{background:var(--pl);font-size:14px;font-weight:700;color:var(--p);}
  .res-name{font-weight:700;font-size:16px;}
  .res-highlight{font-size:12px;color:var(--t2);margin-top:3px;font-style:italic;}
  .tot-n{font-size:32px;font-weight:800;color:var(--p);line-height:1;}
  .tot-of{font-size:11px;color:var(--t3);}
  .res-bd{padding:16px 20px;}
  .sc-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;}
  .sc-row label{display:flex;justify-content:space-between;font-size:11px;font-weight:600;
                color:var(--t2);margin-bottom:5px;}
  .sc-row label span{color:var(--t1);font-weight:700;}
  .bar-bg{height:7px;background:#e2e8f0;border-radius:999px;overflow:hidden;}
  .bar-fill{height:100%;border-radius:999px;width:0%;
            background:linear-gradient(90deg,#6366f1,#a855f7);
            transition:width 1.2s cubic-bezier(.4,0,.2,1);}
  .comment-box{background:#f8fafc;border-left:3px solid var(--p);border-radius:0 8px 8px 0;
               padding:12px 16px;font-size:13px;color:var(--t2);line-height:1.8;}
  .comment-label{font-size:11px;font-weight:700;color:var(--p);
                 text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;}

  .overlay{position:fixed;inset:0;background:rgba(15,23,42,.55);backdrop-filter:blur(6px);
           display:none;align-items:center;justify-content:center;z-index:1000;}
  .overlay.show{display:flex;}
  .ov-box{background:#fff;border-radius:18px;padding:36px 44px;text-align:center;
          box-shadow:0 24px 60px rgba(0,0,0,.2);max-width:360px;width:90%;}
  .spinner{width:50px;height:50px;border:4px solid #e2e8f0;border-top-color:var(--p);
           border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 18px;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .ov-steps{margin-top:14px;text-align:left;}
  .ov-step{font-size:12px;color:var(--t3);padding:3px 0;}
  .ov-step.active{color:var(--p);font-weight:600;}
  .empty{text-align:center;padding:48px;color:var(--t3);}
</style>

<div class="topbar">
  <div class="topbar-in">
    <div>
      <h1>🏆 AI创新评审后台</h1>
      <p>澳大利亚叉车租赁与销售公司</p>
    </div>
    <div class="topbar-actions">
      <span class="pill">{{ current_month }} · {{ month_count }}份</span>
      <a href="/submit-link" class="btn" style="background:rgba(255,255,255,.2);color:#fff;font-size:12px;padding:7px 14px">🔗 员工链接</a>
      <a href="/admin/logout" class="btn" style="background:rgba(255,255,255,.15);color:#fff;font-size:12px;padding:7px 14px">退出</a>
    </div>
  </div>
</div>

<div class="wrap">

  <div class="month-tabs">
    {% for m in months %}
    <button class="mtab {% if m == current_month %}active{% endif %}"
      onclick="window.location.href='/admin/dashboard?month='+encodeURIComponent('{{ m }}')">
      {{ m }}（{{ month_counts[m] }}份）
    </button>
    {% endfor %}
  </div>

  <div class="stats">
    <div class="stat"><div class="num">{{ month_count }}</div><div class="lbl">{{ current_month }} 参赛作品</div></div>
    <div class="stat"><div class="num">{{ total_count }}</div><div class="lbl">历史总作品数</div></div>
    <div class="stat"><div class="num">{{ eval_count }}</div><div class="lbl">历史评审次数</div></div>
  </div>

  <div class="eval-box">
    <h3>🤖 AI 智能评审</h3>
    <p>
      Claude 将自动读取 <strong>{{ current_month }}</strong> 所有作品的文件内容和链接，
      综合评分并生成详细评语。含图片/视频时约需 1-3 分钟。
    </p>
    {% if not api_key_set %}
    <div class="no-api-warn">⚠️ 未检测到 ANTHROPIC_API_KEY，请在下方手动输入</div>
    {% endif %}
    <div class="eval-row">
      {% if not api_key_set %}
      <div class="api-wrap">
        <label>Anthropic API Key</label>
        <input type="password" id="apiKeyInput" placeholder="sk-ant-api03-..."
               style="font-family:'Courier New',monospace;font-size:12px;">
      </div>
      {% endif %}
      <button class="btn btn-g" onclick="startEval()" id="evalBtn"
              >
        🚀 开始评审 {{ current_month }}
      </button>
    </div>
  </div>

  <!-- Latest results -->
  {% if latest_eval %}
  <div style="margin-bottom:32px">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
      <h2 style="font-size:19px;font-weight:800">🏆 评审结果</h2>
      <span style="font-size:12px;color:var(--t3)">{{ latest_eval.created_at }}</span>
    </div>
    {% set results = latest_eval.results %}
    {% for r in results %}
    {% set i = loop.index0 %}
    {% set rc = 'r1' if i==0 else ('r2' if i==1 else ('r3' if i==2 else 'rn')) %}
    {% set medal = '🥇' if i==0 else ('🥈' if i==1 else ('🥉' if i==2 else '#'~loop.index)) %}
    <div class="res-card {{ rc }}">
      <div class="res-hd">
        <div class="medal {{ rc }}">{{ medal }}</div>
        <div style="flex:1;min-width:0">
          <div class="res-name">{{ r.name }}</div>
          {% if r.get('highlights') %}
          <div class="res-highlight">✨ {{ r.highlights }}</div>
          {% endif %}
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div class="tot-n">{{ r.total }}</div>
          <div class="tot-of">/ 100 分</div>
        </div>
      </div>
      <div class="res-bd">
        <div class="sc-grid">
          <div class="sc-row">
            <label>💡 创新性 Innovation <span>{{ r.innovation }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.innovation/25*100)|int }}%"></div></div>
          </div>
          <div class="sc-row">
            <label>⚙️ 实用性 Growth <span>{{ r.practicality }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.practicality/25*100)|int }}%"></div></div>
          </div>
          <div class="sc-row">
            <label>📚 开放学习 Open Learning <span>{{ r.learning_depth }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.learning_depth/25*100)|int }}%"></div></div>
          </div>
          <div class="sc-row">
            <label>🌟 长期影响力 Long-term Impact <span>{{ r.impact }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.impact/25*100)|int }}%"></div></div>
          </div>
        </div>
        <div class="comment-label">📝 评语</div>
        <div class="comment-box">{{ r.comment }}</div>
        {% if r.get('email') %}
        <div style="margin-top:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span style="font-size:12px;color:var(--t3)">✉️ {{ r.email }}</span>
          <a href="https://mail.google.com/mail/?view=cm&to={{ r.email | urlencode }}&su={{ ('DJJ AI创新评选 — 你的专属评语') | urlencode }}&body={{ ('Hi ' + r.name + '，\n\n感谢参与本月 DJJ AI 创新评选！\n\n以下是评审对你作品的专属评语：\n\n' + r.comment + '\n\nDJJ 管理团队') | urlencode }}"
             target="_blank"
             style="font-size:12px;padding:5px 12px;background:#ede9fe;color:#6d28d9;
                    border-radius:7px;text-decoration:none;font-weight:600">
            ✉️ 发送评语给 {{ r.name }}
          </a>
        </div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- Submissions -->
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
    <h2 style="font-size:17px;font-weight:700">{{ current_month }} 参赛作品（{{ month_count }}份）</h2>
  </div>
  <div class="sub-grid">
    {% if filtered_subs %}
    {% for s in filtered_subs %}
    <div class="sub-card">
      <div class="sub-top">
        <div class="av">{{ s.name[0] }}</div>
        <div style="flex:1;min-width:0">
          <div class="sub-name">{{ s.name }}</div>
          {% if s.department %}<span class="tag tag-dept">{{ s.department }}</span>{% endif %}
          {% if s.file_count > 0 %}<span class="tag tag-file">📎 {{ s.file_count }}个文件</span>{% endif %}
          {% if s.link_count > 0 %}<span class="tag tag-link">🔗 {{ s.link_count }}个链接</span>{% endif %}
        </div>
      </div>
      {% if s.note %}<div class="sub-note">{{ s.note }}</div>{% endif %}
      <div class="sub-meta" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px">
        <span>📅 {{ s.created_at[:16] }}</span>
        {% if s.email %}
        <span style="color:#6366f1;font-size:11px">✉️ {{ s.email }}</span>
        {% endif %}
        {% if s.is_deployed %}
        <span style="font-size:11px;padding:2px 7px;border-radius:999px;font-weight:600;
          background:{% if s.is_deployed == '已完全落地，正在日常使用中' %}#f0fdf4;color:#166534
          {% elif s.is_deployed == '部分落地，仍在测试和完善中' %}#fff7ed;color:#9a3412
          {% else %}#f1f5f9;color:#475569{% endif %}">
          {{ s.is_deployed }}
        </span>
        {% endif %}
      </div>
      <div class="sub-foot">
        {% if s.file_count > 0 %}
        <a href="/admin/files/{{ s.id }}" class="btn" target="_blank"
           style="font-size:12px;padding:5px 10px;background:#f0f9ff;color:#0369a1;text-decoration:none">
          ⬇ 查看文件
        </a>
        {% endif %}
        {% if s.email %}
        <a href="https://mail.google.com/mail/?view=cm&to={{ s.email | urlencode }}&su={{ ('DJJ AI创新评选 — 你的专属评语') | urlencode }}&body={{ ('Hi ' + s.name + '，\n\n感谢你参与本月 DJJ AI 创新评选！\n\n（请在此粘贴该员工的评语）\n\nDJJ 管理团队') | urlencode }}"
           target="_blank"
           class="btn" style="font-size:12px;padding:5px 10px;background:#ede9fe;color:#6d28d9;text-decoration:none">
          ✉️ 发送评语
        </a>
        {% endif %}
        <button class="btn btn-d" style="padding:5px 12px;font-size:12px;margin-left:auto"
                onclick="deleteSub({{ s.id }},this)">删除</button>
      </div>
    </div>
    {% endfor %}
    {% else %}
    <div class="empty" style="grid-column:1/-1">
      <div style="font-size:44px;margin-bottom:12px">📂</div>
      <p style="font-size:14px">{{ current_month }} 暂无参赛作品<br>
      <a href="/submit-link" style="color:var(--p)">复制员工提交链接</a> 发送给同事吧</p>
    </div>
    {% endif %}
  </div>
</div>

<!-- Loading overlay -->
<div class="overlay" id="overlay">
  <div class="ov-box">
    <div class="spinner"></div>
    <h3 style="margin-bottom:8px">Claude 正在评审中…</h3>
    <p style="font-size:12px;color:var(--t2)">正在读取文件和链接内容，综合分析打分</p>
    <div class="ov-steps">
      <div class="ov-step active" id="step1">📂 读取文件内容…</div>
      <div class="ov-step" id="step2">🔗 抓取链接内容…</div>
      <div class="ov-step" id="step3">🤖 Claude 综合评审…</div>
      <div class="ov-step" id="step4">✅ 生成评分和评语…</div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const API_KEY_SET = {{ 'true' if api_key_set else 'false' }};

function toast(msg,type='ok'){
  const el=document.getElementById('toast');
  el.textContent=msg;el.className=`toast show ${type}`;
  setTimeout(()=>el.className='toast',4500);
}

async function deleteSub(id,btn){
  if(!confirm('确定删除这份作品？'))return;
  btn.disabled=true;
  const res=await fetch(`/admin/delete/${id}`,{method:'POST'});
  if(res.ok){btn.closest('.sub-card').style.opacity='0';setTimeout(()=>btn.closest('.sub-card').remove(),300);toast('已删除');}
  else{btn.disabled=false;toast('删除失败','err');}
}

let stepTimer;
function startStepAnim(){
  const steps=[1,2,3,4];
  let i=0;
  stepTimer=setInterval(()=>{
    steps.forEach(s=>document.getElementById('step'+s)?.classList.remove('active'));
    document.getElementById('step'+steps[i])?.classList.add('active');
    i=(i+1)%steps.length;
  },2500);
}

async function startEval(){
  const apiKey=API_KEY_SET?'':(document.getElementById('apiKeyInput')?.value.trim()||'');
  if(!API_KEY_SET&&!apiKey){toast('❌ 请输入 API Key','err');return;}
  document.getElementById('overlay').classList.add('show');
  document.getElementById('evalBtn').disabled=true;
  startStepAnim();
  try{
    const res=await fetch('/admin/evaluate',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({api_key:apiKey,month:'{{ current_month }}'})
    });
    const data=await res.json();
    if(!res.ok)throw new Error(data.error||'评审失败');
    toast('🎉 评审完成！');
    setTimeout(()=>location.reload(),800);
  }catch(err){
    toast(`❌ ${err.message}`,'err');
    document.getElementById('evalBtn').disabled=false;
  }finally{
    clearInterval(stepTimer);
    document.getElementById('overlay').classList.remove('show');
  }
}

requestAnimationFrame(()=>requestAnimationFrame(()=>{
  document.querySelectorAll('.bar-fill').forEach(b=>{b.style.width=b.dataset.w;});
}));
</script>
"""

# ── Submit link page ───────────────────────────────────────────────────────────
LINK_HTML = BASE_STYLE + """
<style>body{display:flex;align-items:center;justify-content:center;min-height:100vh;}</style>
<div style="max-width:480px;padding:20px;text-align:center">
  <div style="font-size:52px;margin-bottom:16px">🔗</div>
  <h2 style="font-size:20px;margin-bottom:8px">员工提交链接</h2>
  <p style="color:var(--t2);font-size:13px;margin-bottom:20px">将以下链接发送给员工，他们可以直接提交AI创新作品：</p>
  <div id="linkBox" style="background:#f8fafc;border:1.5px solid var(--bd);border-radius:10px;
       padding:14px 18px;font-family:monospace;font-size:14px;word-break:break-all;
       margin-bottom:16px;color:var(--p)"></div>
  <button class="btn btn-p" onclick="copyLink()">📋 复制链接</button>
  <a href="/admin/dashboard" class="btn" style="margin-left:8px;background:#f1f5f9;color:var(--t1)">← 返回后台</a>
</div>
<script>
  const url=window.location.origin+'/';
  document.getElementById('linkBox').textContent=url;
  function copyLink(){
    navigator.clipboard.writeText(url).then(()=>{
      document.querySelector('.btn-p').textContent='✅ 已复制！';
      setTimeout(()=>document.querySelector('.btn-p').textContent='📋 复制链接',2000);
    });
  }
</script>
"""

# ── Files page ─────────────────────────────────────────────────────────────────
FILES_HTML = BASE_STYLE + """
<style>body{padding:32px 20px;max-width:640px;margin:0 auto;}</style>
<a href="/admin/dashboard" class="btn" style="background:#f1f5f9;color:var(--t1);margin-bottom:20px;display:inline-flex">← 返回后台</a>
<h2 style="font-size:18px;font-weight:700;margin-bottom:16px">{{ name }} 的附件</h2>
{% for f in files %}
<div style="display:flex;align-items:center;gap:12px;background:#fff;border:1.5px solid var(--bd);
     border-radius:10px;padding:12px 16px;margin-bottom:8px;">
  <span style="font-size:22px">📄</span>
  <div style="flex:1;min-width:0">
    <div style="font-size:14px;font-weight:600;word-break:break-all">{{ f.file_name }}</div>
  </div>
  <a href="/admin/download/{{ f.id }}" class="btn btn-p" style="font-size:13px;padding:7px 14px;text-decoration:none;flex-shrink:0">
    ⬇ 下载
  </a>
</div>
{% endfor %}
{% if not files %}<p style="color:var(--t3)">暂无文件</p>{% endif %}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/', methods=['GET'])
def submit_form():
    month = datetime.now().strftime('%Y年%m月')
    return render_template_string(SUBMIT_HTML, month=month, success=False, error=None)

@app.route('/', methods=['POST'])
def submit_post():
    month = datetime.now().strftime('%Y年%m月')
    try:
        name       = request.form.get('name','').strip()
        department = request.form.get('department','').strip()
        note       = request.form.get('note','').strip()

        if not name or not note:
            return render_template_string(SUBMIT_HTML, month=month, success=False,
                                          error='请填写姓名和简要说明（必填项）')

        # Parse links
        raw_links = request.form.getlist('links')
        links = [u.strip() for u in raw_links if u.strip()]
        links_json = json.dumps(links, ensure_ascii=False)

        # Four dimension self-assessments
        dim_innovation = request.form.get('dim_innovation','').strip()
        dim_growth     = request.form.get('dim_growth','').strip()
        dim_learning   = request.form.get('dim_learning','').strip()
        dim_impact     = request.form.get('dim_impact','').strip()
        is_deployed    = request.form.get('is_deployed','').strip()
        email          = request.form.get('email','').strip()

        db = get_db()
        cur = db.execute(
            "INSERT INTO submissions (name,department,note,links,dim_innovation,dim_growth,dim_learning,dim_impact,is_deployed,email,month) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, department, note, links_json, dim_innovation, dim_growth, dim_learning, dim_impact, is_deployed, email, month)
        )
        sub_id = cur.lastrowid

        # Save files to disk (streaming — no memory blowup)
        import uuid
        file_count = 0
        uploaded_files = request.files.getlist('files')
        for f in uploaded_files:
            if not f or not f.filename:
                continue
            fname = os.path.basename(f.filename.replace('\\', '/'))  # strip folder path
            if not fname:
                continue
            ext   = fname.rsplit('.',1)[-1].lower() if '.' in fname else ''
            if ext not in ALLOWED_EXTS:
                continue
            safe_name = f"{uuid.uuid4().hex}_{fname}"
            fpath = os.path.join(UPLOAD_DIR, safe_name)
            f.save(fpath)   # streams directly to disk
            db.execute(
                "INSERT INTO submission_files (submission_id,file_name,file_ext,file_path) VALUES (?,?,?,?)",
                (sub_id, fname, ext, fpath)
            )
            file_count += 1

        # Save screenshots (image files tagged as 'screenshot')
        import uuid as _uuid
        for f in request.files.getlist('screenshots'):
            if not f or not f.filename:
                continue
            fname = os.path.basename(f.filename.replace('\\', '/'))
            if not fname:
                continue
            ext = fname.rsplit('.',1)[-1].lower() if '.' in fname else ''
            if ext not in {'jpg','jpeg','png','gif','webp','bmp'}:
                continue
            safe_name = f"screenshot_{_uuid.uuid4().hex}_{fname}"
            fpath = os.path.join(UPLOAD_DIR, safe_name)
            f.save(fpath)
            db.execute(
                "INSERT INTO submission_files (submission_id,file_name,file_ext,file_path) VALUES (?,?,?,?)",
                (sub_id, f'[截图] {fname}', ext, fpath)
            )
            file_count += 1

        db.commit()

        return render_template_string(
            SUBMIT_HTML, month=month, success=True,
            submitted_name=name, file_count=file_count, link_count=len(links), error=None
        )

    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        print(f"[ERROR] submit_post: {err_detail}")
        return render_template_string(
            SUBMIT_HTML, month=month, success=False,
            error=f'提交失败，请稍后重试。错误信息：{str(e)}'
        ), 500


@app.route('/admin', methods=['GET'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/admin', methods=['POST'])
def admin_login_post():
    if request.form.get('password','') == ADMIN_PASSWORD:
        session['admin_logged_in'] = True
        session.permanent = True
        return redirect(url_for('admin_dashboard'))
    return render_template_string(LOGIN_HTML, error='密码错误，请重试')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    db = get_db()
    current_month = request.args.get('month', datetime.now().strftime('%Y年%m月'))

    months_rows = db.execute("SELECT DISTINCT month FROM submissions ORDER BY month DESC").fetchall()
    months = [r['month'] for r in months_rows]
    if current_month not in months:
        months.insert(0, current_month)

    month_counts = {}
    for m in months:
        c = db.execute("SELECT COUNT(*) FROM submissions WHERE month=?", (m,)).fetchone()[0]
        month_counts[m] = c

    subs_raw = db.execute(
        "SELECT id,name,department,note,links,email,is_deployed,month,created_at FROM submissions WHERE month=? ORDER BY created_at DESC",
        (current_month,)
    ).fetchall()

    filtered_subs = []
    for s in subs_raw:
        row = dict(s)
        links = json.loads(row.get('links') or '[]')
        fc = db.execute("SELECT COUNT(*) FROM submission_files WHERE submission_id=?", (row['id'],)).fetchone()[0]
        row['file_count'] = fc
        row['link_count'] = len(links)
        filtered_subs.append(row)

    total_count = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    eval_count  = db.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]

    latest_eval = None
    eval_row = db.execute(
        "SELECT * FROM evaluations WHERE month=? ORDER BY id DESC LIMIT 1", (current_month,)
    ).fetchone()
    if eval_row:
        latest_eval = {'created_at': eval_row['created_at'], 'results': json.loads(eval_row['results_json'])}

    return render_template_string(
        DASHBOARD_HTML,
        current_month=current_month, months=months, month_counts=month_counts,
        month_count=month_counts.get(current_month,0),
        total_count=total_count, eval_count=eval_count,
        filtered_subs=filtered_subs, latest_eval=latest_eval,
        api_key_set=bool(ANTHROPIC_API_KEY),
    )


@app.route('/admin/files/<int:sub_id>')
@login_required
def admin_files(sub_id):
    db = get_db()
    sub = db.execute("SELECT name FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not sub: abort(404)
    files = db.execute(
        "SELECT id,file_name,file_ext FROM submission_files WHERE submission_id=?", (sub_id,)
    ).fetchall()
    return render_template_string(FILES_HTML, name=sub['name'], files=[dict(f) for f in files])


@app.route('/admin/download/<int:file_id>')
@login_required
def admin_download(file_id):
    db = get_db()
    row = db.execute("SELECT file_name,file_path FROM submission_files WHERE id=?", (file_id,)).fetchone()
    if not row or not os.path.exists(row['file_path']): abort(404)
    return send_file(row['file_path'], download_name=row['file_name'], as_attachment=True)


@app.route('/admin/delete/<int:sub_id>', methods=['POST'])
@login_required
def admin_delete(sub_id):
    db = get_db()
    # Delete files from disk
    frows = db.execute("SELECT file_path FROM submission_files WHERE submission_id=?", (sub_id,)).fetchall()
    for frow in frows:
        try:
            if frow['file_path'] and os.path.exists(frow['file_path']):
                os.remove(frow['file_path'])
        except Exception:
            pass
    db.execute("DELETE FROM submission_files WHERE submission_id=?", (sub_id,))
    db.execute("DELETE FROM submissions WHERE id=?", (sub_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/admin/evaluate', methods=['POST'])
@login_required
def admin_evaluate():
    data  = request.get_json()
    month = data.get('month', datetime.now().strftime('%Y年%m月'))
    api_key = ANTHROPIC_API_KEY or data.get('api_key','').strip()
    if not api_key:
        return jsonify({'error': '请提供 Anthropic API Key'}), 400

    db   = get_db()
    rows = db.execute(
        "SELECT * FROM submissions WHERE month=? ORDER BY created_at ASC", (month,)
    ).fetchall()

    if len(rows) < 1:
        return jsonify({'error': f'{month} 暂无参赛作品'}), 400

    client = anthropic.Anthropic(api_key=api_key)

    subs_with_content = []
    # Build email lookup for results display
    email_map = {dict(row)['name']: dict(row).get('email','') for row in rows}

    for row in rows:
        s = dict(row)
        files = db.execute(
            "SELECT * FROM submission_files WHERE submission_id=?", (s['id'],)
        ).fetchall()
        files = [dict(f) for f in files]
        s['file_count'] = len(files)
        try:
            s['content'] = process_submission(s, files, client)
        except Exception as e:
            s['content'] = f'[处理出错: {e}]'
        subs_with_content.append(s)

    prompt = build_eval_prompt(subs_with_content)
    try:
        message = client.messages.create(
            model='claude-opus-4-6', max_tokens=8192,
            messages=[{'role':'user','content':prompt}]
        )
        raw = message.content[0].text.strip()
    except anthropic.AuthenticationError:
        return jsonify({'error': 'API Key 无效'}), 400
    except anthropic.RateLimitError:
        return jsonify({'error': 'API 请求超限，请稍后重试'}), 429
    except Exception as e:
        return jsonify({'error': f'Claude API 错误: {e}'}), 500

    m = re.search(r'\[[\s\S]*\]', raw)
    if not m:
        return jsonify({'error': 'Claude 返回格式异常，请重试'}), 500
    try:
        results = json.loads(m.group(0))
    except Exception:
        return jsonify({'error': '解析结果失败，请重试'}), 500

    # Build a lookup: name -> is_deployed status
    deployed_map = {dict(row)['name']: dict(row).get('is_deployed','') for row in rows}

    for r in results:
        r['total'] = (r.get('innovation',0)+r.get('practicality',0)+
                      r.get('learning_depth',0)+r.get('impact',0))
        r['is_deployed'] = deployed_map.get(r.get('name',''), '')

    # Hard rule: #1 must be fully deployed; demote others to 2nd place at earliest
    FULLY_DEPLOYED = '已完全落地，正在日常使用中'
    results.sort(key=lambda x: x.get('total',0), reverse=True)

    fully = [r for r in results if r.get('is_deployed') == FULLY_DEPLOYED]
    others = [r for r in results if r.get('is_deployed') != FULLY_DEPLOYED]
    # Re-sort each group by score, then concatenate: fully deployed first
    fully.sort(key=lambda x: x.get('total',0), reverse=True)
    others.sort(key=lambda x: x.get('total',0), reverse=True)
    results = fully + others

    # Attach email to each result for display
    for r in results:
        r['email'] = email_map.get(r.get('name',''), '')

    db.execute("INSERT INTO evaluations (month,results_json) VALUES (?,?)",
               (month, json.dumps(results, ensure_ascii=False)))
    db.commit()
    return jsonify({'ok': True, 'results': results})


@app.route('/submit-link')
@login_required
def submit_link():
    return render_template_string(LINK_HTML)


@app.route('/health')
def health():
    return jsonify({'status':'ok','version':'4.0-multi-file-url'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'\n{"="*58}')
    print('  🏆  AI创新评审系统 — 在线版 v4（多文件 + 链接）')
    print('  🚜  澳大利亚叉车租赁与销售公司')
    print(f'  🌐  http://localhost:{port}/')
    print(f'  🔐  http://localhost:{port}/admin  密码: {ADMIN_PASSWORD}')
    print(f'  🤖  API Key: {"✅ 已配置" if ANTHROPIC_API_KEY else "❌ 未设置"}')
    print(f'{"="*58}\n')
    app.run(host='0.0.0.0', port=port, debug=False)
