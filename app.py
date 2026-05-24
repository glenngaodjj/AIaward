#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI创新应用月度评审系统 — 在线版 v3（支持文件上传）
====================================================
员工通过公开链接提交作品（姓名 + 简要说明 + 一个附件），
管理员登录后一键AI评审。

本地测试:
  pip install flask anthropic gunicorn pdfplumber python-docx beautifulsoup4 Pillow
  ADMIN_PASSWORD=yourpassword ANTHROPIC_API_KEY=sk-ant-... python app.py

云端部署 (Railway):
  设置环境变量 ADMIN_PASSWORD、ANTHROPIC_API_KEY、SECRET_KEY 后直接部署
"""

import os, re, json, sqlite3, secrets, base64, io, subprocess, tempfile
from datetime import datetime
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, g, send_file, abort
)
import anthropic

# ── App Config ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024   # 150 MB upload limit

ADMIN_PASSWORD    = os.environ.get('ADMIN_PASSWORD', 'admin123')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH           = os.environ.get('DB_PATH', 'submissions.db')

ALLOWED_EXTS = {
    'pdf', 'doc', 'docx', 'html', 'htm',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp',
    'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v',
}

def file_cat(ext):
    ext = ext.lower()
    if ext == 'pdf':                         return 'pdf'
    if ext in ('doc','docx'):                return 'word'
    if ext in ('html','htm'):                return 'html'
    if ext in ('jpg','jpeg','png','gif','webp','bmp'): return 'image'
    if ext in ('mp4','mov','avi','mkv','webm','m4v'):  return 'video'
    return 'unknown'


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
        db.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                department  TEXT,
                note        TEXT,
                file_name   TEXT,
                file_ext    TEXT,
                file_data   BLOB,
                month       TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                month        TEXT NOT NULL,
                results_json TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
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


# ── File processing ────────────────────────────────────────────────────────────
def extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages[:20]
            texts = [p.extract_text() or '' for p in pages]
        return '\n'.join(texts)[:6000]
    except Exception as e:
        return f'[PDF解析失败: {e}]'

def extract_docx(data: bytes) -> str:
    try:
        from docx import Document
        import io
        doc = Document(io.BytesIO(data))
        return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())[:6000]
    except Exception as e:
        return f'[Word解析失败: {e}]'

def extract_html(data: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(data, 'html.parser')
        for tag in soup(['script','style','head']):
            tag.decompose()
        return soup.get_text(separator='\n', strip=True)[:6000]
    except Exception as e:
        return f'[HTML解析失败: {e}]'

def image_to_b64(data: bytes, ext: str) -> tuple[str, str]:
    """Returns (base64_str, media_type)"""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        if img.mode not in ('RGB','RGBA'):
            img = img.convert('RGB')
        # Resize if too large
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
        b64 = base64.standard_b64encode(data).decode()
        mt = f'image/{ext.lower()}'
        return b64, mt

def extract_video_frames(data: bytes, ext: str) -> list[tuple[str,str]]:
    """Extract up to 4 frames from video, return list of (b64, media_type)"""
    frames = []
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as vf:
            vf.write(data)
            vpath = vf.name
        out_dir = tempfile.mkdtemp()
        # Extract 4 frames evenly spread
        cmd = [
            'ffmpeg', '-i', vpath,
            '-vf', 'select=\'not(mod(n\\,floor(nb_frames/4+1)))\'',
            '-vsync', 'vfr',
            '-frames:v', '4',
            '-q:v', '3',
            os.path.join(out_dir, 'frame%02d.jpg'),
            '-y', '-loglevel', 'error'
        ]
        subprocess.run(cmd, timeout=30, check=True)
        for fname in sorted(os.listdir(out_dir)):
            if fname.endswith('.jpg'):
                with open(os.path.join(out_dir, fname), 'rb') as f:
                    raw = f.read()
                b64 = base64.standard_b64encode(raw).decode()
                frames.append((b64, 'image/jpeg'))
        os.unlink(vpath)
    except Exception:
        pass
    return frames


def describe_with_vision(client, frames: list[tuple[str,str]], context_note: str) -> str:
    """Ask Claude to describe the visual content"""
    content = [{"type":"text","text":
        f"以下是员工AI作品的截图/画面。员工简要说明：「{context_note}」\n"
        "请详细描述画面中展示的AI应用内容、功能、界面、操作流程等，"
        "用于后续的AI评审打分。请用中文回答，200字以内。"}]
    for b64, mt in frames[:4]:
        content.append({"type":"image","source":{"type":"base64","media_type":mt,"data":b64}})
    try:
        msg = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=512,
            messages=[{"role":"user","content":content}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f'[视觉分析失败: {e}]'


def process_submission_file(sub: dict, client) -> str:
    """Process file from a submission dict, return descriptive text for prompt"""
    file_data = sub.get('file_data')
    file_ext  = (sub.get('file_ext') or '').lower()
    file_name = sub.get('file_name') or ''
    note      = sub.get('note') or ''

    if not file_data or not file_ext:
        return '（未上传文件）'

    cat = file_cat(file_ext)
    result = f'【附件文件名】{file_name}\n'

    if cat == 'pdf':
        text = extract_pdf(file_data)
        result += f'【PDF文件内容】\n{text}'

    elif cat == 'word':
        text = extract_docx(file_data)
        result += f'【Word文档内容】\n{text}'

    elif cat == 'html':
        text = extract_html(file_data)
        result += f'【HTML页面文字内容】\n{text}'

    elif cat == 'image':
        b64, mt = image_to_b64(file_data, file_ext)
        desc = describe_with_vision(client, [(b64, mt)], note)
        result += f'【图片内容描述（AI视觉分析）】\n{desc}'

    elif cat == 'video':
        frames = extract_video_frames(file_data, file_ext)
        if frames:
            desc = describe_with_vision(client, frames, note)
            result += f'【视频关键帧内容描述（AI视觉分析）】\n{desc}'
        else:
            result += '【视频文件（无法提取帧，可能环境缺少ffmpeg）】'
    else:
        result += '【不支持的文件类型】'

    return result


# ── AI Evaluation ──────────────────────────────────────────────────────────────
COMPANY_CONTEXT = """
【公司背景】
我们是一家总部位于澳大利亚的叉车租赁与销售公司，主营业务：
- 叉车短期/长期租赁（仓储、物流、零售、建筑等行业客户）
- 新旧叉车销售与以旧换新
- 叉车维护保养与维修服务
- 操作员培训与安全合规
- 零配件供应

