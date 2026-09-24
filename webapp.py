from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from ai_client import OllamaClient
from config import settings
from growth_engine import show_growth_state
from guest_manager import maybe_create_guest
from main import run_cleanup_uploaded, run_generation, run_private_upload_test
from runtime_control import (
    auto_upload_enabled,
    automation_enabled,
    guest_appearance_every,
    guest_image_auto_enabled,
    guest_new_every,
    post_times,
    posts_per_day,
    set_auto_upload_enabled,
    set_automation_enabled,
    set_guest_appearance_every,
    set_guest_image_auto_enabled,
    set_guest_new_every,
    set_post_times,
    set_posts_per_day,
    set_upload_privacy,
    set_web_interval_seconds,
    upload_privacy,
    web_interval_seconds,
)
from scheduler import prepare_upcoming, run_due, tick
from storage import (
    active_guests,
    analytics_history,
    dashboard_videos,
    get_channel_state,
    init_db,
    queued_items,
)
from voice.voicevox import VoicevoxClient
from studio.asset_store import GENERATED_ROOT, list_assets
from studio.image_generator import (
    generate_background_image,
    generate_guest_image,
    generate_mirai_image,
    studio_status,
)

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "webapp.log"

_job_lock = threading.Lock()
_state_lock = threading.Lock()
_last_cycle_at: str | None = None
_last_cycle_result = "未実行"
_current_job = ""
_current_job_started_at: str | None = None
_stop_event = threading.Event()
_wake_event = threading.Event()
_http_server: ThreadingHTTPServer | None = None


def _append_log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n[{stamp}]\n{text.rstrip()}\n")


def _run_captured(label: str, func) -> dict:
    global _current_job, _current_job_started_at

    if not _job_lock.acquire(blocking=False):
        return {"ok": False, "message": "別の処理を実行中です。"}

    with _state_lock:
        _current_job = label
        _current_job_started_at = datetime.now().isoformat(timespec="seconds")

    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            print(f"[WEB] {label} start")
            func()
            print(f"[WEB] {label} end")
        output = buf.getvalue()
        _append_log(output)
        return {"ok": True, "message": f"{label} 完了", "output": output}
    except Exception as exc:
        traceback.print_exc(file=buf)
        output = buf.getvalue()
        _append_log(output)
        return {
            "ok": False,
            "message": f"{label} 失敗: {exc}",
            "output": output,
        }
    finally:
        with _state_lock:
            _current_job = ""
            _current_job_started_at = None
        _job_lock.release()


def _candidate_voicevox_paths() -> list[Path]:
    candidates: list[Path] = []
    if settings.voicevox_exe:
        candidates.append(Path(settings.voicevox_exe))

    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        candidates.extend([
            local / "Programs" / "VOICEVOX" / "VOICEVOX.exe",
            local / "VOICEVOX" / "VOICEVOX.exe",
            program_files / "VOICEVOX" / "VOICEVOX.exe",
        ])

    return [p for p in candidates if str(p) and p.exists()]

def _ensure_local_services() -> None:
    if not OllamaClient().available():
        ollama = shutil.which("ollama")
        if ollama:
            try:
                subprocess.Popen(
                    [ollama, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if os.name == "nt" else 0
                    ),
                )
                time.sleep(2)
                _append_log("[SERVICE] Ollama自動起動を実行")
            except Exception as exc:
                _append_log(f"[SERVICE] Ollama自動起動失敗: {exc}")

    if not VoicevoxClient().available():
        paths = _candidate_voicevox_paths()
        if paths:
            try:
                subprocess.Popen(
                    [str(paths[0])],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if os.name == "nt" else 0
                    ),
                )
                time.sleep(6)
                _append_log(f"[SERVICE] VOICEVOX自動起動: {paths[0]}")
            except Exception as exc:
                _append_log(f"[SERVICE] VOICEVOX自動起動失敗: {exc}")

def _cycle_worker() -> None:
    global _last_cycle_at, _last_cycle_result
    while not _stop_event.is_set():
        _wake_event.clear()
        try:
            if automation_enabled():
                _ensure_local_services()
                result = _run_captured("自動サイクル", tick)
                with _state_lock:
                    _last_cycle_at = datetime.now().isoformat(timespec="seconds")
                    _last_cycle_result = result["message"]
        except Exception as exc:
            _append_log(f"[WEB] background cycle error: {exc}")

        wait_for = web_interval_seconds()
        _wake_event.wait(wait_for)


