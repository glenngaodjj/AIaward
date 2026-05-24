#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI创新应用月度评审系统 — 在线版
================================
员工通过公开链接提交作品，管理员登录后一键AI评审。

本地测试:
  pip install flask anthropic gunicorn
  ADMIN_PASSWORD=yourpassword ANTHROPIC_API_KEY=sk-ant-... python app.py

云端部署 (Railway / Render):
  设置环境变量 ADMIN_PASSWORD 和 ANTHROPIC_API_KEY 后直接部署
"""

import os
import re
import json
import sqlite3
import secrets
from datetime import datetime
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, g
)
import anthropic

# ── App Config ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

ADMIN_PASSWORD   = os.environ.get('ADMIN_PASSWORD', 'admin123')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH          = os.environ.get('DB_PATH', 'submissions.db')


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
    if db:
        db.close()

def init_db():
    with app.app_context():
        db = sqlite3.connect(DB_PATH)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                department  TEXT,
                title       TEXT NOT NULL,
                description TEXT NOT NULL,
                impact      TEXT,
                learning    TEXT,
                demo_link   TEXT,
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

def build_eval_prompt(subs: list[dict]) -> str:
    block = ''
    for i, s in enumerate(subs, 1):
        block += f"""
{'='*55}
【参赛作品 {i}】
员工姓名：{s['name']}
部门：{s['department'] or '未填写'}
AI项目名称：{s['title']}
详细描述：{s['description']}
实际成果：{s['impact'] or '未填写'}
学习过程：{s['learning'] or '未填写'}
演示链接：{s['demo_link'] or '无'}
提交时间：{s['created_at']}
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
- 评分有区分度，不要集中
- 每人写60-100字综合点评
- 总分 = 四项之和，按总分降序排列
- 严格输出纯JSON，无其他文字

参赛作品：
{block}

