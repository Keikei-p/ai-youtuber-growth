from __future__ import annotations

import io
import json
import os
import shutil
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
from urllib.parse import urlparse

from ai_client import OllamaClient
from config import settings
from growth_engine import show_growth_state
from guest_manager import maybe_create_guest
from main import run_cleanup_uploaded, run_generation
from runtime_control import (
    auto_upload_enabled,
    automation_enabled,
    set_auto_upload_enabled,
    set_automation_enabled,
    set_upload_privacy,
    set_web_interval_seconds,
    upload_privacy,
    web_interval_seconds,
)
from scheduler import prepare_upcoming, run_due, tick
from storage import (
    active_guests,
    analytics_history,
    get_channel_state,
    init_db,
    queued_items,
)
from voice.voicevox import VoicevoxClient

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "webapp.log"

_job_lock = threading.Lock()
_state_lock = threading.Lock()
_last_cycle_at: str | None = None
_last_cycle_result = "未実行"
_stop_event = threading.Event()


def _append_log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n[{stamp}]\n{text.rstrip()}\n")


def _run_captured(label: str, func) -> dict:
    if not _job_lock.acquire(blocking=False):
        return {"ok": False, "message": "別の処理を実行中です。"}

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
        _job_lock.release()


def _cycle_worker() -> None:
    global _last_cycle_at, _last_cycle_result
    while not _stop_event.is_set():
        try:
            if automation_enabled():
                result = _run_captured("自動サイクル", tick)
                with _state_lock:
                    _last_cycle_at = datetime.now().isoformat(timespec="seconds")
                    _last_cycle_result = result["message"]
        except Exception as exc:
            _append_log(f"[WEB] background cycle error: {exc}")

        wait_for = web_interval_seconds()
        _stop_event.wait(wait_for)


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


def _status_payload() -> dict:
    with _state_lock:
        last_cycle_at = _last_cycle_at
        last_cycle_result = _last_cycle_result

    return {
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "privacy": upload_privacy(),
        "interval_seconds": web_interval_seconds(),
        "posts_per_day": settings.posts_per_day,
        "post_times": settings.post_times,
        "guest_every": settings.guest_appearance_every,
        "guest_new_every": settings.guest_new_every,
        "services": _service_status(),
        "queue": _queue_status(),
        "guests": _guest_status(),
        "growth": _growth_status(),
        "last_cycle_at": last_cycle_at,
        "last_cycle_result": last_cycle_result,
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
    </section>

    <section class="card">
      <h2>運用設定</h2>
      <div class="row"><span>1日投稿数</span><strong id="postsPerDay"></strong></div>
      <div class="row"><span>投稿時刻</span><strong id="postTimes"></strong></div>
      <div class="row"><span>ゲスト出演</span><strong id="guestEvery"></strong></div>
      <div class="row"><span>新ゲスト</span><strong id="guestNewEvery"></strong></div>
    </section>

    <section class="card wide">
      <h2>投稿キュー</h2>
      <div id="queue" class="queue"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('prepare')">次の動画を準備</button>
        <button onclick="runAction('due')">投稿時刻を確認</button>
        <button onclick="runAction('generate_one')">1本だけ生成</button>
      </div>
    </section>

    <section class="card">
      <h2>ゲストAI</h2>
      <div id="guests"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('guest')">新ゲスト候補を確認/生成</button>
      </div>
    </section>

    <section class="card wide">
      <h2>ミライの成長戦略</h2>
      <div id="strategy" class="strategy"></div>
      <div id="analytics" style="margin-top:12px"></div>
    </section>

    <section class="card">
      <h2>PC自動起動</h2>
      <p class="small">PCを起動した時に、このWebアプリを自動で起動できます。</p>
      <div class="actions">
        <button onclick="runAction('autostart_on')">自動起動を登録</button>
        <button onclick="runAction('autostart_off')">解除</button>
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
    services.innerHTML=[
      ['Ollama',state.services.ollama],['VOICEVOX',state.services.voicevox],
      ['FFmpeg',state.services.ffmpeg],['YouTube認証',state.services.youtube_token]
    ].map(x=>'<div class="row"><span>'+x[0]+'</span>'+badge(x[1])+'</div>').join('');
    postsPerDay.textContent=state.posts_per_day+'本';
    postTimes.textContent=state.post_times;
    guestEvery.textContent=state.guest_every+'本に1回';
    guestNewEvery.textContent=state.guest_new_every+'本ごと';
    queue.innerHTML=state.queue.length?state.queue.map(x=>'<div class="q"><b>'+escapeHtml(x.title)+'</b><div class="small">'+x.scheduled_for+' / #'+x.video_id+' / retry '+x.attempts+'</div></div>').join(''):'<div class="small">キューなし</div>';
    guests.innerHTML=state.guests.length?state.guests.map(x=>'<div class="row"><span>'+escapeHtml(x.name)+'</span><span class="small">'+x.appearances+'回 '+(x.has_image?'画像あり':'画像未生成')+'</span></div>').join(''):'<div class="small">まだゲストなし</div>';
    strategy.textContent=state.growth.strategy;
    analytics.innerHTML=state.growth.recent.map(x=>'<div class="row"><span>#'+x.video_id+' '+escapeHtml(x.title)+'</span><span class="small">'+x.checkpoint_hours+'h / score '+Number(x.score).toFixed(1)+' / '+x.views+' views</span></div>').join('');
    logs.textContent=state.log_tail;
  }catch(e){alert(e.message)}
}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function toggleAutomation(){await api('/api/settings',{automation_enabled:!state.automation_enabled});refresh()}
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

        self._json({"message": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()

            if path == "/api/settings":
                if "automation_enabled" in body:
                    set_automation_enabled(bool(body["automation_enabled"]))
                if "auto_upload_enabled" in body:
                    set_auto_upload_enabled(bool(body["auto_upload_enabled"]))
                if "privacy" in body:
                    set_upload_privacy(str(body["privacy"]))
                if "interval_seconds" in body:
                    set_web_interval_seconds(int(body["interval_seconds"]))
                self._json({"ok": True, "message": "設定を保存しました。"})
                return

            if path == "/api/action":
                action = str(body.get("action") or "")

                actions = {
                    "cycle": ("1サイクル", tick),
                    "prepare": ("動画準備", prepare_upcoming),
                    "due": ("投稿時刻確認", run_due),
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
                    "autostart_on": (
                        "PC自動起動登録",
                        lambda: print(_install_windows_autostart()),
                    ),
                    "autostart_off": (
                        "PC自動起動解除",
                        lambda: print(_remove_windows_autostart()),
                    ),
                }

                if action not in actions:
                    self._json({"message": "unknown action"}, 400)
                    return

                label, func = actions[action]

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
    init_db()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    worker = threading.Thread(
        target=_cycle_worker,
        name="mirai-cycle",
        daemon=True,
    )
    worker.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"

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