def _read_log_tail(max_chars: int = 12000) -> str:
    if not LOG_FILE.exists():
        return "まだログはありません。"
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def _service_status() -> dict:
    return {
        "ollama": OllamaClient().available(),
        "voicevox": VoicevoxClient().available(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "youtube_token": Path(settings.youtube_token_file).exists(),
    }


def _queue_status() -> list[dict]:
    rows = queued_items()
    return [
        {
            "video_id": row["video_id"],
            "scheduled_for": row["scheduled_for"],
            "title": row["title"],
            "attempts": row["attempts"],
        }
        for row in rows[:20]
    ]


def _video_status() -> list[dict]:
    rows = dashboard_videos(20)
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "queue_status": row.get("queue_status"),
            "scheduled_for": row.get("scheduled_for"),
            "youtube_video_id": row.get("youtube_video_id"),
            "guest_name": row.get("guest_name"),
            "views": row.get("views") or 0,
            "error": row.get("queue_error"),
            "has_local_file": bool(row.get("output_path")),
        }
        for row in rows
    ]

def _studio_assets() -> list[dict]:
    assets = []
    prefix = "assets/generated/"
    for row in list_assets(limit=24):
        item = dict(row)
        relative = str(item.get("path") or "").replace("\\", "/")
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
            item["url"] = "/studio-assets/" + relative
        else:
            item["url"] = None
        assets.append(item)
    return assets


def _guest_status() -> list[dict]:
    rows = active_guests(20)
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "appearances": row["appearances"],
            "has_image": bool(row.get("image_path")),
        }
        for row in rows
    ]


def _growth_status() -> dict:
    history = analytics_history(8)
    return {
        "strategy": get_channel_state(
            "growth_strategy",
            "まだ十分な学習データがありません。",
        ),
        "recent": [
            {
                "video_id": row["video_id"],
                "checkpoint_hours": row["checkpoint_hours"],
                "score": row["score"],
                "views": row["views"],
                "retention": row["avg_view_percentage"],
                "title": row["title"],
            }
            for row in history
        ],
    }


def _windows_autostart_enabled() -> bool:
    if os.name != "nt":
        return False

    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_READ,
        ) as key:
            winreg.QueryValueEx(key, "AIYouTuberGrowthWeb")
        return True
    except (FileNotFoundError, OSError):
        return False

def _status_payload() -> dict:
    with _state_lock:
        last_cycle_at = _last_cycle_at
        last_cycle_result = _last_cycle_result
        current_job = _current_job
        current_job_started_at = _current_job_started_at

    services = _service_status()

    return {
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "privacy": upload_privacy(),
        "interval_seconds": web_interval_seconds(),
        "posts_per_day": posts_per_day(),
        "post_times": post_times(),
        "guest_every": guest_appearance_every(),
        "guest_new_every": guest_new_every(),
        "guest_image_auto_enabled": guest_image_auto_enabled(),
        "studio": studio_status(),
        "studio_assets": _studio_assets(),
        "services": services,
        "system_ready": all(services.values()),
        "queue": _queue_status(),
        "videos": _video_status(),
        "guests": _guest_status(),
        "growth": _growth_status(),
        "last_cycle_at": last_cycle_at,
        "last_cycle_result": last_cycle_result,
        "current_job": current_job,
        "current_job_started_at": current_job_started_at,
        "autostart_enabled": _windows_autostart_enabled(),
        "log_tail": _read_log_tail(),
    }


def _install_windows_autostart() -> str:
    if os.name != "nt":
        return "Windows以外ではこの自動起動登録は使えません。"

    import winreg

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = ROOT / "web_launcher.pyw"
    command = f'"{pythonw}" "{launcher}"'

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(
            key,
            "AIYouTuberGrowthWeb",
            0,
            winreg.REG_SZ,
            command,
        )

    return "PC起動時のWebアプリ自動起動を登録しました。"