输出格式（JSON数组，降序）：
[
  {{
    "name": "姓名",
    "title": "作品名称",
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

# ── Shared styles ──────────────────────────────────────────────────────────────
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
  .wrap{max-width:640px;margin:0 auto;padding:28px 20px 60px;}
  .info-box{background:linear-gradient(135deg,#f0fdf4,#ecfdf5);border:1px solid #86efac;
            border-radius:12px;padding:16px 18px;margin-bottom:24px;
            font-size:13px;color:#166534;line-height:1.7;}
  .info-box strong{display:block;margin-bottom:4px;font-size:14px;}
  .char-count{text-align:right;font-size:11px;color:var(--t3);margin-top:4px;}
  .section-title{font-size:13px;font-weight:700;color:var(--p);
                 text-transform:uppercase;letter-spacing:.8px;
                 margin:28px 0 16px;padding-bottom:8px;
                 border-bottom:2px solid var(--pl);}
  .submit-btn{padding:14px;font-size:16px;border-radius:12px;margin-top:8px;}
  .footer{text-align:center;font-size:12px;color:var(--t3);margin-top:32px;}
</style>

<div class="hero">
  <div class="badge">🏆 {{ month }} · 月度AI创新大奖</div>
  <h1>提交你的AI创新作品</h1>
  <p>分享你在工作中使用AI的创意与成果，有机会赢得本月最佳AI创新应用奖 🚀</p>
</div>

<div class="wrap">
  {% if success %}
  <div style="text-align:center;padding:48px 20px">
    <div style="font-size:64px;margin-bottom:16px">🎉</div>
    <h2 style="font-size:22px;margin-bottom:10px">提交成功！</h2>
    <p style="color:var(--t2);font-size:14px;line-height:1.8;max-width:360px;margin:0 auto">
      感谢 <strong>{{ submitted_name }}</strong> 的参与！<br>
      你的作品「{{ submitted_title }}」已收到，<br>
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
    <strong>📋 评审维度说明</strong>
    本次评审从四个维度打分（各25分），总分100分：
    💡 <strong>创新性</strong> · ⚙️ <strong>实用性</strong> ·
    📚 <strong>学习深度</strong> · 🌟 <strong>影响力</strong>
  </div>

  <form method="POST" action="/" id="subForm">
    <div class="section-title">👤 基本信息</div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div class="fg">
        <label class="lbl">姓名 <span class="req">*</span></label>
        <input type="text" name="name" placeholder="你的姓名" required maxlength="50">
      </div>
      <div class="fg">
        <label class="lbl">部门</label>
        <input type="text" name="department" placeholder="例：销售部、运维部" maxlength="50">
      </div>
    </div>

    <div class="fg">
      <label class="lbl">AI项目 / 作品名称 <span class="req">*</span></label>
      <input type="text" name="title" placeholder="例：智能叉车报价生成工具" required maxlength="100">
    </div>

    <div class="section-title">📝 作品详情</div>

    <div class="fg">
      <label class="lbl">详细描述 <span class="req">*</span></label>
      <textarea name="description" rows="5" required maxlength="2000"
        placeholder="请描述：&#10;• 你使用了哪些AI工具或技术？（如 ChatGPT、Claude、Python脚本、自动化流程等）&#10;• 解决了什么具体的工作问题？&#10;• 具体是怎么做到的？"
        oninput="updateCount(this,'desc-count')"></textarea>
      <div class="char-count"><span id="desc-count">0</span> / 2000</div>
    </div>

    <div class="fg">
      <label class="lbl">实际成果与影响</label>
      <textarea name="impact" rows="3" maxlength="1000"
        placeholder="例：每周节省约X小时、报价效率提升X%、帮助了X位同事、减少了客户等待时间..."
        oninput="updateCount(this,'impact-count')"></textarea>
      <div class="char-count"><span id="impact-count">0</span> / 1000</div>
    </div>

    <div class="fg">
      <label class="lbl">学习与探索过程</label>
      <textarea name="learning" rows="3" maxlength="1000"
        placeholder="你是怎么学习这些AI工具的？遇到了哪些挑战？是如何克服的？"
        oninput="updateCount(this,'learn-count')"></textarea>
      <div class="char-count"><span id="learn-count">0</span> / 1000</div>
    </div>

    <div class="fg">
      <label class="lbl">演示链接（可选）</label>
      <input type="url" name="demo_link"
        placeholder="如有录屏、文档或截图链接，可粘贴于此（Google Drive、YouTube等）">
      <p class="hint">💡 如有视频演示或截图，可上传至 Google Drive 并粘贴共享链接</p>
    </div>

    <button type="submit" class="btn btn-g btn-w submit-btn" id="submitBtn">
      🚀 提交作品
    </button>
  </form>

  <div class="footer">
    提交内容将仅用于内部AI创新评审 · 如有问题请联系管理员
  </div>
  {% endif %}
</div>

<script>
function updateCount(el, countId){
  document.getElementById(countId).textContent = el.value.length;
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

  /* Month tabs */
  .month-tabs{display:flex;gap:6px;margin-bottom:22px;flex-wrap:wrap;}
  .mtab{padding:7px 18px;border-radius:999px;border:1.5px solid var(--bd);
        background:#fff;font-size:13px;font-weight:500;cursor:pointer;
        transition:all .15s;color:var(--t2);}
  .mtab.active{background:var(--p);color:#fff;border-color:var(--p);}
  .mtab:hover:not(.active){border-color:var(--p);color:var(--p);}

  /* Stats */
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px;}
  .stat{padding:18px 20px;border-radius:12px;background:#fff;box-shadow:var(--sh);}
  .stat .num{font-size:30px;font-weight:800;color:var(--p);line-height:1;}
  .stat .lbl{font-size:12px;color:var(--t2);margin-top:4px;}

  /* Submissions grid */
  .sub-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}
  .sub-card{border:1.5px solid var(--bd);border-radius:12px;padding:16px 18px;
            background:#fff;transition:border-color .15s,box-shadow .15s;}
  .sub-card:hover{border-color:var(--p);box-shadow:0 0 0 3px rgba(99,102,241,.07);}
  .sub-top{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;}
  .av{width:40px;height:40px;border-radius:10px;
      background:linear-gradient(135deg,#6366f1,#a855f7);
      display:flex;align-items:center;justify-content:center;
      color:#fff;font-weight:700;font-size:16px;flex-shrink:0;}
  .sub-name{font-weight:700;font-size:15px;}
  .sub-title{font-size:12px;color:var(--t2);margin-top:2px;}
  .tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;}
  .tag-dept{background:var(--pl);color:var(--p);}
  .sub-desc{font-size:13px;color:var(--t2);line-height:1.6;
            display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}
  .sub-meta{font-size:11px;color:var(--t3);margin-top:10px;}
  .sub-foot{display:flex;align-items:center;justify-content:flex-end;gap:6px;
            margin-top:12px;padding-top:10px;border-top:1px solid var(--bd);}
  .link-btn{font-size:12px;color:var(--p);text-decoration:none;
            padding:4px 10px;border-radius:6px;background:var(--pl);}

  /* Evaluate section */
  .eval-box{background:#fff;border-radius:16px;padding:24px 28px;
            margin-bottom:24px;box-shadow:var(--sh);
            border:1.5px solid var(--bd);}
  .eval-box h3{font-size:16px;font-weight:700;margin-bottom:6px;}
  .eval-box p{font-size:13px;color:var(--t2);margin-bottom:16px;line-height:1.6;}
  .eval-row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;}
  .api-wrap{flex:1;min-width:240px;}
  .api-wrap label{font-size:11px;font-weight:600;color:var(--t2);
                  text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:6px;}
  #apiKeyInput{font-family:'Courier New',monospace;font-size:12px;}

  /* Results */
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
  .res-proj{font-size:12px;color:var(--t2);margin-top:2px;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
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

  /* Loading */
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

<!-- Topbar -->
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

  <!-- Month filter -->
  <div class="month-tabs" id="monthTabs">
    {% for m in months %}
    <button class="mtab {% if m == current_month %}active{% endif %}"
            onclick="filterMonth('{{ m }}')">
      {{ m }}（{{ month_counts[m] }}份）
    </button>
    {% endfor %}
  </div>

  <!-- Stats -->
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

  <!-- Evaluate box -->
  <div class="eval-box">
    <h3>🤖 AI智能评审</h3>
    <p>
      点击下方按钮，Claude 将对 <strong>{{ current_month }}</strong> 的
      <strong>{{ month_count }}</strong> 份作品进行综合评审排名。<br>
      评审结合叉车业务背景，从创新性、实用性、学习深度、影响力四个维度打分。
    </p>

    {% if not api_key_set %}
    <div class="no-api-warn">
      ⚠️ 未检测到 ANTHROPIC_API_KEY 环境变量，请在下方手动输入（或在服务器配置中设置）
    </div>
    {% endif %}

    <div class="eval-row">
      {% if not api_key_set %}
      <div class="api-wrap">
        <label>Anthropic API Key</label>
        <input type="password" id="apiKeyInput" placeholder="sk-ant-api03-...">
      </div>
      {% endif %}
      <button class="btn btn-g" onclick="startEval()" id="evalBtn"
              {% if month_count < 2 %}disabled title="至少需要2份作品"{% endif %}>
        🚀 开始评审 {{ current_month }}
      </button>
    </div>
  </div>

  <!-- Latest results -->
  {% if latest_eval %}
  <div class="results-wrap">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
      <h2 style="font-size:18px;font-weight:700">
        🏆 最新评审结果 <span style="font-size:13px;font-weight:400;color:var(--t3)">{{ latest_eval.created_at }}</span>
      </h2>
    </div>
    <div id="resultsDiv">
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
            <div class="res-proj">{{ r.title }}</div>
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
  </div>
  {% endif %}

  <!-- Submissions grid -->
  <div style="display:flex;align-items:center;justify-content:space-between;
              margin-bottom:14px;margin-top:28px">
    <h2 style="font-size:17px;font-weight:700" id="subsTitle">
      {{ current_month }} 参赛作品（{{ month_count }} 份）
    </h2>
  </div>

  <div class="sub-grid" id="subGrid">
    {% if filtered_subs %}
      {% for s in filtered_subs %}
      <div class="sub-card" data-month="{{ s.month }}">
        <div class="sub-top">
          <div class="av">{{ s.name[0] }}</div>
          <div style="flex:1;min-width:0">
            <div class="sub-name">{{ s.name }}</div>
            <div class="sub-title">{{ s.title }}</div>
            {% if s.department %}
            <span class="tag tag-dept" style="margin-top:5px;display:inline-block">
              {{ s.department }}
            </span>
            {% endif %}
          </div>
        </div>
        <div class="sub-desc">{{ s.description }}</div>
        <div class="sub-meta">📅 {{ s.created_at[:16] }}</div>
        <div class="sub-foot">
          {% if s.demo_link %}
          <a href="{{ s.demo_link }}" target="_blank" class="link-btn">🔗 演示链接</a>
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

<!-- Loading overlay -->
<div class="overlay" id="overlay">
  <div class="ov-box">
    <div class="spinner"></div>
    <h3>Claude 正在评审中…</h3>
    <p>正在综合分析每份作品，<br>结合叉车业务背景打分排名，<br>预计需要 30–90 秒</p>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API_KEY_SET = {{ 'true' if api_key_set else 'false' }};
let currentMonth = '{{ current_month }}';

function toast(msg, type='ok'){
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `toast show ${type}`;
  setTimeout(()=>el.className='toast', 4000);
}

function filterMonth(m){
  currentMonth = m;
  document.querySelectorAll('.mtab').forEach(t=>{
    t.classList.toggle('active', t.textContent.startsWith(m));
  });
  // Reload page with month param
  window.location.href = '/admin/dashboard?month=' + encodeURIComponent(m);
}

async function deleteSub(id, btn){
  if(!confirm('确定删除这份作品？')) return;
  btn.disabled = true;
  const res = await fetch(`/admin/delete/${id}`, {method:'POST'});
  if(res.ok){
    btn.closest('.sub-card').style.opacity='0';
    setTimeout(()=>{ btn.closest('.sub-card').remove(); }, 300);
    toast('作品已删除');
  } else {
    btn.disabled = false;
    toast('删除失败', 'err');
  }
}

async function startEval(){
  const apiKey = API_KEY_SET ? '' : (document.getElementById('apiKeyInput')?.value.trim() || '');
  if(!API_KEY_SET && !apiKey){ toast('❌ 请输入 Anthropic API Key', 'err'); return; }

  document.getElementById('overlay').classList.add('show');
  document.getElementById('evalBtn').disabled = true;

  try {
    const res = await fetch('/admin/evaluate', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ api_key: apiKey, month: currentMonth })
    });
    const data = await res.json();
    if(!res.ok) throw new Error(data.error || '评审失败');
    toast('🎉 评审完成！');
    setTimeout(()=>location.reload(), 800);
  } catch(err){
    toast(`❌ ${err.message}`, 'err');
    document.getElementById('evalBtn').disabled = false;
  } finally {
    document.getElementById('overlay').classList.remove('show');
  }
}

// Animate score bars on load
requestAnimationFrame(()=>requestAnimationFrame(()=>{
  document.querySelectorAll('.bar-fill').forEach(b=>{ b.style.width = b.dataset.w; });
}));
</script>
"""

# ── Submit link helper page ────────────────────────────────────────────────────
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

# ── Employee submission form ───────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def submit_form():
    month = datetime.now().strftime('%Y年%m月')
    return render_template_string(
        SUBMIT_HTML,
        month=month, success=False, error=None
    )

@app.route('/', methods=['POST'])
def submit_post():
    month = datetime.now().strftime('%Y年%m月')
    name        = request.form.get('name','').strip()
    department  = request.form.get('department','').strip()
    title       = request.form.get('title','').strip()
    description = request.form.get('description','').strip()
    impact      = request.form.get('impact','').strip()
    learning    = request.form.get('learning','').strip()
    demo_link   = request.form.get('demo_link','').strip()

    if not name or not title or not description:
        return render_template_string(
            SUBMIT_HTML, month=month, success=False,
            error='请填写姓名、项目名称和详细描述（必填项）'
        )

    db = get_db()
    db.execute(
        """INSERT INTO submissions (name,department,title,description,impact,learning,demo_link,month)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name, department, title, description, impact, learning, demo_link, month)
    )
    db.commit()

    return render_template_string(
        SUBMIT_HTML, month=month, success=True,
        submitted_name=name, submitted_title=title, error=None
    )


# ── Admin login ────────────────────────────────────────────────────────────────
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


# ── Admin dashboard ────────────────────────────────────────────────────────────
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    db = get_db()

    current_month = request.args.get(
        'month',
        datetime.now().strftime('%Y年%m月')
    )

    # All unique months (sorted desc)
    months_rows = db.execute(
        "SELECT DISTINCT month FROM submissions ORDER BY month DESC"
    ).fetchall()
    months = [r['month'] for r in months_rows]
    if current_month not in months:
        months.insert(0, current_month)

    month_counts = {}
    for m in months:
        c = db.execute(
            "SELECT COUNT(*) FROM submissions WHERE month=?", (m,)
        ).fetchone()[0]
        month_counts[m] = c

    # Submissions for current month
    subs = db.execute(
        "SELECT * FROM submissions WHERE month=? ORDER BY created_at DESC",
        (current_month,)
    ).fetchall()

    # Total stats
    total_count = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    eval_count  = db.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]

    # Latest eval for current month
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


# ── Delete submission ──────────────────────────────────────────────────────────
@app.route('/admin/delete/<int:sub_id>', methods=['POST'])
@login_required
def admin_delete(sub_id):
    db = get_db()
    db.execute("DELETE FROM submissions WHERE id=?", (sub_id,))
    db.commit()
    return jsonify({'ok': True})


# ── Evaluate ───────────────────────────────────────────────────────────────────
@app.route('/admin/evaluate', methods=['POST'])
@login_required
def admin_evaluate():
    data  = request.get_json()
    month = data.get('month', datetime.now().strftime('%Y年%m月'))

    # API key: env var takes priority
    api_key = ANTHROPIC_API_KEY or (data.get('api_key','').strip())
    if not api_key:
        return jsonify({'error': '请提供 Anthropic API Key'}), 400

    db   = get_db()
    rows = db.execute(
        "SELECT * FROM submissions WHERE month=? ORDER BY created_at ASC",
        (month,)
    ).fetchall()

    if len(rows) < 2:
        return jsonify({'error': f'{month} 至少需要 2 份作品才能评审'}), 400

    subs = [dict(r) for r in rows]

    # Build prompt & call Claude
    prompt = build_eval_prompt(subs)
    try:
        client  = anthropic.Anthropic(api_key=api_key)
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

    # Fix totals & sort
    for r in results:
        r['total'] = (
            r.get('innovation',0) + r.get('practicality',0) +
            r.get('learning_depth',0) + r.get('impact',0)
        )
    results.sort(key=lambda x: x.get('total',0), reverse=True)

    # Save evaluation
    db.execute(
        "INSERT INTO evaluations (month, results_json) VALUES (?,?)",
        (month, json.dumps(results, ensure_ascii=False))
    )
    db.commit()

    return jsonify({'ok': True, 'results': results})


# ── Submit link helper ─────────────────────────────────────────────────────────
@app.route('/submit-link')
@login_required
def submit_link():
    return render_template_string(LINK_HTML)


# ── Health check ───────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '2.0-online'})


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print()
    print('=' * 58)
    print('  🏆  AI创新评审系统 — 在线版')
    print('  🚜  澳大利亚叉车租赁与销售公司')
    print('=' * 58)
    print(f'  🌐  员工提交页:   http://localhost:{port}/')
    print(f'  🔐  管理员后台:   http://localhost:{port}/admin')
    print(f'  🔑  管理员密码:   {ADMIN_PASSWORD}')
    print(f'  🤖  API Key:     {"✅ 已配置" if ANTHROPIC_API_KEY else "❌ 未设置（需在后台手动输入）"}')
    print('=' * 58)
    print()
    app.run(host='0.0.0.0', port=port, debug=False)