员工日常工作涵盖：客户询价与报价、设备调度与合同管理、维修工单处理、
库存盘点、安全巡检、驾照证书管理、财务开票、客服沟通等。
"""

def build_eval_prompt(subs_with_content: list[dict]) -> str:
    block = ''
    for i, s in enumerate(subs_with_content, 1):
        block += f"""
{'='*55}
【参赛作品 {i}】
员工姓名：{s['name']}
部门：{s['department'] or '未填写'}
简要说明：{s['note'] or '（未填写）'}
提交时间：{s['created_at']}

{s.get('file_content','（无附件）')}
"""
    return f"""你是专业公正的AI创新评审专家，正在评选本月"最佳AI创新应用奖"。

{COMPANY_CONTEXT}

【评审标准（每项满分25分，合计100分）】
1. 创新性（25分）：想法是否新颖，是否创造性运用了AI技术或工具
2. 实用性（25分）：能否真实解决叉车业务痛点，实际可行性高
3. 学习深度（25分）：对AI工具/技术的理解掌握程度，学习的系统性
4. 影响力（25分）：惠及人数、业务范围、推广价值与潜在效益

【要求】
- 结合叉车业务背景评分，重点看对业务的实际帮助
- 评分有区分度，不要集中在同一分数
- 每人写60-100字综合点评
- 总分 = 四项之和，按总分降序排列
- 严格输出纯JSON，无其他文字

参赛作品：
{block}