def _update_and_restart() -> None:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Gitが見つかりません")

    status = subprocess.run(
        [git, "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "ローカル変更があるため自動更新を中止しました。"
            "未保存の変更を確認してください。"
        )

    pull = subprocess.run(
        [git, "pull", "--ff-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    print(pull.stdout.strip() or "Already up to date.")

    helper = ROOT / "restart_helper.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen(
        [str(pythonw), str(helper)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" else 0
        ),
    )

    if _http_server is not None:
        threading.Timer(0.8, _http_server.shutdown).start()

def _remove_windows_autostart() -> str:
    if os.name != "nt":
        return "Windows以外ではこの自動起動解除は使えません。"

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, "AIYouTuberGrowthWeb")
        return "PC起動時の自動起動を解除しました。"
    except FileNotFoundError:
        return "自動起動登録はありません。"


HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ミライ AI YouTuber 管理</title>
<style>
:root{font-family:Inter,"Yu Gothic UI",Meiryo,sans-serif;color:#eef5ff;background:#08111f}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07111f,#10223c 55%,#151a36);min-height:100vh}
.wrap{max-width:1280px;margin:auto;padding:20px}.top{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{margin:0;font-size:28px}.sub{color:#a8bad2;font-size:14px}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-top:18px}
.card{grid-column:span 4;background:rgba(14,27,48,.88);border:1px solid #263d5d;border-radius:18px;padding:18px;box-shadow:0 18px 50px rgba(0,0,0,.22)}
.card.wide{grid-column:span 8}.card.full{grid-column:span 12}.card h2{font-size:17px;margin:0 0 14px}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #20344e}.row:last-child{border:0}
.badge{padding:5px 9px;border-radius:999px;font-size:12px;background:#233b59}.ok{background:#124d3c}.ng{background:#5b2630}
button,select,input{border:1px solid #355275;background:#102844;color:#fff;border-radius:10px;padding:10px 12px}
button{cursor:pointer;font-weight:700}button.primary{background:#2274db}button.danger{background:#8b3041}.actions{display:flex;gap:8px;flex-wrap:wrap}
.queue{display:grid;gap:9px}.q{padding:11px;border-radius:12px;background:#0b1b30}.small{font-size:12px;color:#9cb0c9}.strategy{line-height:1.7;background:#0b1b30;padding:13px;border-radius:12px}
pre{white-space:pre-wrap;word-break:break-word;background:#06101c;padding:14px;border-radius:12px;max-height:330px;overflow:auto;color:#bcd0e7}
.toggle{display:flex;align-items:center;gap:8px}.hero{display:flex;gap:12px;align-items:center}.orb{width:52px;height:52px;border-radius:50%;background:radial-gradient(circle at 30% 30%,#c4dcff,#6598ef 45%,#243c7c);box-shadow:0 0 30px #4c82e855}
.studio-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:14px}.asset{background:#0b1b30;border-radius:12px;overflow:hidden;border:1px solid #20344e}.asset img{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;background:#06101c}.asset .meta{padding:9px}.studio-status{margin:8px 0 14px;padding:10px;border-radius:12px;background:#0b1b30}
@media(max-width:900px){.card,.card.wide{grid-column:span 12}.wrap{padding:12px}h1{font-size:22px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="hero"><div class="orb"></div><div><h1>ミライ AI YouTuber 管理</h1><div class="sub">企画 → 制作 → 投稿 → 分析 → 改善を自動運転</div></div></div>
    <div class="actions">
      <button class="primary" onclick="runAction('cycle')">今すぐ1サイクル実行</button>
      <button onclick="refresh()">更新</button>
    </div>
  </div>

  <div class="grid">
    <section class="card">
      <h2>自動運転</h2>
      <div class="row"><span>Web自動運転</span><button id="automationBtn" onclick="toggleAutomation()"></button></div>
      <div class="row"><span>YouTube自動投稿</span><button id="uploadBtn" onclick="toggleUpload()"></button></div>
      <div class="row"><span>公開設定</span>
        <select id="privacy" onchange="savePrivacy()">
          <option value="private">非公開</option>
          <option value="unlisted">限定公開</option>
          <option value="public">公開</option>
        </select>
      </div>
      <div class="row"><span>チェック間隔</span>
        <select id="interval" onchange="saveInterval()">
          <option value="300">5分</option><option value="600">10分</option><option value="900">15分</option><option value="1800">30分</option>
        </select>
      </div>
      <div class="small" id="lastCycle"></div>
    </section>

    <section class="card">
      <h2>システム状態</h2>
      <div id="services"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('services')">AIサービスを起動/再確認</button>
      </div>
    </section>

    <section class="card">
      <h2>運用設定</h2>
      <div class="row"><span>1日投稿数</span><input id="postsPerDay" type="number" min="1" max="10" style="width:92px"></div>
      <div class="row"><span>投稿時刻</span><input id="postTimes" placeholder="09:00,15:00,21:00" style="width:190px"></div>
      <div class="row"><span>ゲスト出演</span><input id="guestEvery" type="number" min="0" max="100" style="width:92px"></div>
      <div class="row"><span>新ゲスト</span><input id="guestNewEvery" type="number" min="0" max="500" style="width:92px"></div>
      <div class="actions" style="margin-top:12px"><button class="primary" onclick="saveOperationSettings()">運用設定を保存</button></div>
    </section>

    <section class="card wide">
      <h2>投稿キュー</h2>
      <div id="queue" class="queue"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('prepare')">次の動画を準備</button>
        <button onclick="runAction('due')">投稿時刻を確認</button>
        <button onclick="runAction('generate_one')">1本だけ生成</button>
        <button onclick="runPrivateTest()">非公開テスト投稿1本</button>
      </div>
    </section>

    <section class="card">
      <h2>ゲストAI</h2>
      <div id="guests"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('guest')">新ゲスト候補を確認/生成</button>
      </div>
    </section>

    <section class="card full">
      <h2>AIスタジオ</h2>
      <div id="studioStatus" class="studio-status small"></div>
      <div class="row"><span>ゲスト画像を自動生成</span><button id="guestImageAutoBtn" onclick="toggleGuestImageAuto()"></button></div>
      <div class="actions" style="margin-top:12px">
        <select id="miraiExpression">
          <option value="normal">ミライ通常</option>
          <option value="smile">ミライ笑顔</option>
          <option value="wink">ミライウインク</option>
          <option value="surprised">ミライ驚き</option>
          <option value="thinking">ミライ考え中</option>
          <option value="serious">ミライ真剣</option>
        </select>
        <button onclick="runStudioMirai()">ミライ画像生成</button>
      </div>
      <div class="actions" style="margin-top:10px">
        <input id="backgroundTheme" placeholder="背景テーマ 例: 未来の青いスタジオ" style="min-width:260px;flex:1">
        <button onclick="runStudioBackground()">背景生成</button>
      </div>
      <div class="actions" style="margin-top:10px">
        <select id="studioGuestSelect" style="min-width:220px"></select>
        <button onclick="runStudioGuest()">選択ゲスト画像生成</button>
      </div>
      <div id="studioAssets" class="studio-grid"></div>
    </section>

    <section class="card full">
      <h2>最近の動画</h2>
      <div id="videos" class="queue"></div>
    </section>

    <section class="card wide">
      <h2>ミライの成長戦略</h2>
      <div id="strategy" class="strategy"></div>
      <div id="analytics" style="margin-top:12px"></div>
    </section>

    <section class="card">
      <h2>PC自動起動</h2>
      <p class="small">PCを起動した時に、このWebアプリを自動で起動できます。</p>
      <div class="row"><span>現在</span><span id="autostartStatus" class="badge"></span></div>
      <div class="actions">
        <button onclick="runAction('autostart_on')">自動起動を登録</button>
        <button onclick="runAction('autostart_off')">解除</button>
      </div>
      <div class="actions" style="margin-top:12px">
        <button onclick="updateRestart()">最新版へ更新して再起動</button>
      </div>
    </section>

    <section class="card full">
      <h2>ログ</h2>
      <div class="actions" style="margin-bottom:10px">
        <button onclick="runAction('cleanup')">投稿済み動画をPCから掃除</button>
      </div>
      <pre id="logs"></pre>
    </section>
  </div>
</div>

<script>
let state=null;
async function api(path, body=null){
  const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{};
  const r=await fetch(path,opt);
  const data=await r.json();
  if(!r.ok) throw new Error(data.message||'error');
  return data;
}
function badge(ok){return '<span class="badge '+(ok?'ok':'ng')+'">'+(ok?'OK':'NG')+'</span>'}
async function refresh(){
  try{
    state=await api('/api/status');
    automationBtn.textContent=state.automation_enabled?'ON':'OFF';
    automationBtn.className=state.automation_enabled?'primary':'';
    uploadBtn.textContent=state.auto_upload_enabled?'ON':'OFF';
    uploadBtn.className=state.auto_upload_enabled?'danger':'';
    privacy.value=state.privacy;
    interval.value=String(state.interval_seconds);
    lastCycle.textContent='最終サイクル: '+(state.last_cycle_at||'未実行')+' / '+state.last_cycle_result;
    document.title=(state.system_ready?'✓ ':'⚠ ')+'ミライ AI YouTuber 管理';
    services.innerHTML=[
      ['Ollama',state.services.ollama],['VOICEVOX',state.services.voicevox],
      ['FFmpeg',state.services.ffmpeg],['YouTube認証',state.services.youtube_token]
    ].map(x=>'<div class="row"><span>'+x[0]+'</span>'+badge(x[1])+'</div>').join('');
    if(document.activeElement!==postsPerDay) postsPerDay.value=state.posts_per_day;
    if(document.activeElement!==postTimes) postTimes.value=state.post_times;
    if(document.activeElement!==guestEvery) guestEvery.value=state.guest_every;
    if(document.activeElement!==guestNewEvery) guestNewEvery.value=state.guest_new_every;
    autostartStatus.textContent=state.autostart_enabled?'登録済み':'未登録';
    autostartStatus.className='badge '+(state.autostart_enabled?'ok':'');
    if(state.current_job){
      lastCycle.textContent='実行中: '+state.current_job+' / 開始 '+(state.current_job_started_at||'');
    }
    queue.innerHTML=state.queue.length?state.queue.map(x=>'<div class="q"><b>'+escapeHtml(x.title)+'</b><div class="small">'+x.scheduled_for+' / #'+x.video_id+' / retry '+x.attempts+'</div></div>').join(''):'<div class="small">キューなし</div>';
    guests.innerHTML=state.guests.length?state.guests.map(x=>'<div class="row"><span>'+escapeHtml(x.name)+'</span><span class="small">'+x.appearances+'回 '+(x.has_image?'画像あり':'画像未生成')+'</span></div>').join(''):'<div class="small">まだゲストなし</div>';
    guestImageAutoBtn.textContent=state.guest_image_auto_enabled?'ON':'OFF';
    guestImageAutoBtn.className=state.guest_image_auto_enabled?'primary':'';
    const backend=state.studio.selected||'未接続';
    studioStatus.textContent='画像エンジン: '+backend+' / Diffusers '+(state.studio.diffusers_installed?'導入済み':'未導入')+' / WebUI '+(state.studio.webui_available?'接続中':'未接続')+' / Model: '+state.studio.model;
    studioGuestSelect.innerHTML=state.guests.length?state.guests.map(x=>'<option value="'+x.id+'">'+escapeHtml(x.name)+'</option>').join(''):'<option value="">ゲストなし</option>';
    studioAssets.innerHTML=state.studio_assets.length?state.studio_assets.map(x=>{
      const img=x.url?'<img src="'+escapeHtml(x.url)+'?t='+encodeURIComponent(x.created_at||'')+'" loading="lazy">':'';
      const name=(x.meta&&x.meta.guest_name)?' / '+escapeHtml(x.meta.guest_name):'';
      return '<div class="asset">'+img+'<div class="meta"><b>'+escapeHtml(x.type||'asset')+name+'</b><div class="small">'+escapeHtml(x.backend||'')+' / '+escapeHtml(x.created_at||'')+'</div></div></div>';
    }).join(''):'<div class="small">まだ生成画像がありません。</div>';
    videos.innerHTML=state.videos.length?state.videos.map(x=>{
      const yt=x.youtube_video_id?' / YouTube投稿済み':'';
      const slot=x.scheduled_for?' / '+x.scheduled_for:'';
      const guest=x.guest_name?' / Guest '+escapeHtml(x.guest_name):'';
      const err=x.error?'<div class="small" style="color:#ff9aa8">エラー: '+escapeHtml(x.error)+'</div>':'';
      return '<div class="q"><b>#'+x.id+' '+escapeHtml(x.title)+'</b><div class="small">'+escapeHtml(x.status||'')+slot+yt+guest+' / '+x.views+' views</div>'+err+'</div>';
    }).join(''):'<div class="small">まだ動画履歴がありません。</div>';
    strategy.textContent=state.growth.strategy;
    analytics.innerHTML=state.growth.recent.map(x=>'<div class="row"><span>#'+x.video_id+' '+escapeHtml(x.title)+'</span><span class="small">'+x.checkpoint_hours+'h / score '+Number(x.score).toFixed(1)+' / '+x.views+' views</span></div>').join('');
    logs.textContent=state.log_tail;
  }catch(e){alert(e.message)}
}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function toggleAutomation(){await api('/api/settings',{automation_enabled:!state.automation_enabled});refresh()}
async function toggleGuestImageAuto(){
  await api('/api/settings',{guest_image_auto_enabled:!state.guest_image_auto_enabled});
  refresh();
}
async function runStudioMirai(){
  const data=await api('/api/action',{action:'studio_mirai',expression:miraiExpression.value});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function runStudioBackground(){
  const theme=backgroundTheme.value.trim();
  if(!theme){alert('背景テーマを入力してください');return;}
  const data=await api('/api/action',{action:'studio_background',theme});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function runStudioGuest(){
  const guestId=Number(studioGuestSelect.value||0);
  if(!guestId){alert('ゲストを選択してください');return;}
  const data=await api('/api/action',{action:'studio_guest',guest_id:guestId});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function toggleUpload(){
  if(!state.auto_upload_enabled && state.privacy==='public' && !confirm('公開設定で自動投稿をONにします。よろしいですか？')) return;
  await api('/api/settings',{auto_upload_enabled:!state.auto_upload_enabled});refresh()
}
async function savePrivacy(){
  const v=privacy.value;
  if(v==='public'&&!confirm('公開に切り替えます。自動投稿ON時は一般公開されます。よろしいですか？')){privacy.value=state.privacy;return}
  await api('/api/settings',{privacy:v});refresh()
}
async function saveInterval(){await api('/api/settings',{interval_seconds:Number(interval.value)});refresh()}
async function saveOperationSettings(){
  const body={
    posts_per_day:Number(postsPerDay.value),
    post_times:postTimes.value,
    guest_every:Number(guestEvery.value),
    guest_new_every:Number(guestNewEvery.value)
  };
  const data=await api('/api/settings',body);
  alert(data.message);
  refresh();
}
async function runPrivateTest(){
  if(!confirm('動画を1本生成してYouTubeへ非公開でテスト投稿します。実行しますか？')) return;
  await runAction('private_test');
}
async function updateRestart(){
  if(!confirm('GitHubの最新版を取り込み、Webアプリを再起動します。よろしいですか？')) return;
  const data=await api('/api/action',{action:'update_restart'});
  alert(data.message+'\n数秒後に自動で再起動します。');
  setTimeout(()=>location.reload(),5000);
}
async function runAction(action){
  const data=await api('/api/action',{action});
  alert(data.message);
  setTimeout(refresh,500);
}
refresh();
setInterval(refresh,15000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MiraiWeb/1.0"

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            raw = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/status":
            self._json(_status_payload())
            return

        if path.startswith("/studio-assets/"):
            relative = unquote(path[len("/studio-assets/"):])
            root = GENERATED_ROOT.resolve()
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                self._json({"message": "invalid asset path"}, 400)
                return
            if not candidate.is_file():
                self._json({"message": "asset not found"}, 404)
                return
            raw = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            self.wfile.write(raw)
            return

        self._json({"message": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()

            if path == "/api/settings":
                if "automation_enabled" in body:
                    set_automation_enabled(bool(body["automation_enabled"]))
                if "auto_upload_enabled" in body:
                    enabled = bool(body["auto_upload_enabled"])
                    if enabled and not Path(settings.youtube_token_file).exists():
                        raise ValueError("YouTube認証が未完了のため自動投稿をONにできません")
                    set_auto_upload_enabled(enabled)
                if "privacy" in body:
                    set_upload_privacy(str(body["privacy"]))
                if "interval_seconds" in body:
                    set_web_interval_seconds(int(body["interval_seconds"]))
                if "posts_per_day" in body:
                    set_posts_per_day(int(body["posts_per_day"]))
                if "post_times" in body:
                    set_post_times(str(body["post_times"]))
                if "guest_every" in body:
                    set_guest_appearance_every(int(body["guest_every"]))
                if "guest_new_every" in body:
                    set_guest_new_every(int(body["guest_new_every"]))
                if "guest_image_auto_enabled" in body:
                    set_guest_image_auto_enabled(
                        bool(body["guest_image_auto_enabled"])
                    )

                _wake_event.set()
                self._json({"ok": True, "message": "設定を保存しました。自動運転へ反映します。"})
                return

            if path == "/api/action":
                action = str(body.get("action") or "")

                if action == "studio_mirai":
                    expression = str(body.get("expression") or "normal")
                    label = f"ミライ画像生成({expression})"
                    func = lambda: print(generate_mirai_image(expression))
                elif action == "studio_background":
                    theme = str(body.get("theme") or "").strip()
                    if not theme:
                        self._json({"message": "背景テーマが必要です"}, 400)
                        return
                    label = "背景画像生成"
                    func = lambda: print(generate_background_image(theme))
                elif action == "studio_guest":
                    guest_id = int(body.get("guest_id") or 0)
                    row = next(
                        (
                            guest for guest in active_guests(100)
                            if int(guest["id"]) == guest_id
                        ),
                        None,
                    )
                    if not row:
                        self._json({"message": "ゲストが見つかりません"}, 404)
                        return
                    label = f"ゲスト画像生成({row['name']})"
                    func = lambda: print(generate_guest_image(row))
                else:
                    label = ""
                    func = None

                if func is not None:
                    def studio_runner():
                        result = _run_captured(label, func)
                        _append_log(result.get("message", ""))

                    threading.Thread(
                        target=studio_runner,
                        daemon=True,
                    ).start()
                    self._json(
                        {"ok": True, "message": f"{label}を開始しました。"}
                    )
                    return

                actions = {
                    "cycle": ("1サイクル", tick),
                    "prepare": ("動画準備", prepare_upcoming),
                    "due": ("投稿時刻確認", run_due),
                    "private_test": ("非公開テスト投稿", run_private_upload_test),
                    "generate_one": (
                        "1本生成",
                        lambda: run_generation(
                            render=True,
                            upload=False,
                            target_override=1,
                        ),
                    ),
                    "guest": ("ゲスト生成確認", maybe_create_guest),
                    "cleanup": ("投稿済みファイル掃除", run_cleanup_uploaded),
                    "services": ("AIサービス起動確認", _ensure_local_services),
                    "autostart_on": (
                        "PC自動起動登録",
                        lambda: print(_install_windows_autostart()),
                    ),
                    "autostart_off": (
                        "PC自動起動解除",
                        lambda: print(_remove_windows_autostart()),
                    ),
                    "update_restart": (
                        "最新版更新と再起動",
                        _update_and_restart,
                    ),
                }

                if action not in actions:
                    self._json({"message": "unknown action"}, 400)
                    return

                label, func = actions[action]

                if action == "update_restart":
                    result = _run_captured(label, func)
                    _append_log(result.get("message", ""))
                    self._json(
                        {
                            "ok": result["ok"],
                            "message": result["message"],
                        },
                        200 if result["ok"] else 500,
                    )
                    return

                def runner():
                    result = _run_captured(label, func)
                    _append_log(result.get("message", ""))

                threading.Thread(target=runner, daemon=True).start()
                self._json({"ok": True, "message": f"{label}を開始しました。"})
                return

            self._json({"message": "not found"}, 404)
        except Exception as exc:
            self._json({"message": str(exc)}, 500)

    def log_message(self, format: str, *args) -> None:
        return


def run(open_browser: bool = True) -> None:
    global _http_server

    init_db()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PORT}"

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        _http_server = server
    except OSError:
        if open_browser:
            webbrowser.open(url)
        return

    _ensure_local_services()

    worker = threading.Thread(
        target=_cycle_worker,
        name="mirai-cycle",
        daemon=True,
    )
    worker.start()

    print(f"[WEB] ミライ管理画面: {url}")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        server.server_close()


if __name__ == "__main__":
    run(open_browser=True)