输出格式（JSON数组，降序）：
[
  {{
    "name": "姓名",
    "innovation": 分数,
    "practicality": 分数,
    "learning_depth": 分数,
    "impact": 分数,
    "total": 合计,
    "comment": "点评"
  }}
]"""


# ══════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

BASE_STYLE = """
<style>
  :root {
    --p:#6366f1; --pd:#4f46e5; --pl:#ede9fe;
    --g:#10b981; --r:#ef4444; --y:#f59e0b;
    --bg:#f1f5f9; --card:#fff;
    --t1:#1e293b; --t2:#475569; --t3:#94a3b8;
    --bd:#e2e8f0;
    --sh:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.05);
    --shm:0 4px 8px rgba(0,0,0,.07),0 2px 4px rgba(0,0,0,.04);
    --shl:0 12px 24px rgba(0,0,0,.08),0 4px 8px rgba(0,0,0,.04);
  }
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',
       'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--t1);min-height:100vh;}
  input,textarea,select{width:100%;padding:10px 13px;border:1.5px solid var(--bd);
    border-radius:9px;font-size:14px;font-family:inherit;color:var(--t1);background:#fff;
    transition:border-color .15s,box-shadow .15s;outline:none;}
  input:focus,textarea:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(99,102,241,.1);}
  textarea{resize:vertical;line-height:1.6;}
  .btn{padding:11px 20px;border:none;border-radius:9px;font-size:14px;font-weight:600;
       cursor:pointer;transition:all .18s;display:inline-flex;align-items:center;
       justify-content:center;gap:6px;font-family:inherit;text-decoration:none;}
  .btn-p{background:var(--p);color:#fff;}
  .btn-p:hover{background:var(--pd);transform:translateY(-1px);box-shadow:var(--shm);}
  .btn-d{background:#fee2e2;color:var(--r);}
  .btn-d:hover{background:var(--r);color:#fff;}
  .btn-g{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;
         box-shadow:0 4px 14px rgba(99,102,241,.3);}
  .btn-g:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(99,102,241,.38);}
  .btn:disabled{opacity:.45;cursor:not-allowed;transform:none!important;box-shadow:none!important;}
  .btn-w{width:100%;}
  .card{background:var(--card);border-radius:16px;box-shadow:var(--shm);overflow:hidden;}
  .fg{margin-bottom:16px;}
  .lbl{display:block;font-size:12px;font-weight:600;color:var(--t2);
       text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px;}
  .req{color:var(--r);margin-left:2px;}
  .hint{font-size:12px;color:var(--t3);margin-top:5px;line-height:1.5;}
  .toast{position:fixed;bottom:24px;right:24px;padding:12px 18px;border-radius:10px;
         color:#fff;font-size:13px;font-weight:500;transform:translateY(70px);opacity:0;
         transition:all .28s;z-index:2000;box-shadow:var(--shl);max-width:320px;}
  .toast.show{transform:translateY(0);opacity:1;}
  .toast.ok{background:var(--g);} .toast.err{background:var(--r);}
</style>
"""

# ── Employee Submission Form ───────────────────────────────────────────────────
SUBMIT_HTML = BASE_STYLE + r"""
<style>
  .hero{background:linear-gradient(135deg,#4338ca 0%,#7c3aed 55%,#a855f7 100%);
        padding:40px 24px;text-align:center;color:#fff;}
  .hero .badge{display:inline-block;background:rgba(255,255,255,.2);
               border-radius:999px;padding:5px 16px;font-size:13px;
               font-weight:600;margin-bottom:14px;backdrop-filter:blur(8px);}
  .hero h1{font-size:26px;font-weight:800;letter-spacing:-.3px;margin-bottom:8px;}
  .hero p{font-size:14px;opacity:.82;max-width:480px;margin:0 auto;line-height:1.7;}
  .wrap{max-width:620px;margin:0 auto;padding:28px 20px 60px;}
  .section-title{font-size:13px;font-weight:700;color:var(--p);
                 text-transform:uppercase;letter-spacing:.8px;
                 margin:24px 0 14px;padding-bottom:8px;
                 border-bottom:2px solid var(--pl);}

  /* Drag-drop zone */
  .drop-zone{border:2px dashed var(--bd);border-radius:12px;
             background:#fafbff;padding:32px 20px;text-align:center;
             cursor:pointer;transition:all .2s;position:relative;}
  .drop-zone:hover,.drop-zone.over{border-color:var(--p);background:var(--pl);}
  .drop-zone .dz-icon{font-size:40px;margin-bottom:10px;}
  .drop-zone .dz-title{font-size:15px;font-weight:600;color:var(--t1);margin-bottom:4px;}
  .drop-zone .dz-sub{font-size:12px;color:var(--t3);line-height:1.6;}
  .drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;}
  .file-preview{display:none;align-items:center;gap:12px;
                background:#f0f9ff;border:1.5px solid #bae6fd;border-radius:10px;
                padding:12px 16px;margin-top:10px;}
  .file-preview .fp-icon{font-size:24px;}
  .file-preview .fp-name{font-size:13px;font-weight:600;color:var(--t1);
                          word-break:break-all;}
  .file-preview .fp-size{font-size:11px;color:var(--t3);margin-top:2px;}
  .file-preview .fp-rm{margin-left:auto;cursor:pointer;color:var(--r);
                        font-size:20px;flex-shrink:0;background:none;border:none;}

  .char-count{text-align:right;font-size:11px;color:var(--t3);margin-top:4px;}
  .submit-btn{padding:14px;font-size:16px;border-radius:12px;margin-top:8px;}
  .footer{text-align:center;font-size:12px;color:var(--t3);margin-top:32px;}
  .info-box{background:linear-gradient(135deg,#f0fdf4,#ecfdf5);border:1px solid #86efac;
            border-radius:12px;padding:14px 18px;margin-bottom:20px;
            font-size:13px;color:#166534;line-height:1.7;}
</style>

<div class="hero">
  <div class="badge">🏆 {{ month }} · 月度AI创新大奖</div>
  <h1>提交你的AI创新作品</h1>
  <p>上传你的AI作品文件，加上一句简要说明，即可参与评选 🚀</p>
</div>

<div class="wrap">
  {% if success %}
  <div style="text-align:center;padding:48px 20px">
    <div style="font-size:64px;margin-bottom:16px">🎉</div>
    <h2 style="font-size:22px;margin-bottom:10px">提交成功！</h2>
    <p style="color:var(--t2);font-size:14px;line-height:1.8;max-width:360px;margin:0 auto">
      感谢 <strong>{{ submitted_name }}</strong> 的参与！<br>
      {% if submitted_file %}已收到文件：{{ submitted_file }}<br>{% endif %}
      评审结果将由管理员公布。加油！💪
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
    📋 评审从四个维度打分（各25分）：
    💡 <strong>创新性</strong> · ⚙️ <strong>实用性</strong> ·
    📚 <strong>学习深度</strong> · 🌟 <strong>影响力</strong>
  </div>

  <form method="POST" action="/" enctype="multipart/form-data" id="subForm">

    <div class="section-title">👤 基本信息</div>
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

    <div class="section-title">💬 简要说明</div>
    <div class="fg">
      <textarea name="note" rows="3" maxlength="500"
        placeholder="用1-3句话描述你的AI作品：用了什么AI工具、解决了什么问题、有什么效果？"
        required oninput="updateCount(this,'note-count')"></textarea>
      <div class="char-count"><span id="note-count">0</span> / 500</div>
    </div>

    <div class="section-title">📎 上传作品文件</div>
    <div class="fg">
      <div class="drop-zone" id="dropZone">
        <input type="file" name="file" id="fileInput" accept=".pdf,.doc,.docx,.html,.htm,.jpg,.jpeg,.png,.gif,.webp,.bmp,.mp4,.mov,.avi,.mkv,.webm,.m4v">
        <div class="dz-icon" id="dzIcon">📂</div>
        <div class="dz-title" id="dzTitle">点击选择文件，或拖放到此处</div>
        <div class="dz-sub" id="dzSub">
          支持：PDF · Word · HTML · 图片（JPG/PNG等） · 视频（MP4/MOV等）<br>
          文件大小限制：100 MB
        </div>
      </div>
      <div class="file-preview" id="filePreview">
        <span class="fp-icon" id="fpIcon">📄</span>
        <div>
          <div class="fp-name" id="fpName"></div>
          <div class="fp-size" id="fpSize"></div>
        </div>
        <button type="button" class="fp-rm" onclick="clearFile()" title="移除文件">✕</button>
      </div>
    </div>

    <button type="submit" class="btn btn-g btn-w submit-btn" id="submitBtn">
      🚀 提交作品
    </button>
  </form>

  <div class="footer">提交内容仅用于内部AI创新评审 · 如有问题请联系管理员</div>
  {% endif %}
</div>

<script>
function updateCount(el, id){ document.getElementById(id).textContent = el.value.length; }

const EXT_ICONS = {
  pdf:'📕', doc:'📘', docx:'📘', html:'🌐', htm:'🌐',
  jpg:'🖼️', jpeg:'🖼️', png:'🖼️', gif:'🖼️', webp:'🖼️', bmp:'🖼️',
  mp4:'🎬', mov:'🎬', avi:'🎬', mkv:'🎬', webm:'🎬', m4v:'🎬',
};

function fmtSize(bytes){
  if(bytes<1024) return bytes+'B';
  if(bytes<1048576) return (bytes/1024).toFixed(1)+'KB';
  return (bytes/1048576).toFixed(1)+'MB';
}

const fi = document.getElementById('fileInput');
const dz = document.getElementById('dropZone');
const fp = document.getElementById('filePreview');

fi.addEventListener('change', ()=>{ if(fi.files[0]) showFile(fi.files[0]); });

dz.addEventListener('dragover', e=>{ e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', ()=> dz.classList.remove('over'));
dz.addEventListener('drop', e=>{
  e.preventDefault(); dz.classList.remove('over');
  const f = e.dataTransfer.files[0];
  if(f){ fi.files = e.dataTransfer.files; showFile(f); }
});

function showFile(f){
  const ext = f.name.split('.').pop().toLowerCase();
  document.getElementById('fpIcon').textContent = EXT_ICONS[ext] || '📄';
  document.getElementById('fpName').textContent = f.name;
  document.getElementById('fpSize').textContent = fmtSize(f.size);
  fp.style.display = 'flex';
  document.getElementById('dzTitle').textContent = '已选择文件（点击更换）';
  document.getElementById('dzSub').style.display = 'none';
}

function clearFile(){
  fi.value = '';
  fp.style.display = 'none';
  document.getElementById('dzTitle').textContent = '点击选择文件，或拖放到此处';
  document.getElementById('dzSub').style.display = '';
}

document.getElementById('subForm')?.addEventListener('submit', function(){
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = '⏳ 提交中…';
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
  <div class="card" style="padding:28px">
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
          padding:0;position:sticky;top:0;z-index:100;box-shadow:var(--shm);}
  .topbar-in{max-width:1100px;margin:0 auto;padding:14px 28px;
             display:flex;align-items:center;gap:12px;}
  .topbar h1{font-size:17px;font-weight:700;}
  .topbar p{font-size:12px;opacity:.75;margin-top:1px;}
  .topbar-actions{margin-left:auto;display:flex;gap:8px;align-items:center;}
  .pill{background:rgba(255,255,255,.2);border-radius:999px;padding:4px 12px;
        font-size:12px;font-weight:600;backdrop-filter:blur(8px);}
  .wrap{max-width:1100px;margin:0 auto;padding:28px;}

  .month-tabs{display:flex;gap:6px;margin-bottom:22px;flex-wrap:wrap;}
  .mtab{padding:7px 18px;border-radius:999px;border:1.5px solid var(--bd);
        background:#fff;font-size:13px;font-weight:500;cursor:pointer;
        transition:all .15s;color:var(--t2);}
  .mtab.active{background:var(--p);color:#fff;border-color:var(--p);}
  .mtab:hover:not(.active){border-color:var(--p);color:var(--p);}

  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px;}
  .stat{padding:18px 20px;border-radius:12px;background:#fff;box-shadow:var(--sh);}
  .stat .num{font-size:30px;font-weight:800;color:var(--p);line-height:1;}
  .stat .lbl{font-size:12px;color:var(--t2);margin-top:4px;}

  .sub-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;}
  .sub-card{border:1.5px solid var(--bd);border-radius:12px;padding:16px 18px;
            background:#fff;transition:border-color .15s,box-shadow .15s;}
  .sub-card:hover{border-color:var(--p);box-shadow:0 0 0 3px rgba(99,102,241,.07);}
  .sub-top{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;}
  .av{width:40px;height:40px;border-radius:10px;
      background:linear-gradient(135deg,#6366f1,#a855f7);
      display:flex;align-items:center;justify-content:center;
      color:#fff;font-weight:700;font-size:16px;flex-shrink:0;}
  .sub-name{font-weight:700;font-size:15px;}
  .tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;display:inline-block;margin-top:4px;}
  .tag-dept{background:var(--pl);color:var(--p);}
  .tag-file{background:#f0f9ff;color:#0369a1;}
  .sub-note{font-size:13px;color:var(--t2);line-height:1.6;
            display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
            margin-top:8px;}
  .sub-meta{font-size:11px;color:var(--t3);margin-top:8px;}
  .sub-foot{display:flex;align-items:center;justify-content:flex-end;gap:6px;
            margin-top:12px;padding-top:10px;border-top:1px solid var(--bd);}

  .eval-box{background:#fff;border-radius:16px;padding:24px 28px;
            margin-bottom:24px;box-shadow:var(--sh);border:1.5px solid var(--bd);}
  .eval-box h3{font-size:16px;font-weight:700;margin-bottom:6px;}
  .eval-box p{font-size:13px;color:var(--t2);margin-bottom:16px;line-height:1.6;}
  .eval-row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;}
  .api-wrap{flex:1;min-width:240px;}
  .api-wrap label{font-size:11px;font-weight:600;color:var(--t2);
                  text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:6px;}

  .results-wrap{margin-top:28px;}
  .res-card{border:1.5px solid var(--bd);border-radius:14px;overflow:hidden;
            margin-bottom:14px;transition:transform .2s,box-shadow .2s;}
  .res-card:hover{transform:translateY(-2px);box-shadow:var(--shl);}
  .res-card.r1{border-color:#f59e0b;} .res-card.r2{border-color:#9ca3af;}
  .res-card.r3{border-color:#b45309;}
  .res-hd{padding:13px 18px;background:#fafafa;display:flex;align-items:center;gap:11px;}
  .medal{width:36px;height:36px;border-radius:9px;display:flex;align-items:center;
         justify-content:center;font-size:20px;flex-shrink:0;}
  .medal.r1{background:#fef3c7;} .medal.r2{background:#f3f4f6;}
  .medal.r3{background:#fde68a;} .medal.rn{background:var(--pl);font-size:13px;font-weight:700;color:var(--p);}
  .res-info{flex:1;min-width:0;}
  .res-name{font-weight:700;font-size:15px;}
  .res-proj{font-size:12px;color:var(--t2);margin-top:2px;}
  .tot{text-align:right;flex-shrink:0;}
  .tot-n{font-size:28px;font-weight:800;color:var(--p);line-height:1;}
  .tot-of{font-size:11px;color:var(--t3);}
  .res-bd{padding:14px 18px;}
  .sc-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px;}
  .sc-row label{display:flex;justify-content:space-between;font-size:11px;
                font-weight:600;color:var(--t2);margin-bottom:4px;}
  .sc-row label span{color:var(--t1);}
  .bar-bg{height:6px;background:#e2e8f0;border-radius:999px;overflow:hidden;}
  .bar-fill{height:100%;border-radius:999px;width:0%;
            background:linear-gradient(90deg,#6366f1,#a855f7);
            transition:width 1.2s cubic-bezier(.4,0,.2,1);}
  .comment{background:#f8fafc;border-left:3px solid var(--p);
           border-radius:0 7px 7px 0;padding:9px 13px;
           font-size:12px;color:var(--t2);line-height:1.7;font-style:italic;}

  .overlay{position:fixed;inset:0;background:rgba(15,23,42,.55);backdrop-filter:blur(6px);
           display:none;align-items:center;justify-content:center;z-index:1000;}
  .overlay.show{display:flex;}
  .ov-box{background:#fff;border-radius:18px;padding:36px 44px;text-align:center;
          box-shadow:0 24px 60px rgba(0,0,0,.2);max-width:340px;width:90%;}
  .spinner{width:48px;height:48px;border:4px solid #e2e8f0;border-top-color:var(--p);
           border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 18px;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .ov-box h3{font-size:16px;margin-bottom:7px;}
  .ov-box p{color:var(--t2);font-size:12px;line-height:1.7;}
  .empty{text-align:center;padding:48px;color:var(--t3);}
  .empty .ei{font-size:44px;margin-bottom:12px;}
  .no-api-warn{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;
               padding:12px 16px;font-size:13px;color:#9a3412;margin-bottom:16px;}
</style>

<div class="topbar">
  <div class="topbar-in">
    <div>
      <h1>🏆 AI创新评审后台</h1>
      <p>澳大利亚叉车租赁与销售公司</p>
    </div>
    <div class="topbar-actions">
      <span class="pill">{{ current_month }} 共 {{ total_count }} 份</span>
      <a href="/submit-link" class="btn" style="background:rgba(255,255,255,.2);color:#fff;
         font-size:12px;padding:7px 14px;text-decoration:none">🔗 员工提交链接</a>
      <a href="/admin/logout" class="btn" style="background:rgba(255,255,255,.15);color:#fff;
         font-size:12px;padding:7px 14px;text-decoration:none">退出</a>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="month-tabs" id="monthTabs">
    {% for m in months %}
    <button class="mtab {% if m == current_month %}active{% endif %}"
            onclick="window.location.href='/admin/dashboard?month='+encodeURIComponent('{{ m }}')">
      {{ m }}（{{ month_counts[m] }}份）
    </button>
    {% endfor %}
  </div>

  <div class="stats">
    <div class="stat">
      <div class="num">{{ month_count }}</div>
      <div class="lbl">{{ current_month }} 参赛作品</div>
    </div>
    <div class="stat">
      <div class="num">{{ total_count }}</div>
      <div class="lbl">历史总作品数</div>
    </div>
    <div class="stat">
      <div class="num">{{ eval_count }}</div>
      <div class="lbl">历史评审次数</div>
    </div>
  </div>

  <div class="eval-box">
    <h3>🤖 AI智能评审</h3>
    <p>
      点击下方按钮，Claude 将读取 <strong>{{ current_month }}</strong> 所有作品的文件内容，
      进行综合评审排名（含文档解析、图片/视频视觉分析）。<br>
      含图片/视频时评审时间约 1-2 分钟，请耐心等待。
    </p>
    {% if not api_key_set %}
    <div class="no-api-warn">
      ⚠️ 未检测到 ANTHROPIC_API_KEY，请在下方手动输入
    </div>
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
              {% if month_count < 2 %}disabled title="至少需要2份作品"{% endif %}>
        🚀 开始评审 {{ current_month }}
      </button>
    </div>
  </div>

  {% if latest_eval %}
  <div class="results-wrap">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
      <h2 style="font-size:18px;font-weight:700">
        🏆 最新评审结果
        <span style="font-size:13px;font-weight:400;color:var(--t3)">{{ latest_eval.created_at }}</span>
      </h2>
    </div>
    {% set results = latest_eval.results %}
    {% for r in results %}
    {% set i = loop.index0 %}
    {% set rc = 'r1' if i==0 else ('r2' if i==1 else ('r3' if i==2 else 'rn')) %}
    {% set medal = '🥇' if i==0 else ('🥈' if i==1 else ('🥉' if i==2 else '#'~loop.index)) %}
    <div class="res-card {{ rc }}">
      <div class="res-hd">
        <div class="medal {{ rc }}">{{ medal }}</div>
        <div class="res-info">
          <div class="res-name">{{ r.name }}</div>
          <div class="res-proj" style="font-size:12px;color:var(--t2);margin-top:2px">
            {{ r.get('comment','')[:40] }}…
          </div>
        </div>
        <div class="tot">
          <div class="tot-n">{{ r.total }}</div>
          <div class="tot-of">/ 100 分</div>
        </div>
      </div>
      <div class="res-bd">
        <div class="sc-grid">
          <div class="sc-row">
            <label>💡 创新性 <span>{{ r.innovation }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.innovation/25*100)|int }}%"></div></div>
          </div>
          <div class="sc-row">
            <label>⚙️ 实用性 <span>{{ r.practicality }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.practicality/25*100)|int }}%"></div></div>
          </div>
          <div class="sc-row">
            <label>📚 学习深度 <span>{{ r.learning_depth }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.learning_depth/25*100)|int }}%"></div></div>
          </div>
          <div class="sc-row">
            <label>🌟 影响力 <span>{{ r.impact }}/25</span></label>
            <div class="bar-bg"><div class="bar-fill" data-w="{{ (r.impact/25*100)|int }}%"></div></div>
          </div>
        </div>
        <div class="comment">{{ r.comment }}</div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div style="display:flex;align-items:center;justify-content:space-between;
              margin-bottom:14px;margin-top:28px">
    <h2 style="font-size:17px;font-weight:700">
      {{ current_month }} 参赛作品（{{ month_count }} 份）
    </h2>
  </div>

  <div class="sub-grid">
    {% if filtered_subs %}
      {% for s in filtered_subs %}
      <div class="sub-card">
        <div class="sub-top">
          <div class="av">{{ s.name[0] }}</div>
          <div style="flex:1;min-width:0">
            <div class="sub-name">{{ s.name }}</div>
            {% if s.department %}
            <span class="tag tag-dept">{{ s.department }}</span>
            {% endif %}
            {% if s.file_name %}
            <span class="tag tag-file">📎 {{ s.file_name }}</span>
            {% endif %}
          </div>
        </div>
        {% if s.note %}
        <div class="sub-note">{{ s.note }}</div>
        {% endif %}
        <div class="sub-meta">📅 {{ s.created_at[:16] }}</div>
        <div class="sub-foot">
          {% if s.file_name %}
          <a href="/admin/download/{{ s.id }}" class="btn" style="font-size:12px;padding:5px 10px;background:#f0f9ff;color:#0369a1;text-decoration:none">
            ⬇ 下载文件
          </a>
          {% endif %}
          <button class="btn btn-d" style="padding:5px 12px;font-size:12px"
                  onclick="deleteSub({{ s.id }}, this)">删除</button>
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty" style="grid-column:1/-1">
        <div class="ei">📂</div>
        <p style="font-size:14px">{{ current_month }} 暂无参赛作品<br>
        <a href="/submit-link" style="color:var(--p)">复制员工提交链接</a> 发送给同事吧</p>
      </div>
    {% endif %}
  </div>
</div>

<div class="overlay" id="overlay">
  <div class="ov-box">
    <div class="spinner"></div>
    <h3>Claude 正在评审中…</h3>
    <p>正在读取文件内容并综合分析，<br>含图片/视频时约需 1-2 分钟，<br>请勿关闭此页面</p>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const API_KEY_SET = {{ 'true' if api_key_set else 'false' }};

function toast(msg, type='ok'){
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `toast show ${type}`;
  setTimeout(()=>el.className='toast', 4000);
}

async function deleteSub(id, btn){
  if(!confirm('确定删除这份作品？')) return;
  btn.disabled = true;
  const res = await fetch(`/admin/delete/${id}`, {method:'POST'});
  if(res.ok){
    btn.closest('.sub-card').style.opacity='0';
    setTimeout(()=>btn.closest('.sub-card').remove(), 300);
    toast('作品已删除');
  } else {
    btn.disabled = false; toast('删除失败','err');
  }
}

async function startEval(){
  const apiKey = API_KEY_SET ? '' : (document.getElementById('apiKeyInput')?.value.trim()||'');
  if(!API_KEY_SET && !apiKey){ toast('❌ 请输入 Anthropic API Key','err'); return; }
  document.getElementById('overlay').classList.add('show');
  document.getElementById('evalBtn').disabled = true;
  try {
    const res = await fetch('/admin/evaluate', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({api_key: apiKey, month: '{{ current_month }}'})
    });
    const data = await res.json();
    if(!res.ok) throw new Error(data.error||'评审失败');
    toast('🎉 评审完成！');
    setTimeout(()=>location.reload(), 800);
  } catch(err){
    toast(`❌ ${err.message}`,'err');
    document.getElementById('evalBtn').disabled = false;
  } finally {
    document.getElementById('overlay').classList.remove('show');
  }
}

requestAnimationFrame(()=>requestAnimationFrame(()=>{
  document.querySelectorAll('.bar-fill').forEach(b=>{ b.style.width = b.dataset.w; });
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
       padding:14px 18px;font-family:monospace;font-size:14px;
       word-break:break-all;margin-bottom:16px;color:var(--p)"></div>
  <button class="btn btn-p" onclick="copyLink()">📋 复制链接</button>
  <a href="/admin/dashboard" class="btn" style="margin-left:8px;background:#f1f5f9;color:var(--t1)">
    ← 返回后台
  </a>
</div>
<script>
  const url = window.location.origin + '/';
  document.getElementById('linkBox').textContent = url;
  function copyLink(){
    navigator.clipboard.writeText(url).then(()=>{
      document.querySelector('.btn-p').textContent = '✅ 已复制！';
      setTimeout(()=>document.querySelector('.btn-p').textContent='📋 复制链接', 2000);
    });
  }
</script>
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
    month      = datetime.now().strftime('%Y年%m月')
    name       = request.form.get('name','').strip()
    department = request.form.get('department','').strip()
    note       = request.form.get('note','').strip()

    if not name or not note:
        return render_template_string(
            SUBMIT_HTML, month=month, success=False,
            error='请填写姓名和简要说明（必填项）'
        )

    # Handle file
    file_name = file_ext = None
    file_data = None
    f = request.files.get('file')
    if f and f.filename:
        fname = f.filename
        ext   = fname.rsplit('.',1)[-1].lower() if '.' in fname else ''
        if ext not in ALLOWED_EXTS:
            return render_template_string(
                SUBMIT_HTML, month=month, success=False,
                error=f'不支持的文件类型 .{ext}，请上传 PDF、Word、HTML、图片或视频文件'
            )
        file_name = fname
        file_ext  = ext
        file_data = f.read()

    db = get_db()
    db.execute(
        """INSERT INTO submissions (name,department,note,file_name,file_ext,file_data,month)
           VALUES (?,?,?,?,?,?,?)""",
        (name, department, note, file_name, file_ext, file_data, month)
    )
    db.commit()

    return render_template_string(
        SUBMIT_HTML, month=month, success=True,
        submitted_name=name, submitted_file=file_name, error=None
    )


@app.route('/admin', methods=['GET'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/admin', methods=['POST'])
def admin_login_post():
    pwd = request.form.get('password','')
    if pwd == ADMIN_PASSWORD:
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

    months_rows = db.execute(
        "SELECT DISTINCT month FROM submissions ORDER BY month DESC"
    ).fetchall()
    months = [r['month'] for r in months_rows]
    if current_month not in months:
        months.insert(0, current_month)

    month_counts = {}
    for m in months:
        c = db.execute("SELECT COUNT(*) FROM submissions WHERE month=?", (m,)).fetchone()[0]
        month_counts[m] = c

    subs = db.execute(
        "SELECT id,name,department,note,file_name,file_ext,month,created_at "
        "FROM submissions WHERE month=? ORDER BY created_at DESC",
        (current_month,)
    ).fetchall()

    total_count = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    eval_count  = db.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]

    latest_eval = None
    eval_row = db.execute(
        "SELECT * FROM evaluations WHERE month=? ORDER BY id DESC LIMIT 1",
        (current_month,)
    ).fetchone()
    if eval_row:
        latest_eval = {
            'created_at': eval_row['created_at'],
            'results':    json.loads(eval_row['results_json'])
        }

    return render_template_string(
        DASHBOARD_HTML,
        current_month=current_month,
        months=months,
        month_counts=month_counts,
        month_count=month_counts.get(current_month, 0),
        total_count=total_count,
        eval_count=eval_count,
        filtered_subs=[dict(s) for s in subs],
        latest_eval=latest_eval,
        api_key_set=bool(ANTHROPIC_API_KEY),
    )


@app.route('/admin/download/<int:sub_id>')
@login_required
def admin_download(sub_id):
    db = get_db()
    row = db.execute(
        "SELECT file_name, file_ext, file_data FROM submissions WHERE id=?",
        (sub_id,)
    ).fetchone()
    if not row or not row['file_data']:
        abort(404)
    buf = io.BytesIO(row['file_data'])
    return send_file(buf, download_name=row['file_name'], as_attachment=True)


@app.route('/admin/delete/<int:sub_id>', methods=['POST'])
@login_required
def admin_delete(sub_id):
    db = get_db()
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
        "SELECT * FROM submissions WHERE month=? ORDER BY created_at ASC",
        (month,)
    ).fetchall()

    if len(rows) < 2:
        return jsonify({'error': f'{month} 至少需要 2 份作品才能评审'}), 400

    client = anthropic.Anthropic(api_key=api_key)

    # Process files for each submission
    subs_with_content = []
    for row in rows:
        s = dict(row)
        try:
            file_content = process_submission_file(s, client)
        except Exception as e:
            file_content = f'[文件处理出错: {e}]'
        s['file_content'] = file_content
        subs_with_content.append(s)

    # Build prompt & call Claude
    prompt = build_eval_prompt(subs_with_content)
    try:
        message = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=4096,
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

    for r in results:
        r['total'] = (
            r.get('innovation',0) + r.get('practicality',0) +
            r.get('learning_depth',0) + r.get('impact',0)
        )
    results.sort(key=lambda x: x.get('total',0), reverse=True)

    db.execute(
        "INSERT INTO evaluations (month, results_json) VALUES (?,?)",
        (month, json.dumps(results, ensure_ascii=False))
    )
    db.commit()

    return jsonify({'ok': True, 'results': results})


@app.route('/submit-link')
@login_required
def submit_link():
    return render_template_string(LINK_HTML)


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '3.0-file-upload'})


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print()
    print('=' * 58)
    print('  🏆  AI创新评审系统 — 在线版 v3（支持文件上传）')
    print('  🚜  澳大利亚叉车租赁与销售公司')
    print('=' * 58)
    print(f'  🌐  员工提交页:   http://localhost:{port}/')
    print(f'  🔐  管理员后台:   http://localhost:{port}/admin')
    print(f'  🔑  管理员密码:   {ADMIN_PASSWORD}')
    print(f'  🤖  API Key:     {"✅ 已配置" if ANTHROPIC_API_KEY else "❌ 未设置"}')
    print('=' * 58)
    print()
    app.run(host='0.0.0.0', port=port, debug=False)
